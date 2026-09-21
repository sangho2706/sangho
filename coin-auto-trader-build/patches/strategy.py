"""
매매 전략.

전략은 OHLCV DataFrame(시간순)과 '지금 이 종목을 얼마에 들고 있는지'를 받아
마지막 시점의 신호를 "buy" | "sell" | "hold" 로 반환한다.

왜 보유 정보까지 받는가
----------------------
예전 버전은 decide(df) 만 받아서 '내가 얼마에 샀는지'를 몰랐다. 그래서
손절도 익절도 구조적으로 불가능했고, 매도는 오직 데드크로스나 RSI 과매수일
때만 일어났다. 그 결과가 전형적인 손실 구조다.

    - 떨어지는 종목: 데드크로스가 올 때까지 계속 들고 있음 -> 손실이 무한정 커짐
    - 오르는 종목  : 고점을 찍고 한참 내려와서야 데드크로스 -> 이익을 반납

즉 "이익은 짧게 끊고 손실은 길게 끌고 가는" 모양이 된다. 손실 비율이 이익보다
높아지는 가장 흔한 원인이다. 이를 막으려면 전략이 진입가와 보유 중 최고가를
알아야 한다.

매도 우선순위 (지표보다 먼저 본다)
---------------------------------
1. 손절      : 진입가 대비 -stop_loss_pct 이하로 내려가면 무조건 자른다.
2. 트레일링  : 이익이 trail_start_pct 이상 난 뒤, 보유 중 최고가 대비
               trail_drawdown_pct 만큼 밀리면 판다.
               ("상한가에 판다" 에 가장 가까운 현실적인 방법 - 아래 설명)
3. 익절 상한 : take_profit_pct 이상 먹으면 확정 (0 이면 사용 안 함).
4. 시간 손절 : max_hold_bars 봉 이상 들고도 성과가 없으면 정리 (0 이면 사용 안 함).
5. 위 어디에도 해당 없으면 기존 RSI + 이동평균 판단.

'상한가에서 판다' 에 대하여
--------------------------
정확한 꼭대기에서 파는 것은 미래를 알아야 가능하므로 불가능하다. 현실적인
대안이 트레일링 스톱이다. 가격이 오르는 동안에는 계속 따라 올라가며 최고가를
갱신하고, 흐름이 꺾여 최고가 대비 일정 % 밀리는 순간 판다. 꼭대기를 정확히
맞히지는 못하지만 상승분의 대부분을 가져가고, 되돌림에 이익을 반납하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

Signal = Literal["buy", "sell", "hold"]


@dataclass
class Decision:
    signal: Signal
    reason: str


@dataclass
class PositionState:
    """전략에게 넘기는 '지금 보유 상태'."""

    entry_price: float = 0.0      # 평균 매수가 (없으면 0)
    high_water: float = 0.0       # 보유한 뒤 기록한 최고가
    bars_held: int = 0            # 매수 후 지난 캔들 수

    @property
    def has_position(self) -> bool:
        return self.entry_price > 0


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


class Strategy:
    name = "base"

    def decide(self, df: pd.DataFrame,
               position: Optional[PositionState] = None) -> Decision:
        raise NotImplementedError


class RsiMaCrossStrategy(Strategy):
    """이동평균 교차 + RSI 진입 판단에, 위험 관리(손절/트레일링)를 더한 전략.

    매수: 단기 MA 가 장기 MA 를 상향 돌파(골든크로스) AND RSI < rsi_buy_below
    매도: 손절 / 트레일링 / 익절 / 시간손절 중 하나라도 걸리면 즉시,
          아니면 데드크로스 또는 RSI > rsi_sell_above

    비율 인자는 전부 소수다 (0.05 = 5%).
    """

    name = "rsi_ma_cross_v2"

    def __init__(self, short_window: int = 5, long_window: int = 20,
                 rsi_period: int = 14, rsi_buy_below: float = 60.0,
                 rsi_sell_above: float = 75.0,
                 stop_loss_pct: float = 0.05,
                 trail_start_pct: float = 0.05,
                 trail_drawdown_pct: float = 0.03,
                 take_profit_pct: float = 0.0,
                 max_hold_bars: int = 0,
                 ride_winners: bool = True,
                 use_pullback_entry: bool = True,
                 pullback_rsi_below: float = 45.0):
        self.short_window = short_window
        self.long_window = long_window
        self.rsi_period = rsi_period
        self.rsi_buy_below = rsi_buy_below
        self.rsi_sell_above = rsi_sell_above
        self.stop_loss_pct = stop_loss_pct
        self.trail_start_pct = trail_start_pct
        self.trail_drawdown_pct = trail_drawdown_pct
        self.take_profit_pct = take_profit_pct
        self.max_hold_bars = max_hold_bars
        self.ride_winners = ride_winners
        self.use_pullback_entry = use_pullback_entry
        self.pullback_rsi_below = pullback_rsi_below

    # ---------- 위험 관리 (지표보다 우선) ----------

    def _risk_exit(self, price: float, pos: PositionState) -> Optional[Decision]:
        entry = pos.entry_price
        if entry <= 0:
            return None
        gain = (price - entry) / entry
        high = max(pos.high_water, entry, price)

        # 1) 손절 - 가장 먼저. 손실을 정해진 크기에서 끊는다.
        if self.stop_loss_pct > 0 and gain <= -self.stop_loss_pct:
            return Decision("sell", f"손절 ({gain*100:+.1f}%, 기준 -{self.stop_loss_pct*100:.0f}%)")

        # 2) 트레일링 스톱 - 이익이 난 뒤 고점 대비 밀리면 확정
        if self.trail_drawdown_pct > 0:
            peak_gain = (high - entry) / entry
            if peak_gain >= self.trail_start_pct:
                drawdown = (high - price) / high if high > 0 else 0.0
                if drawdown >= self.trail_drawdown_pct:
                    return Decision(
                        "sell",
                        f"고점 대비 -{drawdown*100:.1f}% 되돌림 - 이익 확정 "
                        f"(최고 {peak_gain*100:+.1f}% → 현재 {gain*100:+.1f}%)",
                    )

        # 3) 익절 상한 (0 이면 사용 안 함)
        if self.take_profit_pct > 0 and gain >= self.take_profit_pct:
            return Decision("sell", f"목표 수익 도달 ({gain*100:+.1f}%)")

        # 4) 시간 손절 (0 이면 사용 안 함)
        if self.max_hold_bars > 0 and pos.bars_held >= self.max_hold_bars and gain <= 0:
            return Decision("sell", f"{pos.bars_held}봉 보유했으나 성과 없음 ({gain*100:+.1f}%) - 정리")

        return None

    # ---------- 판단 ----------

    def decide(self, df: pd.DataFrame,
               position: Optional[PositionState] = None) -> Decision:
        if len(df) < self.long_window + 2:
            return Decision("hold", "데이터 부족 (아직 지표 계산 불가)")

        close = df["close"]
        price = float(close.iloc[-1])
        pos = position or PositionState()

        # 보유 중이면 위험 관리를 지표보다 먼저 확인한다.
        if pos.has_position:
            risk = self._risk_exit(price, pos)
            if risk is not None:
                return risk

        short_ma = close.rolling(self.short_window).mean()
        long_ma = close.rolling(self.long_window).mean()
        rsi = _rsi(close, self.rsi_period)

        prev_diff = short_ma.iloc[-2] - long_ma.iloc[-2]
        curr_diff = short_ma.iloc[-1] - long_ma.iloc[-1]
        curr_rsi = rsi.iloc[-1]

        golden_cross = prev_diff <= 0 and curr_diff > 0
        dead_cross = prev_diff >= 0 and curr_diff < 0

        if golden_cross and curr_rsi < self.rsi_buy_below:
            return Decision(
                "buy",
                f"골든크로스 발생 + RSI {curr_rsi:.1f} < {self.rsi_buy_below} (과매도권 이탈)",
            )

        # 두 번째 진입 경로 - 눌림목 매수.
        #
        # 골든크로스는 추세가 바뀌는 순간에만 한 번 생긴다. 그래서 이미 오름세인
        # 종목은 아무리 좋아 보여도 살 기회가 없고, 한 번 팔고 나면 다시 들어갈
        # 방법이 없다. 실제로 매수가 안 나가는 가장 큰 이유가 이것이었다.
        #
        # 그래서 '오름세는 유지되는데 잠깐 눌렸다가 다시 올라오는' 지점을
        # 두 번째 진입으로 잡는다. 조건은 세 가지다.
        #   1) 단기 이동평균이 장기 위에 있다 (오름세가 살아 있다)
        #   2) 직전 봉에서 RSI 가 기준 아래로 내려가 있었다 (눌렸다)
        #   3) 이번 봉에서 그 기준 위로 다시 올라왔다 (돌아섰다)
        if (self.use_pullback_entry and not pos.has_position
                and curr_diff > 0):
            prev_rsi = rsi.iloc[-2]
            if prev_rsi < self.pullback_rsi_below <= curr_rsi:
                return Decision(
                    "buy",
                    f"눌림목 반등 (오름세 유지, RSI {prev_rsi:.1f}→{curr_rsi:.1f})",
                )

        # 상승을 끝까지 타기 위한 처리.
        #
        # 강하게 오르는 구간에서는 RSI 가 매수 직후 곧바로 75 를 넘는다. 그대로
        # 두면 몇 봉 만에 아주 작은 이익만 먹고 나가게 되어, 정작 크게 오르는
        # 구간을 통째로 놓친다. 반대로 손실은 손절선까지 그대로 나므로
        # "이익은 조금, 손실은 크게" 가 되어 손익비가 나빠진다.
        #
        # 그래서 트레일링 스톱이 작동할 만큼 이익이 난 상태에서는 RSI 과매수
        # 매도를 건너뛰고, 언제 팔지는 트레일링 스톱에 맡긴다.
        # (추세가 실제로 꺾이는 신호인 데드크로스는 그대로 매도로 본다.)
        riding = False
        if self.ride_winners and pos.has_position and self.trail_drawdown_pct > 0:
            gain = (price - pos.entry_price) / pos.entry_price
            riding = gain >= self.trail_start_pct

        if dead_cross:
            return Decision("sell", "데드크로스 발생")
        if curr_rsi > self.rsi_sell_above:
            if riding:
                return Decision(
                    "hold",
                    f"RSI {curr_rsi:.1f} 과매수지만 상승 중이라 계속 보유 "
                    f"(고점 대비 -{self.trail_drawdown_pct*100:.0f}% 밀리면 매도)",
                )
            return Decision("sell", f"RSI {curr_rsi:.1f} > {self.rsi_sell_above} (과매수)")

        return Decision("hold", f"신호 없음 (RSI {curr_rsi:.1f})")


DEFAULT_STRATEGY = RsiMaCrossStrategy
