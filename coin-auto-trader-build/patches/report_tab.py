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
    def __init__(self, parent, get_db, get_trader, reports_dir: Path):
        super().__init__(parent, padding=10)
        self.get_db = get_db
        self.get_trader = get_trader
        self.reports_dir = Path(reports_dir)
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
                             ("progress", "total", "accuracy", "trained", "filter")}
        prows = [
            ("상승 패턴 수집", "progress"),
            ("전체 표본", "total"),
            ("학습 정확도", "accuracy"),
            ("마지막 학습", "trained"),
            ("매매 반영 여부", "filter"),
        ]
        for i, (label, key) in enumerate(prows):
            ttk.Label(pat_tab, text=label, width=16).grid(row=i, column=0, sticky="w", pady=5)
            ttk.Label(pat_tab, textvariable=self.pattern_vars[key],
                      font=("", 10, "bold")).grid(row=i, column=1, sticky="w", pady=5)
        self.pattern_bar = ttk.Progressbar(pat_tab, length=320, maximum=100)
        self.pattern_bar.grid(row=len(prows), column=0, columnspan=2,
                              sticky="w", pady=(12, 6))
        ttk.Label(
            pat_tab, foreground="#666", wraplength=620, justify="left",
            text="프로그램을 실행하면 상승 패턴 표본이 목표치(기본 1000개)에 못 미칠 때\n"
                 "과거 캔들에서 자동으로 채운 뒤 학습합니다. 구동 중에도 계속 모읍니다.\n\n"
                 "학습 결과는 기본적으로 '참고용'입니다. 실제 매수 판단에 반영하려면\n"
                 ".env 에서 USE_PATTERN_FILTER=true 로 직접 켜야 합니다.",
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
        target = 1000
        filter_on = False
        accuracy = trained = ""
        if trader is not None:
            ps = trader.pattern_summary()
            target = ps.get("target", 1000) or 1000
            filter_on = ps.get("filter_on", False)
            accuracy = f"{ps.get('accuracy', 0) * 100:.1f}%" if ps.get("n_samples") else ""
            trained = ps.get("trained_at", "")
        pct = min(rising / target * 100, 100) if target else 0
        self.vars["pattern"].set(f"{rising:,} / {target:,}개")
        self.pattern_vars["progress"].set(
            f"{rising:,} / {target:,}개  ({pct:.0f}%)"
            + ("  목표 달성" if rising >= target else "  수집 중"))
        self.pattern_vars["total"].set(f"{total_pat:,}개")
        self.pattern_vars["accuracy"].set(accuracy or "(아직 학습 전)")
        self.pattern_vars["trained"].set(
            trained.replace("T", " ")[:19] if trained else "(아직 학습 전)")
        self.pattern_vars["filter"].set(
            "반영함 (USE_PATTERN_FILTER=true)" if filter_on
            else "참고용 - 매매에 반영 안 함")
        self.pattern_bar["value"] = pct

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
