# 코인 자동매매 — 단일 EXE 만들기

대상 폴더: `C:\Users\hohoh\Downloads\coin-auto-trader_5\coin-auto-trader`

---

## 0. 먼저 이 5개 파일을 프로젝트 폴더에 복사

`coin-auto-trader` 폴더(= `main.py` 같은 파일이 있는 곳)에 아래 5개를 **그대로 넣습니다.**

| 파일 | 역할 |
|---|---|
| `debug_run.bat` | **창이 꺼지는 원인을 찾는 진단 실행** |
| `build_exe.bat` | 더블클릭 한 번으로 단일 exe 빌드 |
| `coin-auto-trader.spec` | PyInstaller 빌드 설정 |
| `launcher.py` | exe가 꺼지지 않게 잡아주는 런처 |
| `_find_entry.py` | 실행할 .py 파일 자동 탐지 |

---

## 1. STEP 1 — 먼저 `debug_run.bat` 더블클릭 ⭐ 가장 중요

지금 "창이 바로 꺼지는" 증상은 **시작하자마자 오류가 나는데 메시지가 순식간에 사라지는 것**입니다.
이 상태에서 exe로 만들어도 **exe도 똑같이 꺼집니다.** 그래서 원인부터 봐야 합니다.

`debug_run.bat` 은 절대 자동으로 닫히지 않고, 오류를 화면에 그대로 보여줍니다.

화면 맨 아래에 이런 식으로 나옵니다:

```
ModuleNotFoundError: No module named 'pyupbit'      ← 패키지 미설치
KeyError: 'UPBIT_ACCESS_KEY'                        ← API 키 설정 안 됨
FileNotFoundError: 'config.json'                    ← 설정파일 경로 문제
SyntaxError: invalid syntax                         ← 코드 오류
```

**이 마지막 줄을 복사해서 저에게 알려주시면 바로 고쳐드립니다.**

같은 내용이 `logs\run_날짜_시각.log` 파일로도 저장되니, 그 파일을 열어 복사하셔도 됩니다.

### 가장 흔한 원인 3가지

**① 파이썬이 아예 없음** → `debug_run.bat` 이 알려줍니다.
[python.org/downloads](https://www.python.org/downloads/) 에서 설치할 때 첫 화면의
**`Add python.exe to PATH` 체크박스를 반드시 체크**하세요. (이거 안 하면 cmd에서 안 됩니다)

**② 패키지 미설치** → cmd를 폴더에서 열고:
```cmd
pip install -r requirements.txt
```
`requirements.txt` 가 없으면 오류에 나온 이름으로: `pip install pyupbit` 처럼요.

**③ API 키 미설정** → 폴더에 `.env` 파일을 만들고:
```
UPBIT_ACCESS_KEY=발급받은_액세스키
UPBIT_SECRET_KEY=발급받은_시크릿키
```

---

## 2. STEP 2 — `build_exe.bat` 더블클릭

`debug_run.bat` 이 정상적으로 돌아가면, 이제 빌드합니다.

```
[1/6] 파이썬 확인
[2/6] 진입 파일 자동 탐지 (main.py 등)
[3/6] 빌드 전용 가상환경 생성   ← 처음 1회, 1~2분
[4/6] 패키지 설치
[5/6] 빌드 전 동작 확인
[6/6] 단일 exe 빌드            ← 2~5분
```

완료되면 탐색기가 자동으로 열리고 결과물이 나옵니다:

```
dist\coin-auto-trader.exe    ← 이 파일 하나면 끝
dist\.env                    ← API 키 (여기서 수정)
```

> 진입 파일이 자동으로 안 잡히면 파일명을 직접 지정하세요.
> cmd에서: `build_exe.bat main.py`

---

## 3. 완성된 exe 사용법

- `dist` 폴더를 통째로 원하는 위치(바탕화면 등)에 옮깁니다.
- `coin-auto-trader.exe` 를 **더블클릭**하면 실행됩니다. cmd 필요 없습니다.
- **API 키를 바꿀 때는** exe 옆의 `.env` 파일만 메모장으로 수정하면 됩니다. 재빌드 불필요.
- 실행 기록은 exe 옆 `logs` 폴더에 자동 저장됩니다.
- 오류가 나도 **창이 자동으로 닫히지 않고** 원인을 보여줍니다.

### exe 안에 API 키를 넣지 않는 이유
exe는 압축을 풀면 내용을 볼 수 있어, 키를 박아 넣으면 유출 위험이 있습니다.
그래서 키는 항상 exe **옆의 `.env` 파일**에서 읽도록 만들었습니다.
`.env` 는 절대 남에게 주거나 GitHub에 올리지 마세요.

---

## 4. 자주 겪는 문제

| 증상 | 해결 |
|---|---|
| 백신이 exe를 삭제/차단 | PyInstaller exe의 흔한 오탐입니다. 백신 예외에 `dist` 폴더를 추가하세요. (UPX 압축은 오탐을 늘려서 이미 꺼둠) |
| exe 실행 시 `ModuleNotFoundError` | 해당 이름을 `coin-auto-trader.spec` 의 `FORCE_COLLECT` 목록에 추가하고 다시 빌드 |
| exe가 `config.json` 을 못 찾음 | 그 파일을 exe와 **같은 폴더**에 두세요 (런처가 exe 폴더 기준으로 경로를 잡습니다) |
| exe 용량이 수백 MB | pandas/numpy 때문입니다. 정상입니다. 줄이려면 `spec` 의 `excludes` 에 안 쓰는 패키지를 추가 |
| 빌드가 실패 | 화면의 `ERROR:` 줄을 복사해서 알려주세요 |
| 빌드를 처음부터 다시 하고 싶음 | `build`, `dist`, `.build_venv` 폴더를 지우고 `build_exe.bat` 재실행 |

---

## 5. 참고 — 이 런처가 해결해 주는 것

PyInstaller의 `--onefile` exe는 실행 시 내용물을 **임시폴더에 풀고 거기서 돕니다.**
그래서 원본 코드가 `open("config.json")` 처럼 상대경로를 쓰면 **exe로 만든 순간 전부 실패합니다.**
이게 "cmd로는 되는데 exe로는 안 되는" 대표적인 원인입니다.

`launcher.py` 가 작업 디렉터리를 **exe가 놓인 폴더**로 되돌려 놓기 때문에,
기존 코드를 한 줄도 고치지 않아도 상대경로가 그대로 동작합니다.
