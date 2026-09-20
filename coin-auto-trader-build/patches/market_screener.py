"""
종목 자동 선정 (마켓 스크리너).

"오를 것 같은 코인을 알아서 골라서 매매" 를 위한 1단계 필터다. 여기서 하는
일은 "이 코인이 무조건 오른다" 를 예측하는 게 아니라 (그건 아무도 못한다),
아래 기준으로 매매 후보군을 좁히는 것 뿐이다:

1. 유동성 필터: 최근 24시간 거래대금이 너무 작은 코인은 제외 (급변동/시세
   조작 위험이 크고, 매도하려 할 때 체결이 잘 안 될 수 있음)
2. 안정성 필터: 스테이블코인류(USDT 등)는 제외 - 변동성이 거의 없어서
   매매 대상으로 의미가 없음
3. 등락률 구간 필터: 24시간 등락률이 min_change_rate_for_entry 이상,
   max_change_rate_for_entry 이하인 코인만 남긴다.
   - 하한(min)을 올리면 "많이 오른 종목만" 노리는 모멘텀 매매가 된다.
     예: 0.15 로 두면 24시간에 15% 이상 오른 종목만 후보가 된다.
   - 상한(max)은 추격매수 방지선이다. 하한을 올렸다면 상한도 같이 올려야
     후보가 남는다 (하한 >= 상한이면 통과할 종목이 하나도 없다).
4. 남은 후보를 24시간 등락률(모멘텀) 기준으로 정렬해 상위 N개를 후보군으로 반환

⚠️ 이 스크리너는 "지금 오르는 중인 코인" 후보를 좁혀줄 뿐, 실제 매수/매도
타이밍은 여전히 strategy.py 의 RSI+이동평균 전략이 판단한다. 즉 후보에
뽑혔다고 바로 사는 게 아니라, 그 안에서도 strategy.py 가 "지금이 살 타이밍"
이라고 판단해야 실제 매수가 나간다.

한계: 이 스크리너 자체(어떤 종목을 후보로 넣을지)는 backtest.py 로 검증되지
않는다 - 과거 특정 시점의 "전체 마켓 스냅샷"을 그대로 재현하기 어렵기
때문이다. 전략의 매수/매도 타이밍(RSI+MA)만 backtest.py 로 검증 가능하다.
"""
from __future__ import annotations

from dataclasses import dataclass

from exchange import ExchangeClient


@dataclass
class Candidate:
    market: str
    signed_change_rate: float  # 0.05 = +5%
    acc_trade_price_24h: float
    reason: str


def scan_top_candidates(exchange: ExchangeClient, top_n: int, min_volume_krw: float,
                         max_change_rate_for_entry: float,
                         exclude_markets: list[str],
                         min_change_rate_for_entry: float = 0.0) -> list[Candidate]:
    all_markets = exchange.get_krw_tickers()
    universe = [m for m in all_markets if m not in set(exclude_markets)]
    if not universe:
        return []

    snapshots = exchange.get_ticker_snapshot(universe)

    candidates: list[Candidate] = []
    for s in snapshots:
        market = s.get("market")
        if not market:
            continue
        volume_24h = float(s.get("acc_trade_price_24h", 0) or 0)
        change_rate = float(s.get("signed_change_rate", 0) or 0)

        if volume_24h < min_volume_krw:
            continue
        if change_rate <= 0:
            continue  # 지금 내리고 있는 코인은 "오를 것 같은" 후보에서 제외
        if change_rate < min_change_rate_for_entry:
            continue  # 상승폭이 기준에 못 미침 (모멘텀 부족)
        if change_rate > max_change_rate_for_entry:
            continue  # 이미 너무 오른 코인은 추격매수 위험 - 제외

        candidates.append(Candidate(
            market=market,
            signed_change_rate=change_rate,
            acc_trade_price_24h=volume_24h,
            reason=(
                f"24h {change_rate*100:+.1f}%, 거래대금 {volume_24h/1e8:.0f}억원"
            ),
        ))

    candidates.sort(key=lambda c: c.signed_change_rate, reverse=True)
    return candidates[:top_n]
