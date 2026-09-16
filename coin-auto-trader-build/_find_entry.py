# -*- coding: utf-8 -*-
"""프로젝트의 진입 스크립트(.py)를 찾아 모듈명만 출력한다. build_exe.bat 이 사용."""
import os
import re
import sys

PRIORITY = [
    "main.py", "app.py", "run.py", "start.py", "bot.py", "trader.py",
    "auto_trade.py", "auto_trader.py", "coin_auto_trader.py", "trade.py",
    "__main__.py",
]

SKIP = {"launcher.py", "setup.py", "conftest.py", "_find_entry.py"}


def looks_like_entry(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except Exception:
        return False
    return bool(re.search(r"__name__\s*==\s*[\"']__main__[\"']", src))


def main():
    here = os.path.abspath(os.getcwd())
    files = [f for f in os.listdir(here)
             if f.endswith(".py") and f not in SKIP and not f.startswith("test_")]

    for cand in PRIORITY:
        if cand in files:
            print(cand[:-3])
            return 0

    guessed = [f for f in sorted(files) if looks_like_entry(os.path.join(here, f))]
    if len(guessed) == 1:
        print(guessed[0][:-3])
        return 0
    if len(guessed) > 1:
        sys.stderr.write("진입 후보가 여러 개입니다: %s\n" % ", ".join(guessed))
        sys.stderr.write("build_exe.bat 뒤에 파일명을 붙여 실행하세요.  예) build_exe.bat %s\n" % guessed[0])
        return 2

    sys.stderr.write("실행할 .py 파일을 찾지 못했습니다.\n")
    sys.stderr.write("build_exe.bat 뒤에 파일명을 붙여 실행하세요.  예) build_exe.bat main.py\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
