"""
설정 로더.

.env 파일에서 API 키와 각종 파라미터를 읽어온다.
API 키/시크릿을 코드에 하드코딩하지 않고, 반드시 .env 파일(또는 환경변수)로만
주입한다. setup_wizard.py 를 실행하면 대화형으로 .env 를 생성/수정할 수 있다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# exe(PyInstaller)로 빌드해도 기준 폴더가 임시폴더로 튀지 않도록 공용 모듈에서 가져온다.
from app_paths import BASE_DIR
ENV_PATH = BASE_DIR / ".env"

# .env 가 있으면 로드 (없으면 환경변수만 사용 - 서버 배포 시 유용)
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


def reload_env() -> None:
    """GUI(app.py)에서 설정을 저장한 뒤, 이미 실행 중인 프로세스가 새 .env 값을
    읽어오게 하려면 load_settings() 전에 이 함수를 호출한다."""
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=True)


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return [x.strip() for x in val.split(",") if x.strip()]


@dataclass
class Settings:
    # --- API 인증 ---
    upbit_access_key: str = field(default_factory=lambda: os.getenv("UPBIT_ACCESS_KEY", ""))
    upbit_secret_key: str = field(default_factory=lambda: os.getenv("UPBIT_SECRET_KEY", ""))

    # --- 매매 안전장치 ---
    live_trading: bool = field(default_factory=lambda: _get_bool("LIVE_TRADING", False))
    auto_trading_budget_krw: float = field(
        default_factory=lambda: _get_float("AUTO_TRADING_BUDGET_KRW", 500_000)
    )
    target_markets: list[str] = field(
        default_factory=lambda: _get_list("TARGET_MARKETS", ["KRW-BTC"])
    )
    max_position_ratio: float = field(
        default_factory=lambda: _get_float("MAX_POSITION_RATIO", 0.3)
    )
    daily_loss_limit_ratio: float = field(
        default_factory=lambda: _get_float("DAILY_LOSS_LIMIT_RATIO", 0.05)
    )

    # --- 종목 자동 선정 (AUTO_SELECT_MARKETS=true 면 TARGET_MARKETS 대신 사용) ---
    auto_select_markets: bool = field(
        default_factory=lambda: _get_bool("AUTO_SELECT_MARKETS", True)
    )
    top_n_markets: int = field(default_factory=lambda: _get_int("TOP_N_MARKETS", 5))
    min_24h_volume_krw: float = field(
        default_factory=lambda: _get_float("MIN_24H_VOLUME_KRW", 3_000_000_000)
    )
    max_24h_change_rate_for_entry: float = field(
        default_factory=lambda: _get_float("MAX_24H_CHANGE_RATE_FOR_ENTRY", 0.15)
    )
    market_rescan_interval_min: int = field(
        default_factory=lambda: _get_int("MARKET_RESCAN_INTERVAL_MIN", 60)
    )
    exclude_markets: list[str] = field(
        default_factory=lambda: _get_list(
            "EXCLUDE_MARKETS",
            ["KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-TUSD", "KRW-BUSD"],
        )
    )

    # --- 주기 설정 ---
    trade_loop_interval_min: int = field(
        default_factory=lambda: _get_int("TRADE_LOOP_INTERVAL_MIN", 5)
    )
    report_interval_hours: int = field(
        default_factory=lambda: _get_int("REPORT_INTERVAL_HOURS", 24)
    )
    report_hour_kst: int = field(default_factory=lambda: _get_int("REPORT_HOUR_KST", 9))

    # --- 매수 기회 (거래가 너무 뜸할 때 늘리는 설정) ---
    # 골든크로스는 추세가 바뀌는 순간에만 생겨서, 이미 오름세인 종목은 살
    # 기회가 없고 한 번 팔면 다시 들어갈 방법이 없다. 그래서 '오름세인데
    # 잠깐 눌렸다 반등하는' 지점을 두 번째 진입으로 쓴다.
    use_pullback_entry: bool = field(
        default_factory=lambda: _get_bool("USE_PULLBACK_ENTRY", True)
    )
    # 이 RSI 아래로 눌렸다가 다시 올라오면 매수. 높일수록 기회가 많아진다.
    pullback_rsi_below: float = field(
        default_factory=lambda: _get_float("PULLBACK_RSI_BELOW", 55.0)
    )

    # --- 위험 관리 (손실이 이익보다 커지는 것을 막는 핵심 설정) ---
    # 진입가 대비 이만큼 내려가면 무조건 자른다. 0 이면 손절 안 함(위험).
    stop_loss_pct: float = field(
        default_factory=lambda: _get_float("STOP_LOSS_PCT", 0.05)
    )
    # 이익이 이만큼 난 뒤부터 트레일링 스톱이 작동한다.
    trail_start_pct: float = field(
        default_factory=lambda: _get_float("TRAIL_START_PCT", 0.05)
    )
    # 보유 중 최고가 대비 이만큼 밀리면 이익을 확정한다 ("상한가 근처에서 매도").
    trail_drawdown_pct: float = field(
        default_factory=lambda: _get_float("TRAIL_DRAWDOWN_PCT", 0.03)
    )
    # 이 수익률에 도달하면 무조건 확정. 0 이면 사용 안 함(트레일링에 맡김).
    take_profit_pct: float = field(
        default_factory=lambda: _get_float("TAKE_PROFIT_PCT", 0.0)
    )
    # 이만큼의 판단 주기를 보유하고도 이익이 없으면 정리. 0 이면 사용 안 함.
    max_hold_bars: int = field(
        default_factory=lambda: _get_int("MAX_HOLD_BARS", 0)
    )
    # 24시간 등락률 하한. 0.15 로 두면 '15% 이상 오른 종목만' 후보가 된다.
    min_24h_change_rate_for_entry: float = field(
        default_factory=lambda: _get_float("MIN_24H_CHANGE_RATE_FOR_ENTRY", 0.0)
    )

    # --- 상승 패턴 학습 ---
    # 프로그램을 켤 때 상승 패턴 표본이 목표치보다 적으면 과거 캔들에서 채운다.
    pattern_learning_enabled: bool = field(
        default_factory=lambda: _get_bool("PATTERN_LEARNING_ENABLED", True)
    )
    pattern_target_rising: int = field(
        default_factory=lambda: _get_int("PATTERN_TARGET_RISING", 1000)
    )
    # 몇 % 이상 오른 것을 '급등' 으로 볼지. 이 기준의 직전 모습을 학습한다.
    pattern_rise_threshold_pct: float = field(
        default_factory=lambda: _get_float("PATTERN_RISE_THRESHOLD_PCT", 15.0)
    )
    # 몇 봉 안에 그만큼 오르면 급등으로 볼지 (15분봉 24개 = 6시간).
    pattern_horizon_bars: int = field(
        default_factory=lambda: _get_int("PATTERN_HORIZON_BARS", 24)
    )
    # 학습 모델을 매매에 어떻게 쓸지.
    #   signal = 급등 전조를 발견하면 모델이 직접 매수를 낸다 (미리 사는 방식)
    #   filter = RSI/이동평균이 낸 매수를 검토만 한다 (거부만 가능)
    #   off    = 사용하지 않는다
    pattern_entry_mode: str = field(
        default_factory=lambda: (os.getenv("PATTERN_ENTRY_MODE", "signal") or "signal").strip().lower()
    )
    pattern_retrain_hours: int = field(
        default_factory=lambda: _get_int("PATTERN_RETRAIN_HOURS", 6)
    )
    # 학습 결과를 실제 매수 판단에 반영할지. 기본은 false(참고용)이다.
    # 돈이 걸린 로직이라 사용자가 직접 켜기 전에는 매매가 달라지지 않는다.
    use_pattern_filter: bool = field(
        default_factory=lambda: _get_bool("USE_PATTERN_FILTER", True)
    )
    pattern_min_proba: float = field(
        default_factory=lambda: _get_float("PATTERN_MIN_PROBA", 0.55)
    )
    # 구동 중 이 비율 이상 오르면 '상승'으로 기록한다 (%).
    rise_record_threshold_pct: float = field(
        default_factory=lambda: _get_float("RISE_RECORD_THRESHOLD_PCT", 1.0)
    )

    # --- 경로 ---
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "trader.db")
    reports_dir: Path = field(default_factory=lambda: BASE_DIR / "reports")
    logs_dir: Path = field(default_factory=lambda: BASE_DIR / "logs")

    def validate_for_live(self) -> list[str]:
        """실거래 모드로 전환하기 전에 확인해야 할 문제 목록을 반환한다."""
        problems = []
        if not self.upbit_access_key or "여기에" in self.upbit_access_key:
            problems.append("UPBIT_ACCESS_KEY 가 설정되지 않았습니다.")
        if not self.upbit_secret_key or "여기에" in self.upbit_secret_key:
            problems.append("UPBIT_SECRET_KEY 가 설정되지 않았습니다.")
        if self.auto_trading_budget_krw <= 0:
            problems.append("AUTO_TRADING_BUDGET_KRW 는 0보다 커야 합니다.")
        if not (0 < self.max_position_ratio <= 1):
            problems.append("MAX_POSITION_RATIO 는 0~1 사이여야 합니다.")
        if self.pattern_entry_mode not in ("signal", "filter", "off"):
            problems.append(
                "PATTERN_ENTRY_MODE 는 signal / filter / off 중 하나여야 합니다."
            )
        if self.pattern_rise_threshold_pct <= 0:
            problems.append("PATTERN_RISE_THRESHOLD_PCT 는 0보다 커야 합니다 (15 = 15%).")
        if self.pattern_horizon_bars <= 0:
            problems.append("PATTERN_HORIZON_BARS 는 1 이상이어야 합니다.")
        if not (0 < self.pullback_rsi_below < 100):
            problems.append("PULLBACK_RSI_BELOW 는 0과 100 사이여야 합니다 (55 권장).")
        if self.stop_loss_pct <= 0:
            problems.append(
                "STOP_LOSS_PCT 가 0 입니다. 손절이 없으면 한 종목의 손실이 무한정 "
                "커질 수 있습니다 (0.05 = -5% 권장)."
            )
        if self.min_24h_change_rate_for_entry >= self.max_24h_change_rate_for_entry:
            problems.append(
                "MIN_24H_CHANGE_RATE_FOR_ENTRY 가 MAX 보다 크거나 같습니다. "
                "이러면 후보가 하나도 남지 않습니다 (예: MIN=0.15 이면 MAX=0.40)."
            )
        if self.auto_select_markets:
            if self.top_n_markets <= 0:
                problems.append("TOP_N_MARKETS 는 1 이상이어야 합니다.")
        elif not self.target_markets:
            problems.append("TARGET_MARKETS 가 비어 있습니다 (또는 AUTO_SELECT_MARKETS=true 로 자동 선정을 쓰세요).")
        return problems


def load_settings() -> Settings:
    s = Settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    return s


if __name__ == "__main__":
    s = load_settings()
    print("현재 설정:")
    for k, v in s.__dict__.items():
        if "key" in k.lower():
            v = (v[:4] + "****") if v else "(미설정)"
        print(f"  {k}: {v}")
