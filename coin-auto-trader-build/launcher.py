# -*- coding: utf-8 -*-
"""
coin-auto-trader 단일 exe 런처

역할
  1) exe가 어떤 이유로 죽어도 창이 즉시 닫히지 않게 한다 (traceback + 대기).
  2) onefile exe의 고질적 문제인 '경로'를 바로잡는다.
     - PyInstaller onefile은 실행 시 임시폴더(_MEIPASS)에 풀린 뒤 거기서 돈다.
       그래서 .env / config.json / *.db / 로그를 상대경로로 열면 전부 실패한다.
     - 여기서 작업 디렉터리를 'exe가 놓인 폴더'로 고정해, 기존 상대경로 코드를
       고치지 않아도 그대로 동작하게 한다.
  3) exe 옆의 .env 를 자동으로 읽어들인다(python-dotenv 설치돼 있을 때).
  4) 모든 출력과 오류를 logs\\ 폴더에 파일로도 남긴다.
"""
import os
import sys
import runpy
import traceback
import datetime
import faulthandler

# 실행할 원본 진입 모듈 (build_exe.bat 이 빌드 시 자동으로 채워 넣는다)
ENTRY_MODULE = os.environ.get("CAT_ENTRY", "main")


def app_dir() -> str:
    """exe(또는 스크립트)가 실제로 놓여 있는 폴더. _MEIPASS 임시폴더가 아니다."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class _Tee:
    """콘솔과 로그파일에 동시에 쓴다."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        try:
            if self._stream is not None:
                self._stream.write(data)
        except Exception:
            pass
        try:
            self._fh.write(data)
            self._fh.flush()
        except Exception:
            pass

    def flush(self):
        for t in (self._stream, self._fh):
            try:
                if t is not None:
                    t.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._stream is not None and self._stream.isatty()
        except Exception:
            return False


def main() -> int:
    base = app_dir()

    # (2) 상대경로가 exe 옆을 가리키도록 작업 디렉터리 고정
    try:
        os.chdir(base)
    except Exception:
        pass

    # 한글 깨짐 방지
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # (4) 로그 파일 준비
    log_dir = os.path.join(base, "logs")
    log_path = None
    log_fh = None
    try:
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(log_dir, "run_%s.log" % stamp)
        log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.stdout, log_fh)
        sys.stderr = _Tee(sys.stderr, log_fh)
        faulthandler.enable(log_fh)
    except Exception:
        pass

    print("=" * 62)
    print(" coin-auto-trader")
    print(" 실행 폴더 : %s" % base)
    print(" 진입 모듈 : %s" % ENTRY_MODULE)
    if log_path:
        print(" 로그 파일 : %s" % log_path)
    print(" 시작 시각 : %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 62)

    # (3) exe 옆 .env 자동 로드
    env_path = os.path.join(base, ".env")
    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
            print("[설정] .env 를 불러왔습니다.")
        except ImportError:
            print("[설정] .env 가 있지만 python-dotenv 가 없어 건너뜁니다.")
        except Exception as exc:
            print("[설정] .env 로드 실패: %s" % exc)

    # exe 옆 폴더도 import 경로에 넣어, 나중에 .py 를 추가해도 읽히게 한다
    if base not in sys.path:
        sys.path.insert(0, base)

    # (1) 원본 프로그램을 __main__ 으로 실행
    try:
        runpy.run_module(ENTRY_MODULE, run_name="__main__", alter_sys=True)
        print()
        print("[종료] 프로그램이 정상적으로 끝났습니다.")
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        print()
        print("[종료] 프로그램이 종료 코드 %s 로 끝났습니다." % code)
        return code
    except KeyboardInterrupt:
        print()
        print("[종료] 사용자가 Ctrl+C 로 중단했습니다.")
        return 130
    except BaseException:
        print()
        print("!" * 62)
        print(" 오류가 발생해 프로그램이 중단되었습니다.")
        print("!" * 62)
        traceback.print_exc()
        print()
        print(" ↑ 위 내용(특히 맨 아래 줄)을 그대로 복사해서 알려주시면 원인을 잡아드립니다.")
        if log_path:
            print(" 같은 내용이 여기에도 저장되어 있습니다: %s" % log_path)
        return 1


if __name__ == "__main__":
    rc = main()
    # 더블클릭으로 실행했을 때 창이 그냥 닫히지 않도록 잡아둔다
    if os.environ.get("CAT_NO_PAUSE") != "1":
        try:
            print()
            input("창을 닫으려면 Enter 키를 누르세요... ")
        except Exception:
            pass
    sys.exit(rc)
