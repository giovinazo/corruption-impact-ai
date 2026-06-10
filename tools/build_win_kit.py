#!/usr/bin/env python3
"""부패영향평가 AI — Windows 실증 키트 빌더 (Mac에서 실행)

산출물: 28_워크플로우경진대회3/09_실증키트/cia-pilot-kit-v<버전>.zip
    cia-pilot-kit/
    ├── 0_읽어주세요.txt           설치 순서 (설치자용, 5단계)
    ├── install.bat                플러그인 설치 자동화 (더블클릭)
    ├── doctor.bat                 설치 후 재진단 (더블클릭)
    ├── setup_permissions.py       cia 도구 권한 사전허용 (install.bat이 호출)
    └── 2_플러그인/cia-win/        플러그인 본체 + 동봉 런타임
        ├── .claude-plugin/ commands/ skills/ mcp/(data·peer_cache 포함)
        ├── .mcp.json              ← Windows용으로 재작성 (동봉 python + 토큰 + -X utf8)
        └── runtime/python/        ← 임베디드 Python 3.12 + site-packages(의존성 6종 전체)

설계 근거:
  - ${CLAUDE_PLUGIN_ROOT}는 .mcp.json의 command 필드에서도 치환됨 (공식 문서 확인)
  - 임베디드 배포판은 ._pth가 sys.path를 전적으로 결정 → 'Lib/site-packages'와
    '../../mcp'(server.py 형제 모듈)를 명시해 스크립트 경로 삽입 여부에 비의존
  - -X utf8 + PYTHONUTF8=1 이중 적용: cp949 콘솔/파이프에서 stdio JSON-RPC 한글 보호
  - zip은 zipfile+NFC로 생성: Finder/ditto의 __MACOSX·NFD 자모분해 문제 원천 회피
  - 토큰은 빌드 시 ~/.claude.json(open-law env)에서 읽어 주입 — 레포에 미보관

1_사전설치/(Claude Code·Git 설치파일)는 용량·라이선스상 키트 zip에 미동봉.
읽어주세요.txt에 공식 다운로드 URL 명기 — 설치자가 사전 1회 받아 USB에 추가.
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unicodedata
import zipfile

import certifi

# macOS python.org 빌드의 흔한 함정: stdlib ssl의 기본 CA 번들
# (etc/openssl/cert.pem)이 미설치라 https 검증이 전부 실패한다.
# 빌드 머신에서 확인(2026-06-10) — SSL 가로채기가 아니라 CA 경로 누락.
# certifi 번들을 명시해 TLS 검증을 '유지'한 채 정상화한다(verify를 끄지 않음).
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

KIT_VERSION = "0.3"
PY_VER = "3.12.10"          # 3.12 마지막 바이너리 릴리스 (임베디드 zip 제공)
PY_TAG = "312"
EMBED_URL = (f"https://www.python.org/ftp/python/{PY_VER}/"
             f"python-{PY_VER}-embed-amd64.zip")

TOOLS = pathlib.Path(__file__).resolve().parent
PLUGIN = TOOLS.parent                                  # corruption-impact-ai/
OUT_ROOT = PLUGIN.parent.parent / "09_실증키트"         # 28_워크플로우경진대회3/09_실증키트
BUILD = OUT_ROOT / "build" / "cia-pilot-kit"
PLUG_DST = BUILD / "2_플러그인" / "cia-win"
CACHE = TOOLS / "cache"                                # 다운로드 캐시 (재빌드 가속)

EXCLUDE_DIRS = {".git", "__pycache__", "tools", ".claude"}
EXCLUDE_FILES = {".DS_Store"}

PERMISSION_RULE = "mcp__plugin_corruption-impact-ai_cia__*"


def log(msg):
    print(f"[build] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────
# 1. 플러그인 본체 복사 (peer_cache 의도적 포함 — 첫 전국비교 즉답용)
# ──────────────────────────────────────────────────────────────────
def copy_plugin():
    if BUILD.exists():
        shutil.rmtree(BUILD)
    PLUG_DST.mkdir(parents=True)

    def ignore(d, names):
        out = set()
        for n in names:
            if n in EXCLUDE_DIRS or n in EXCLUDE_FILES or n.endswith(".pyc"):
                out.add(n)
        return out

    for child in PLUGIN.iterdir():
        if child.name in EXCLUDE_DIRS or child.name in EXCLUDE_FILES:
            continue
        dst = PLUG_DST / child.name
        if child.is_dir():
            shutil.copytree(child, dst, ignore=ignore)
        else:
            shutil.copy2(child, dst)
    n_cache = len(list((PLUG_DST / "mcp/data/peer_cache").glob("*")))
    log(f"플러그인 복사 완료 (peer_cache {n_cache}개 동봉)")


# ──────────────────────────────────────────────────────────────────
# 2. 임베디드 Python + Windows용 의존성 설치 (교차 플랫폼)
# ──────────────────────────────────────────────────────────────────
def _download(url, dest):
    """requests로 다운로드. requests는 certifi 번들을 사용하므로 위에서
    교정한 CA 경로로 TLS 검증이 정상 통과한다(verify는 끄지 않는다)."""
    import requests
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)


def install_runtime():
    CACHE.mkdir(exist_ok=True)
    embed_zip = CACHE / EMBED_URL.rsplit("/", 1)[1]
    if not embed_zip.exists():
        log(f"임베디드 Python 다운로드: {EMBED_URL}")
        _download(EMBED_URL, embed_zip)
    rt = PLUG_DST / "runtime" / "python"
    rt.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(rt)
    log(f"임베디드 Python {PY_VER} 전개")

    # ._pth = 임베디드의 sys.path 전체 정의. 스크립트 경로 자동삽입에 기대지 않고
    # site-packages와 server.py 형제모듈 폴더(mcp/)를 명시한다.
    pth = rt / f"python{PY_TAG}._pth"
    pth.write_text(
        f"python{PY_TAG}.zip\n.\nLib/site-packages\n../../mcp\n",
        encoding="ascii")

    site = rt / "Lib" / "site-packages"
    site.mkdir(parents=True)
    req = PLUGIN / "mcp" / "requirements.txt"
    log("Windows(win_amd64)용 휠 교차 설치 — pip --platform")
    cmd = [sys.executable, "-m", "pip", "install",
           "--target", str(site),
           "--platform", "win_amd64",
           "--python-version", "3.12",
           "--implementation", "cp",
           "--only-binary=:all:",
           "--quiet", "--no-compile",
           "-r", str(req)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        raise SystemExit("pip 교차 설치 실패")
    pkgs = sorted(p.name for p in site.iterdir()
                  if p.is_dir() and not p.name.endswith(".dist-info"))
    pyds = list(site.rglob("*.pyd"))
    log(f"site-packages {len(pkgs)}개 패키지 / 컴파일 확장(.pyd) {len(pyds)}개(win 바이너리 증거)")
    return pkgs


# ──────────────────────────────────────────────────────────────────
# 3. Windows용 .mcp.json — 동봉 런타임 + 토큰 주입
# ──────────────────────────────────────────────────────────────────
def resolve_token() -> str:
    cfg = json.loads((pathlib.Path.home() / ".claude.json").read_text())
    tok = (cfg.get("mcpServers", {}).get("open-law", {})
           .get("env", {}).get("LAW_PROXY_TOKEN", ""))
    if not tok:
        raise SystemExit("~/.claude.json에서 LAW_PROXY_TOKEN을 찾지 못함")
    return tok


def write_mcp_json(token: str):
    mcp_json = {
        "mcpServers": {
            "cia": {
                "command": "${CLAUDE_PLUGIN_ROOT}/runtime/python/python.exe",
                "args": ["-X", "utf8", "${CLAUDE_PLUGIN_ROOT}/mcp/server.py"],
                "env": {
                    "LAW_PROXY_TOKEN": token,
                    "PYTHONUTF8": "1",
                },
            }
        }
    }
    (PLUG_DST / ".mcp.json").write_text(
        json.dumps(mcp_json, ensure_ascii=False, indent=2), encoding="utf-8")
    log(".mcp.json 재작성 (동봉 런타임·토큰 주입·UTF-8 강제)")


# ──────────────────────────────────────────────────────────────────
# 4. 키트 보조 파일 — install.bat / doctor.bat / 권한 / 읽어주세요
#    (bat은 한국어 Windows 콘솔 기본 cp949로 저장 — chcp 불필요·한글 안 깨짐)
# ──────────────────────────────────────────────────────────────────
INSTALL_BAT = r"""@echo off
setlocal
echo ============================================================
echo  부패영향평가 AI - 플러그인 설치 (실증 키트 v{ver})
echo ============================================================
echo.

