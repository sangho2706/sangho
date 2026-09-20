"""
백테스트 엔진.

실거래를 켜기 전에 반드시 여기서 전략을 과거 데이터로 검증할 것.
ledger.py 의 규칙(예산 초과 금지 등)을 그대로 재사용해서, 백테스트와 실전
로직이 최대한 같은 코드 경로를 타도록 했다.

사용 예:
    python backtest.py --market KRW-BTC --interval minute15 --count 2000
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pandas as pd

from exchange import ExchangeClient, UPBIT_FEE_RATE
from ledger import AutoLedger
from strategy import DEFAULT_STRATEGY, Strategy


def run_backtest(df: pd.DataFrame, strategy: Strategy, budget_krw: float,
                  max_position_ratio: float = 1.0, min_bars: int = 30) -> dict:
    """df: 시간순 정렬된 OHLCV DataFrame (컬럼: open, high, low, close, volume).

    한 번에 한 종목만 백테스트한다 (여러 종목 조합/리밸런싱은 이후 단계 과제).
    """
    with tempfile.TemporaryDirectory() as tmp:
        ledger = AutoLedger(Path(tmp) / "state.json", budget_krw)
        market = "BACKTEST"
        trades = []
        equity_curve = []

        for i in range(min_bars, len(df)):
            window = df.iloc[: i + 1]
            price = float(window["close"].iloc[-1])
            # 실전과 같은 경로: 고점/보유기간을 갱신하고 그 상태를 전략에 넘긴다.
            # 이걸 빠뜨리면 손절/트레일링이 백테스트에서 전혀 작동하지 않아
            # 실제 성과와 전혀 다른 결과가 나온다.
            ledger.touch_position(market, price)
            decision = strategy.decide(window, ledger.position_state(market))

            if decision.signal == "buy":
                krw_amount = ledger.max_buyable_krw(market, max_position_ratio)
                if krw_amount > 1000:  # 업비트 최소 주문금액 근사치
                    fee = krw_amount * UPBIT_FEE_RATE
                    volume = (krw_amount - fee) / price
                    ledger.record_buy(market, krw_amount, volume, fee)
                    trades.append({"i": i, "side": "buy", "price": price,
                                    "krw": krw_amount, "reason": decision.reason})

            elif decision.signal == "sell":
                volume = ledger.sellable_volume(market)
                if volume > 0:
                    krw_amount = volume * price
                    fee = krw_amount * UPBIT_FEE_RATE
                    net = krw_amount - fee
                    realized = ledger.record_sell(market, volume, net, fee)
                    trades.append({"i": i, "side": "sell", "price": price,
                                    "krw": net, "reason": decision.reason,
                                    "realized_pnl": realized})

            equity_curve.append(ledger.total_value(lambda m: price))

        final_price = float(df["close"].iloc[-1])
        final_value = ledger.total_value(lambda m: final_price)

        buy_trades = [t for t in trades if t["side"] == "buy"]
        sell_trades = [t for t in trades if t["side"] == "sell"]
        wins = [t for t in sell_trades if t.get("realized_pnl", 0) > 0]
        losses = [t for t in sell_trades if t.get("realized_pnl", 0) < 0]

        avg_win = sum(t["realized_pnl"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(-t["realized_pnl"] for t in losses) / len(losses) if losses else 0.0
        total_win = sum(t["realized_pnl"] for t in wins)
        total_loss = sum(-t["realized_pnl"] for t in losses)
        # 손익비: 이익 총합 / 손실 총합. 1 보다 커야 돈을 번다.
        profit_factor = (total_win / total_loss) if total_loss > 0 else float("inf")
        # 최대 낙폭: 자산 곡선의 고점 대비 최대 하락률
        peak = -float("inf")
        max_dd = 0.0
        for v in equity_curve:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)

        return {
            "avg_win_krw": avg_win,
            "avg_loss_krw": avg_loss,
            "total_win_krw": total_win,
            "total_loss_krw": total_loss,
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_dd * 100,
            "num_wins": len(wins),
            "num_losses": len(losses),
            "start_value": budget_krw,
            "final_value": final_value,
            "pnl_krw": final_value - budget_krw,
            "pnl_pct": (final_value - budget_krw) / budget_krw * 100,
            "num_trades": len(trades),
            "num_buys": len(buy_trades),
            "num_sells": len(sell_trades),
            "win_rate_pct": (len(wins) / len(sell_trades) * 100) if sell_trades else 0.0,
            "equity_curve": equity_curve,
            "trades": trades,
            # 참고용 벤치마크: 그냥 처음에 다 사서 안 팔고 들고만 있었으면?
            "buy_and_hold_pct": (final_price / float(df["close"].iloc[min_bars]) - 1) * 100,
        }


def sweep(df: pd.DataFrame, budget_krw: float,
          stop_losses=(0.03, 0.05, 0.08, 0.10),
          trail_starts=(0.03, 0.05, 0.10),
          trail_drawdowns=(0.02, 0.03, 0.05)) -> list[dict]:
    """손절/트레일링 값을 바꿔 가며 돌려 보고, 성적 순으로 정렬해 돌려준다.

    '어떤 설정값이 좋은가' 는 시장과 종목마다 달라서 정답이 없다. 추측하지 말고
    이 함수로 실제 데이터에서 직접 확인하는 것이 맞다.
    """
    rows = []
    for sl in stop_losses:
        for ts in trail_starts:
            for td in trail_drawdowns:
                strat = DEFAULT_STRATEGY(stop_loss_pct=sl, trail_start_pct=ts,
                                         trail_drawdown_pct=td)
                r = run_backtest(df, strat, budget_krw)
                rows.append({
                    "손절": sl, "트레일시작": ts, "되돌림": td,
                    "손익%": r["pnl_pct"], "손익비": r["profit_factor"],
                    "승률%": r["win_rate_pct"], "매도": r["num_sells"],
                    "평균이익": r["avg_win_krw"], "평균손실": r["avg_loss_krw"],
                    "최대낙폭%": r["max_drawdown_pct"],
                })
    rows.sort(key=lambda x: x["손익%"], reverse=True)
    return rows


def _print_result(strategy, df, result, min_bars: int = 30) -> None:
    print("\n===== 백테스트 결과 =====")
    print(f"전략: {strategy.name}")
    print(f"기간: {df.index[0]} ~ {df.index[-1]} ({len(df)} 캔들)")
    print(f"시작 자산: {result['start_value']:,.0f}원")
    print(f"종료 자산: {result['final_value']:,.0f}원")
    print(f"손익: {result['pnl_krw']:,.0f}원 ({result['pnl_pct']:.2f}%)")
    print(f"매매 횟수: {result['num_trades']} (매수 {result['num_buys']} / 매도 {result['num_sells']})")
    print(f"승률(매도 기준): {result['win_rate_pct']:.1f}% "
          f"(이익 {result['num_wins']}건 / 손실 {result['num_losses']}건)")
    print(f"평균 이익: {result['avg_win_krw']:,.0f}원 / 평균 손실: {result['avg_loss_krw']:,.0f}원")
    pf = result["profit_factor"]
    print(f"손익비(이익합/손실합): {pf:.2f}  <- 1.0 보다 커야 돈을 법니다")
    print(f"최대 낙폭: {result['max_drawdown_pct']:.1f}%")
    print(f"참고 - 그냥 계속 보유했다면: {result['buy_and_hold_pct']:.2f}%")
    print("\n※ 과거 데이터 백테스트 결과는 미래 수익을 보장하지 않습니다.")


def main():
    parser = argparse.ArgumentParser(description="전략 백테스트")
    parser.add_argument("--market", default="KRW-BTC")
    parser.add_argument("--interval", default="minute15",
                         help="minute1/minute5/minute15/minute60/day 등")
    parser.add_argument("--count", type=int, default=2000, help="가져올 캔들 개수 (최대 200*여러페이지)")
    parser.add_argument("--budget", type=float, default=500_000)
    parser.add_argument("--sweep", action="store_true",
                         help="손절/트레일링 값을 바꿔 가며 최적 조합을 찾는다")
    parser.add_argument("--stop-loss", type=float, default=None, help="손절 비율 (0.05 = -5%%)")
    parser.add_argument("--trail-start", type=float, default=None, help="트레일링 시작 수익률")
    parser.add_argument("--trail-drawdown", type=float, default=None, help="고점 대비 되돌림 허용")
    args = parser.parse_args()

    client = ExchangeClient("", "", live_trading=False)
    print(f"{args.market} {args.interval} 캔들 {args.count}개 가져오는 중...")
    df = client.get_ohlcv(args.market, interval=args.interval, count=args.count)
    if df is None or len(df) < 50:
        print("데이터를 충분히 가져오지 못했습니다. 네트워크/마켓 코드를 확인하세요.")
        return

    if args.sweep:
        print("\n설정값을 바꿔 가며 돌려봅니다 (수십 초 걸릴 수 있습니다)...")
        rows = sweep(df, args.budget)
        print(f"\n===== {args.market} 최적 설정 상위 10개 =====")
        print(f"{'손절':>6} {'트레일시작':>10} {'되돌림':>7} "
              f"{'손익%':>9} {'손익비':>7} {'승률%':>7} {'매도':>5} {'최대낙폭%':>9}")
        for r in rows[:10]:
            pf = "inf" if r["손익비"] == float("inf") else f"{r['손익비']:.2f}"
            print(f"{r['손절']*100:5.0f}% {r['트레일시작']*100:9.0f}% {r['되돌림']*100:6.0f}% "
                  f"{r['손익%']:8.2f}% {pf:>7} {r['승률%']:6.1f}% {r['매도']:5d} "
                  f"{r['최대낙폭%']:8.1f}%")
        best = rows[0]
        print("\n이 종목/기간에서 가장 좋았던 값 (.env 에 넣으세요):")
        print(f"  STOP_LOSS_PCT={best['손절']}")
        print(f"  TRAIL_START_PCT={best['트레일시작']}")
        print(f"  TRAIL_DRAWDOWN_PCT={best['되돌림']}")
        print("\n※ 한 종목의 과거 한 구간에서 좋았던 값일 뿐입니다. 여러 종목으로")
        print("   돌려보고 공통적으로 무난한 값을 쓰세요. 특정 구간에만 잘 맞는")
        print("   값을 고르면 실전에서 오히려 나빠집니다(과최적화).")
        return

    kwargs = {}
    if args.stop_loss is not None:
        kwargs["stop_loss_pct"] = args.stop_loss
    if args.trail_start is not None:
        kwargs["trail_start_pct"] = args.trail_start
    if args.trail_drawdown is not None:
        kwargs["trail_drawdown_pct"] = args.trail_drawdown

    strategy = DEFAULT_STRATEGY(**kwargs)
    result = run_backtest(df, strategy, args.budget)
    _print_result(strategy, df, result)


if __name__ == "__main__":
    main()
