#!/usr/bin/env python3
"""부패영향평가 AI — Windows 실증 키트 빌더 (Mac에서 실행)

산출물: 28_워크플로우경진대회3/09_실증키트/cia-pilot-kit-v<버전>.zip

v0.5부터 키트는 "1회용 부트스트랩"입니다 — 플러그인 코드는 동봉하지 않고
GitHub에서 설치하며, 키트는 코드가 의존하는 세 가지만 깔아줍니다:
  ① 전용 런타임  : 임베디드 Python → %LOCALAPPDATA%\\cia\\runtime (고정 위치)
  ② 환경변수     : CIA_PYTHON(런타임 경로)·LAW_PROXY_URL·LAW_PROXY_TOKEN (setx)
  ③ 설치·권한    : GitHub 마켓플레이스 등록 → plugin install → 권한 사전허용 → doctor
이후 버전 업데이트는 USB 재배포 없이 `claude plugin update` 한 줄.

    cia-pilot-kit/
    ├── 0_읽어주세요.txt           설치 순서 (설치자용)
    ├── install.bat                런타임 복사 + env 등록 + GitHub 설치 (더블클릭)
    ├── doctor.bat                 설치 후 재진단 (더블클릭)
    ├── test_runtime.bat           런타임·의존성 자가시험 (Claude Code 불필요)
    ├── cleanup.bat                시험 PC 정리 (플러그인·런타임·env 제거)
    ├── setup_permissions.py       cia 도구 권한 사전허용 (install.bat이 호출)
    └── 2_런타임/runtime/python/   임베디드 Python 3.12 + site-packages(의존성 6종)

설계 근거:
  - 레포 .mcp.json의 command=`${CIA_PYTHON:-python3}` env 폴백(공식 문서: command
    필드도 치환)이 키트가 setx한 런타임 경로를 받는다 — .mcp.json 재작성 불필요
  - server.py가 자기 폴더를 sys.path에 부트스트랩(v0.4.1)하므로 임베디드 ._pth에
    플러그인 상대경로를 둘 필요가 없다 → 런타임과 플러그인이 분리 배치 가능
  - 토큰·주소는 빌드 시 ~/.claude.json(open-law env)에서 읽어 install.bat에 주입
    (setx 영구 + set 현재세션 이중 적용: 같은 bat 안에서 doctor가 바로 읽도록)
  - -X utf8 강제: 임베디드는 ._pth 격리 모드라 PYTHON* env가 무시될 수 있음
  - zip은 zipfile+NFC로 생성: Finder/ditto의 __MACOSX·NFD 자모분해 문제 원천 회피
  - 버전 폴더 탐색(for /d 마지막 항목)은 알파벳순이라 0.10.x부터는 오판 —
    플러그인 버전은 0.5→0.9→1.0 식으로 자릿수 유지 권장

주의: peer_cache(타기관 규정 캐시)는 v0.5부터 미동봉 — GitHub 설치본은 빈 캐시로
시작하고 사용하며 채워진다(첫 전국비교만 느림). 플러그인 업데이트 시 캐시 폴더가
교체되므로 동봉 의미도 작음.

1_사전설치/(Claude Code·Git 설치파일)는 용량·라이선스상 키트 zip에 미동봉.
읽어주세요.txt에 공식 다운로드 URL 명기 — 설치자가 사전 1회 받아 USB에 추가.
"""
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

KIT_VERSION = "0.5"
GITHUB_REPO = "giovinazo/corruption-impact-ai"
PY_VER = "3.12.10"          # 3.12 마지막 바이너리 릴리스 (임베디드 zip 제공)
PY_TAG = "312"
EMBED_URL = (f"https://www.python.org/ftp/python/{PY_VER}/"
             f"python-{PY_VER}-embed-amd64.zip")

TOOLS = pathlib.Path(__file__).resolve().parent
PLUGIN = TOOLS.parent                                  # corruption-impact-ai/
OUT_ROOT = PLUGIN.parent.parent / "09_실증키트"         # 28_워크플로우경진대회3/09_실증키트
BUILD = OUT_ROOT / "build" / "cia-pilot-kit"
RT_DST = BUILD / "2_런타임" / "runtime" / "python"
CACHE = TOOLS / "cache"                                # 다운로드 캐시 (재빌드 가속)

EXCLUDE_FILES = {".DS_Store"}

PERMISSION_RULE = "mcp__plugin_corruption-impact-ai_cia__*"


