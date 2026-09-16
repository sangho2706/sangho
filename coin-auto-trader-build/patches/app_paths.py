"""
프로그램의 기준 폴더(BASE_DIR)를 한 곳에서 정한다.

왜 필요한가
-----------
기존 코드는 각 모듈마다 아래처럼 기준 폴더를 잡고 있었다.

    BASE_DIR = Path(__file__).resolve().parent

이 방식은 python 으로 직접 실행할 때는 잘 동작하지만,
PyInstaller 로 만든 단일 exe(--onefile)에서는 깨진다.

단일 exe 는 실행될 때 내용물을 임시폴더(예: C:\\Users\\...\\Temp\\_MEIxxxxx)에
풀어놓고 그 안에서 동작한다. 그래서 __file__ 은 exe 가 놓인 폴더가 아니라
그 임시폴더를 가리키게 되고, 결과적으로

    .env            -> 임시폴더에서 찾음 -> 설정을 못 읽음
    data/trader.db  -> 임시폴더에 만들어짐 -> 종료 시 통째로 사라짐
    reports/        -> 임시폴더에 생성됨 -> 리포트를 찾을 수 없음
    data/app_lock.json -> 매번 새로 만들라고 나옴

같은 증상이 생긴다. 이 파일은 frozen(=exe) 상태를 감지해 기준 폴더를
'exe 가 실제로 놓인 폴더'로 되돌린다. 일반 python 실행 시에는 예전과
완전히 동일하게 동작하므로 run.bat 사용에는 아무 영향이 없다.
"""
from __future__ import annotations

import sys
from pathlib import Path


def get_base_dir() -> Path:
    """설정/DB/리포트를 읽고 쓸 기준 폴더."""
    if getattr(sys, "frozen", False):
        # PyInstaller exe: exe 파일이 놓인 폴더
        return Path(sys.executable).resolve().parent
    # 일반 python 실행: 소스가 있는 폴더 (기존 동작과 동일)
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
