"""법제처 OPEN API 클라이언트 (NAS 프록시 경유) — open-law MCP에서 이식.

환경변수:
    LAW_PROXY_URL   중계 프록시 base URL (필수 — 기본값 없음, 운영자 제공·키트 자동 주입)
    LAW_PROXY_TOKEN X-Proxy-Token 값 (필수)

env 미설정 시 폴백: ① ~/.claude.json의 open-law MCP env(개발 환경)
② 플러그인 루트 .mcp.json의 cia env(배포 키트) 순서로 두 값을 해석한다.

프록시가 OC를 자동 주입하므로 토큰만 보내면 됨 — IP 화이트리스트 제약 없이
어느 환경에서나 호출 가능 (부패영향평가 담당자 PC에서 동작하는 핵심 조건).
"""
import hashlib
import json
import os
import pathlib
import time
from urllib.parse import urlencode

import requests

TIMEOUT = 30
_CACHE_DIR = pathlib.Path.home() / ".cache" / "corruption-impact-ai"
_CACHE_TTL_SEC = 7 * 86400  # 7일

# 사규 부패영향평가에서 자주 등장하는 법령 약칭 → 정식 명칭
LAW_ABBR = {
    "산집법": "산업집적활성화 및 공장설립에 관한 법률",
    "산업단지법": "산업입지 및 개발에 관한 법률",
    "공운법": "공공기관의 운영에 관한 법률",
    "공공기관운영법": "공공기관의 운영에 관한 법률",
    "청탁금지법": "부정청탁 및 금품등 수수의 금지에 관한 법률",
    "부정청탁법": "부정청탁 및 금품등 수수의 금지에 관한 법률",
    "부패방지권익위법": "부패방지 및 국민권익위원회의 설치와 운영에 관한 법률",
    "부패방지법": "부패방지 및 국민권익위원회의 설치와 운영에 관한 법률",
    "이해충돌방지법": "공직자의 이해충돌 방지법",
    "국가계약법": "국가를 당사자로 하는 계약에 관한 법률",
    "공익신고자법": "공익신고자 보호법",
    "개보법": "개인정보 보호법",
    "근기법": "근로기준법",
    "산안법": "산업안전보건법",
}


def _cache_path(path: str, params: dict) -> pathlib.Path:
    key_src = path + "|" + urlencode(sorted(params.items()))
    key = hashlib.sha1(key_src.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{key}.json"


def _resolve_from_configs(key: str) -> str:
    """env 우선, 없으면 설정파일 폴백 — ① ~/.claude.json open-law env(개발 환경)
    ② 플러그인 루트 .mcp.json cia env(배포 키트). 찾으면 env에 캐시해 재사용."""
    val = os.environ.get(key, "")
    if val:
        return val
    sources = [
        (pathlib.Path.home() / ".claude.json", ("mcpServers", "open-law", "env")),
        (pathlib.Path(__file__).resolve().parent.parent / ".mcp.json",
         ("mcpServers", "cia", "env")),
    ]
    for path, keys in sources:
        try:
            node = json.loads(path.read_text(encoding="utf-8"))
            for k in keys:
                node = node.get(k, {}) if isinstance(node, dict) else {}
            val = node.get(key, "") if isinstance(node, dict) else ""
            if val:
                os.environ[key] = val
                return val
        except (OSError, ValueError):
            continue
    return ""


def proxy_url() -> str:
    """중계 프록시 base URL — 공개 저장소에 운영 설비 주소를 박지 않기 위해
    코드 기본값을 두지 않는다 (env·설정 주입 전용)."""
    return _resolve_from_configs("LAW_PROXY_URL").rstrip("/")


def _resolve_token() -> str:
    return _resolve_from_configs("LAW_PROXY_TOKEN")


def call(path: str, params: dict) -> dict:
    """프록시 경유 법제처 API 호출 (7일 디스크 캐시)."""
    token = _resolve_token()
    if not token:
        raise RuntimeError("LAW_PROXY_TOKEN 미설정 — 환경변수 또는 플러그인 설정에서 주입 필요")
    base = proxy_url()
    if not base:
        raise RuntimeError("LAW_PROXY_URL 미설정 — 환경변수 또는 플러그인 설정에서 주입 필요")
    full = {"type": "JSON", **params}
    use_cache = os.environ.get("CIA_LAW_CACHE", "1") != "0"

    if use_cache:
        cache_file = _cache_path(path, full)
        if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < _CACHE_TTL_SEC:
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

    headers = {"X-Proxy-Token": token}
    r = requests.get(f"{base}/{path}", params=full, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if use_cache:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _cache_path(path, full).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return data


def normalize_buchik(buchik_block) -> list:
    """부칙 블록(이중 리스트)을 [{공포일자, 공포번호, 내용}]으로 평탄화."""
    if not buchik_block:
        return []
    units = buchik_block.get("부칙단위", [])
    if isinstance(units, dict):
        units = [units]
    out = []
    for unit in units:
        lines = []
        for block in unit.get("부칙내용", []):
            if isinstance(block, list):
                lines.extend(str(x) for x in block if x is not None)
            elif block:
                lines.append(str(block))
        out.append({"공포일자": unit.get("부칙공포일자"),
                    "공포번호": unit.get("부칙공포번호"),
                    "내용": "\n".join(lines)})
    return out