def log(msg):
    print(f"[build] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────
# 1. 빌드 전제 — 법령 프록시 주소·토큰 (bat 주입 안전성 검사 포함)
# ──────────────────────────────────────────────────────────────────
def resolve_proxy() -> tuple:
    """법령 프록시 주소·토큰 — 빌드 머신의 ~/.claude.json open-law env에서 읽어
    install.bat에 주입한다 (공개 저장소 코드에는 운영 설비 주소·토큰을 두지 않음)."""
    cfg = json.loads((pathlib.Path.home() / ".claude.json").read_text())
    env = cfg.get("mcpServers", {}).get("open-law", {}).get("env", {})
    url = env.get("LAW_PROXY_URL", "").rstrip("/")
    tok = env.get("LAW_PROXY_TOKEN", "")
    if not tok:
        raise SystemExit("~/.claude.json open-law env에서 LAW_PROXY_TOKEN을 찾지 못함")
    if not url:
        raise SystemExit("~/.claude.json open-law env에 LAW_PROXY_URL이 없음 — "
                         "프록시 주소를 등록 후 재빌드")
    # bat에 리터럴로 들어가므로 cmd 특수문자가 섞이면 조용히 깨진다 — 빌드에서 차단
    unsafe = set('%!^&<>|"') & (set(tok) | set(url))
    if unsafe:
        raise SystemExit(f"토큰/주소에 bat 비안전 문자 {sorted(unsafe)} 포함 — 주입 불가")
    return url, tok


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
    if BUILD.exists():
        shutil.rmtree(BUILD)
    CACHE.mkdir(exist_ok=True)
    embed_zip = CACHE / EMBED_URL.rsplit("/", 1)[1]
    if not embed_zip.exists():
        log(f"임베디드 Python 다운로드: {EMBED_URL}")
        _download(EMBED_URL, embed_zip)
    RT_DST.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(RT_DST)
    log(f"임베디드 Python {PY_VER} 전개")

    # ._pth = 임베디드의 sys.path 전체 정의. 플러그인 상대경로는 넣지 않는다 —
    # server.py(v0.4.1)·doctor.py가 자기 폴더를 스스로 부트스트랩하므로
    # 런타임은 어떤 위치의 플러그인이든 실행할 수 있다.
    pth = RT_DST / f"python{PY_TAG}._pth"
    pth.write_text(
        f"python{PY_TAG}.zip\n.\nLib/site-packages\n",
        encoding="ascii")

    site = RT_DST / "Lib" / "site-packages"
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
# 3. 키트 보조 파일 — install / doctor / test / cleanup / 권한 / 읽어주세요
#    (bat은 한국어 Windows 콘솔 기본 cp949로 저장 — chcp 불필요·한글 안 깨짐)
# ──────────────────────────────────────────────────────────────────
INSTALL_BAT = r"""@echo off
setlocal
echo ============================================================
echo  부패영향평가 AI - 설치 (실증 키트 v{ver} / 코드는 GitHub에서)
echo ============================================================
echo.

where claude >nul 2>&1
if errorlevel 1 (
  echo [실패] claude 명령을 찾을 수 없습니다.
  echo        0_읽어주세요.txt 의 1번 - Claude Code 설치 - 를 마친 뒤,
  echo        "새" 명령창에서 다시 실행해 주세요.
  pause & exit /b 1
)
where git >nul 2>&1
if errorlevel 1 (
  echo [실패] git 명령을 찾을 수 없습니다.
  echo        0_읽어주세요.txt 의 2번 - Git 설치 - 를 마친 뒤 다시 실행해 주세요.
  pause & exit /b 1
)

set "RTDIR=%LOCALAPPDATA%\cia\runtime"
set "PYEXE=%RTDIR%\python\python.exe"
echo [1/5] 전용 Python 런타임을 %RTDIR% 로 복사합니다 (1~2분)...
robocopy "%~dp02_런타임\runtime" "%RTDIR%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( echo [실패] 파일 복사 오류 & pause & exit /b 1 )
if not exist "%PYEXE%" ( echo [실패] 런타임 복사 확인 실패 & pause & exit /b 1 )

echo [2/5] 환경변수를 등록합니다 (이 PC 사용자 계정에 1회)...
set "CIA_PYTHON=%PYEXE%"
set "LAW_PROXY_URL={url}"
set "LAW_PROXY_TOKEN={tok}"
setx CIA_PYTHON "%PYEXE%" >nul
setx LAW_PROXY_URL "{url}" >nul
setx LAW_PROXY_TOKEN "{tok}" >nul

echo [3/5] GitHub에서 플러그인을 설치합니다 (인터넷 필요)...
call claude plugin marketplace remove corruption-impact-ai >nul 2>&1
call claude plugin marketplace add {repo}
if errorlevel 1 (
  echo [실패] GitHub 마켓플레이스 등록 실패 - 인터넷 연결과 Git 설치를 확인해 주세요.
  pause & exit /b 1
)
call claude plugin uninstall corruption-impact-ai@corruption-impact-ai --scope user >nul 2>&1
call claude plugin install corruption-impact-ai@corruption-impact-ai --scope user
if errorlevel 1 (
  echo [실패] 플러그인 설치 실패 - 위 메시지를 확인해 주세요.
  pause & exit /b 1
)

echo [4/5] 도구 사용권한을 사전 허용합니다 (권한 팝업 제거)...
"%PYEXE%" -X utf8 "%~dp0setup_permissions.py"

echo [5/5] 설치 진단(doctor)을 실행합니다...
set "PLUGDIR="
for /d %%v in ("%USERPROFILE%\.claude\plugins\cache\corruption-impact-ai\corruption-impact-ai\*") do set "PLUGDIR=%%v"
if "%PLUGDIR%"=="" (
  echo [실패] 설치된 플러그인 폴더를 찾지 못했습니다 - 위 설치 메시지를 확인해 주세요.
  pause & exit /b 1
)
echo.
"%PYEXE%" -X utf8 "%PLUGDIR%\mcp\doctor.py"
echo.
echo ============================================================
echo  위 결과가 정상이면 설치 완료입니다.
echo  Claude Code를 "완전히 종료"한 뒤 새로 열고  /사규검토  를 입력해 보세요.
echo  (환경변수 반영을 위해 완전 재시작이 꼭 필요합니다)
echo.
echo  새 버전 안내를 받으면 명령창에서 한 줄이면 됩니다:
echo     claude plugin update corruption-impact-ai
echo ============================================================
pause
"""

DOCTOR_BAT = r"""@echo off
set "PYEXE=%LOCALAPPDATA%\cia\runtime\python\python.exe"
if not exist "%PYEXE%" (
  echo [실패] 전용 런타임이 없습니다. install.bat 을 먼저 실행해 주세요.
  pause & exit /b 1
)
set "PLUGDIR="
for /d %%v in ("%USERPROFILE%\.claude\plugins\cache\corruption-impact-ai\corruption-impact-ai\*") do set "PLUGDIR=%%v"
if "%PLUGDIR%"=="" (
  echo [실패] 설치된 플러그인이 없습니다. install.bat 을 먼저 실행해 주세요.
  pause & exit /b 1
)
"%PYEXE%" -X utf8 "%PLUGDIR%\mcp\doctor.py"
pause
"""

TEST_BAT = r"""@echo off
setlocal
set "RT=%~dp02_런타임\runtime\python\python.exe"
if not exist "%RT%" (
  echo [실패] 동봉 런타임이 없습니다. zip을 "통째로" 푼 뒤 실행해 주세요.
  pause & exit /b 1
)
echo ============================================================
echo  부패영향평가 AI - 런타임 자가시험 (Claude Code 설치 불필요)
echo ============================================================
echo.
echo [1] 동봉 Python 기동
"%RT%" -X utf8 -c "import platform; print('   OK', platform.python_version(), '/', platform.platform())" || goto :fail
echo [2] 의존성 6종 import
"%RT%" -X utf8 -c "import mcp,requests,urllib3,olefile,fitz,docx; print('   OK 전부 import 성공')" || goto :fail
echo.
echo 여기까지 정상이면 이 노트북에서 런타임이 동작합니다. 결과 화면을 캡처해 주세요.
echo (서버·데이터·접속 점검은 플러그인 설치 후 doctor.bat 이 수행합니다)
pause & exit /b 0
:fail
echo.
echo [실패] 위 단계 오류 - 화면을 캡처해 감사실 감사팀에 전달해 주세요.
pause & exit /b 1
"""

CLEANUP_BAT = r"""@echo off
echo 부패영향평가 AI - 설치 흔적 정리 (시험용 PC 정리)
call claude plugin uninstall corruption-impact-ai@corruption-impact-ai --scope user 2>nul
call claude plugin marketplace remove corruption-impact-ai 2>nul
if exist "%LOCALAPPDATA%\cia" rd /s /q "%LOCALAPPDATA%\cia" && echo  - 전용 런타임 삭제 완료
if exist "%LOCALAPPDATA%\cia-plugin" rd /s /q "%LOCALAPPDATA%\cia-plugin" && echo  - 구 키트(v0.4 이하) 복사본 삭제 완료
reg delete "HKCU\Environment" /v CIA_PYTHON /f >nul 2>&1
reg delete "HKCU\Environment" /v LAW_PROXY_URL /f >nul 2>&1
reg delete "HKCU\Environment" /v LAW_PROXY_TOKEN /f >nul 2>&1
echo  - 환경변수 정리 완료 (이 키트가 등록한 접속 토큰 제거)
echo 정리 끝. (zip을 푼 이 폴더는 직접 삭제해 주세요)
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

   플러그인 코드는 GitHub에서 설치되고, 이 키트는 전용 런타임과
   접속 설정만 깔아주는 "1회용 부트스트랩"입니다.
   → 이후 버전 업데이트는 USB 없이 명령 한 줄로 끝납니다.

 0. (선택·권장) 기기 자가시험 — Claude Code 설치 전에도 가능
    이 폴더의  test_runtime.bat  더블클릭
    → 동봉 런타임·의존성을 1분 안에 점검 (이 PC에서 도는지 확인)

설치 순서 (관리자 권한 불필요, 인터넷 필요):

 1. [1회만] Claude Code 설치
    PowerShell을 열고:  irm https://claude.ai/install.ps1 | iex
    (또는 winget install Anthropic.ClaudeCode)

 2. [1회만] Git for Windows 설치  https://git-scm.com/download/win
    (기본 옵션으로 다음다음 — GitHub 설치·업데이트에 필요)

 3. Claude 로그인: 새 명령창에서  claude  실행 → 브라우저 로그인
    (직원 본인의 Claude 계정)

 4. 이 폴더의  install.bat  더블클릭
    → GitHub에서 최신 플러그인을 받아 설치하고 진단까지 실행합니다

 5. Claude Code를 "완전히 종료"한 뒤 새로 열고  /사규검토  입력 → 검토 시작
    (환경변수 반영을 위해 완전 재시작 필수)

새 버전으로 업데이트하려면 (USB 불필요):
    명령창에서  claude plugin update corruption-impact-ai

문제가 생기면: doctor.bat 더블클릭 → 결과 화면을 감사실 감사팀에 전달
(법령[4] 항목만 빨간불이면 법령 기능 외에는 모두 사용 가능합니다)

시험용 PC를 정리하려면: cleanup.bat 더블클릭 후 이 폴더 삭제
(install.bat에는 법령 프록시 접속 토큰이 들어 있으니 타인 PC에서는 정리 필수)
"""


def write_kit_files(url: str, tok: str):
    bat = (INSTALL_BAT.replace("{ver}", KIT_VERSION)
                      .replace("{url}", url)
                      .replace("{tok}", tok)
                      .replace("{repo}", GITHUB_REPO))
    (BUILD / "install.bat").write_bytes(bat.encode("cp949"))
    (BUILD / "doctor.bat").write_bytes(DOCTOR_BAT.encode("cp949"))
    (BUILD / "test_runtime.bat").write_bytes(TEST_BAT.encode("cp949"))
    (BUILD / "cleanup.bat").write_bytes(CLEANUP_BAT.encode("cp949"))
    (BUILD / "setup_permissions.py").write_text(
        SETUP_PERMISSIONS.format(rule=PERMISSION_RULE), encoding="utf-8")
    (BUILD / "0_읽어주세요.txt").write_text(
        README_TXT.format(ver=KIT_VERSION), encoding="utf-8-sig")
    (BUILD / "1_사전설치").mkdir()
    (BUILD / "1_사전설치" / "여기에_ClaudeCode와_Git_설치파일을_넣으세요.txt").write_text(
        "0_읽어주세요.txt의 1·2번 URL에서 받아 이 폴더에 보관하면\n"
        "인터넷이 느린 현장에서도 바로 설치할 수 있습니다.\n", encoding="utf-8-sig")
    log("키트 보조 파일 작성 (bat=cp949·토큰 주입, txt=utf-8-sig)")


# ──────────────────────────────────────────────────────────────────
# 4. zip 패키징 — NFC 정규화 + __MACOSX 원천 차단
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
    log(f"빌드 시작 — 키트 v{KIT_VERSION} (GitHub 설치형), Python {PY_VER} embed")
    OUT_ROOT.mkdir(exist_ok=True)
    url, token = resolve_proxy()     # 빌드 전제 확인을 먼저 (실패 조기 발견)
    pkgs = install_runtime()
    write_kit_files(url, token)
    out = make_zip()

    # 중간 빌드트리는 zip에 다 들어갔으므로 제거 (NAS 동기화 부담·중복 방지)
    shutil.rmtree(BUILD.parent, ignore_errors=True)

    size_mb = out.stat().st_size / 1e6
    log("─" * 50)
    log(f"완료: {out}")
    log(f"zip 크기: {size_mb:.0f} MB")
    log(f"동봉 패키지: {', '.join(pkgs)}")
    log(f"플러그인 코드 출처: github.com/{GITHUB_REPO} (키트 미동봉)")


if __name__ == "__main__":
    main()