where claude >nul 2>&1
if errorlevel 1 (
  echo [실패] claude 명령을 찾을 수 없습니다.
  echo        1_사전설치의 Claude Code를 먼저 설치한 뒤,
  echo        "새" 명령창에서 다시 실행해 주세요.
  pause & exit /b 1
)

set DEST=%LOCALAPPDATA%\cia-plugin
echo [1/4] 플러그인을 %DEST% 로 복사합니다 (수 분 소요)...
robocopy "%~dp02_플러그인\cia-win" "%DEST%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( echo [실패] 파일 복사 오류 & pause & exit /b 1 )

echo [2/4] Claude Code에 플러그인을 등록합니다...
call claude plugin marketplace add "%DEST%" >nul 2>&1
call claude plugin install corruption-impact-ai@corruption-impact-ai --scope user
if errorlevel 1 (
  echo [실패] 플러그인 설치 실패 - 위 메시지를 확인해 주세요.
  pause & exit /b 1
)

echo [3/4] 도구 사용권한을 사전 허용합니다 (권한 팝업 제거)...
"%DEST%\runtime\python\python.exe" "%~dp0setup_permissions.py"

echo [4/4] 설치 진단(doctor)을 실행합니다...
echo.
"%DEST%\runtime\python\python.exe" "%DEST%\mcp\doctor.py"
echo.
echo ============================================================
echo  위 결과가 "모든 점검 통과"면 설치 완료입니다.
echo  Claude Code를 새로 열고  /사규검토  를 입력해 보세요.
echo ============================================================
pause
"""

DOCTOR_BAT = r"""@echo off
set DEST=%LOCALAPPDATA%\cia-plugin
if not exist "%DEST%\runtime\python\python.exe" (
  echo [실패] 설치본이 없습니다. install.bat을 먼저 실행해 주세요.
  pause & exit /b 1
)
"%DEST%\runtime\python\python.exe" "%DEST%\mcp\doctor.py"
pause
"""

SETUP_PERMISSIONS = '''#!/usr/bin/env python3
"""cia MCP 도구 전체를 권한 사전허용 — 직원이 도구 권한 팝업을 보지 않게 한다.
규칙 문법(공식 문서): mcp__<server>__* / 플러그인 서버는 plugin_<플러그인>_<서버>로 네임스페이스.
"""
import json
import pathlib

RULE = "{rule}"
p = pathlib.Path.home() / ".claude" / "settings.json"
p.parent.mkdir(exist_ok=True)
try:
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {{}}
except Exception:
    cfg = {{}}
allow = cfg.setdefault("permissions", {{}}).setdefault("allow", [])
if RULE not in allow:
    allow.append(RULE)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  권한 추가: {{RULE}}")
else:
    print("  권한 이미 설정됨")
'''

README_TXT = """■ 부패영향평가 AI — 실증 키트 v{ver} (Windows)

