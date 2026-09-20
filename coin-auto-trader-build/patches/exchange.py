"""
업비트 거래소 래퍼.

- LIVE_TRADING=false (기본값) 이면 실제 주문을 절대 내지 않고, 공개 시세만
  받아와 메모리 상에서 체결을 시뮬레이션한다 (모의/페이퍼 트레이딩).
- LIVE_TRADING=true 이고 API 키가 설정되어 있을 때만 실제 pyupbit 주문 함수를
  호출한다.

이 클래스는 "얼마를 살지/팔지" 는 결정하지 않는다 (그건 strategy.py + ledger.py
몫). 여기서는 순수하게 거래소와의 통신만 담당한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    import pyupbit
except ImportError:  # pyupbit 미설치 상태에서도 백테스트 등은 동작하게
    pyupbit = None

UPBIT_FEE_RATE = 0.0005  # 업비트 기본 원화마켓 수수료 0.05% (변경될 수 있으니 실거래 전 확인)


@dataclass
class OrderResult:
    market: str
    side: str  # buy | sell
    price: float
    volume: float
    krw_amount: float
    fee_krw: float
    mode: str  # paper | live
    raw: Optional[dict] = None


class ExchangeClient:
    def __init__(self, access_key: str, secret_key: str, live_trading: bool):
        self.live_trading = live_trading
        self._upbit = None

        if live_trading:
            if pyupbit is None:
                raise RuntimeError(
                    "pyupbit 패키지가 설치되어 있지 않습니다. pip install -r requirements.txt 를 먼저 실행하세요."
                )
            if not access_key or not secret_key:
                raise RuntimeError(
                    "LIVE_TRADING=true 인데 API 키가 비어 있습니다. .env 를 확인하세요."
                )
            self._upbit = pyupbit.Upbit(access_key, secret_key)
        elif access_key and secret_key and pyupbit is not None:
            # 모의매매 모드에서도 '내 전체 자산 / 수동 투자 자산' 을 보여주려면
            # 계좌 조회가 필요하다. 주문 함수(buy_market/sell_market)는 아래에서
            # live_trading 을 따로 확인하므로, 여기서 클라이언트를 만들어도
            # 실제 주문이 나갈 일은 없다 (조회 전용).
            try:
                self._upbit = pyupbit.Upbit(access_key, secret_key)
            except Exception:
                self._upbit = None

    # ---------- 시세 조회 (인증 불필요, 항상 실주문 여부와 무관하게 실제 API 사용) ----------

    def get_current_price(self, market: str) -> float:
        if pyupbit is None:
            raise RuntimeError("pyupbit 패키지가 필요합니다.")
        price = pyupbit.get_current_price(market)
        if price is None:
            raise RuntimeError(f"{market} 현재가 조회 실패")
        return float(price)

    def get_ohlcv(self, market: str, interval: str = "minute15", count: int = 200):
        """캔들 데이터를 pandas DataFrame 으로 반환 (백테스트/전략 계산용)."""
        if pyupbit is None:
            raise RuntimeError("pyupbit 패키지가 필요합니다.")
        df = pyupbit.get_ohlcv(market, interval=interval, count=count)
        return df

    def get_krw_tickers(self) -> list[str]:
        """업비트에 상장된 모든 원화(KRW) 마켓 코드 목록 (예: ['KRW-BTC', 'KRW-ETH', ...])."""
        if pyupbit is None:
            raise RuntimeError("pyupbit 패키지가 필요합니다.")
        tickers = pyupbit.get_tickers(fiat="KRW")
        return tickers or []

    def get_ticker_snapshot(self, markets: list[str]) -> list[dict]:
        """여러 마켓의 24시간 시세 스냅샷(등락률, 거래대금 등)을 한 번에 가져온다.

        market_screener.py 에서 "지금 어떤 코인이 오르고 있고 거래가 활발한지"
        판단하는 데 쓰인다. 반환값은 업비트 공개 API(/v1/ticker) 원본 dict 리스트.
        (내부적으로 200개씩 나눠 요청하므로 마켓 전체를 한 번에 넘겨도 된다.)
        """
        if pyupbit is None:
            raise RuntimeError("pyupbit 패키지가 필요합니다.")
        if not markets:
            return []
        raw = pyupbit.get_current_price(markets, verbose=True)
        if isinstance(raw, dict):
            raw = [raw]
        return raw or []

    # ---------- 실제 계좌 잔고 (live_trading=True 일 때만 의미 있음) ----------

    def get_account_balances(self) -> list[dict]:
        """업비트 계정 전체 잔고를 그대로 반환한다.

        주의: 이 값에는 '기존에 수동으로 보유하던 자산'도 전부 포함되어 있다.
        자동매매는 이 함수의 결과를 매매 판단에 절대 사용하지 않고, ledger.py 가
        관리하는 내부 장부만 사용한다 (그것이 수동/자동 분리의 핵심).
        """
        if self._upbit is None:
            return []
        try:
            balances = self._upbit.get_balances()
        except Exception:
            return []
        return balances or []

    def get_account_summary(self) -> dict:
        """업비트 계정 전체를 원화로 환산한 요약.

        수동으로 들고 있는 코인까지 전부 포함한 '내 전체 자산' 이다.
        자동매매 장부(ledger)와는 무관하며, 조회만 한다.

        반환: {"available": bool, "krw": 원화잔고, "coin_value": 코인평가액,
               "total": 합계, "items": [종목별 내역]}
        """
        balances = self.get_account_balances()
        if not balances:
            return {"available": False, "krw": 0.0, "coin_value": 0.0,
                    "total": 0.0, "items": []}

        krw = 0.0
        items: list[dict] = []
        coin_value = 0.0

        for b in balances:
            currency = b.get("currency")
            try:
                amount = float(b.get("balance", 0)) + float(b.get("locked", 0) or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            if currency == "KRW":
                krw += amount
                continue

            market = f"KRW-{currency}"
            try:
                price = self.get_current_price(market)
            except Exception:
                # 원화마켓에 없는 코인(해외 전용 등)은 매수평균가로 대신 추정
                try:
                    price = float(b.get("avg_buy_price", 0) or 0)
                except (TypeError, ValueError):
                    price = 0.0
            value = amount * price
            coin_value += value
            items.append({"currency": currency, "amount": amount, "value_krw": value})

        items.sort(key=lambda x: x["value_krw"], reverse=True)
        return {"available": True, "krw": krw, "coin_value": coin_value,
                "total": krw + coin_value, "items": items}

    # ---------- 주문 ----------

    def buy_market(self, market: str, krw_amount: float, current_price: float) -> OrderResult:
        fee = krw_amount * UPBIT_FEE_RATE
        volume = (krw_amount - fee) / current_price

        if not self.live_trading:
            return OrderResult(market, "buy", current_price, volume, krw_amount, fee, "paper")

        raw = self._upbit.buy_market_order(market, krw_amount)
        return OrderResult(market, "buy", current_price, volume, krw_amount, fee, "live", raw)

    def sell_market(self, market: str, volume: float, current_price: float) -> OrderResult:
        krw_amount = volume * current_price
        fee = krw_amount * UPBIT_FEE_RATE

        if not self.live_trading:
            return OrderResult(market, "sell", current_price, volume, krw_amount - fee, fee, "paper")

        raw = self._upbit.sell_market_order(market, volume)
        return OrderResult(market, "sell", current_price, volume, krw_amount - fee, fee, "live", raw)
