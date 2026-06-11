@echo off
setlocal
REM ============================================================
REM  corruption-impact-ai : one-time setup for GitHub installs
REM  - Stores law-proxy values as USER environment variables
REM    (the two values are provided separately by the operator)
REM  - Picks a Python command for the MCP server (CIA_PYTHON)
REM  Not needed for the offline Windows kit (token bundled).
REM ============================================================
echo.
echo === CIA one-time setup (law-proxy token) ===
echo Ask the plugin operator for the two values below.
echo.
set "PURL="
set "PTOK="
set /p PURL="LAW_PROXY_URL  : "
set /p PTOK="LAW_PROXY_TOKEN: "
if "%PURL%"=="" goto missing
if "%PTOK%"=="" goto missing

setx LAW_PROXY_URL "%PURL%" >nul
setx LAW_PROXY_TOKEN "%PTOK%" >nul
echo [OK] proxy values saved (user environment variables)

REM --- pick a Python command (.mcp.json default "python3" is for macOS)
set "PYCMD="
where python >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if "%PYCMD%"=="" (
    where py >nul 2>nul
    if not errorlevel 1 set "PYCMD=py"
)
if "%PYCMD%"=="" (
    echo [WARN] Python not found in PATH. Install Python 3.10+ then run this again.
) else (
    setx CIA_PYTHON "%PYCMD%" >nul
    echo [OK] CIA_PYTHON=%PYCMD%
)

echo.
echo Done. Fully quit and restart Claude Code to apply.
goto end

:missing
echo [FAIL] Empty value - nothing saved. Run again.

:end
endlocal
pause
