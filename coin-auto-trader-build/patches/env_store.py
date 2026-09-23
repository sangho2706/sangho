"""
.env 파일 읽기/쓰기 공용 유틸.

setup_wizard.py(CLI) 와 app.py(GUI 설정 화면) 가 같은 형식으로 .env 를
읽고 쓰도록 공용화했다. 키 순서와 주석은 .env.example 과 최대한 비슷하게
유지한다.
"""
from __future__ import annotations

from pathlib import Path

# exe(PyInstaller)로 빌드해도 기준 폴더가 임시폴더로 튀지 않도록 공용 모듈에서 가져온다.
from app_paths import BASE_DIR
ENV_PATH = BASE_DIR / ".env"

# (키, 기본값, 설명) - 설명은 GUI/CLI에서 툴팁/주석으로 재사용
FIELDS: list[tuple[str, str, str]] = [
    ("UPBIT_ACCESS_KEY", "", "업비트 Open API Access Key"),
    ("UPBIT_SECRET_KEY", "", "업비트 Open API Secret Key"),
    ("LIVE_TRADING", "false", "true=실제 주문, false=모의매매(기본, 안전)"),
    ("AUTO_TRADING_BUDGET_KRW", "500000", "자동매매에 배정할 예산(원). 이 금액과 이 돈으로 산 코인만 봇이 관리"),
    ("AUTO_SELECT_MARKETS", "true", "true=봇이 종목을 알아서 선정, false=TARGET_MARKETS 직접 지정"),
    ("TOP_N_MARKETS", "5", "자동 선정 시 동시에 감시할 후보 코인 개수"),
    ("MIN_24H_VOLUME_KRW", "3000000000", "자동 선정 시 최소 24시간 거래대금 필터(원)"),
    ("MAX_24H_CHANGE_RATE_FOR_ENTRY", "0.40", "자동 선정 시 추격매수 방지 상한 (0.40=40%, 상승장 대응)"),
    ("MIN_24H_CHANGE_RATE_FOR_ENTRY", "0", "24시간 등락률 하한 (0.15=15%% 이상 오른 종목만 노림)"),
    ("USE_PULLBACK_ENTRY", "true", "눌림목 매수 사용 (매수 기회를 늘림)"),
    ("PULLBACK_RSI_BELOW", "55", "눌림목 기준 RSI (50=신중 55=권장 60=적극)"),
    ("USE_STRONG_TREND_ENTRY", "true", "강한 상승 추세면 RSI 높아도 매수 (상승장 대응)"),
    ("STRONG_ENTRY_RSI_MAX", "85", "이 RSI 를 넘으면 추세가 강해도 매수 안 함 (과열 방지)"),
    ("STOP_LOSS_PCT", "0.05", "손절 기준 (0.05 = 진입가 대비 -5%%에서 자름)"),
    ("TRAIL_START_PCT", "0.05", "이익이 이만큼 난 뒤부터 트레일링 스톱 작동"),
    ("TRAIL_DRAWDOWN_PCT", "0.03", "고점 대비 이만큼 밀리면 이익 확정 (상한가 근처 매도)"),
    ("TAKE_PROFIT_PCT", "0", "목표 수익률 도달 시 무조건 확정 (0=미사용)"),
    ("MAX_HOLD_BARS", "0", "이만큼 주기를 들고도 성과 없으면 정리 (0=미사용)"),
    ("MARKET_RESCAN_INTERVAL_MIN", "60", "자동 선정 시 전체 마켓 재스캔 주기(분)"),
    ("TARGET_MARKETS", "KRW-BTC,KRW-ETH", "AUTO_SELECT_MARKETS=false 일 때 직접 지정하는 종목"),
    ("PATTERN_LEARNING_ENABLED", "true", "상승 패턴 학습 사용 (표본이 목표치보다 적으면 과거 데이터로 채움)"),
    ("PATTERN_TARGET_RISING", "1000", "모을 상승 패턴 개수"),
    ("USE_PATTERN_FILTER", "true", "학습 결과를 매수 판단에 반영 (모델이 검증을 통과했을 때만 실제 적용)"),
    ("PATTERN_MIN_PROBA", "0.55", "위를 켰을 때 매수에 필요한 최소 상승 확률 (0.55=55%)"),
    ("RISE_RECORD_THRESHOLD_PCT", "1.0", "구동 중 이 %% 이상 오르면 상승 기록에 남김"),
    ("EXCLUDE_MARKETS", "KRW-USDT,KRW-USDC,KRW-DAI,KRW-TUSD,KRW-BUSD", "자동 선정 대상에서 제외할 마켓"),
    ("MAX_POSITION_RATIO", "0.3", "종목당 최대 비중 (예산 대비, 0~1)"),
    ("DAILY_LOSS_LIMIT_RATIO", "0.05", "일일 손실 한도 (예산 대비, 0~1) 초과 시 당일 매수 중단"),
    ("TRADE_LOOP_INTERVAL_MIN", "5", "매매 판단 주기(분)"),
    ("REPORT_INTERVAL_HOURS", "24", "리포트 생성 주기(시간)"),
    ("REPORT_HOUR_KST", "9", "REPORT_INTERVAL_HOURS=24 일 때 리포트를 만들 시각(0-23)"),
]


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    """values 에 있는 키는 덮어쓰고, 없는 키는 FIELDS 기본값을 채워 전체를
    다시 쓴다 (항상 완전한 .env 를 생성 - 부분 수정으로 인한 누락 방지)."""
    existing = read_env()
    merged = {**existing, **values}

    lines = ["# app.py 설정 화면 / setup_wizard.py 로 생성/수정됨. 절대 공유/커밋하지 마세요."]
    for key, default, desc in FIELDS:
        val = merged.get(key, default)
        lines.append(f"# {desc}")
        lines.append(f"{key}={val}")
        lines.append("")

    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