설치 순서 (관리자 권한 불필요):

 1. [1회만] Claude Code 설치
    PowerShell을 열고:  irm https://claude.ai/install.ps1 | iex
    (또는 winget install Anthropic.ClaudeCode)

 2. [1회만] Git for Windows 설치  https://git-scm.com/download/win
    (Claude Code 데스크톱 앱의 필수 구성요소 — 기본 옵션으로 다음다음)

 3. Claude 로그인: 새 명령창에서  claude  실행 → 브라우저 로그인
    (직원 본인의 Claude 계정)

 4. 이 폴더의  install.bat  더블클릭
    → 마지막에 "모든 점검 통과"가 나오면 성공

 5. Claude Code(앱 또는 명령창)에서  /사규검토  입력 → 검토 시작

문제가 생기면: doctor.bat 더블클릭 → 결과 화면을 감사실 감사팀에 전달
(법령[4] 항목만 빨간불이면 법령 기능 외에는 모두 사용 가능합니다)
"""


def write_kit_files():
    (BUILD / "install.bat").write_bytes(
        INSTALL_BAT.replace("{ver}", KIT_VERSION).encode("cp949"))
    (BUILD / "doctor.bat").write_bytes(DOCTOR_BAT.encode("cp949"))
    (BUILD / "setup_permissions.py").write_text(
        SETUP_PERMISSIONS.format(rule=PERMISSION_RULE), encoding="utf-8")
    (BUILD / "0_읽어주세요.txt").write_text(
        README_TXT.format(ver=KIT_VERSION), encoding="utf-8-sig")
    (BUILD / "1_사전설치").mkdir()
    (BUILD / "1_사전설치" / "여기에_ClaudeCode와_Git_설치파일을_넣으세요.txt").write_text(
        "0_읽어주세요.txt의 1·2번 URL에서 받아 이 폴더에 보관하면\n"
        "인터넷이 느린 현장에서도 바로 설치할 수 있습니다.\n", encoding="utf-8-sig")
    log("키트 보조 파일 작성 (bat=cp949, txt=utf-8-sig)")


# ──────────────────────────────────────────────────────────────────
# 5. zip 패키징 — NFC 정규화 + __MACOSX 원천 차단
# ──────────────────────────────────────────────────────────────────
def make_zip() -> pathlib.Path:
    out = OUT_ROOT / f"cia-pilot-kit-v{KIT_VERSION}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(BUILD.rglob("*")):
            if p.is_dir() or p.name in EXCLUDE_FILES:
                continue
            arc = unicodedata.normalize(
                "NFC", str(p.relative_to(BUILD.parent)))
            z.write(p, arc)
    return out


def main():
    log(f"빌드 시작 — 키트 v{KIT_VERSION}, Python {PY_VER} embed")
    OUT_ROOT.mkdir(exist_ok=True)
    token = resolve_token()          # 빌드 전제 확인을 먼저 (실패 조기 발견)
    copy_plugin()
    pkgs = install_runtime()
    write_mcp_json(token)
    write_kit_files()
    out = make_zip()

    size_mb = out.stat().st_size / 1e6
    log("─" * 50)
    log(f"완료: {out}")
    log(f"zip 크기: {size_mb:.0f} MB")
    log(f"동봉 패키지: {', '.join(pkgs)}")


if __name__ == "__main__":
    main()
