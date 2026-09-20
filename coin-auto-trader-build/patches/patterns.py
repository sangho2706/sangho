"""
상승 패턴 수집 · 학습.

목표
----
"프로그램을 켰을 때 상승 패턴 표본이 1000개보다 적으면 1000개가 될 때까지
채워서 학습한다."

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
HORIZON = 8                 # 앞으로 몇 개 캔들 안에 오르면 '상승'으로 볼지
RISE_THRESHOLD_PCT = 1.0    # 몇 % 이상 올라야 '상승'으로 볼지
INTERVAL = "minute15"
FEATURE_NAMES = [
    "rsi14",          # RSI (과매수/과매도)
    "ma_ratio",       # 단기MA / 장기MA - 1  (추세 방향)
    "mom_1",          # 직전 1봉 수익률
    "mom_3",          # 직전 3봉 수익률
    "mom_6",          # 직전 6봉 수익률
    "volatility",     # 최근 변동성
    "vol_ratio",      # 최근 거래량 / 평균 거래량
    "range_pos",      # 최근 고저 범위에서 현재가 위치 (0=저점, 1=고점)
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
    return feat[FEATURE_NAMES].replace([np.inf, -np.inf], np.nan)


def extract_samples(market: str, df: pd.DataFrame, interval: str = INTERVAL,
                    horizon: int = HORIZON,
                    rise_threshold_pct: float = RISE_THRESHOLD_PCT,
                    source: str = "backfill") -> list[tuple]:
    """캔들에서 (ts, market, interval, features_json, future_return_pct, label, source)
    튜플 목록을 만든다. db.insert_patterns() 에 그대로 넣을 수 있다."""
    if df is None or len(df) < 40 + horizon:
        return []

    feat = build_feature_frame(df)
    close = df["close"].astype(float).to_numpy()
    n = len(df)
    rows: list[tuple] = []

    for i in range(30, n - horizon):
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


def collect_until_target(exchange, database, target_rising: int = TARGET_RISING,
                         interval: str = INTERVAL, candles_per_call: int = 200,
                         max_markets: int = 60,
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

    have = database.count_patterns(label=1)
    if have >= target_rising:
        _say("상승 패턴 %d개 확보됨 (목표 %d) - 추가 수집이 필요 없습니다."
             % (have, target_rising))
        return {"collected": 0, "rising": have, "scanned_markets": 0, "skipped": True}

    _say("상승 패턴이 %d개뿐입니다. 목표 %d개까지 과거 데이터로 채웁니다."
         % (have, target_rising))

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
        if df is None or len(df) < 40:
            continue

        rows = extract_samples(market, df, interval=interval)
        added = database.insert_patterns(rows)
        total_new += added
        scanned += 1
        if added:
            _say("  %s: 표본 %d개 추가 (누적 상승 패턴 %d개)"
                 % (market, added, database.count_patterns(label=1)))

    rising = database.count_patterns(label=1)
    _say("패턴 수집 완료: 표본 %d개 추가, 상승 패턴 %d개 / 전체 %d개 (마켓 %d개 조회)"
         % (total_new, rising, database.count_patterns(), scanned))
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
    for r in rows:
        try:
            f = json.loads(r["features_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        X.append([float(f.get(k, 0.0)) for k in FEATURE_NAMES])
        y.append(int(r["label"]))

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
    signal = p_va >= threshold
    n_signals = int(signal.sum())
    base_rate = float(y_va.mean())                      # 아무거나 샀을 때 오를 확률
    precision = float(y_va[signal].mean()) if n_signals else 0.0
    recall = float(signal[y_va == 1].mean()) if y_va.sum() else 0.0
    val_acc = float((signal.astype(float) == y_va).mean())
    edge = precision - base_rate

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

    MIN_SIGNALS = 50      # 이보다 적으면 판단 근거로 삼지 않는다
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
        log.info("  검증(안 본 %d개): 매수 신호 %d회, 그중 실제 상승 %.1f%% "
                 "(아무거나 샀을 때 %.1f%%) → 이득 %+.1f%%p",
                 model.val_samples, n_signals, precision * 100,
                 base_rate * 100, edge * 100)
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
                   progress=None) -> tuple[Optional[PatternModel], dict]:
    """표본을 목표치까지 채우고 학습한 모델을 돌려준다. 프로그램 시작 시 호출."""
    stats = collect_until_target(exchange, database, target_rising=target_rising,
                                 progress=progress)
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
