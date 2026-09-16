@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==============================================================
echo  진단 실행 - "창이 바로 꺼지는" 원인 찾기
echo ==============================================================
echo  이 창은 절대 자동으로 닫히지 않습니다.
echo  아래 내용을 그대로 복사해서 알려주세요.
echo ==============================================================
echo.

REM ---------- Microsoft Store 가짜 python 감지 -----------------------------
REM 파이썬 미설치 PC 에서 'python' 을 치면 Windows 가 Store 앱을 띄우고
REM 아무 일도 없이 종료 코드 0 을 돌려준다. run.bat 은 오류가 아니라고
REM 판단해 pause 없이 끝나고, 그래서 창이 즉시 닫힌다.
set "STORE_STUB="
for /f "delims=" %%p in ('where python 2^>nul') do (
    echo %%p | find /i "WindowsApps" >nul && set "STORE_STUB=%%p"
)
if defined STORE_STUB (
    echo  [원인 발견] Microsoft Store 의 '가짜 python' 이 잡혀 있습니다.
    echo              %STORE_STUB%
    echo.
    echo  이것은 진짜 파이썬이 아니라 Store 를 여는 바로가기입니다.
    echo  아무 일도 안 하고 "정상 종료"로 처리되기 때문에
    echo  run.bat 이 오류를 감지하지 못하고 창이 그냥 닫힙니다.
    echo.
    echo  [해결]
    echo   1) https://www.python.org/downloads/ 에서 파이썬 설치
    echo      ^(첫 화면 "Add python.exe to PATH" 반드시 체크^)
    echo   2) 또는 설정 - 앱 - 고급 앱 설정 - 앱 실행 별칭 에서
    echo      python.exe / python3.exe 를 끄기
    echo.
)

REM ---------- 진짜 파이썬 찾기 ---------------------------------------------
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python --version 2>nul | find "Python" >nul && set "PY=python"
)
if not defined PY (
    echo  [원인 확정] 사용 가능한 파이썬이 없습니다.
    echo              https://www.python.org/downloads/ 에서 설치하고
    echo              "Add python.exe to PATH" 를 꼭 체크하세요.
    goto :end
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo  파이썬 : %%v
echo  폴더   : %CD%
echo.

echo  [이 폴더의 .py 파일]
dir /b *.py 2>nul
echo.

if exist ".env" (echo  .env          : 있음) else (echo  .env          : 없음  ^<- API 키 설정 필요할 수 있음)
if exist "requirements.txt" (echo  requirements  : 있음) else (echo  requirements  : 없음)
echo.

echo  [설치된 패키지 확인]
for %%m in (pyupbit pandas numpy dotenv) do (
    %PY% -c "import %%m" >nul 2>&1 && (echo   %%m : OK) || (echo   %%m : 없음  ^<- install.bat 을 먼저 실행하세요)
)
%PY% -c "import tkinter" >nul 2>&1 && (echo   tkinter : OK) || (echo   tkinter : 없음  ^<- 파이썬 재설치 필요 ^(tcl/tk 옵션 포함^))
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
echo  실행 종료 ^(종료 코드 %ERRORLEVEL%^)
echo  logs 폴더에 같은 내용이 저장되어 있습니다.
echo ==============================================================

:end
echo.
pause
