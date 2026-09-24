"""
자동매매 전용 원장 (수동/자동 자금 분리의 핵심 모듈).

업비트 계정 자체에는 "이 돈은 자동매매용, 이 돈은 수동 투자용" 같은 구분이
없다. 그래서 이 프로그램은 거래소 잔고를 직접 매매 판단에 쓰지 않고, 아래
AutoLedger 가 자체적으로 관리하는 가상의 장부만 사용한다.

규칙 (매우 중요):
1. 봇은 시작 시 사용자가 정한 예산(AUTO_TRADING_BUDGET_KRW)만큼만 자기 현금으로
   갖고 시작한다. 이 금액을 넘는 매수 주문은 절대 내지 않는다.
2. 봇은 자기가 "직접 산" 수량만 자기 보유량으로 기록한다.
3. 매도할 때도 이 장부에 기록된 수량 이상은 절대 팔 수 없다 (기존에 사용자가
   수동으로 보유하고 있던 코인은 장부에 없으므로 구조적으로 건드릴 수 없다).
4. 실거래 모드에서는 매도 직전 실제 계좌 잔고가 장부 수량보다 적으면
   (사용자가 그새 업비트 앱에서 수동으로 팔았거나 한 경우) 주문을 내지 않고
   경고를 남긴다 - 절대 사용자의 다른 자산에 손대지 않기 위함이다.

장부는 data/ledger_state.json 에 저장되어 프로그램을 껐다 켜도 유지된다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class LedgerError(Exception):
    pass


@dataclass
class Position:
    volume: float = 0.0
    cost_basis_krw: float = 0.0  # 이 포지션을 사는 데 들어간 총 원화 (수수료 포함)
    # 아래 두 개는 손절/트레일링 스톱을 위해 필요하다. 기본값이 있으므로
    # 이 항목이 없던 예전 ledger_state.json 도 그대로 읽힌다.
    high_water_price: float = 0.0  # 보유한 뒤 기록한 최고가
    bars_held: int = 0             # 매수 후 지난 판단 주기 수

    @property
    def avg_price(self) -> float:
        return self.cost_basis_krw / self.volume if self.volume > 0 else 0.0


class AutoLedger:
    def __init__(self, state_path: Path, budget_krw: float):
        self.state_path = Path(state_path)
        self.budget_krw = budget_krw
        self.cash_krw: float = budget_krw
        self.positions: dict[str, Position] = {}
        self.realized_pnl_krw: float = 0.0
        self._load()

    # ---------- 영속화 ----------

    def _load(self) -> None:
        if not self.state_path.exists():
            self._save()
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        # budget 이 바뀌었으면(사용자가 .env에서 예산을 수정) 기존 현금에 차액을 반영
        stored_budget = data.get("budget_krw", self.budget_krw)
        budget_delta = self.budget_krw - stored_budget
        self.cash_krw = data.get("cash_krw", self.budget_krw) + budget_delta
        self.realized_pnl_krw = data.get("realized_pnl_krw", 0.0)
        known = set(Position.__dataclass_fields__)
        self.positions = {
            m: Position(**{k: v for k, v in p.items() if k in known})
            for m, p in data.get("positions", {}).items()
        }

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "budget_krw": self.budget_krw,
            "cash_krw": self.cash_krw,
            "realized_pnl_krw": self.realized_pnl_krw,
            "positions": {
                m: {"volume": p.volume, "cost_basis_krw": p.cost_basis_krw,
                    "high_water_price": p.high_water_price, "bars_held": p.bars_held}
                for m, p in self.positions.items()
            },
        }
        self.state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 조회 ----------

    def get_position(self, market: str) -> Position:
        return self.positions.get(market, Position())

    def available_cash(self) -> float:
        return self.cash_krw

    def holdings_value(self, price_lookup) -> float:
        """price_lookup: market -> 현재가 함수. 보유 코인 평가액 합계."""
        total = 0.0
        for market, pos in self.positions.items():
            if pos.volume > 0:
                total += pos.volume * price_lookup(market)
        return total

    def total_value(self, price_lookup) -> float:
        return self.cash_krw + self.holdings_value(price_lookup)

    def daily_loss_exceeded(self, price_lookup, loss_limit_ratio: float,
                             start_of_day_value: float) -> bool:
        current = self.total_value(price_lookup)
        loss = start_of_day_value - current
        return loss > self.budget_krw * loss_limit_ratio

    # ---------- 매수/매도 (실제 주문 전, 장부 기준으로 가능 여부 판단) ----------

    def max_buyable_krw(self, market: str, max_position_ratio: float) -> float:
        """이 종목에 추가로 넣을 수 있는 최대 원화 금액.

        - 전체 배정 예산의 max_position_ratio 를 이 종목의 상한으로 둔다.
        - 남은 현금(cash_krw)을 넘을 수 없다.
        """
        position_cap = self.budget_krw * max_position_ratio
        current_pos = self.get_position(market)
        room_in_position = max(position_cap - current_pos.cost_basis_krw, 0.0)
        return min(room_in_position, self.cash_krw)

    def update_high_water(self, market: str, price: float) -> None:
        """최고가만 갱신한다 (보유 기간은 건드리지 않음).

        빠른 위험 점검(주기 사이사이 현재가만 자주 확인)에서 쓴다.
        touch_position 은 '판단 주기 한 번' 을 의미하므로 매번 부르면
        보유 기간이 실제보다 훨씬 빨리 늘어난다. 이 메서드는 그 부작용
        없이 트레일링 스톱의 기준(최고가)만 최신으로 유지한다.
        """
        pos = self.positions.get(market)
        if pos is None or pos.volume <= 0:
            return
        if price > pos.high_water_price:
            pos.high_water_price = price
            self._save()

    def touch_position(self, market: str, price: float) -> None:
        """판단 주기마다 한 번 호출. 보유 중이면 최고가를 갱신하고 보유 기간을 센다.

        최고가는 트레일링 스톱의 기준이고, 보유 기간은 시간 손절의 기준이다.
        둘을 같이 처리해 저장을 한 번만 한다.
        """
        pos = self.positions.get(market)
        if pos is None or pos.volume <= 0:
            return
        pos.bars_held += 1
        if price > pos.high_water_price:
            pos.high_water_price = price
        self._save()

    def position_state(self, market: str):
        """전략에 넘길 보유 상태. 보유가 없으면 빈 상태를 돌려준다."""
        from strategy import PositionState

        pos = self.positions.get(market)
        if pos is None or pos.volume <= 0:
            return PositionState()
        return PositionState(
            entry_price=pos.avg_price,
            high_water=max(pos.high_water_price, pos.avg_price),
            bars_held=pos.bars_held,
        )

    def record_buy(self, market: str, krw_amount: float, volume: float, fee_krw: float) -> None:
        if krw_amount > self.cash_krw + 1e-6:
            raise LedgerError(
                f"자동매매 예산 초과 시도 차단: 남은 현금 {self.cash_krw:,.0f}원 < "
                f"요청 {krw_amount:,.0f}원. 기존 보유 자산에는 절대 손대지 않습니다."
            )
        self.cash_krw -= krw_amount
        pos = self.positions.setdefault(market, Position())
        was_empty = pos.volume <= 0
        pos.volume += volume
        pos.cost_basis_krw += krw_amount
        # 새로 진입하는 것이면 고점/보유기간을 여기서부터 다시 센다.
        entry_price = krw_amount / volume if volume > 0 else 0.0
        if was_empty:
            pos.high_water_price = entry_price
            pos.bars_held = 0
        else:
            pos.high_water_price = max(pos.high_water_price, entry_price)
        self._save()

    def sellable_volume(self, market: str, actual_account_volume: Optional[float] = None) -> float:
        """실제로 팔아도 되는 수량. 장부상 수량과, (실거래 시) 실제 계좌 수량 중 작은 값.

        actual_account_volume 이 장부보다 작다면 사용자가 그 사이에 앱에서
        수동으로 일부를 팔았다는 뜻이므로, 그만큼만 판다 (절대 그 이상은 팔지 않음).
        """
        ledger_volume = self.get_position(market).volume
        if actual_account_volume is None:
            return ledger_volume
        return min(ledger_volume, actual_account_volume)

    def record_sell(self, market: str, volume: float, krw_amount: float, fee_krw: float) -> float:
        """매도 기록. 실현손익(원화)을 반환한다."""
        pos = self.get_position(market)
        if volume > pos.volume + 1e-9:
            raise LedgerError(
                f"장부에 없는 수량 매도 시도 차단: {market} 장부 보유 {pos.volume} < "
                f"요청 {volume}. 기존 보유 자산은 절대 건드리지 않습니다."
            )
        # 평단가 기준으로 원가 계산 (선입선출 대신 평균단가법 - 단순화)
        avg_price = pos.avg_price
        cost_of_sold = avg_price * volume
        realized = krw_amount - cost_of_sold  # 수수료는 이미 krw_amount 에 반영됨

        pos.volume -= volume
        pos.cost_basis_krw -= cost_of_sold
        if pos.volume <= 1e-9:
            pos.volume = 0.0
            pos.cost_basis_krw = 0.0
            pos.high_water_price = 0.0
            pos.bars_held = 0

        self.cash_krw += krw_amount
        self.realized_pnl_krw += realized
        self._save()
        return realized
