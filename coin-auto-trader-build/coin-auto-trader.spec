# -*- mode: python ; coding: utf-8 -*-
"""
coin-auto-trader 단일 exe 빌드 스펙 (PyInstaller)

build_exe.bat 이 이 파일을 사용합니다. 직접 쓰려면:
    pyinstaller --clean --noconfirm coin-auto-trader.spec
"""
import os
import re
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_DIR = os.path.abspath(os.getcwd())
ENTRY_MODULE = os.environ.get("CAT_ENTRY", "main")
APP_NAME = os.environ.get("CAT_APPNAME", "coin-auto-trader")
ICON_FILE = os.environ.get("CAT_ICON", "").strip()

hiddenimports = [ENTRY_MODULE]
datas = []
binaries = []


def _log(msg):
    print("[spec] %s" % msg)


# --------------------------------------------------------------------------
# 1) 프로젝트 안의 모든 최상위 .py 를 hiddenimport 로 넣는다.
#    (조건부 import / importlib 로 불러오는 모듈이 빠지는 사고를 막는다)
# --------------------------------------------------------------------------
for fname in sorted(os.listdir(PROJECT_DIR)):
    if not fname.endswith(".py"):
        continue
    mod = fname[:-3]
    if mod in ("launcher", "_find_entry", "setup", "conftest") or mod.startswith("test_"):
        continue
    if mod not in hiddenimports:
        hiddenimports.append(mod)

# 하위 패키지 폴더(__init__.py 보유)도 통째로 수집
for name in sorted(os.listdir(PROJECT_DIR)):
    pkg_dir = os.path.join(PROJECT_DIR, name)
    if os.path.isdir(pkg_dir) and os.path.exists(os.path.join(pkg_dir, "__init__.py")):
        if name in ("build", "dist", "venv", ".venv"):
            continue
        try:
            hiddenimports += collect_submodules(name)
            _log("로컬 패키지 수집: %s" % name)
        except BaseException as exc:
            _log("로컬 패키지 %s 수집 실패: %s" % (name, exc))


# --------------------------------------------------------------------------
# 2) 동적 import 때문에 PyInstaller 가 놓치기 쉬운 거래 관련 라이브러리는
#    통째로(collect_all) 끌어온다. 설치돼 있는 것만 처리한다.
# --------------------------------------------------------------------------
FORCE_COLLECT = [
    "ccxt",          # 해외 거래소 통합 (거래소별 파일을 동적 로드 -> 필수)
    "pyupbit",       # 업비트
    "python_bithumb",
    "jwt",           # PyJWT (업비트/빗썸 인증)
    "dotenv",
    "websockets",
    "websocket",     # websocket-client
    "aiohttp",
    "certifi",
    "charset_normalizer",
    "idna",
    "schedule",
    "yaml",
    "ta",            # 기술적 지표
    "pandas_ta",
    "telegram",      # 텔레그램 알림
    "telebot",
    "tzdata",
    "pytz",
    "dateutil",
]

def _importable(mod):
    """설치 여부만 본다. 설치가 깨진 패키지는 Exception 이 아닌 예외를 던지므로
    BaseException 까지 받아내야 빌드 전체가 죽지 않는다."""
    try:
        __import__(mod)
        return True
    except BaseException as exc:
        _log("%s 는 import 되지 않아 건너뜁니다 (%s)" % (mod, type(exc).__name__))
        return False


for pkg in FORCE_COLLECT:
    if not _importable(pkg):
        continue
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
        _log("전체 수집: %s" % pkg)
    except BaseException as exc:
        _log("%s 수집 실패(무시): %s" % (pkg, exc))


# --------------------------------------------------------------------------
# 3) requirements.txt 에 적힌 패키지도 빠짐없이 훑는다.
# --------------------------------------------------------------------------
IMPORT_NAME_FIX = {
    "python-dotenv": "dotenv",
    "pyjwt": "jwt",
    "websocket-client": "websocket",
    "beautifulsoup4": "bs4",
    "pyyaml": "yaml",
    "python-dateutil": "dateutil",
    "python-telegram-bot": "telegram",
    "pytelegrambotapi": "telebot",
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "ta-lib": "talib",
}
SKIP_REQ = {"pyinstaller", "pyinstaller-hooks-contrib", "pip", "setuptools", "wheel"}

