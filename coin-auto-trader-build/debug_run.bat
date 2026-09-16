@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==============================================================
echo  진단 실행 - "창이 바로 꺼지는" 원인 찾기
echo ==============================================================
echo  이 창은 절대 자동으로 닫히지 않습니다.
echo  아래에 나오는 오류 내용을 그대로 복사해서 알려주세요.
echo ==============================================================
echo.

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY (
    echo [원인 발견] 파이썬이 설치되어 있지 않거나 PATH 에 없습니다.
    echo             -^> 이것이 창이 바로 꺼지는 가장 흔한 이유입니다.
    echo             https://www.python.org/downloads/ 에서 설치하고
    echo             "Add python.exe to PATH" 를 꼭 체크하세요.
    goto :end
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo  파이썬 : %%v
echo  폴더   : %CD%
echo.

echo  [이 폴더의 .py 파일]
dir /b *.py 2>nul
echo.

if exist ".env" (echo  .env          : 있음) else (echo  .env          : 없음)
if exist "config.json" (echo  config.json   : 있음) else (echo  config.json   : 없음)
if exist "requirements.txt" (echo  requirements  : 있음) else (echo  requirements  : 없음)
echo.

set "ENTRY_FILE=%~1"
if "%ENTRY_FILE%"=="" (
    for /f "usebackq tokens=*" %%m in (`%PY% "_find_entry.py" 2^>nul`) do set "ENTRY=%%m"
    if not defined ENTRY (
        %PY% "_find_entry.py"
        goto :end
    )
) else (
    set "ENTRY=%ENTRY_FILE:.py=%"
)

echo ==============================================================
echo  !ENTRY!.py 실행 시작
echo ==============================================================
set "CAT_ENTRY=!ENTRY!"
set "CAT_NO_PAUSE=1"
set "PYTHONUTF8=1"
set "PYTHONFAULTHANDLER=1"
%PY% -u "launcher.py"
echo.
echo ==============================================================
echo  실행 종료 (종료 코드 %ERRORLEVEL%)
echo  logs 폴더에 같은 내용이 저장되어 있습니다.
echo ==============================================================

:end
echo.
pause
