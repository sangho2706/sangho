"""
메인 실행 루프.

설정된 주기(TRADE_LOOP_INTERVAL_MIN)마다 대상 마켓들을 순회하며:
  1. 최근 캔들을 가져와 전략에 판단을 맡기고
  2. ledger.py 규칙 안에서만 매수/매도 크기를 정하고
  3. exchange.py 로 주문(모의 또는 실제)을 실행하고
  4. db.py 에 시세/매매/잔고 스냅샷을 기록한다.

동시에 REPORT_INTERVAL_HOURS 주기로 report.py 를 호출해 리포트를 생성한다.

안전장치:
- .env 의 LIVE_TRADING 이 true 이고, config.validate_for_live() 를 통과해야만
  실주문을 시도한다. 기본값(false)에서는 항상 모의매매.
- 하루 손실 한도(DAILY_LOSS_LIMIT_RATIO)를 넘으면 그날은 매수를 중단한다
  (보유 포지션 매도/리스크 관리는 계속 동작).
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from config import Settings, load_settings
from db import Database
from exchange import ExchangeClient
from ledger import AutoLedger, LedgerError
from market_screener import scan_top_candidates
from strategy import DEFAULT_STRATEGY
import patterns as pattern_mod
import report as report_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("runner")


class SettingsError(Exception):
    """실거래 전환에 필요한 설정이 미비할 때 발생 (GUI/CLI가 각자의 방식으로 처리)."""


class TraderApp:
    def __init__(self, settings: Settings, on_tick: Optional[Callable[[dict], None]] = None):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.exchange = ExchangeClient(
            settings.upbit_access_key, settings.upbit_secret_key, settings.live_trading
        )
        self.ledger = AutoLedger(
            settings.db_path.parent / "ledger_state.json", settings.auto_trading_budget_krw
        )
        self.strategy = DEFAULT_STRATEGY(
            stop_loss_pct=settings.stop_loss_pct,
            trail_start_pct=settings.trail_start_pct,
            trail_drawdown_pct=settings.trail_drawdown_pct,
            take_profit_pct=settings.take_profit_pct,
            max_hold_bars=settings.max_hold_bars,
        )
        self.on_tick = on_tick  # GUI 등에서 매 루프 결과를 받아가기 위한 콜백 (선택)
        self.last_snapshot: dict = {}
        self._day_start_value: float | None = None
        self._day_start_date = None
        self._last_report_at: datetime | None = None
        self._watchlist: list[str] = list(settings.target_markets)
        self._last_scan_at: datetime | None = None
        self._last_scan_summary: str = ""

        # 구동 세션 (구동 시간 / 구간 손익 집계용)
        self._session_id: int | None = None
        self._session_started_at: datetime | None = None
        self._session_start_realized: float = 0.0
        self._session_trade_count: int = 0

        self._last_account_error = ""

        # 상승 기록용 기준가 (market -> (관측시각, 기준가))
        self._rise_baseline: dict[str, tuple[datetime, float]] = {}

        # 상승 패턴 학습
        self.pattern_model_path = settings.db_path.parent / "pattern_model.json"
        self.pattern_model = pattern_mod.PatternModel.load(self.pattern_model_path)
        self.pattern_stats: dict = {}
        self._last_train_at: datetime | None = None

        if settings.live_trading:
            problems = settings.validate_for_live()
            if problems:
                for p in problems:
                    log.error("  - %s", p)
                raise SettingsError(
                    "실거래(LIVE_TRADING=true) 설정에 문제가 있습니다: " + " / ".join(problems)
                )
            log.warning(
                "⚠️  실거래 모드로 시작합니다. 자동매매 예산: %s원, 대상: %s",
                f"{settings.auto_trading_budget_krw:,.0f}",
                "자동 선정" if settings.auto_select_markets else settings.target_markets,
            )
        else:
            log.info(
                "모의매매(paper) 모드로 시작합니다. 실제 주문은 나가지 않습니다. "
                "실거래로 전환하려면 .env 의 LIVE_TRADING=true 로 바꾸세요."
            )
        if settings.auto_select_markets:
            log.info(
                "종목 자동 선정 모드: 매 %d분마다 전체 원화마켓을 스캔해 상위 %d개를 "
                "후보로 삼고, 그 안에서 RSI+이동평균 전략이 매수 타이밍을 판단합니다.",
                settings.market_rescan_interval_min, settings.top_n_markets,
            )

    def _price_lookup(self, cache: dict):
        def _fn(market: str) -> float:
            if market not in cache:
                cache[market] = self.exchange.get_current_price(market)
            return cache[market]
        return _fn

    def _maybe_reset_day(self, price_cache: dict) -> None:
        today = datetime.now().date()
        if self._day_start_date != today:
            self._day_start_date = today
            self._day_start_value = self.ledger.total_value(self._price_lookup(price_cache))
            log.info("새 거래일 시작. 기준 자산: %.0f원", self._day_start_value)

    def _held_markets(self) -> list[str]:
        return [m for m, pos in self.ledger.positions.items() if pos.volume > 0]

    def _maybe_refresh_watchlist(self) -> None:
        """AUTO_SELECT_MARKETS=true 면 주기적으로 전체 마켓을 스캔해 감시 종목을
        갱신한다. 이미 보유 중인 종목은 스캔 결과와 무관하게 항상 감시 목록에
        남겨서 (매도 판단을 계속 할 수 있도록) 절대 누락되지 않게 한다."""
        if not self.settings.auto_select_markets:
            self._watchlist = list(self.settings.target_markets)
            return

        now = datetime.now()
        interval = timedelta(minutes=self.settings.market_rescan_interval_min)
        held = self._held_markets()
        if self._last_scan_at is not None and now - self._last_scan_at < interval:
            # 재스캔 주기가 안 됐어도 보유 종목은 항상 포함되도록 보정
            self._watchlist = sorted(set(self._watchlist) | set(held))
            return

        try:
            candidates = scan_top_candidates(
                self.exchange,
                top_n=self.settings.top_n_markets,
                min_volume_krw=self.settings.min_24h_volume_krw,
                max_change_rate_for_entry=self.settings.max_24h_change_rate_for_entry,
                min_change_rate_for_entry=self.settings.min_24h_change_rate_for_entry,
                exclude_markets=self.settings.exclude_markets,
            )
        except Exception:
            log.exception("마켓 자동 스캔 실패 - 이번 주기는 기존 감시 목록으로 진행")
            self._watchlist = sorted(set(self._watchlist) | set(held))
            return

        screened = [c.market for c in candidates]
        self._watchlist = sorted(set(screened) | set(held))
        self._last_scan_at = now
        self._last_scan_summary = (
            "; ".join(f"{c.market}({c.reason})" for c in candidates) or "후보 없음"
        )
        log.info("종목 스캔 결과 (상위 %d): %s", self.settings.top_n_markets, self._last_scan_summary)
        if held:
            log.info("보유 중이라 감시 유지되는 종목: %s", held)

    # ---------- 구동 세션 ----------

    def begin_session(self) -> None:
        """봇 구동 시작을 기록한다. 구동 시간과 구간 손익의 기준점이 된다."""
        self.db.close_orphan_sessions()   # 지난번 비정상 종료 흔적 정리
        try:
            start_value = self.ledger.total_value(lambda m: self._safe_price(m))
        except Exception:
            start_value = self.ledger.cash_krw
        self._session_started_at = datetime.now()
        self._session_start_realized = self.ledger.realized_pnl_krw
        self._session_trade_count = 0
        self._session_id = self.db.start_session(
            "live" if self.settings.live_trading else "paper",
            self.ledger.budget_krw, start_value,
        )
        log.info("구동 시작 (세션 #%d). 시작 자산 %.0f원", self._session_id, start_value)

    def finish_session(self, reason: str = "정상 종료") -> None:
        if self._session_id is None:
            return
        try:
            end_value = self.ledger.total_value(lambda m: self._safe_price(m))
        except Exception:
            end_value = self.ledger.cash_krw
        realized = self.ledger.realized_pnl_krw - self._session_start_realized
        self.db.end_session(self._session_id, end_value, realized,
                            self._session_trade_count, reason)
        log.info("구동 종료 (세션 #%d). 구동 시간 %s, 이번 구간 실현손익 %.0f원, 매매 %d회",
                 self._session_id, self.uptime_text(), realized, self._session_trade_count)
        self._session_id = None
        self._session_started_at = None

    def _safe_price(self, market: str) -> float:
        """현재가 조회 실패 시 0 으로 처리 (세션 집계가 예외로 깨지지 않게)."""
        try:
            return self.exchange.get_current_price(market)
        except Exception:
            return 0.0

    def uptime_seconds(self) -> float:
        if self._session_started_at is None:
            return 0.0
        return max((datetime.now() - self._session_started_at).total_seconds(), 0.0)

    def uptime_text(self) -> str:
        sec = int(self.uptime_seconds())
        days, rem = divmod(sec, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}일")
        if hours or days:
            parts.append(f"{hours}시간")
        parts.append(f"{minutes}분")
        return " ".join(parts)

    # ---------- 상승 패턴 학습 ----------

    def prepare_patterns(self, progress=None) -> dict:
        """구동 시작 시 호출. 상승 패턴 표본이 목표치보다 적으면 과거 데이터로
        채운 뒤 학습한다. 이미 충분하면 학습만 다시 한다."""
        if not self.settings.pattern_learning_enabled:
            log.info("패턴 학습이 꺼져 있습니다 (PATTERN_LEARNING_ENABLED=false).")
            return {}
        try:
            model, stats = pattern_mod.ensure_trained(
                self.exchange, self.db, self.pattern_model_path,
                target_rising=self.settings.pattern_target_rising,
                threshold=self.settings.pattern_min_proba,
                rise_threshold_pct=self.settings.pattern_rise_threshold_pct,
                horizon=self.settings.pattern_horizon_bars,
                progress=progress,
            )
        except Exception:
            log.exception("패턴 학습 중 오류 - 매매는 그대로 계속합니다.")
            return {}
        if model is not None:
            self.pattern_model = model
            self._last_train_at = datetime.now()
        self.pattern_stats = stats or {}
        return self.pattern_stats

    def _maybe_retrain(self) -> None:
        if not self.settings.pattern_learning_enabled:
            return
        hours = self.settings.pattern_retrain_hours
        if hours <= 0:
            return
        now = datetime.now()
        if self._last_train_at is not None and now - self._last_train_at < timedelta(hours=hours):
            return
        try:
            model = pattern_mod.train(self.db,
                                      threshold=self.settings.pattern_min_proba)
        except Exception:
            log.exception("패턴 재학습 실패 - 기존 모델을 계속 사용합니다.")
            self._last_train_at = now
            return
        if model is not None:
            model.save(self.pattern_model_path)
            self.pattern_model = model
        self._last_train_at = now

    def pattern_summary(self) -> dict:
        """GUI 표시용 학습 현황."""
        m = self.pattern_model
        applying = bool(self.settings.use_pattern_filter
                        and m is not None and getattr(m, "is_useful", False))
        return {
            "total": self.db.count_patterns(),
            "rising": self.db.count_patterns(label=1),
            "target": self.settings.pattern_target_rising,
            "trained_at": m.trained_at if m else "",
            "accuracy": m.accuracy if m else 0.0,
            "n_samples": m.n_samples if m else 0,
            "filter_on": self.settings.use_pattern_filter,
            "entry_mode": self.settings.pattern_entry_mode,
            "rise_threshold_pct": self.settings.pattern_rise_threshold_pct,
            "horizon_bars": self.settings.pattern_horizon_bars,
            "is_useful": bool(getattr(m, "is_useful", False)) if m else False,
            "applying": applying,
            "val_precision": getattr(m, "val_precision", 0.0) if m else 0.0,
            "val_base_rate": getattr(m, "val_base_rate", 0.0) if m else 0.0,
            "val_signals": getattr(m, "val_signals", 0) if m else 0,
            "val_samples": getattr(m, "val_samples", 0) if m else 0,
            "edge": getattr(m, "edge", 0.0) if m else 0.0,
            "z_score": getattr(m, "z_score", 0.0) if m else 0.0,
            "threshold": getattr(m, "threshold", self.settings.pattern_min_proba) if m
                         else self.settings.pattern_min_proba,
        }

    # ---------- 상승 기록 ----------

    def _maybe_record_rise(self, market: str, price: float) -> None:
        """구동 중 감시 종목이 기준가 대비 일정 % 이상 오르면 기록한다.

        기준가는 값이 더 내려갈 때만 낮춘다. 그래야 천천히 오르는 흐름도
        누적으로 잡힌다 (매 루프마다 기준을 갱신하면 완만한 상승을 놓친다)."""
        now = datetime.now()
        base = self._rise_baseline.get(market)
        if base is None or price < base[1]:
            self._rise_baseline[market] = (now, price)
            return

        base_ts, base_price = base
        if base_price <= 0:
            return
        change_pct = (price - base_price) / base_price * 100
        if change_pct < self.settings.rise_record_threshold_pct:
            return

        window_min = max(int((now - base_ts).total_seconds() // 60), 1)
        held = self.ledger.get_position(market).volume > 0
        self.db.log_rise_event(self._session_id, market, base_price, price,
                               window_min, held)
        log.info("[상승] %s %+.2f%% (%.0f→%.0f, %d분)%s", market, change_pct,
                 base_price, price, window_min, " *보유중*" if held else "")
        self._rise_baseline[market] = (now, price)   # 기록 후 기준 갱신

    def trade_loop_once(self) -> None:
        price_cache: dict[str, float] = {}
        self._maybe_reset_day(price_cache)
        self._maybe_refresh_watchlist()

        loss_exceeded = self.ledger.daily_loss_exceeded(
            self._price_lookup(price_cache),
            self.settings.daily_loss_limit_ratio,
            self._day_start_value or self.ledger.budget_krw,
        )
        if loss_exceeded:
            log.warning("일일 손실 한도 초과 - 오늘은 신규 매수를 중단합니다 (보유분 관리만 계속).")

        for market in self._watchlist:
            try:
                self._process_market(market, allow_buy=not loss_exceeded, price_cache=price_cache)
            except Exception:
                log.exception("마켓 %s 처리 중 오류", market)

        total_value = self.ledger.total_value(self._price_lookup(price_cache))
        holdings_value = total_value - self.ledger.cash_krw
        self.db.log_ledger_snapshot(self.ledger.cash_krw, holdings_value, self.ledger.budget_krw)

        self.last_snapshot = {
            "ts": datetime.now(),
            "cash_krw": self.ledger.cash_krw,
            "holdings_value_krw": holdings_value,
            "total_value_krw": total_value,
            "budget_krw": self.ledger.budget_krw,
            "pnl_krw": total_value - self.ledger.budget_krw,
            "pnl_pct": ((total_value - self.ledger.budget_krw) / self.ledger.budget_krw * 100)
                        if self.ledger.budget_krw else 0.0,
            "watchlist": list(self._watchlist),
            "holdings": {m: p.volume for m, p in self.ledger.positions.items() if p.volume > 0},
            "live_trading": self.settings.live_trading,
            "uptime_sec": self.uptime_seconds(),
            "uptime_text": self.uptime_text(),
            "session_id": self._session_id,
            "session_realized_pnl_krw": self.ledger.realized_pnl_krw - self._session_start_realized,
            "session_trades": self._session_trade_count,
            "pattern": self.pattern_summary(),
            "account": self.account_overview(total_value),
        }
        if self.on_tick:
            try:
                self.on_tick(self.last_snapshot)
            except Exception:
                log.exception("on_tick 콜백 처리 중 오류")

        self._maybe_retrain()
        self._maybe_generate_report()

    def _process_market(self, market: str, allow_buy: bool, price_cache: dict) -> None:
        df = self.exchange.get_ohlcv(market, interval="minute15", count=200)
        if df is None or len(df) < 30:
            log.warning("%s 캔들 데이터를 충분히 가져오지 못했습니다.", market)
            return

        price = float(df["close"].iloc[-1])
        price_cache[market] = price

        # 보유 중이면 최고가/보유기간을 갱신한 뒤, 그 상태를 전략에 넘긴다.
        # 이걸 넘겨야 전략이 손절과 트레일링 스톱을 판단할 수 있다.
        self.ledger.touch_position(market, price)
        decision = self.strategy.decide(df, self.ledger.position_state(market))
        self.db.log_price(market, price)
        self._maybe_record_rise(market, price)
        self._collect_live_patterns(market, df)

        mode = "live" if self.settings.live_trading else "paper"

        # 매수를 낼지 결정한다.
        #  - 전략(RSI/이동평균)이 매수를 내면 학습 모델이 한 번 검토한다.
        #  - signal 모드에서는 전략이 조용하더라도 학습 모델이 '급등 직전 모습'
        #    을 발견하면 스스로 매수를 낸다. 이것이 오르기 전에 미리 사는 경로다.
        buy_reason = None
        if allow_buy:
            if decision.signal == "buy":
                if self._pattern_allows_buy(market, df):
                    buy_reason = decision.reason
            elif decision.signal == "hold":
                buy_reason = self._pattern_entry_signal(market, df)

        if buy_reason:
            krw_amount = self.ledger.max_buyable_krw(market, self.settings.max_position_ratio)
            if krw_amount < 5000:
                log.info("%s 매수 신호이나 배정 가능 금액이 너무 작음 (%.0f원). 스킵.",
                          market, krw_amount)
                return
            try:
                order = self.exchange.buy_market(market, krw_amount, price)
            except Exception:
                log.exception("%s 매수 주문 실패", market)
                return
            self.ledger.record_buy(market, order.krw_amount, order.volume, order.fee_krw)
            self.db.log_trade(market, "buy", order.price, order.volume, order.krw_amount,
                               order.fee_krw, buy_reason, mode, self.strategy.name)
            self._session_trade_count += 1
            log.info("[매수] %s %.0f원 @ %.0f (%s)", market, order.krw_amount, order.price,
                      buy_reason)

        elif decision.signal == "sell":
            actual_volume = None
            if self.settings.live_trading:
                actual_volume = self._get_actual_balance_volume(market)
            volume = self.ledger.sellable_volume(market, actual_volume)
            if volume <= 0:
                return
            try:
                order = self.exchange.sell_market(market, volume, price)
            except Exception:
                log.exception("%s 매도 주문 실패", market)
                return
            try:
                realized = self.ledger.record_sell(
                    market, order.volume, order.krw_amount, order.fee_krw
                )
            except LedgerError:
                log.exception("장부 불일치로 매도 기록 실패 (수동 매매와 충돌 가능성) - 확인 필요")
                return
            self.db.log_trade(market, "sell", order.price, order.volume, order.krw_amount,
                               order.fee_krw, decision.reason, mode, self.strategy.name, realized)
            self._session_trade_count += 1
            log.info("[매도] %s %.0f원 @ %.0f 실현손익 %.0f원 (%s)", market, order.krw_amount,
                      order.price, realized, decision.reason)
        else:
            log.debug("%s hold: %s", market, decision.reason)

    def _collect_live_patterns(self, market: str, df) -> None:
        """구동 중에도 학습 표본을 계속 모은다.

        방금 받아온 캔들 중 '미래가 이미 지난' 구간은 라벨을 확정할 수 있다.
        같은 (종목, 주기, 시각) 은 DB 의 고유 인덱스가 중복을 막아 준다."""
        if not self.settings.pattern_learning_enabled:
            return
        try:
            rows = pattern_mod.extract_samples(
                market, df, source="live",
                horizon=self.settings.pattern_horizon_bars,
                rise_threshold_pct=self.settings.pattern_rise_threshold_pct)
            if rows:
                self.db.insert_patterns(rows)
        except Exception:
            log.debug("%s 실시간 패턴 수집 실패 (무시)", market, exc_info=True)

    def _pattern_entry_signal(self, market: str, df):
        """학습 모델이 '지금이 급등 직전 모습' 이라고 보면 매수 사유를 돌려준다.

        이미 오른 종목을 쫓아 사는 것이 아니라, 과거에 크게 올랐던 종목들이
        오르기 직전에 보였던 모습과 지금이 얼마나 닮았는지를 보는 것이다.

        아무 근거 없이 사지 않도록 조건을 건다.
        - PATTERN_ENTRY_MODE 가 signal 일 것
        - 모델이 '안 본 데이터' 검증에서 기준선을 의미 있게 넘었을 것(is_useful)
        - 상승 확률이 기준(PATTERN_MIN_PROBA) 이상일 것
        """
        if self.settings.pattern_entry_mode != "signal":
            return None
        model = self.pattern_model
        if model is None or not getattr(model, "is_useful", False):
            return None
        if self.ledger.get_position(market).volume > 0:
            return None   # 이미 들고 있으면 추가 매수하지 않는다

        try:
            feats = pattern_mod.latest_features(df)
            if feats is None:
                return None
            proba = model.predict_proba(feats)
        except Exception:
            log.debug("%s 급등 전조 계산 실패 (무시)", market, exc_info=True)
            return None

        # 기준은 학습할 때 '적중률이 가장 높아지는 값' 으로 모델이 직접 고른다.
        # 설정값(PATTERN_MIN_PROBA)은 그보다 더 엄격하게 하고 싶을 때의 하한이다.
        need = max(getattr(model, "threshold", 0.55), self.settings.pattern_min_proba)
        if proba < need:
            return None

        return (f"급등 전조 포착 - 상승 확률 {proba*100:.0f}% "
                f"(기준 {need*100:.0f}%, 검증 적중률 {model.val_precision*100:.0f}%)")

    def _pattern_allows_buy(self, market: str, df) -> bool:
        """학습된 패턴 모델의 의견을 매수 판단에 반영한다.

        적용 조건이 두 가지다.
        1) 사용자가 USE_PATTERN_FILTER 로 켜 두었을 것
        2) 모델이 '안 본 데이터'로 한 검증에서 기준선을 넘었을 것 (is_useful)

        2번을 두는 이유: 기준선보다 못한 모델로 매수를 걸러내면 아무 근거 없이
        기회만 날린다. 표본이 더 쌓여 성능이 올라오면 자동으로 적용되기 시작한다.
        """
        model = self.pattern_model
        if model is None:
            return True
        try:
            feats = pattern_mod.latest_features(df)
            if feats is None:
                return True
            proba = model.predict_proba(feats)
        except Exception:
            log.debug("%s 패턴 확률 계산 실패 (무시)", market, exc_info=True)
            return True

        if not self.settings.use_pattern_filter:
            log.info("%s 매수 신호 - 학습 모델 상승 확률 %.0f%% (필터 꺼짐, 매매에 미반영)",
                     market, proba * 100)
            return True

        if not getattr(model, "is_useful", False):
            log.info("%s 매수 신호 - 학습 모델 상승 확률 %.0f%%. "
                     "다만 모델이 아직 검증 기준을 못 넘어(이득 %+.1f%%p) 이번 판단에는 쓰지 않습니다.",
                     market, proba * 100, getattr(model, "edge", 0.0) * 100)
            return True

        if proba < self.settings.pattern_min_proba:
            log.info("%s 매수 신호였으나 상승 확률 %.0f%% 가 기준 %.0f%% 미만 - 건너뜁니다. "
                     "(이 필터의 검증 적중률 %.0f%%)",
                     market, proba * 100, self.settings.pattern_min_proba * 100,
                     model.val_precision * 100)
            return False

        log.info("%s 매수 신호 + 상승 확률 %.0f%% (기준 %.0f%% 통과) - 진행합니다.",
                 market, proba * 100, self.settings.pattern_min_proba * 100)
        return True

    def account_overview(self, auto_total_krw: float) -> dict:
        """업비트 계정 전체 자산과 '수동 투자 자산' 을 계산한다.

        모드에 따라 계산이 다르다 - 이걸 헷갈리면 숫자가 틀린다.

        - 실거래: 봇이 산 코인도 실제 계좌에 들어 있다.
                  따라서  수동 = 계정 전체 - 자동매매 장부 평가액
        - 모의매매: 봇의 매수는 장부상 가상이라 계좌에 없다.
                    따라서  수동 = 계정 전체 (전부가 사용자의 수동 자산)
                    자동매매 풀은 계정과 별개인 가상 금액이다.
        """
        try:
            acc = self.exchange.get_account_summary()
        except Exception as exc:
            log.debug("계좌 조회 실패", exc_info=True)
            acc = {"available": False, "reason": str(exc)}

        if not acc.get("available"):
            reason = acc.get("reason") or "API 키 미설정 또는 조회 실패"
            if reason != self._last_account_error:
                # 같은 오류로 로그를 도배하지 않도록 바뀔 때만 남긴다
                log.warning("계좌 조회 실패: %s", reason)
                self._last_account_error = reason
            return {"available": False, "reason": reason}
        self._last_account_error = ""

        account_total = float(acc["total"])
        if self.settings.live_trading:
            manual = max(account_total - auto_total_krw, 0.0)
            grand_total = account_total
        else:
            manual = account_total
            grand_total = account_total + auto_total_krw

        return {
            "available": True,
            "account_total_krw": account_total,   # 업비트 계정 전체
            "manual_krw": manual,                 # 수동 투자 자산
            "auto_krw": auto_total_krw,           # 자동매매 자산
            "grand_total_krw": grand_total,       # 화면에 보여줄 '내 전체 자산'
            "krw_cash": float(acc["krw"]),
            "coin_value": float(acc["coin_value"]),
            "top_items": acc["items"][:5],
            "live": self.settings.live_trading,
        }

    def _get_actual_balance_volume(self, market: str) -> float:
        currency = market.split("-")[1]
        for b in self.exchange.get_account_balances():
            if b.get("currency") == currency:
                return float(b.get("balance", 0))
        return 0.0

    def _maybe_generate_report(self) -> None:
        now = datetime.now()
        interval = timedelta(hours=self.settings.report_interval_hours)
        if self._last_report_at is None or now - self._last_report_at >= interval:
            try:
                path = report_mod.generate_report(self.settings, self.db, self.ledger,
                                                    self._price_lookup({}),
                                                    watchlist=self._watchlist,
                                                    scan_summary=self._last_scan_summary)
                log.info("리포트 생성 완료: %s", path)
            except Exception:
                log.exception("리포트 생성 실패")
            self._last_report_at = now

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        """계속 실행. stop_event 가 set() 되면 최대 1초 안에 루프를 빠져나온다
        (GUI의 '중지' 버튼 등에서 사용)."""
        interval_sec = self.settings.trade_loop_interval_min * 60
        log.info("매매 루프 시작. 주기: %d분", self.settings.trade_loop_interval_min)
        if self._session_id is None:
            self.begin_session()
        try:
            self._run_loop(stop_event, interval_sec)
        finally:
            self.finish_session("정상 종료" if stop_event is None or stop_event.is_set()
                                else "루프 종료")

    def _run_loop(self, stop_event, interval_sec: float) -> None:
        while stop_event is None or not stop_event.is_set():
            start = time.time()
            try:
                self.trade_loop_once()
            except Exception:
                log.exception("매매 루프 실행 중 예상치 못한 오류 - 다음 주기에 재시도")
            elapsed = time.time() - start
            sleep_for = max(interval_sec - elapsed, 5)
            # 1초 단위로 나눠서 자서 stop_event 를 빠르게 감지할 수 있게 함
            slept = 0.0
            while slept < sleep_for:
                if stop_event is not None and stop_event.is_set():
                    break
                chunk = min(1.0, sleep_for - slept)
                time.sleep(chunk)
                slept += chunk
        log.info("중지 신호를 받아 매매 루프를 종료합니다.")


def main():
    parser = argparse.ArgumentParser(description="코인 자동매매 봇")
    parser.add_argument("--once", action="store_true", help="루프 한 번만 실행하고 종료 (테스트용)")
    args = parser.parse_args()

    settings = load_settings()
    try:
        app = TraderApp(settings)
    except SettingsError as e:
        log.error("%s", e)
        sys.exit(1)
    if args.once:
        app.trade_loop_once()
    else:
        app.run_forever()


if __name__ == "__main__":
    main()