req_path = os.path.join(PROJECT_DIR, "requirements.txt")
if os.path.exists(req_path):
    with open(req_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[\s\[<>=!~;]", line, 1)[0].strip().lower()
            if not name or name in SKIP_REQ:
                continue
            mod = IMPORT_NAME_FIX.get(name, name.replace("-", "_"))
            if mod in FORCE_COLLECT:
                continue
            if not _importable(mod):
                continue
            try:
                hiddenimports += collect_submodules(mod)
                _log("서브모듈 수집: %s" % mod)
            except BaseException as exc:
                _log("%s 서브모듈 수집 실패(무시): %s" % (mod, exc))


# --------------------------------------------------------------------------
# 4) 프로그램이 읽는 데이터 파일을 exe 안에도 포함(기본값 역할).
#    실제로 사용자가 수정하는 설정 파일은 build_exe.bat 이 exe 옆에 복사한다.
# --------------------------------------------------------------------------
for fname in ("config.json", "config.yaml", "config.yml", "settings.json", "config.ini"):
    p = os.path.join(PROJECT_DIR, fname)
    if os.path.exists(p):
        datas.append((p, "."))
        _log("데이터 포함: %s" % fname)

for dname in ("data", "assets", "templates", "resources", "static", "strategies"):
    p = os.path.join(PROJECT_DIR, dname)
    if os.path.isdir(p):
        datas.append((p, dname))
        _log("데이터 폴더 포함: %s" % dname)


# --------------------------------------------------------------------------
# 5) 용량 줄이기 - 트레이딩 봇에 보통 필요 없는 무거운 GUI/과학 패키지 제외.
#    (프로그램이 실제로 쓰면 아래 목록에서 해당 줄을 지우세요)
# --------------------------------------------------------------------------
excludes = [
    "tkinter", "matplotlib", "IPython", "jupyter", "notebook",
    "pytest", "sphinx", "PyQt6", "wx",
]


def _imported_names():
    """프로젝트 소스가 실제로 import 하는 최상위 모듈 이름을 모은다.
    단순 문자열 포함 검사는 'pip' 가 'recipe' 에 걸리는 식으로 오작동하므로
    import 구문만 정규식으로 본다."""
    names = set()
    pattern = re.compile(
        r"^\s*(?:from\s+([A-Za-z_][\w]*)|import\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*))",
        re.MULTILINE,
    )
    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs
                   if d not in ("build", "dist", "venv", ".venv", ".build_venv",
                                "__pycache__", ".git")]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, fname), "r", encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
            except Exception:
                continue
            for m_from, m_import in pattern.findall(src):
                if m_from:
                    names.add(m_from)
                for part in m_import.split(","):
                    part = part.strip()
                    if part:
                        names.add(part)
    return names


# 프로젝트가 실제로 import 하면 제외 목록에서 자동으로 뺀다
try:
    used = _imported_names()
    kept = [e for e in excludes if e not in used]
    if len(kept) != len(excludes):
        _log("실제로 사용 중이라 제외하지 않음: %s"
             % ", ".join(e for e in excludes if e in used))
    excludes = kept
except Exception as exc:
    _log("제외 목록 판정 실패(기본값 사용): %s" % exc)

hiddenimports = sorted(set(hiddenimports))
_log("hiddenimports %d개, datas %d개" % (len(hiddenimports), len(datas)))


a = Analysis(
    ["launcher.py"],
    pathex=[PROJECT_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX 압축은 백신 오탐을 크게 늘려 끔
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,             # 로그를 봐야 하므로 콘솔 창 유지
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE if ICON_FILE and os.path.exists(ICON_FILE) else None,
)
