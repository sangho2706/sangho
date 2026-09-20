"""
상승 패턴 수집 · 학습.

목표
----
"단기간에 크게 오른(기본 15% 이상) 종목들이 오르기 '직전' 에 어떤 모습이었는지
그 패턴을 모아서 학습한다. 그리고 지금 그 모습인 종목을 미리 사서, 고점에서
트레일링 스톱으로 판다."

이미 오른 종목을 쫓아 사는 것이 아니다. 오르기 전의 신호를 찾는 것이다.

- 표본이 부족하면 과거 캔들을 내려받아 소급 수집(backfill)한다.
- 구동 중에도 매 루프마다 새 표본을 모은다(수집만; 라벨은 미래가 지나야
  확정되므로 다음 backfill 때 채워진다).

라벨 정의
---------
어떤 시점의 캔들에서 앞으로 HORIZON 개 캔들 안에 종가가 RISE_THRESHOLD_PCT
이상 오르면 그 시점의 특징을 "상승 패턴(label=1)"으로 본다. 아니면 0.

중요 — 미래 정보 누설 방지
-------------------------
특징(features)은 반드시 그 시점까지의 데이터로만 계산한다. 라벨을 만들 때만
미래 캔들을 쓴다. 이 구분이 깨지면 학습 결과는 실전에서 무의미해진다.

학습 모델
---------
외부 머신러닝 라이브러리(scikit-learn 등)를 새로 받지 않고, numpy 만으로
로지스틱 회귀를 경사하강법으로 학습한다. exe 용량을 늘리지 않으면서도
"이 패턴이 오를 확률"을 0~1 로 내놓을 수 있다.

안전 원칙
---------
학습 결과는 기본적으로 '참고용'이다. .env 의 USE_PATTERN_FILTER=true 로
명시적으로 켜기 전에는 실제 매매 판단을 바꾸지 않는다. 사용자의 돈이 걸린
로직을 말없이 바꾸지 않기 위함이다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("patterns")

# 기본 파라미터
TARGET_RISING = 1000        # 목표 상승 패턴 개수
HORIZON = 24                # 앞으로 몇 개 캔들 안에 오르면 '급등'으로 볼지 (15분봉 24개 = 6시간)
RISE_THRESHOLD_PCT = 15.0   # 몇 % 이상 올라야 '급등'으로 볼지
INTERVAL = "minute15"
# 특징 목록을 바꾸면 예전 표본과 호환되지 않는다. 그래서 버전을 같이 둔다.
FEATURE_VERSION = 2
WARMUP_BARS = 60            # 특징 계산에 필요한 최소 과거 봉 수

FEATURE_NAMES = [
    # --- 기본 지표 ---
    "rsi14",          # RSI (과매수/과매도)
    "ma_ratio",       # 단기MA / 장기MA - 1  (추세 방향)
    "mom_1",          # 직전 1봉 수익률
    "mom_3",          # 직전 3봉 수익률
    "mom_6",          # 직전 6봉 수익률
    "volatility",     # 최근 변동성
    "vol_ratio",      # 최근 거래량 / 평균 거래량(20봉)
    "range_pos",      # 최근 고저 범위에서 현재가 위치 (0=저점, 1=고점)
    # --- 급등 직전에 나타나기 쉬운 모습들 ---
    "vol_surge",      # 거래량이 평소(50봉)보다 몇 배인가 - 자금 유입 신호
    "squeeze",        # 최근 10봉 변동폭 / 50봉 변동폭 - 1보다 작으면 '눌림(압축)'
    "dist_from_high", # 50봉 최고가 대비 얼마나 아래인가 - 바닥 근처인지
    "up_streak",      # 최근 연속 상승 봉 수 (5봉 기준으로 정규화)
]


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """캔들 DataFrame 에서 시점별 특징을 계산한다.

    각 행의 값은 '그 시점까지의 정보' 만으로 만들어진다 (미래 정보 없음).
    """
    close = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df else pd.Series(
        np.ones(len(df)), index=df.index
    )

    ma_short = close.rolling(5).mean()
    ma_long = close.rolling(20).mean()
    roll_max = close.rolling(20).max()
    roll_min = close.rolling(20).min()
    vol_mean = volume.rolling(20).mean()

    feat = pd.DataFrame(index=df.index)
    feat["rsi14"] = _rsi(close, 14)
    feat["ma_ratio"] = (ma_short / ma_long - 1.0) * 100
    feat["mom_1"] = close.pct_change(1) * 100
    feat["mom_3"] = close.pct_change(3) * 100
    feat["mom_6"] = close.pct_change(6) * 100
    feat["volatility"] = close.pct_change().rolling(20).std() * 100
    feat["vol_ratio"] = volume / vol_mean.replace(0, np.nan)
    rng = (roll_max - roll_min).replace(0, np.nan)
    feat["range_pos"] = (close - roll_min) / rng

    # --- 급등 직전 신호 ---
    # 거래량 급증: 조용하던 종목에 갑자기 돈이 들어오는 모습
    vol_mean_50 = volume.rolling(50).mean()
    feat["vol_surge"] = volume / vol_mean_50.replace(0, np.nan)

    # 변동성 압축: 최근 움직임이 평소보다 작아진 상태(눌림). 급등 직전에 흔하다.
    rng_10 = (close.rolling(10).max() - close.rolling(10).min())
    rng_50 = (close.rolling(50).max() - close.rolling(50).min())
    feat["squeeze"] = rng_10 / rng_50.replace(0, np.nan)

    # 50봉 최고가 대비 현재 위치. 0 에 가까우면 고점, 크면 눌려 있는 상태.
    max_50 = close.rolling(50).max()
    feat["dist_from_high"] = (max_50 - close) / max_50.replace(0, np.nan) * 100

    # 연속 상승 봉 수 (5봉으로 나눠 크기를 맞춤)
    up = (close.diff() > 0).astype(float)
    streak = up.groupby((up != up.shift()).cumsum()).cumsum() * up
    feat["up_streak"] = streak / 5.0

    return feat[FEATURE_NAMES].replace([np.inf, -np.inf], np.nan)


def extract_samples(market: str, df: pd.DataFrame, interval: str = INTERVAL,
                    horizon: int = HORIZON,
                    rise_threshold_pct: float = RISE_THRESHOLD_PCT,
                    source: str = "backfill") -> list[tuple]:
    """캔들에서 (ts, market, interval, features_json, future_return_pct, label, source)
    튜플 목록을 만든다. db.insert_patterns() 에 그대로 넣을 수 있다."""
    if df is None or len(df) < WARMUP_BARS + horizon + 5:
        return []

    feat = build_feature_frame(df)
    close = df["close"].astype(float).to_numpy()
    n = len(df)
    rows: list[tuple] = []

    for i in range(WARMUP_BARS, n - horizon):
        values = feat.iloc[i]
        if values.isna().any():
            continue
        # 라벨: 앞으로 horizon 개 캔들 중 '최고 종가' 기준 상승률
        future_max = close[i + 1:i + 1 + horizon].max()
        future_return = (future_max - close[i]) / close[i] * 100
        label = 1 if future_return >= rise_threshold_pct else 0
        ts = df.index[i]
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        rows.append((
            ts_str, market, interval,
            json.dumps({k: float(values[k]) for k in FEATURE_NAMES}),
            float(future_return), int(label), source,
        ))
    return rows


def rule_signature(rise_threshold_pct: float, horizon: int, interval: str) -> str:
    """지금의 학습 규칙을 한 줄로 나타낸다."""
    return (f"v{FEATURE_VERSION}|{interval}|{horizon}bars|"
            f"{rise_threshold_pct:g}pct|{len(FEATURE_NAMES)}feat")


def ensure_rule(database, rise_threshold_pct: float, horizon: int,
                interval: str, progress=None) -> bool:
    """학습 규칙이 예전과 다르면 표본을 비운다.

    급등 기준(몇 %)이나 관찰 기간, 특징 목록이 바뀌면 예전 표본은 라벨과
    특징이 모두 달라져 그대로 섞어 쓰면 학습이 망가진다. 지우고 다시 모으는
    것이 맞다. 반환값은 '비웠는가'.
    """
    sig = rule_signature(rise_threshold_pct, horizon, interval)
    old = database.get_meta("pattern_rule")
    if old == sig:
        return False
    removed = database.clear_patterns()
    database.set_meta("pattern_rule", sig)
    if old is not None and removed:
        msg = (f"학습 기준이 바뀌어(이전: {old} → 현재: {sig}) "
               f"기존 표본 {removed:,}개를 비우고 다시 모읍니다.")
        log.info(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass
    return True


def collect_until_target(exchange, database, target_rising: int = TARGET_RISING,
                         interval: str = INTERVAL, candles_per_call: int = 1200,
                         max_markets: int = 120,
                         rise_threshold_pct: float = RISE_THRESHOLD_PCT,
                         horizon: int = HORIZON,
                         progress=None) -> dict:
    """상승 패턴이 target_rising 개가 될 때까지 과거 캔들에서 표본을 모은다.

    이미 충분하면 아무 것도 하지 않고 즉시 돌아온다.
    progress: 진행 상황을 문자열로 받는 콜백 (GUI 로그용, 선택).
    """
    def _say(msg: str) -> None:
        log.info(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    # 급등 기준이 바뀌었으면 예전 표본은 쓸 수 없으므로 먼저 정리한다.
    ensure_rule(database, rise_threshold_pct, horizon, interval, progress=progress)

    have = database.count_patterns(label=1)
    if have >= target_rising:
        _say("상승 패턴 %d개 확보됨 (목표 %d) - 추가 수집이 필요 없습니다."
             % (have, target_rising))
        return {"collected": 0, "rising": have, "scanned_markets": 0, "skipped": True}

    _say("급등(%.0f%% 이상 / %d봉 이내) 패턴이 %d개뿐입니다. 목표 %d개까지 "
         "과거 데이터에서 찾습니다. 기준이 높을수록 드물어서 시간이 걸립니다."
         % (rise_threshold_pct, horizon, have, target_rising))

    try:
        markets = exchange.get_krw_tickers()
    except Exception:
        log.exception("마켓 목록 조회 실패 - 패턴 수집을 건너뜁니다.")
        return {"collected": 0, "rising": have, "scanned_markets": 0, "error": True}

    markets = markets[:max_markets]
    total_new = 0
    scanned = 0

    for market in markets:
        if database.count_patterns(label=1) >= target_rising:
            break
        try:
            df = exchange.get_ohlcv(market, interval=interval, count=candles_per_call)
        except Exception:
            log.warning("%s 캔들 조회 실패 - 건너뜁니다.", market)
            continue
        if df is None or len(df) < WARMUP_BARS + horizon + 5:
            continue

        rows = extract_samples(market, df, interval=interval,
                               horizon=horizon,
                               rise_threshold_pct=rise_threshold_pct)
        added = database.insert_patterns(rows)
        total_new += added
        scanned += 1
        if added:
            _say("  %s: 표본 %d개 추가 (누적 상승 패턴 %d개)"
                 % (market, added, database.count_patterns(label=1)))

    rising = database.count_patterns(label=1)
    total_all = database.count_patterns()
    ratio = (rising / total_all * 100) if total_all else 0.0
    _say("패턴 수집 완료: 표본 %d개 추가, 급등 패턴 %d개 / 전체 %d개 (%.1f%%), 마켓 %d개 조회"
         % (total_new, rising, total_all, ratio, scanned))
    if rising < target_rising:
        _say("목표(%d개)에 못 미쳤습니다. %.0f%% 급등은 드물어서 그렇습니다. "
             "기준을 낮추거나(예: 10%%) 관찰 기간을 늘리면 표본이 늘어납니다."
             % (target_rising, rise_threshold_pct))
    return {"collected": total_new, "rising": rising, "scanned_markets": scanned,
            "skipped": False}


# --------------------------------------------------------------------------
# 학습 (numpy 로지스틱 회귀)
# --------------------------------------------------------------------------

@dataclass
class PatternModel:
    weights: list[float]
    bias: float
    mean: list[float]
    std: list[float]
    feature_names: list[str]
    trained_at: str = ""
    n_samples: int = 0
    n_rising: int = 0
    accuracy: float = 0.0          # 학습 데이터 기준 (참고용, 낙관적으로 나옴)

    # --- 검증(안 본 데이터) 기준 지표. 실제 판단은 전부 이 값으로 한다. ---
    threshold: float = 0.55        # 이 확률 이상일 때 '오른다'고 본 것
    val_samples: int = 0           # 검증에 쓴 표본 수
    val_signals: int = 0           # 그중 모델이 '오른다'고 한 횟수
    val_precision: float = 0.0     # 그 예측이 실제로 맞은 비율  ← 매매에서 가장 중요
    val_recall: float = 0.0        # 실제 상승 중 잡아낸 비율
    val_accuracy: float = 0.0
    val_base_rate: float = 0.0     # 아무거나 샀을 때의 상승 비율 (비교 기준선)
    edge: float = 0.0              # val_precision - val_base_rate (0 이하면 쓸모 없음)
    z_score: float = 0.0           # 이득이 우연인지 판별하는 값 (2 이상이면 우연일 확률 5% 미만)
    is_useful: bool = False        # 기준선을 '우연이 아니게' 넘었는지

    def to_dict(self) -> dict:
        return {
            "weights": self.weights, "bias": self.bias, "mean": self.mean,
            "std": self.std, "feature_names": self.feature_names,
            "trained_at": self.trained_at, "n_samples": self.n_samples,
            "n_rising": self.n_rising, "accuracy": self.accuracy,
            "threshold": self.threshold, "val_samples": self.val_samples,
            "val_signals": self.val_signals, "val_precision": self.val_precision,
            "val_recall": self.val_recall, "val_accuracy": self.val_accuracy,
            "val_base_rate": self.val_base_rate, "edge": self.edge,
            "z_score": self.z_score, "is_useful": self.is_useful,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PatternModel":
        # 예전 버전이 저장한 파일에는 검증 지표가 없으므로 아는 항목만 취한다
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def predict_proba(self, features: dict) -> float:
        """이 패턴이 오를 확률 (0~1)."""
        x = np.array([features.get(k, 0.0) for k in self.feature_names], dtype=float)
        mean = np.array(self.mean)
        std = np.array(self.std)
        std = np.where(std == 0, 1.0, std)
        z = (x - mean) / std
        logit = float(np.dot(z, np.array(self.weights)) + self.bias)
        return float(1.0 / (1.0 + np.exp(-np.clip(logit, -30, 30))))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["PatternModel"]:
        if not path.exists():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return None


def train(database, epochs: int = 400, lr: float = 0.1,
          threshold: float = 0.55, val_ratio: float = 0.25) -> Optional[PatternModel]:
    """저장된 표본으로 로지스틱 회귀를 학습하고, '안 본 데이터'로 검증한다.

    검증을 시간 순으로 나누는 이유
    -----------------------------
    시세 데이터는 무작위로 섞어 나누면 안 된다. 뒤 시점의 정보가 앞 시점
    학습에 섞여 들어가 실제보다 훨씬 좋은 점수가 나오기 때문이다.
    그래서 앞쪽 75%로 배우고, 뒤쪽 25%(한 번도 안 본 구간)로 채점한다.

    무엇을 보고 쓸모를 판단하나
    --------------------------
    단순 정확도는 속기 쉽다. 상승이 전체의 37%뿐이면 "무조건 안 오른다"고만
    찍어도 정확도가 63%로 나온다. 매매에서 실제로 중요한 값은

        정밀도(precision) = 모델이 "오른다"고 한 것 중 진짜 오른 비율

    이고, 이 값이 '아무거나 샀을 때의 상승 비율(기준선)'보다 의미 있게
    높아야만 쓸모가 있다. 그 차이를 edge 로 기록한다.
    """
    from datetime import datetime

    rows = database.all_patterns()
    if len(rows) < 200:
        log.info("표본이 %d개뿐이라 학습을 건너뜁니다 (최소 200개 필요).", len(rows))
        return None

    X, y = [], []
    skipped = 0
    for r in rows:
        try:
            f = json.loads(r["features_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        # 특징 목록이 바뀐 뒤 남아 있는 옛 표본은 건너뛴다. 없는 값을 0 으로
        # 채워 넣으면 학습이 엉뚱한 방향으로 간다.
        if not all(k in f for k in FEATURE_NAMES):
            skipped += 1
            continue
        X.append([float(f[k]) for k in FEATURE_NAMES])
        y.append(int(r["label"]))

    if skipped:
        log.info("특징 형식이 다른 옛 표본 %d개는 학습에서 제외했습니다.", skipped)
    if len(y) < 200:
        log.info("쓸 수 있는 표본이 %d개뿐이라 학습을 건너뜁니다.", len(y))
        return None

    X = np.array(X, dtype=float)
    y = np.array(y, dtype=float)
    if len(np.unique(y)) < 2:
        log.info("라벨이 한 종류뿐이라 학습할 수 없습니다 (상승/비상승이 모두 필요).")
        return None

    # 시간 순 분할 (all_patterns 는 ts 오름차순으로 돌려준다)
    split = int(len(y) * (1 - val_ratio))
    X_tr, y_tr = X[:split], y[:split]
    X_va, y_va = X[split:], y[split:]
    if len(y_va) < 40 or len(np.unique(y_tr)) < 2:
        log.info("검증에 쓸 표본이 부족해 학습을 건너뜁니다.")
        return None

    mean = X_tr.mean(axis=0)
    std = X_tr.std(axis=0)
    std = np.where(std == 0, 1.0, std)
    Z_tr = (X_tr - mean) / std
    Z_va = (X_va - mean) / std

    # 상승 표본이 적을 때 한쪽으로 쏠리지 않도록 가중치를 준다
    pos_weight = float(len(y_tr) / (2 * max(y_tr.sum(), 1)))
    neg_weight = float(len(y_tr) / (2 * max(len(y_tr) - y_tr.sum(), 1)))
    sample_w = np.where(y_tr == 1, pos_weight, neg_weight)

    w = np.zeros(Z_tr.shape[1])
    b = 0.0
    for _ in range(epochs):
        logit = np.clip(Z_tr @ w + b, -30, 30)
        pred = 1.0 / (1.0 + np.exp(-logit))
        err = (pred - y_tr) * sample_w
        w -= lr * (Z_tr.T @ err) / len(y_tr)
        b -= lr * float(err.mean())

    def _proba(Z):
        return 1.0 / (1.0 + np.exp(-np.clip(Z @ w + b, -30, 30)))

    train_acc = float(((_proba(Z_tr) >= 0.5).astype(float) == y_tr).mean())

    # --- 검증 ---
    p_va = _proba(Z_va)
    base_rate = float(y_va.mean())                      # 아무거나 샀을 때 오를 확률

    # 기준 확률을 자동으로 고른다.
    #
    # 기준을 낮게 잡으면 신호가 쏟아져 거의 항상 매수하게 되고, 그러면 아무
    # 종목이나 사는 것과 다를 바 없어진다. 반대로 너무 높이면 신호가 없다.
    # 그래서 '신호가 충분히 나오는 선에서 적중률이 가장 높은 기준' 을 찾는다.
    MIN_SIGNALS = 50      # 이보다 적으면 판단 근거로 삼지 않는다
    candidates = [threshold] + [x / 100 for x in range(50, 96, 5)]
    best = None
    for th in sorted(set(candidates)):
        sig = p_va >= th
        n = int(sig.sum())
        if n < MIN_SIGNALS:
            continue
        prec = float(y_va[sig].mean())
        if best is None or (prec - base_rate) > best[1]:
            best = (th, prec - base_rate, n, prec)
    if best is not None:
        threshold = best[0]
    else:
        # 어떤 기준에서도 신호가 부족하면 요청받은 기준을 그대로 쓴다.
        log.info("신호가 충분히 나오는 기준을 찾지 못해 요청값 %.2f 를 씁니다.", threshold)

    signal = p_va >= threshold
    n_signals = int(signal.sum())
    precision = float(y_va[signal].mean()) if n_signals else 0.0
    recall = float(signal[y_va == 1].mean()) if y_va.sum() else 0.0
    val_acc = float((signal.astype(float) == y_va).mean())
    edge = precision - base_rate
    signal_rate = (n_signals / len(y_va) * 100) if len(y_va) else 0.0

    # 우연히 좋아 보이는 것을 걸러낸다.
    #
    # 신호가 적으면 실력이 없어도 적중률이 쉽게 출렁인다. 예를 들어 기준선이
    # 28% 인데 신호 43회 중 우연히 14회를 맞히면 32.6% 가 되어 "기준선보다
    # 낫다"고 착각하게 된다. 그래서 관측된 이득이 '동전 던지기로 이 정도가
    # 나올 확률' 을 충분히 넘는지까지 본다.
    #
    #   표준오차 = sqrt(기준선 x (1-기준선) / 신호수)
    #   z       = 이득 / 표준오차      (z >= 2 이면 우연일 확률이 약 5% 미만)
    if n_signals > 0 and 0 < base_rate < 1:
        std_err = float(np.sqrt(base_rate * (1 - base_rate) / n_signals))
        z_score = edge / std_err if std_err > 0 else 0.0
    else:
        std_err, z_score = 0.0, 0.0

    MIN_EDGE = 0.03       # 수수료를 생각하면 최소 3%p 는 나아야 의미가 있다
    MIN_Z = 2.0           # 우연일 확률 약 5% 미만
    useful = bool(n_signals >= MIN_SIGNALS and edge >= MIN_EDGE and z_score >= MIN_Z)

    model = PatternModel(
        weights=[float(v) for v in w], bias=float(b),
        mean=[float(v) for v in mean], std=[float(v) for v in std],
        feature_names=list(FEATURE_NAMES),
        trained_at=datetime.now().isoformat(timespec="seconds"),
        n_samples=int(len(y)), n_rising=int(y.sum()), accuracy=train_acc,
        threshold=float(threshold), val_samples=int(len(y_va)),
        val_signals=n_signals, val_precision=precision, val_recall=recall,
        val_accuracy=val_acc, val_base_rate=base_rate, edge=edge,
        z_score=float(z_score), is_useful=useful,
    )

    log.info("패턴 학습 완료 - 표본 %d개(상승 %d개)", model.n_samples, model.n_rising)
    if n_signals:
        log.info("  검증(안 본 %d개): 기준 확률 %.0f%% 채택 → 매수 신호 %d회"
                 "(전체의 %.0f%%), 그중 실제 급등 %.1f%% "
                 "(아무거나 샀을 때 %.1f%%) → 이득 %+.1f%%p",
                 model.val_samples, threshold * 100, n_signals, signal_rate,
                 precision * 100, base_rate * 100, edge * 100)
    else:
        log.info("  검증(안 본 %d개): 기준 확률 %.0f%% 를 넘는 매수 신호가 한 번도 없었습니다.",
                 model.val_samples, threshold * 100)
    if useful:
        log.info("  판정: 쓸모 있음 (우연일 가능성 낮음, z=%.1f) → 매수 판단에 반영합니다.",
                 z_score)
    else:
        reasons = []
        if n_signals < MIN_SIGNALS:
            reasons.append("매수 신호가 %d회뿐 (최소 %d회 필요)" % (n_signals, MIN_SIGNALS))
        if edge < MIN_EDGE:
            reasons.append("기준선 대비 이득 %+.1f%%p (최소 %.0f%%p 필요)"
                           % (edge * 100, MIN_EDGE * 100))
        elif z_score < MIN_Z:
            reasons.append("이득이 우연일 수 있음 (z=%.1f, 2.0 이상 필요)" % z_score)
        log.warning("  판정: 아직 근거 부족 - %s. 매수 필터는 적용을 보류합니다 "
                    "(표본이 쌓이면 자동으로 다시 판정합니다).", " / ".join(reasons))
    return model


def ensure_trained(exchange, database, model_path: Path,
                   target_rising: int = TARGET_RISING,
                   threshold: float = 0.55,
                   rise_threshold_pct: float = RISE_THRESHOLD_PCT,
                   horizon: int = HORIZON,
                   progress=None) -> tuple[Optional[PatternModel], dict]:
    """표본을 목표치까지 채우고 학습한 모델을 돌려준다. 프로그램 시작 시 호출."""
    stats = collect_until_target(exchange, database, target_rising=target_rising,
                                 rise_threshold_pct=rise_threshold_pct,
                                 horizon=horizon, progress=progress)
    model = train(database, threshold=threshold)
    if model is not None:
        model.save(model_path)
    return model, stats


def latest_features(df: pd.DataFrame) -> Optional[dict]:
    """가장 최근 캔들의 특징. 실시간 판단용."""
    feat = build_feature_frame(df)
    if feat.empty:
        return None
    row = feat.iloc[-1]
    if row.isna().any():
        return None
    return {k: float(row[k]) for k in FEATURE_NAMES}
