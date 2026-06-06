"""알리오(공공기관 경영정보 공개시스템) 내부규정 조회·다운로드 클라이언트.

alio-crawler/alio-mcp의 alio_core.py에서 이식 (검증 자산 재사용).
주의: create_session 기본 verify_ssl=False 유지 필수 — 일부 외부망의
SSL 검사(가로채기) 보안장비 환경에서 verify=True면 전 API가
CERTIFICATE_VERIFY_FAILED로 실패한다 (v5.4.2 교훈). ALIO_VERIFY_SSL=1로 재활성화.
"""
import json
import os
import random
import time
from typing import Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter

BASE_URL = "https://www.alio.go.kr"
_DEFAULT_TIMEOUT = (10, 30)
_DEFAULT_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}

if os.environ.get("ALIO_VERIFY_SSL") != "1":
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def create_session(verify_ssl: Optional[bool] = None,
                   timeout: tuple = _DEFAULT_TIMEOUT) -> requests.Session:
    if verify_ssl is None:
        verify_ssl = os.environ.get("ALIO_VERIFY_SSL") == "1"
    session = requests.Session()
    session.verify = verify_ssl
    session.headers.update({"User-Agent": _DEFAULT_USER_AGENT})
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session._default_timeout = timeout
    return session


def retry_request(session, method, url, max_retries=3, backoff=1.0, **kwargs):
    if "timeout" not in kwargs:
        kwargs["timeout"] = getattr(session, "_default_timeout", _DEFAULT_TIMEOUT)
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code not in _RETRIABLE_STATUS_CODES:
                return response
            wait = backoff * (2 ** attempt)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
            if attempt < max_retries:
                time.sleep(wait + random.uniform(0, wait * 0.5))
                continue
            return response
        except requests.RequestException as e:
            last_exception = e
            if attempt < max_retries:
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, 0.5))
                continue
    raise last_exception


def fetch_rule_list(session, inst_name: str, divis: str = "", page: int = 1) -> dict:
    """기관명(apbaNa 부분일치)으로 내부규정 목록 1페이지 조회."""
    url = f"{BASE_URL}/occasional/findRuleList.json"
    params = {"type": "apbaNa", "word": inst_name, "pageNo": page, "divis": divis}
    try:
        resp = retry_request(session, "GET", url, params=params, timeout=30)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "result": []}
        data = resp.json().get("data") or {}
        return {"totalCnt": data.get("totalCnt", 0),
                "result": data.get("result", []) or []}
    except (requests.RequestException, ValueError) as e:
        return {"error": str(e), "result": []}


def fetch_all_rules(session, inst_name: str, divis: str = "") -> list:
    """기관 내부규정 전체 페이지 자동 순회."""
    first = fetch_rule_list(session, inst_name, divis, page=1)
    items = list(first.get("result", []))
    total_cnt = first.get("totalCnt", 0)
    if total_cnt <= len(items) or not items:
        return items
    page_size = max(len(items), 1)
    total_page = (total_cnt + page_size - 1) // page_size
    for p in range(2, total_page + 1):
        more = fetch_rule_list(session, inst_name, divis, page=p)
        items.extend(more.get("result", []))
    return items


def fetch_rule_detail(session, seq: str) -> dict:
    """규정 상세 조회 — bFiles에서 zip 제외 최신(file_no 최대) 파일 메타."""
    url = f"{BASE_URL}/occasional/findRuleDtl.json"
    try:
        resp = retry_request(session, "GET", url, params={"seq": seq}, timeout=15)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "files": [], "latest": None}
        data = resp.json().get("data") or {}
        b_files = data.get("bFiles", "") or ""
        files = []
        for entry in b_files.split(","):
            entry = entry.strip()
            if "|" not in entry:
                continue
            file_no, file_name = entry.split("|", 1)
            if file_name.strip().lower().endswith(".zip"):
                continue
            files.append({"file_no": file_no.strip(), "file_name": file_name.strip()})
        latest = None
        if files:
            try:
                latest = max(files, key=lambda f: int(f["file_no"]))
            except (ValueError, TypeError):
                latest = files[-1]
        return {"seq": seq, "files": files, "latest": latest}
    except (requests.RequestException, ValueError) as e:
        return {"error": str(e), "files": [], "latest": None}


def download_rule_file_to_path(session, file_no: str, save_path: str):
    """내부규정 파일 단건 다운로드 — .part 원자교체 + 스트리밍 끊김 재시도.

    반환: (success, saved_path, message)
    """
    url = f"{BASE_URL}/download/rulefiledown.json"
    tmp = save_path + ".part"
    last_err = ""
    for attempt in range(4):
        try:
            resp = retry_request(session, "GET", url, params={"fileNo": file_no},
                                 timeout=60, stream=True, max_retries=0)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code in _RETRIABLE_STATUS_CODES and attempt < 3:
                    time.sleep(2 ** attempt + random.uniform(0, 0.5))
                    continue
                return False, "", last_err
            ct = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ct:
                try:
                    err = resp.json()
                    return False, "", f"API error: {err.get('message') or 'unknown'}"
                except (json.JSONDecodeError, ValueError):
                    pass
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, save_path)
            return True, save_path, "OK"
        except (requests.RequestException, OSError) as e:
            last_err = str(e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if attempt < 3:
                time.sleep(2 ** attempt + random.uniform(0, 0.5))
                continue
    return False, "", last_err
