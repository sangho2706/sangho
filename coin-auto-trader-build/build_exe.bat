@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==============================================================
echo  coin-auto-trader  단일 EXE 빌드
echo  폴더: %CD%
echo ==============================================================
echo.

REM ---------- 1) 파이썬 찾기 ----------------------------------------------
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY (
    echo [실패] 파이썬을 찾을 수 없습니다.
    echo        https://www.python.org/downloads/ 에서 설치하고,
    echo        설치 화면에서 "Add python.exe to PATH" 를 반드시 체크하세요.
    goto :fail
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo [1/6] 파이썬: %%v

REM ---------- 2) 진입 파일 정하기 ------------------------------------------
set "ENTRY_FILE=%~1"
if "%ENTRY_FILE%"=="" (
    for /f "usebackq tokens=*" %%m in (`%PY% "_find_entry.py" 2^>nul`) do set "ENTRY=%%m"
    if not defined ENTRY (
        echo.
        %PY% "_find_entry.py"
        goto :fail
    )
) else (
    set "ENTRY=%ENTRY_FILE:.py=%"
)
if not exist "!ENTRY!.py" (
    echo [실패] !ENTRY!.py 파일이 이 폴더에 없습니다.
    goto :fail
)
echo [2/6] 진입 파일: !ENTRY!.py

REM ---------- 3) 빌드 전용 가상환경 ----------------------------------------
set "VENV=.build_venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo [3/6] 빌드용 가상환경 만드는 중... ^(처음 한 번만, 1~2분^)
    %PY% -m venv "%VENV%" || goto :fail
) else (
    echo [3/6] 빌드용 가상환경 재사용
)
set "VPY=%CD%\%VENV%\Scripts\python.exe"

REM ---------- 4) 의존성 설치 ------------------------------------------------
echo [4/6] 필요한 패키지 설치 중... ^(네트워크 상태에 따라 몇 분 걸립니다^)
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
if exist "requirements.txt" (
    "%VPY%" -m pip install -r requirements.txt --disable-pip-version-check || goto :depfail
) else (
    echo      requirements.txt 가 없어 import 구문에서 패키지를 추정합니다.
    "%VPY%" -m pip install --quiet --disable-pip-version-check ^
        requests pandas numpy python-dotenv pyjwt websocket-client
)
"%VPY%" -m pip install --upgrade pyinstaller --quiet --disable-pip-version-check || goto :fail

REM ---------- 5) 먼저 그냥 돌려본다 (여기서 원인이 드러난다) ---------------
echo.
echo [5/6] 빌드 전에 스크립트가 정상 동작하는지 3초만 확인합니다...
echo      ^(여기서 오류가 나면 exe 로 만들어도 똑같이 꺼집니다^)
echo --------------------------------------------------------------
set "CAT_ENTRY=!ENTRY!"
set "CAT_NO_PAUSE=1"
"%VPY%" -c "import importlib,sys; importlib.import_module(sys.argv[1])" "!ENTRY!" 2>&1 | more
echo --------------------------------------------------------------
echo.

REM ---------- 6) 단일 exe 빌드 ---------------------------------------------
echo [6/6] 단일 exe 빌드 중... ^(2~5분^)
set "CAT_ENTRY=!ENTRY!"
set "CAT_APPNAME=coin-auto-trader"
if exist "icon.ico" set "CAT_ICON=%CD%\icon.ico"
"%VPY%" -m PyInstaller --clean --noconfirm "coin-auto-trader.spec" || goto :buildfail

REM ---------- 설정 파일을 exe 옆에 복사 ------------------------------------
if exist ".env"          copy /y ".env"          "dist\" >nul
if exist "config.json"   copy /y "config.json"   "dist\" >nul
if exist "config.yaml"   copy /y "config.yaml"   "dist\" >nul
if exist "config.yml"    copy /y "config.yml"    "dist\" >nul
if exist "settings.json" copy /y "settings.json" "dist\" >nul
if exist "config.ini"    copy /y "config.ini"    "dist\" >nul

echo.
echo ==============================================================
echo  빌드 성공!
echo ==============================================================
echo  결과물 : %CD%\dist\coin-auto-trader.exe
echo.
echo  dist 폴더를 통째로 원하는 곳에 옮겨서 exe 를 더블클릭하세요.
echo  API 키는 exe 와 같은 폴더의 .env 파일에서 읽습니다.
echo ==============================================================
explorer "%CD%\dist"
goto :done

:depfail
echo.
echo [실패] requirements.txt 의 패키지 설치에 실패했습니다.
echo        위에 빨간 글씨로 나온 패키지 이름을 알려주시면 대응해 드립니다.
goto :fail

:buildfail
echo.
echo [실패] PyInstaller 빌드가 실패했습니다.
echo        위 출력에서 "ModuleNotFoundError" 또는 "ERROR:" 줄을 복사해서 알려주세요.
goto :fail

:fail
echo.
pause
exit /b 1

:done
echo.
pause
exit /b 0
