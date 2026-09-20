"""
'리포트' 탭.

'자산 현황' 오른쪽에 붙는 화면으로, 다음을 한곳에서 보여준다.

- 요약   : 구동 시간, 누적/이번 구동 손익, 매매 횟수, 패턴 학습 현황
- 그래프 : 자산 추이(선), 일별 실현손익(막대)
- 매매 이력 : 날짜·종목·금액 등을 엑셀처럼 표로 (엑셀 파일로 내보내기 가능)
- 상승 기록 : 구동 중에 오른 종목
- 패턴 학습 : 모아둔 표본 수와 목표 달성 여부

봇이 돌고 있지 않아도 DB 에 쌓인 기록으로 전부 표시된다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import charts
import history_export as hx

REFRESH_MS = 5000


def _fmt_krw(v) -> str:
    try:
        return f"{float(v):,.0f}원"
    except (TypeError, ValueError):
        return "-"


def _fmt_signed(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{v:+,.0f}원"


def _open_path(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        messagebox.showinfo("경로", f"다음 경로에서 직접 열어주세요:\n{path}")


class _Table(ttk.Frame):
    """스크롤 되는 표 (엑셀 느낌)."""

    def __init__(self, parent, headers: list[str], widths: list[int] | None = None,
                 empty_text: str = "아직 기록이 없습니다."):
        super().__init__(parent)
        self.headers = headers
        self.empty_text = empty_text

        self.tree = ttk.Treeview(self, columns=headers, show="headings", height=10)
        for i, h in enumerate(headers):
            self.tree.heading(h, text=h)
            w = (widths[i] if widths and i < len(widths) else 100)
            # 긴 설명 칸만 남는 폭을 가져가게 한다. 시각/금액 칸까지 늘어나면
            # 한쪽만 휑하게 벌어져서 보기 나쁘다.
            wide = w >= 200
            self.tree.column(h, width=w, anchor=("w" if wide else "center"),
                             stretch=wide)

        ysb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        # 이익/손실 색 구분
        self.tree.tag_configure("profit", foreground="#c5221f")
        self.tree.tag_configure("loss", foreground="#1a73e8")
        self.tree.tag_configure("empty", foreground="#8a8a8a")

        self.empty_label = ttk.Label(self, text=empty_text, foreground="#8a8a8a")

    @staticmethod
    def _with_commas(value) -> str:
        """화면 표시용 천단위 구분. 엑셀로 내보낼 때는 원본 숫자를 그대로 쓰므로
        여기서만 문자열로 바꾼다 (엑셀에서 계산이 되어야 하기 때문)."""
        if value is None or value == "":
            return ""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return str(value)
        if v == int(v) and abs(v) < 1e15:
            return f"{int(v):,}"
        return f"{v:,.2f}"

    def set_rows(self, rows: list[list], pnl_col: int | None = None,
                 comma_cols: tuple[int, ...] = ()) -> None:
        self.tree.delete(*self.tree.get_children())
        if not rows:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return
        self.empty_label.place_forget()
        for r in rows:
            tag = ""
            if pnl_col is not None and pnl_col < len(r):
                try:
                    v = float(r[pnl_col])
                    tag = "profit" if v > 0 else "loss" if v < 0 else ""
                except (TypeError, ValueError):
                    tag = ""
            shown = []
            for i, c in enumerate(r):
                if c is None:
                    shown.append("")
                elif i in comma_cols:
                    shown.append(self._with_commas(c))
                else:
                    shown.append(c)
            self.tree.insert("", "end", values=shown, tags=(tag,) if tag else ())


class ReportTab(ttk.Frame):
    def __init__(self, parent, get_db, get_trader, reports_dir: Path,
                 model_path: Path | None = None):
        super().__init__(parent, padding=10)
        self.get_db = get_db
        self.get_trader = get_trader
        self.reports_dir = Path(reports_dir)
        self.model_path = Path(model_path) if model_path else None
        self._build()
        self.after(800, self._auto_refresh)

    # ---------- 화면 구성 ----------

    def _build(self) -> None:
        # 요약 줄
        summary = ttk.LabelFrame(self, text="요약", padding=10)
        summary.pack(fill="x")

        self.vars = {k: tk.StringVar(value="-") for k in
                     ("uptime", "total_pnl", "session_pnl", "trades",
                      "realized", "pattern", "updated")}
        cells = [
            ("구동 시간", "uptime"), ("누적 손익", "total_pnl"),
            ("이번 구동 손익", "session_pnl"), ("매매 횟수", "trades"),
            ("확정 손익 합계", "realized"), ("패턴 학습", "pattern"),
        ]
        for i, (label, key) in enumerate(cells):
            r, c = divmod(i, 3)
            cell = ttk.Frame(summary)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 26), pady=3)
            ttk.Label(cell, text=label, foreground="#666").pack(anchor="w")
            ttk.Label(cell, textvariable=self.vars[key],
                      font=("", 11, "bold")).pack(anchor="w")

        # 버튼 줄
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(8, 6))
        ttk.Button(bar, text="새로고침", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="엑셀로 내보내기",
                   command=self._on_export).pack(side="left", padx=6)
        ttk.Button(bar, text="리포트 폴더 열기",
                   command=lambda: _open_path(self.reports_dir)).pack(side="left")
        ttk.Label(bar, textvariable=self.vars["updated"],
                  foreground="#8a8a8a").pack(side="right")

        # 하위 탭
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # --- 그래프 ---
        graph_tab = ttk.Frame(nb, padding=10)
        nb.add(graph_tab, text="그래프")
        self.equity_panel = charts.ChartPanel(
            graph_tab, "자산 추이", kind="line", height=180,
            empty_text="봇을 한 번 실행하면 자산 추이가 그려집니다.")
        self.equity_panel.pack(fill="both", expand=True, pady=(0, 10))
        self.pnl_panel = charts.ChartPanel(
            graph_tab, "일별 실현손익 (빨강=이익, 파랑=손실)", kind="bar", height=170,
            empty_text="매도가 한 번이라도 체결되면 막대가 표시됩니다.")
        self.pnl_panel.pack(fill="both", expand=True)

        # --- 매매 이력 ---
        trade_tab = ttk.Frame(nb, padding=6)
        nb.add(trade_tab, text="매매 이력")
        self.trade_table = _Table(
            trade_tab, hx.TRADE_HEADERS,
            widths=[46, 158, 92, 52, 108, 104, 108, 80, 104, 58, 104, 240],
            empty_text="아직 매매 기록이 없습니다. 봇을 실행하면 여기에 쌓입니다.")
        self.trade_table.pack(fill="both", expand=True)

        # --- 상승 기록 ---
        rise_tab = ttk.Frame(nb, padding=6)
        nb.add(rise_tab, text="상승 기록")
        ttk.Label(rise_tab, foreground="#666",
                  text="구동 중에 감시 종목이 오른 기록입니다. '보유 중'이 예면 봇이 그 상승을 탄 것입니다."
                  ).pack(anchor="w", pady=(0, 4))
        self.rise_table = _Table(
            rise_tab, hx.RISE_HEADERS, widths=[158, 92, 108, 108, 82, 92, 82],
            empty_text="아직 상승 기록이 없습니다.")
        self.rise_table.pack(fill="both", expand=True)

        # --- 구동 이력 ---
        session_tab = ttk.Frame(nb, padding=6)
        nb.add(session_tab, text="구동 이력")
        self.session_table = _Table(
            session_tab, hx.SESSION_HEADERS,
            widths=[46, 158, 158, 104, 58, 104, 108, 108, 104, 74, 160],
            empty_text="아직 구동 기록이 없습니다.")
        self.session_table.pack(fill="both", expand=True)

        # --- 패턴 학습 ---
        pat_tab = ttk.Frame(nb, padding=12)
        nb.add(pat_tab, text="패턴 학습")
        self.pattern_vars = {k: tk.StringVar(value="-") for k in
                             ("progress", "total", "precision", "edge",
                              "trained", "filter")}
        prows = [
            ("상승 패턴 수집", "progress"),
            ("전체 표본", "total"),
            ("검증 적중률", "precision"),
            ("기준선 대비", "edge"),
            ("마지막 학습", "trained"),
            ("매매 반영 상태", "filter"),
        ]
        for i, (label, key) in enumerate(prows):
            ttk.Label(pat_tab, text=label, width=16).grid(row=i, column=0, sticky="w", pady=5)
            ttk.Label(pat_tab, textvariable=self.pattern_vars[key],
                      font=("", 10, "bold")).grid(row=i, column=1, sticky="w", pady=5)
        self.pattern_bar = ttk.Progressbar(pat_tab, length=320, maximum=100)
        self.pattern_bar.grid(row=len(prows), column=0, columnspan=2,
                              sticky="w", pady=(12, 6))
        ttk.Label(
            pat_tab, foreground="#666", wraplength=660, justify="left",
            text="'검증 적중률' 은 학습에 쓰지 않은 최근 구간으로 채점한 값입니다.\n"
                 "모델이 \"오른다\" 고 한 것 중 실제로 오른 비율이며, 이 값이 '기준선'\n"
                 "(아무거나 샀을 때 오를 확률)보다 높아야 쓸모가 있습니다.\n\n"
                 "학습 결과는 매수 판단에 반영됩니다. 다만 기준선을 넘지 못한 동안에는\n"
                 "적용을 보류합니다 - 근거 없는 모델로 매수 기회를 날리지 않기 위해서입니다.\n"
                 "표본이 쌓여 성능이 올라오면 자동으로 적용이 시작됩니다.",
        ).grid(row=len(prows) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---------- 갱신 ----------

    def _auto_refresh(self) -> None:
        try:
            self.refresh()
        except Exception:
            pass
        self.after(REFRESH_MS, self._auto_refresh)

    def refresh(self) -> None:
        db = self.get_db()
        if db is None:
            return
        trader = self.get_trader()

        # --- 요약 ---
        sessions = db.all_sessions()
        total_sec = sum(hx.session_seconds(s["started_at"], s["ended_at"])
                        for s in sessions)
        if trader is not None and trader.uptime_seconds() > 0:
            self.vars["uptime"].set(
                f"{trader.uptime_text()}  (누적 {hx.humanize_seconds(total_sec)})")
        else:
            self.vars["uptime"].set(f"누적 {hx.humanize_seconds(total_sec)}")

        trades = db.all_trades()
        realized = sum((t["realized_pnl_krw"] or 0) for t in trades)
        self.vars["realized"].set(_fmt_signed(realized))
        self.vars["trades"].set(f"{len(trades):,}회")

        snap = db.latest_ledger_snapshot()
        if snap is not None:
            self.vars["total_pnl"].set(
                f"{snap['pnl_krw']:+,.0f}원 ({snap['pnl_pct']:+.2f}%)")
        else:
            self.vars["total_pnl"].set("-")

        if trader is not None and trader._session_id is not None:
            sp = trader.ledger.realized_pnl_krw - trader._session_start_realized
            self.vars["session_pnl"].set(
                f"{_fmt_signed(sp)} / {trader._session_trade_count}회")
        elif sessions:
            self.vars["session_pnl"].set(_fmt_signed(sessions[0]["realized_pnl_krw"] or 0))
        else:
            self.vars["session_pnl"].set("-")

        # --- 패턴 학습 ---
        rising = db.count_patterns(label=1)
        total_pat = db.count_patterns()
        ps = trader.pattern_summary() if trader is not None else self._saved_model_summary()
        target = ps.get("target", 1000) or 1000
        pct = min(rising / target * 100, 100) if target else 0

        self.vars["pattern"].set(f"{rising:,} / {target:,}개")
        self.pattern_vars["progress"].set(
            f"{rising:,} / {target:,}개  ({pct:.0f}%)"
            + ("  목표 달성" if rising >= target else "  수집 중"))
        self.pattern_vars["total"].set(f"{total_pat:,}개")
        self.pattern_bar["value"] = pct

        trained = ps.get("trained_at", "")
        self.pattern_vars["trained"].set(
            trained.replace("T", " ")[:19] if trained else "(아직 학습 전)")

        if ps.get("val_samples"):
            prec = ps.get("val_precision", 0.0) * 100
            base = ps.get("val_base_rate", 0.0) * 100
            edge = ps.get("edge", 0.0) * 100
            if ps.get("val_signals"):
                self.pattern_vars["precision"].set(
                    f"{prec:.1f}%   (매수 신호 {ps['val_signals']:,}회 / "
                    f"검증 표본 {ps['val_samples']:,}개)")
            else:
                self.pattern_vars["precision"].set(
                    f"(기준 {ps.get('threshold', 0.55) * 100:.0f}% 를 넘는 신호가 없었음)")
            z = ps.get("z_score", 0.0)
            if not ps.get("val_signals"):
                # 신호가 한 번도 없으면 적중률 자체가 없다. 이때 이득을 숫자로
                # 보여주면 '크게 손해' 처럼 읽혀서 오해를 준다.
                self.pattern_vars["edge"].set(
                    f"판정 불가 - 매수 신호 없음 (기준선 {base:.1f}%)")
            else:
                self.pattern_vars["edge"].set(
                    f"기준선 {base:.1f}% → {edge:+.1f}%p "
                    + ("이득" if edge > 0 else "손해")
                    + f"   (우연 아닐 확신도 z={z:.1f})")
        else:
            self.pattern_vars["precision"].set("(아직 학습 전)")
            self.pattern_vars["edge"].set("(아직 학습 전)")

        if not ps:
            self.pattern_vars["filter"].set("(봇을 실행하면 표시됩니다)")
        elif ps.get("stopped"):
            self.pattern_vars["filter"].set(
                ("검증 통과 - 실행하면 매수 판단에 반영됩니다" if ps.get("is_useful")
                 else "아직 기준선 미달 - 실행해도 적용은 보류됩니다") + "  (봇 정지 중)")
        elif not ps.get("filter_on"):
            self.pattern_vars["filter"].set("꺼짐 - USE_PATTERN_FILTER=false")
        elif ps.get("applying"):
            self.pattern_vars["filter"].set("반영 중 - 검증 통과")
        else:
            self.pattern_vars["filter"].set(
                "대기 중 - 아직 기준선을 못 넘어 적용 보류")

        # --- 그래프 ---
        equity = hx.equity_series(db)
        self.equity_panel.set_data(equity)
        if snap is not None:
            self.equity_panel.chart.set_baseline(float(snap["budget_krw"]))
        self.pnl_panel.set_data(hx.daily_pnl_series(db))

        # --- 표 ---
        self.trade_table.set_rows(hx.trade_rows(db), pnl_col=8,
                                  comma_cols=(4, 6, 7, 8))
        self.rise_table.set_rows(hx.rise_rows(db), pnl_col=4, comma_cols=(2, 3))
        self.session_table.set_rows(hx.session_rows(db), pnl_col=8,
                                    comma_cols=(5, 6, 7, 8))

        self.vars["updated"].set("갱신 " + datetime.now().strftime("%H:%M:%S"))

    def _saved_model_summary(self) -> dict:
        """봇이 꺼져 있을 때는 마지막으로 저장된 학습 모델을 읽어 보여준다."""
        if self.model_path is None or not self.model_path.exists():
            return {}
        try:
            import patterns
            m = patterns.PatternModel.load(self.model_path)
        except Exception:
            return {}
        if m is None:
            return {}
        return {
            "target": 1000, "trained_at": m.trained_at, "accuracy": m.accuracy,
            "n_samples": m.n_samples, "threshold": m.threshold,
            "val_samples": m.val_samples, "val_signals": m.val_signals,
            "val_precision": m.val_precision, "val_base_rate": m.val_base_rate,
            "edge": m.edge, "z_score": m.z_score, "is_useful": m.is_useful,
            "filter_on": None,   # 봇이 꺼져 있어 현재 설정을 알 수 없음
            "applying": False, "stopped": True,
        }

    # ---------- 내보내기 ----------

    def _on_export(self) -> None:
        db = self.get_db()
        if db is None:
            messagebox.showinfo("내보내기", "아직 저장된 기록이 없습니다.")
            return
        ext = ".xlsx" if hx.openpyxl is not None else ".csv"
        default = "투자이력_" + datetime.now().strftime("%Y%m%d_%H%M") + ext
        path = filedialog.asksaveasfilename(
            parent=self, title="투자 이력 저장", defaultextension=ext,
            initialfile=default, initialdir=str(self.reports_dir),
            filetypes=[("엑셀 파일", "*.xlsx")] if ext == ".xlsx"
            else [("CSV 파일", "*.csv")],
        )
        if not path:
            return
        out = Path(path)
        try:
            saved = hx.export(db, out.parent, out.stem)
        except Exception as exc:
            messagebox.showerror("내보내기 실패", f"저장 중 문제가 발생했습니다.\n\n{exc}")
            return
        if messagebox.askyesno("내보내기 완료",
                               f"저장했습니다.\n\n{saved}\n\n지금 열어볼까요?"):
            _open_path(saved)
