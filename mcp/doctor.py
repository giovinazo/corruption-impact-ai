#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""부패영향평가 AI(cia) — 배포 프리플라이트 진단기 (preflight doctor)

실증 시작 전, 각 직원 PC에서 1회 실행해 '내 PC가 준비됐는지'를 자동 판정한다.

    Windows  :  python doctor.py     (또는  py doctor.py)
    macOS    :  python3 doctor.py

점검 5계: 환경 → 인터프리터 명령 → 필수 패키지 6종 → 알리오(공개) → 법령 프록시/토큰.
각 항목을 [OK]/[WARN]/[FAIL]로 표시하고, FAIL이 하나라도 있으면 종료코드 1을 돌려준다.
이모지·ANSI색 미사용(Windows cp949 콘솔 안전), 외부 의존은 requests 하나뿐.

서버가 실제로 쓰는 alio_client.py / law_client.py를 그대로 import해 점검하므로,
'서버는 되는데 doctor만 다른 결과'가 나오지 않는다(동일 코드경로 보장).
"""
import os
import platform
import shutil
import sys

# Windows 콘솔(cp949)에서 한글 깨짐 방지 — 가능하면 UTF-8로 재설정
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_RESULTS = []  # (level, label, detail)


def add(level, label, detail=""):
    _RESULTS.append((level, label, detail))
    line = f"  [{level:^4}] {label}"
    if detail:
        line += f"\n         └ {detail}"
    print(line)


def _ver(mod_name):
    try:
        m = __import__(mod_name)
        return getattr(m, "__version__", "") or ""
    except Exception:
        return ""


def _mask(tok):
    if not tok:
        return "(없음)"
    return tok[:4] + "…" + tok[-3:] if len(tok) > 9 else "****"


def main():
    print("=" * 64)
    print(" 부패영향평가 AI — 배포 프리플라이트 진단 (doctor)")
    print("=" * 64)
    print(f" OS        : {platform.platform()}")
    print(f" Python    : {platform.python_version()}")
    print(f" 실행 파이썬: {sys.executable}")

    # ── [1] 인터프리터 (실증 키트는 플러그인에 동봉된 런타임 사용) ──
    print("\n[1] 인터프리터  (Claude Code가 cia 서버를 띄울 때 쓰는 파이썬)")
    bundled_root = os.path.normpath(os.path.join(HERE, "..", "runtime"))
    if os.path.normpath(sys.executable).lower().startswith(bundled_root.lower()):
        add("OK", "동봉 런타임 사용 중 (무설치 배포판)",
            "플러그인에 포함된 Python으로 실행 — 이 PC의 파이썬 설치 여부와 무관하게 동작.")
    elif os.path.isdir(bundled_root):
        add("OK", "동봉 런타임 발견 (현재는 시스템 파이썬으로 진단 중)",
            "서버는 .mcp.json에 따라 동봉 런타임으로 기동됨. doctor.bat 사용 권장.")
    else:
        w3, w, wpy = shutil.which("python3"), shutil.which("python"), shutil.which("py")
        if w3 and "WindowsApps" not in w3:
            add("OK", "python3 명령 존재", w3)
        elif os.name == "nt":
            avail = "py" if wpy else ("python" if w else "없음")
            add("WARN", "python3 명령이 없음 (Windows에선 정상)",
                f"이 PC에서 사용 가능한 명령: {avail}. 개발판 .mcp.json은 python3로 기동하므로 "
                "Windows에선 동봉 런타임이 포함된 실증 키트 배포판을 사용할 것.")
        else:
            add("FAIL", "python3 명령을 찾을 수 없음",
                "macOS/Linux인데 python3가 PATH에 없음 — 파이썬 설치 또는 PATH 확인 필요.")

    # ── [2] 필수 패키지 6종 ─────────────────────────────────────────
    print("\n[2] 필수 패키지  (extract_document·법령·알리오가 의존)")
    # (설치명, import명, 용도)
    pkgs = [
        ("requests", "requests", "HTTP 호출 공통"),
        ("urllib3", "urllib3", "SSL 경고억제"),
        ("mcp", "mcp", "MCP 서버 본체"),
        ("olefile", "olefile", "구형 .hwp 추출"),
        ("PyMuPDF", "fitz", "PDF 추출"),
        ("python-docx", "docx", "DOCX 추출"),
    ]
    missing = []
    for inst, mod, use in pkgs:
        try:
            __import__(mod)
            v = _ver(mod)
            add("OK", f"{inst} {v}".rstrip(), use)
        except Exception:
            add("FAIL", f"{inst} 미설치", f"용도: {use}")
            missing.append(inst)
    if missing:
        py = "python" if os.name == "nt" else "python3"
        add("FAIL", "패키지 일괄 설치 필요",
            f"{py} -m pip install " + " ".join(missing))

    # ── [3] 알리오 (공개·인증 불필요) ───────────────────────────────
    print("\n[3] 알리오  (타 기관 규정 비교 — 공개 시스템, 인증 불필요)")
    try:
        import alio_client
        s = alio_client.create_session()
        r = alio_client.fetch_rule_list(s, "한국산업단지공단")
        if r.get("error"):
            add("FAIL", "알리오 호출 실패", str(r["error"]) +
                "  → 회사 방화벽이 www.alio.go.kr(443) 아웃바운드를 막는지 확인. "
                "막혀도 캐시(peer_cache) 비교는 동작.")
        else:
            add("OK", "알리오 라이브 정상", f"산단공 규정 {r.get('totalCnt')}건 수신")
    except ImportError:
        add("WARN", "alio_client 미발견", "doctor를 mcp/ 폴더 안에서 실행했는지 확인.")
    except Exception as e:
        add("FAIL", "알리오 점검 중 예외", f"{type(e).__name__}: {e}")

    # ── [4] 법령 프록시 + 토큰 ──────────────────────────────────────
    print("\n[4] 법령(법제처)  (상위법령 정합성 — NAS 프록시 경유)")
    try:
        import law_client
        tok = law_client._resolve_token()
        if not tok:
            # 키트 배포본 폴백: 플러그인 루트 .mcp.json(cia env)에 토큰이 주입돼 있다
            try:
                import json as _j
                with open(os.path.join(HERE, "..", ".mcp.json"), encoding="utf-8") as _f:
                    env = (_j.load(_f).get("mcpServers", {})
                           .get("cia", {}).get("env", {}))
                tok = env.get("LAW_PROXY_TOKEN", "")
                if tok:
                    os.environ["LAW_PROXY_TOKEN"] = tok
            except (OSError, ValueError):
                pass
        url = law_client.PROXY_URL
        if tok:
            add("OK", "토큰 해석됨", f"{_mask(tok)} (env/동봉 .mcp.json에서 로드)")
        else:
            add("WARN", "토큰 없음",
                "env LAW_PROXY_TOKEN 또는 플러그인 설정에 주입 필요(설치가이드). "
                "토큰 없으면 법령 도구만 비활성, 알리오·문서추출은 정상.")
        # 도달성 — 토큰 유무와 무관하게 '네트워크가 닿는가'부터 확인
        try:
            import requests
            r = requests.get(
                f"{url}/lawSearch.do",
                params={"type": "JSON", "target": "law", "query": "테스트"},
                headers={"X-Proxy-Token": tok or "preflight-probe"},
                timeout=8,
            )
            if r.status_code == 200:
                add("OK", "법령 프록시 완전 동작", f"{url} → 200")
            elif r.status_code in (401, 403):
                add("WARN", "프록시 도달 OK · 토큰만 필요/불일치",
                    f"{url} → {r.status_code}. 네트워크는 뚫림 — 올바른 토큰 주입 후 재점검하면 OK.")
            else:
                add("WARN", "프록시 응답 이상", f"{url} → HTTP {r.status_code}")
        except Exception as e:
            add("FAIL", "법령 프록시 도달 불가",
                f"{type(e).__name__} — 회사망이 8765 평문 아웃바운드를 차단했거나 NAS가 내려갔을 수 있음. "
                "이 항목이 FAIL이면 회사 네트워크팀에 'law-proxy.example.com:8765/TCP 아웃바운드 허용'을 문의.")
    except ImportError:
        add("WARN", "law_client 미발견", "doctor를 mcp/ 폴더 안에서 실행했는지 확인.")
    except Exception as e:
        add("FAIL", "법령 점검 중 예외", f"{type(e).__name__}: {e}")

    # ── [5] Anthropic API 도달성 (Claude Code 자체의 생존 조건) ─────
    print("\n[5] Anthropic 접속  (Claude Code가 모델과 통신하는 경로)")
    try:
        import requests
        try:
            r = requests.get("https://api.anthropic.com/v1/models", timeout=8)
            # 401/403 = 도달 + 인증서 체인 정상 (키 없이 호출했으니 거부가 정상)
            add("OK", "api.anthropic.com 도달 + 인증서 정상", f"HTTP {r.status_code}")
        except requests.exceptions.SSLError:
            add("WARN", "SSL 인증서 검증 실패 — SSL 가로채기(검사) 장비 의심",
                "이 망은 외부 https를 재서명함. Claude Code 로그인/응답이 실패하면 "
                "장비의 루트인증서를 받아 NODE_EXTRA_CA_CERTS 환경변수로 등록 필요.")
        except requests.RequestException as e:
            add("WARN", "api.anthropic.com 도달 불가",
                f"{type(e).__name__} — 이 망에서 Claude Code 자체가 동작하지 않을 수 있음. "
                "프록시·방화벽 설정 확인.")
    except ImportError:
        add("WARN", "requests 미설치로 Anthropic 접속 점검 생략", "")

    # ── 요약 ────────────────────────────────────────────────────────
    n_ok = sum(1 for r in _RESULTS if r[0] == "OK")
    n_warn = sum(1 for r in _RESULTS if r[0] == "WARN")
    n_fail = sum(1 for r in _RESULTS if r[0] == "FAIL")
    print("\n" + "=" * 64)
    print(f" 결과 :  OK {n_ok}   WARN {n_warn}   FAIL {n_fail}")
    print("=" * 64)
    if n_fail:
        print(" → FAIL 항목을 해결해야 해당 기능이 동작합니다. 위 '└' 안내를 따르세요.")
        print("   (알리오/문서추출 FAIL과 법령 FAIL은 서로 독립 — 일부만 막혀도 나머지는 사용 가능)")
    elif n_warn:
        print(" → 치명적 문제는 없습니다. WARN은 배포 설치 단계에서 자동 해소됩니다.")
    else:
        print(" → 모든 점검 통과. 실증 준비 완료.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
