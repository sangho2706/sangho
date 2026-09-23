"""
코인 자동매매 봇 - 데스크톱 앱 (GUI).

터미널 명령을 몰라도 실행/설정할 수 있게 만든 화면이다. 실행.bat 을
더블클릭하면 이 파일이 실행된다.

- [실행]/[중지] 버튼으로 봇을 켜고 끌 수 있다.
- [설정] 버튼은 앱 잠금 비밀번호를 입력해야 열린다 (업비트 비밀번호와는
  다른, 이 프로그램 자체의 잠금 비밀번호 - 최초 클릭 시 새로 만든다).
  설정 창에서 API 키, 자동매매 예산, 종목 자동 선정 여부/개수, 매매·리포트
  주기 등을 전부 바꿀 수 있다.
- [백테스트] 버튼으로 실거래 전에 전략을 검증할 수 있다.
- [최신 리포트 열기] 버튼으로 reports/ 폴더의 가장 최근 리포트를 연다.

핵심 로직(설정 로딩/매매 루프/장부/전략)은 전부 기존 모듈(config.py,
runner.py, backtest.py 등)을 그대로 재사용한다 - 이 파일은 그 위에 얹은
화면일 뿐이다.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, scrolledtext

import app_auth
import config
import env_store
import report_tab
from db import Database
from runner import TraderApp, SettingsError

# exe(PyInstaller)로 빌드해도 기준 폴더가 임시폴더로 튀지 않도록 공용 모듈에서 가져온다.
from app_paths import BASE_DIR

BOOL_TRUE = ("1", "true", "yes", "y", "on")


def _is_true(s: str) -> bool:
    return s.strip().lower() in BOOL_TRUE


class QueueLogHandler(logging.Handler):
    """로그 레코드를 큐에 넣기만 한다. 실제 위젯 갱신은 Tk 메인루프(after)에서."""

    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


class SettingsDialog(tk.Toplevel):
    def __init__(self, master: "App"):
        super().__init__(master)
        self.master_app = master
        self.title("설정")
        self.geometry("660x760")
        self.resizable(False, True)
        self.grab_set()  # 모달

        current = env_store.read_env()
        self.vars: dict[str, tk.Variable] = {}
        self._choice_maps: dict[str, dict[str, str]] = {}

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=14, pady=10)

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def section(title: str):
            lbl = ttk.Label(scroll_frame, text=title, font=("", 10, "bold"))
            lbl.pack(anchor="w", pady=(12, 4))

        def text_field(key: str, label: str, default: str, secret: bool = False, width: int = 40):
            row = ttk.Frame(scroll_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=32).pack(side="left")
            var = tk.StringVar(value=current.get(key, default))
            entry = ttk.Entry(row, textvariable=var, width=width,
                               show="*" if secret else "")
            entry.pack(side="left", fill="x", expand=True)
            if secret:
                def toggle():
                    entry.config(show="" if entry.cget("show") == "*" else "*")
                ttk.Button(row, text="표시", width=5, command=toggle).pack(side="left", padx=4)
            self.vars[key] = var
            return var

        def choice_field(key: str, label: str, default: str,
                         options: list[tuple[str, str]]):
            """보기 중에서 고르는 항목. options 는 (저장값, 화면표시) 목록."""
            row = ttk.Frame(scroll_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=32).pack(side="left")
            cur = current.get(key, default)
            labels = [t for _, t in options]
            var = tk.StringVar(value=dict(options).get(cur, labels[0]))
            box = ttk.Combobox(row, textvariable=var, values=labels,
                               state="readonly", width=34)
            box.pack(side="left", fill="x", expand=True)
            # 화면 표시를 저장값으로 되돌리기 위한 역방향 표
            self._choice_maps[key] = {t: v for v, t in options}
            self.vars[key] = var
            return var

        def bool_field(key: str, label: str, default: str, warn: str = ""):
            var = tk.BooleanVar(value=_is_true(current.get(key, default)))
            cb = ttk.Checkbutton(scroll_frame, text=label, variable=var)
            cb.pack(anchor="w", pady=2)
            if warn:
                ttk.Label(scroll_frame, text=warn, foreground="#b00020",
                          wraplength=480, justify="left").pack(anchor="w", pady=(0, 6))
            self.vars[key] = var
            return var

        section("1. 업비트 API 키")
        ttk.Label(
            scroll_frame,
            text="upbit.com/mypage/open_api_management 에서 발급 (자산 출금 권한은 절대 켜지 마세요)",
            foreground="#555", wraplength=480, justify="left",
        ).pack(anchor="w")
        text_field("UPBIT_ACCESS_KEY", "Access Key", "")
        text_field("UPBIT_SECRET_KEY", "Secret Key", "", secret=True)

        section("2. 실거래 전환")
        bool_field(
            "LIVE_TRADING", "실제 주문 사용 (끄면 항상 모의매매)",
            "false",
            warn="⚠️ 체크하면 실제 돈으로 주문이 나갑니다. 충분히 모의매매로 검증한 뒤에 켜세요.",
        )

        section("3. 자동매매 예산 (수동 자산과 분리됨)")
        text_field("AUTO_TRADING_BUDGET_KRW", "배정 예산 (원)", "500000")
        text_field("MAX_POSITION_RATIO", "종목당 최대 비중 (0~1)", "0.3", width=10)
        text_field("DAILY_LOSS_LIMIT_RATIO", "일일 손실 한도 (0~1)", "0.05", width=10)

        section("4. 종목 선정")
        self.auto_select_var = bool_field(
            "AUTO_SELECT_MARKETS", "종목 자동 추천 사용 (끄면 아래 직접 지정 목록 사용)", "true"
        )
        text_field("TOP_N_MARKETS", "동시 감시 종목 개수", "5", width=10)
        text_field("MIN_24H_VOLUME_KRW", "최소 24시간 거래대금 필터 (원)", "3000000000")
        text_field("MIN_24H_CHANGE_RATE_FOR_ENTRY",
                   "얼마나 오른 종목을 살지 (하한)", "0", width=10)
        text_field("MAX_24H_CHANGE_RATE_FOR_ENTRY",
                   "너무 오른 건 제외 (상한)", "0.40", width=10)
        ttk.Label(
            scroll_frame, foreground="#555", wraplength=560, justify="left",
            text="24시간 등락률 기준입니다. 0.40 = 40%.\n"
                 "  · 하한 0 / 상한 0.40  → 상승장에서도 후보가 충분히 남음 (기본)\n"
                 "  · 하한 0.15 / 상한 0.4 → 15% 이상 크게 오른 종목만 노림\n"
                 "  ※ 상한을 너무 낮추면(예: 0.15) 상승장에서 대부분의 코인이\n"
                 "     이미 그만큼 올라 있어 후보가 오히려 줄어듭니다\n"
                 "     (\"다 오르는데 하나도 못 산다\" 증상의 원인).\n"
                 "  ※ 하한을 올리면 상한도 반드시 같이 올리세요. 하한이 상한보다\n"
                 "     크거나 같으면 살 종목이 하나도 남지 않습니다.",
        ).pack(anchor="w", pady=(2, 6))
        text_field("MARKET_RESCAN_INTERVAL_MIN", "전체 마켓 재스캔 주기 (분)", "60", width=10)
        text_field("TARGET_MARKETS", "직접 지정 종목 (자동추천 끌 때만 사용, 콤마 구분)",
                   "KRW-BTC,KRW-ETH")
        text_field("EXCLUDE_MARKETS", "자동 추천에서 제외할 마켓",
                   "KRW-USDT,KRW-USDC,KRW-DAI,KRW-TUSD,KRW-BUSD")

        bool_field("USE_PULLBACK_ENTRY",
                   "눌림목 매수 사용 (매수 기회를 늘림)", "true")
        text_field("PULLBACK_RSI_BELOW", "눌림목 기준 RSI (높일수록 적극적)", "55", width=10)
        ttk.Label(
            scroll_frame, foreground="#555", wraplength=560, justify="left",
            text="골든크로스는 추세가 바뀌는 순간에만 생겨서, 이미 오르는 종목은\n"
                 "살 기회가 없고 한 번 팔면 다시 들어가기 어렵습니다.\n"
                 "눌림목 매수는 '오름세인데 잠깐 눌렸다 반등할 때' 를 추가로 잡습니다.\n"
                 "  · 50 = 신중   · 55 = 권장   · 60 = 적극(거래 많고 낙폭도 큼)",
        ).pack(anchor="w", pady=(2, 6))

        bool_field("USE_STRONG_TREND_ENTRY",
                   "상승장 대응 매수 사용 (RSI 높아도 추세면 매수)", "true")
        text_field("STRONG_ENTRY_RSI_MAX", "이 RSI 넘으면 과열로 보고 매수 안 함", "85", width=10)
        ttk.Label(
            scroll_frame, foreground="#555", wraplength=560, justify="left",
            text="강하게 오르는 코인은 RSI 가 오래 60 이상에 머뭅니다. 골든크로스/\n"
                 "눌림목만 쓰면 이럴 때 매수 기회가 거의 사라집니다(\"다 오르는데\n"
                 "하나도 못 산다\"). 이 옵션은 RSI 가 높아도 상승 이동평균의\n"
                 "간격이 계속 벌어지는 중(추세가 강해지는 중)이면 매수합니다.\n"
                 "과열 방지선(기본 85) 위에서는 이 경로도 매수하지 않습니다.",
        ).pack(anchor="w", pady=(2, 6))

        section("5. 손절과 익절 (손실이 커지는 것을 막는 설정)")
        ttk.Label(
            scroll_frame, foreground="#555", wraplength=560, justify="left",
            text="전부 소수로 넣습니다. 0.05 = 5%.",
        ).pack(anchor="w", pady=(0, 4))
        text_field("STOP_LOSS_PCT", "손절선 (산 값보다 이만큼 빠지면 매도)", "0.05", width=10)
        text_field("TRAIL_START_PCT", "이익 지키기 시작 (이만큼 벌면 감시)", "0.05", width=10)
        text_field("TRAIL_DRAWDOWN_PCT", "고점 대비 이만큼 빠지면 매도", "0.03", width=10)
        text_field("TAKE_PROFIT_PCT", "목표 수익률 (0 = 제한 없음)", "0", width=10)
        ttk.Label(
            scroll_frame, foreground="#555", wraplength=560, justify="left",
            text="예) 1,000원에 샀을 때 (손절 0.05 / 시작 0.05 / 고점하락 0.03)\n"
                 "  · 950원까지 빠지면  → 손절하고 나옵니다 (더 큰 손실을 막음)\n"
                 "  · 1,200원까지 올랐다가 1,164원으로 밀리면 → 매도 (+16.4% 확보)\n"
                 "    오르는 동안에는 계속 들고 갑니다. 꺾일 때 파는 방식입니다.\n"
                 "  ※ 손절선을 0 으로 두면 손절을 아예 안 합니다. 매우 위험합니다.",
        ).pack(anchor="w", pady=(2, 6))

        section("6. 급등 패턴 학습 (오르기 전에 미리 사기)")
        ttk.Label(
            scroll_frame, foreground="#555", wraplength=560, justify="left",
            text="과거에 크게 올랐던 종목들이 '오르기 직전' 에 어떤 모습이었는지\n"
                 "학습해서, 지금 그 모습인 종목을 미리 삽니다.\n"
                 "이미 오른 종목을 따라 사는 것과는 다릅니다.",
        ).pack(anchor="w", pady=(0, 4))
        text_field("PATTERN_RISE_THRESHOLD_PCT", "몇 % 오르면 '급등'으로 볼지", "15", width=10)
        text_field("PATTERN_HORIZON_BARS", "몇 봉 안에 오르면 급등인지 (24=6시간)", "24", width=10)
        text_field("PATTERN_TARGET_RISING", "모을 급등 사례 개수", "1000", width=10)
        choice_field(
            "PATTERN_ENTRY_MODE", "학습 결과를 매매에 쓰는 방식", "signal",
            [("signal", "급등 전조를 찾으면 직접 매수 (권장)"),
             ("filter", "기존 전략의 매수를 검토만 함"),
             ("off", "사용 안 함")],
        )
        ttk.Label(
            scroll_frame, foreground="#555", wraplength=560, justify="left",
            text="· 기준을 높일수록(예: 20%) 사례가 드물어 모으는 데 오래 걸립니다.\n"
                 "· 매수 확률 기준은 프로그램이 '적중률이 가장 높아지는 값'으로\n"
                 "  알아서 정합니다. 직접 정할 필요 없습니다.\n"
                 "· 검증에서 성적이 기준에 못 미치면 매수하지 않고 기다립니다.\n"
                 "  (리포트 > 패턴 학습 탭에서 상태를 볼 수 있습니다)",
        ).pack(anchor="w", pady=(2, 6))

        section("7. 매매 판단 및 리포트 주기")
        text_field("TRADE_LOOP_INTERVAL_MIN", "매매 판단 주기 (분)", "5", width=10)
        text_field("REPORT_INTERVAL_HOURS", "리포트 생성 주기 (시간, 24=하루 한 번)", "24", width=10)
        text_field("REPORT_HOUR_KST", "매일 리포트 생성 시각 (0-23)", "9", width=10)

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=(0, 12), padx=14)
        ttk.Button(btns, text="취소", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="저장", command=self._save).pack(side="right")

    def _save(self) -> None:
        values: dict[str, str] = {}
        try:
            for key, var in self.vars.items():
                if isinstance(var, tk.BooleanVar):
                    values[key] = "true" if var.get() else "false"
                else:
                    raw = str(var.get()).strip()
                    # 선택형은 화면에 보이는 글자를 저장값으로 되돌린다
                    values[key] = self._choice_maps.get(key, {}).get(raw, raw)

            budget = float(values["AUTO_TRADING_BUDGET_KRW"])
            if budget <= 0:
                raise ValueError("자동매매 예산은 0보다 커야 합니다.")
            max_pos = float(values["MAX_POSITION_RATIO"])
            if not (0 < max_pos <= 1):
                raise ValueError("종목당 최대 비중은 0~1 사이여야 합니다.")
            loss_limit = float(values["DAILY_LOSS_LIMIT_RATIO"])
            if not (0 < loss_limit <= 1):
                raise ValueError("일일 손실 한도는 0~1 사이여야 합니다.")
            top_n = int(values["TOP_N_MARKETS"])
            if top_n <= 0:
                raise ValueError("동시 감시 종목 개수는 1 이상이어야 합니다.")
            float(values["MIN_24H_VOLUME_KRW"])
            change_cap = float(values["MAX_24H_CHANGE_RATE_FOR_ENTRY"])
            if not (0 < change_cap <= 1):
                raise ValueError("추격매수 방지 상한은 0~1 사이여야 합니다 (예: 0.15).")
            rescan = int(values["MARKET_RESCAN_INTERVAL_MIN"])
            if rescan <= 0:
                raise ValueError("재스캔 주기는 1분 이상이어야 합니다.")
            trade_interval = int(values["TRADE_LOOP_INTERVAL_MIN"])
            if trade_interval <= 0:
                raise ValueError("매매 판단 주기는 1분 이상이어야 합니다.")
            report_hours = int(values["REPORT_INTERVAL_HOURS"])
            if report_hours <= 0:
                raise ValueError("리포트 생성 주기는 1시간 이상이어야 합니다.")
            report_hour = int(values["REPORT_HOUR_KST"])
            if not (0 <= report_hour <= 23):
                raise ValueError("리포트 생성 시각은 0~23 사이여야 합니다.")
            if not _is_true(values["AUTO_SELECT_MARKETS"]) and not values["TARGET_MARKETS"].strip():
                raise ValueError("종목 자동 추천을 끄셨다면 직접 지정 종목을 최소 1개 입력하세요.")

            rise_th = float(values["PATTERN_RISE_THRESHOLD_PCT"])
            if not (0 < rise_th <= 100):
                raise ValueError("급등 기준은 0보다 크고 100 이하여야 합니다 (15 = 15%).")
            horizon = int(values["PATTERN_HORIZON_BARS"])
            if horizon <= 0:
                raise ValueError("관찰 기간은 1봉 이상이어야 합니다.")
            if int(values["PATTERN_TARGET_RISING"]) <= 0:
                raise ValueError("모을 급등 사례 개수는 1 이상이어야 합니다.")

            pb = float(values["PULLBACK_RSI_BELOW"])
            if not (0 < pb < 100):
                raise ValueError("눌림목 기준 RSI 는 0과 100 사이여야 합니다 (55 권장).")
            strong_max = float(values["STRONG_ENTRY_RSI_MAX"])
            if not (0 < strong_max <= 100):
                raise ValueError("과열 방지 RSI 는 0보다 크고 100 이하여야 합니다 (85 권장).")
            stop_loss = float(values["STOP_LOSS_PCT"])
            if not (0 <= stop_loss < 1):
                raise ValueError("손절선은 0 이상 1 미만이어야 합니다 (0.05 = 5%).")
            if stop_loss == 0:
                raise ValueError(
                    "손절선이 0 입니다. 손절을 안 하면 한 종목의 손실이 끝없이 커질 수 "
                    "있습니다. 0.05(-5%) 정도를 권장합니다."
                )
            trail_start = float(values["TRAIL_START_PCT"])
            trail_dd = float(values["TRAIL_DRAWDOWN_PCT"])
            if not (0 <= trail_start < 1) or not (0 <= trail_dd < 1):
                raise ValueError("이익 지키기 값은 0 이상 1 미만이어야 합니다 (0.03 = 3%).")
            take_profit = float(values["TAKE_PROFIT_PCT"])
            if not (0 <= take_profit < 10):
                raise ValueError("목표 수익률이 이상합니다. 0(제한 없음) 또는 0.3(30%) 같은 값을 넣으세요.")

            min_chg = float(values["MIN_24H_CHANGE_RATE_FOR_ENTRY"])
            if min_chg < 0:
                raise ValueError("등락률 하한은 0 이상이어야 합니다.")
            if min_chg >= change_cap:
                raise ValueError(
                    f"등락률 하한({min_chg})이 상한({change_cap})보다 크거나 같습니다.\n"
                    "이러면 살 수 있는 종목이 하나도 남지 않습니다.\n"
                    "예: 15% 이상 오른 종목을 노리려면 하한 0.15 / 상한 0.4 로 넣으세요."
                )
        except (ValueError, KeyError) as e:
            messagebox.showerror("입력 오류", str(e))
            return

        if _is_true(values.get("LIVE_TRADING", "false")):
            ok = messagebox.askyesno(
                "실거래 확인",
                "실제 주문(LIVE_TRADING)을 켜시려는 게 맞나요?\n\n"
                "이 설정을 켜면 실제 돈으로 매수/매도가 실행됩니다.\n"
                "충분히 모의매매로 검증한 뒤에 켜는 것을 권장합니다.\n\n"
                "정말 실거래를 켜시겠습니까?",
                icon="warning",
            )
            if not ok:
                values["LIVE_TRADING"] = "false"
                self.auto_select_var  # no-op, keep lints quiet

        env_store.write_env(values)
        config.reload_env()
        messagebox.showinfo(
            "저장 완료",
            "설정이 저장되었습니다.\n봇이 이미 실행 중이라면, 새 설정을 적용하려면 "
            "중지 후 다시 시작해주세요.",
        )
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("코인 자동매매 봇")
        self.geometry("1040x720")
        self.minsize(900, 600)

        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.trader: TraderApp | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()

        # 리포트 화면이 봇과 무관하게 기록을 읽을 수 있도록 설정/DB 를 미리 잡아둔다
        self._settings_snapshot = config.load_settings()
        self._report_db = None

        self._account_busy = False
        self._account_ok = False

        self._build_widgets()
        self._attach_log_handler()
        self.after(300, self._drain_log_queue)
        self.after(1200, self._tick_account)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI 구성 ----------

    def _build_widgets(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        self.start_btn = ttk.Button(top, text="▶ 실행", command=self._on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(top, text="■ 중지", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="중지됨")
        ttk.Label(top, textvariable=self.status_var, font=("", 10, "bold")).pack(
            side="left", padx=16
        )

        ttk.Button(top, text="설정", command=self._on_open_settings).pack(side="right")
        ttk.Button(top, text="백테스트", command=self._on_backtest).pack(side="right", padx=6)
        ttk.Button(top, text="최신 리포트 열기", command=self._on_open_report).pack(
            side="right", padx=6
        )

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        log_tab = ttk.Frame(notebook)
        notebook.add(log_tab, text="실행 로그")
        self.log_text = scrolledtext.ScrolledText(log_tab, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

        status_tab = ttk.Frame(notebook, padding=14)
        notebook.add(status_tab, text="자산 현황")

        self.snapshot_vars = {
            "budget": tk.StringVar(value="-"),
            "total": tk.StringVar(value="-"),
            "pnl": tk.StringVar(value="-"),
            "cash": tk.StringVar(value="-"),
            "watchlist": tk.StringVar(value="-"),
            "holdings": tk.StringVar(value="-"),
            "uptime": tk.StringVar(value="-"),
            "updated": tk.StringVar(value="-"),
            "account_total": tk.StringVar(value="-"),
            "manual": tk.StringVar(value="-"),
            "manual_detail": tk.StringVar(value="-"),
        }
        # 위: 업비트 계정 전체 (수동 보유분 포함)  /  아래: 자동매매 풀
        rows = [
            ("__head__", "내 계좌 전체 (업비트)"),
            ("내 전체 자산", "account_total"),
            ("수동 투자 자산", "manual"),
            ("  └ 내역", "manual_detail"),
            ("__head__", "자동매매 (봇이 관리하는 몫)"),
            ("배정 예산", "budget"),
            ("현재 평가액", "total"),
            ("누적 손익", "pnl"),
            ("남은 현금", "cash"),
            ("감시 종목", "watchlist"),
            ("보유 포지션", "holdings"),
            ("구동 시간", "uptime"),
            ("마지막 갱신", "updated"),
        ]
        for i, (label, key) in enumerate(rows):
            if label == "__head__":
                ttk.Separator(status_tab, orient="horizontal").grid(
                    row=i, column=0, columnspan=2, sticky="ew", pady=(12, 2))
                ttk.Label(status_tab, text=key, font=("", 9, "bold"),
                          foreground="#555").grid(row=i, column=0, columnspan=2,
                                                  sticky="w", pady=(16, 2))
                continue
            ttk.Label(status_tab, text=label, width=14).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Label(status_tab, textvariable=self.snapshot_vars[key], wraplength=560,
                      justify="left").grid(row=i, column=1, sticky="w", pady=4)

        # '자산 현황' 오른쪽에 리포트 탭
        self.report_tab = report_tab.ReportTab(
            notebook, get_db=self._get_report_db,
            get_trader=lambda: self.trader,
            reports_dir=self._settings_snapshot.reports_dir,
            model_path=self._settings_snapshot.db_path.parent / "pattern_model.json",
        )
        notebook.add(self.report_tab, text="리포트")

    def _get_report_db(self):
        """리포트 화면이 읽을 DB. 봇이 꺼져 있어도 기록을 볼 수 있어야 하므로
        실행 스레드와 별도로 하나 더 연다 (SQLite 는 매 작업마다 연결을
        열고 닫으므로 동시에 써도 안전하다)."""
        if self._report_db is None:
            try:
                self._report_db = Database(self._settings_snapshot.db_path)
            except Exception:
                logging.getLogger("app").exception("리포트용 DB 를 열지 못했습니다.")
                return None
        return self._report_db

    # ---------- 계좌 조회 (봇이 꺼져 있어도 자산을 보여주기 위해) ----------

    ACCOUNT_REFRESH_MS = 60_000

    def _tick_account(self) -> None:
        """1분마다 업비트 계정을 조회해 '내 전체 자산 / 수동 투자 자산' 을 갱신한다.

        봇이 돌고 있을 때는 매매 루프가 이미 계좌 정보를 스냅샷에 담아 주므로
        중복 조회하지 않는다. 네트워크 호출이라 반드시 별도 스레드에서 한다
        (메인 스레드에서 하면 화면이 그 시간만큼 멈춘다)."""
        self._tick_account_once()
        self.after(self.ACCOUNT_REFRESH_MS, self._tick_account)

    def _tick_account_once(self) -> None:
        """조회가 필요하면 한 번 돌린다.

        봇이 돌고 있으면 매매 루프가 계좌 정보를 스냅샷에 담아 주므로 보통은
        건너뛴다. 다만 아직 한 번도 성공하지 못했다면(예: 키를 넣기 전에 봇을
        먼저 켠 경우) 봇이 돌고 있어도 직접 조회해서 화면을 채운다."""
        if self._account_busy:
            return
        if self.trader is not None and self._account_ok:
            return
        self._account_busy = True
        threading.Thread(target=self._fetch_account, daemon=True).start()

    def _fetch_account(self) -> None:
        acc = {"available": False, "reason": ""}
        try:
            from exchange import ExchangeClient
            st = self._settings_snapshot
            client = ExchangeClient(st.upbit_access_key, st.upbit_secret_key,
                                    live_trading=False)
            summary = client.get_account_summary()
            acc["reason"] = summary.get("reason", "")
            if summary.get("available"):
                total = float(summary["total"])
                acc = {
                    "available": True, "account_total_krw": total,
                    "manual_krw": total, "auto_krw": 0.0,
                    "grand_total_krw": total, "krw_cash": float(summary["krw"]),
                    "coin_value": float(summary["coin_value"]),
                    "top_items": summary["items"][:5], "live": False,
                    "idle": True, "reason": "",
                }
        except Exception:
            logging.getLogger("app").debug("계좌 조회 실패 (무시)", exc_info=True)
        finally:
            self._account_busy = False
        self._account_ok = False
        self.after(0, lambda: self._render_account(acc))

    def _attach_log_handler(self) -> None:
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

    def _drain_log_queue(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", line + "\n")
                # 너무 길어지지 않게 최근 2000줄만 유지
                if float(self.log_text.index("end-1c").split(".")[0]) > 2000:
                    self.log_text.delete("1.0", "500.0")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.after(300, self._drain_log_queue)

    # ---------- 실행/중지 ----------

    def _on_start(self) -> None:
        config.reload_env()
        settings = config.load_settings()

        if settings.live_trading:
            ok = messagebox.askyesno(
                "실거래 확인",
                "지금 실거래(LIVE_TRADING) 설정으로 시작합니다.\n"
                "실제 돈으로 매수/매도가 실행됩니다. 계속할까요?",
                icon="warning",
            )
            if not ok:
                return

        try:
            self.trader = TraderApp(settings, on_tick=self._on_tick)
        except SettingsError as e:
            messagebox.showerror("설정 오류", str(e))
            return

        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run_thread, args=(self.trader, self.stop_event), daemon=True
        )
        self.thread.start()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        mode = "실거래" if settings.live_trading else "모의매매"
        self.status_var.set(f"실행 중 ({mode})")

    def _run_thread(self, trader: TraderApp, stop_event: threading.Event) -> None:
        try:
            # 상승 패턴 표본이 목표치보다 적으면 과거 데이터로 채우고 학습한다.
            # 네트워크를 쓰므로 반드시 이 작업 스레드에서 실행한다 (화면이 멈추지 않게).
            log = logging.getLogger("app")
            log.info("상승 패턴 학습 준비를 시작합니다...")
            trader.prepare_patterns(progress=lambda m: log.info("%s", m))
            if stop_event.is_set():
                return
            trader.run_forever(stop_event=stop_event)
        except Exception:
            logging.getLogger("runner").exception("실행 스레드에서 처리되지 않은 오류로 중단됨")
        finally:
            self.after(0, self._on_thread_finished)

    def _on_thread_finished(self) -> None:
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("중지됨")

    def _on_stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        self.status_var.set("중지 중...")
        self.stop_btn.config(state="disabled")

    def _on_tick(self, snapshot: dict) -> None:
        self.after(0, lambda: self._update_snapshot(snapshot))

    def _update_snapshot(self, snap: dict) -> None:
        v = self.snapshot_vars
        v["budget"].set(f"{snap['budget_krw']:,.0f}원")
        v["total"].set(f"{snap['total_value_krw']:,.0f}원")
        v["pnl"].set(f"{snap['pnl_krw']:,.0f}원 ({snap['pnl_pct']:+.2f}%)")
        v["cash"].set(f"{snap['cash_krw']:,.0f}원")
        v["watchlist"].set(", ".join(snap.get("watchlist", [])) or "-")
        holdings = snap.get("holdings", {})
        v["holdings"].set(
            ", ".join(f"{m} {vol:.6f}" for m, vol in holdings.items()) or "(없음)"
        )
        v["uptime"].set(snap.get("uptime_text") or "-")

        self._render_account(snap.get("account") or {})
        v["updated"].set(snap["ts"].strftime("%Y-%m-%d %H:%M:%S"))

    def _render_account(self, acc: dict) -> None:
        v = self.snapshot_vars
        self._account_ok = bool(acc.get("available"))
        if acc.get("available"):
            if acc.get("idle"):
                v["account_total"].set(f"{acc['grand_total_krw']:,.0f}원   (봇 정지 중)")
            elif acc.get("live"):
                v["account_total"].set(f"{acc['grand_total_krw']:,.0f}원")
            else:
                v["account_total"].set(
                    f"{acc['grand_total_krw']:,.0f}원"
                    f"   (업비트 {acc['account_total_krw']:,.0f}원 "
                    f"+ 모의 자동매매 {acc['auto_krw']:,.0f}원)")
            v["manual"].set(f"{acc['manual_krw']:,.0f}원")
            items = acc.get("top_items") or []
            detail = f"현금 {acc['krw_cash']:,.0f}원"
            if items:
                detail += " / " + ", ".join(
                    f"{it['currency']} {it['value_krw']:,.0f}원" for it in items)
            v["manual_detail"].set(detail)
        else:
            reason = (acc.get("reason") or "").strip()
            v["manual"].set("-")
            if not reason or "입력되지 않" in reason:
                v["account_total"].set("(업비트 API 키를 넣으면 표시됩니다)")
                v["manual_detail"].set(
                    "설정 화면에서 Access Key / Secret Key 를 입력하세요. "
                    "조회 전용으로만 쓰며, 모의매매 모드에서는 주문이 나가지 않습니다.")
            else:
                v["account_total"].set("조회 실패")
                v["manual_detail"].set(reason)

    # ---------- 설정 ----------

    def _on_open_settings(self) -> None:
        if not app_auth.is_password_set():
            messagebox.showinfo(
                "앱 잠금 비밀번호 설정",
                "설정 화면(API 키가 보이는 곳)을 보호할 비밀번호를 처음 설정합니다.\n"
                "업비트 비밀번호와는 별개로, 이 프로그램 자체의 잠금 비밀번호입니다.",
            )
            while True:
                pw1 = simpledialog.askstring("비밀번호 설정", "새 비밀번호", show="*", parent=self)
                if pw1 is None:
                    return
                if len(pw1) < 4:
                    messagebox.showerror("오류", "비밀번호는 4자 이상으로 설정하세요.")
                    continue
                pw2 = simpledialog.askstring("비밀번호 확인", "비밀번호 다시 입력", show="*", parent=self)
                if pw1 != pw2:
                    messagebox.showerror("오류", "비밀번호가 일치하지 않습니다. 다시 시도하세요.")
                    continue
                app_auth.set_password(pw1)
                break
        else:
            pw = simpledialog.askstring("설정 잠금 해제", "비밀번호를 입력하세요", show="*", parent=self)
            if pw is None:
                return
            if not app_auth.verify_password(pw):
                messagebox.showerror("오류", "비밀번호가 올바르지 않습니다.")
                return

        SettingsDialog(self)
        # 설정 창에서 API 키 등을 바꿨을 수 있으므로 즉시 다시 읽는다.
        # (이걸 빠뜨려서, 키를 새로 넣어도 프로그램을 껐다 켜기 전까지
        #  계좌가 계속 "API 키를 넣으세요" 로 표시되는 문제가 있었다.)
        try:
            config.reload_env()
            self._settings_snapshot = config.load_settings()
        except Exception:
            logging.getLogger("app").exception("설정을 다시 읽지 못했습니다.")
        self._account_ok = False
        self.after(200, self._tick_account_once)

    # ---------- 백테스트 ----------

    def _on_backtest(self) -> None:
        market = simpledialog.askstring(
            "백테스트", "백테스트할 마켓 코드 (예: KRW-BTC)", initialvalue="KRW-BTC", parent=self
        )
        if not market:
            return
        settings = config.load_settings()
        self._log_direct(f"[백테스트] {market} 데이터 조회 중... (몇 초 걸릴 수 있습니다)")
        threading.Thread(
            target=self._run_backtest_thread, args=(market, settings.auto_trading_budget_krw),
            daemon=True,
        ).start()

    def _run_backtest_thread(self, market: str, budget: float) -> None:
        try:
            from exchange import ExchangeClient
            from backtest import run_backtest
            from strategy import DEFAULT_STRATEGY

            client = ExchangeClient("", "", live_trading=False)
            df = client.get_ohlcv(market, interval="minute15", count=2000)
            if df is None or len(df) < 50:
                self._log_direct(f"[백테스트] {market} 데이터를 충분히 가져오지 못했습니다.")
                return
            result = run_backtest(df, DEFAULT_STRATEGY(), budget_krw=budget)
            msg = (
                f"[백테스트] {market} 결과 - 손익 {result['pnl_krw']:,.0f}원 "
                f"({result['pnl_pct']:+.2f}%), 매매 {result['num_trades']}건, "
                f"승률 {result['win_rate_pct']:.1f}%, "
                f"단순보유 대비 {result['buy_and_hold_pct']:+.2f}%"
            )
            self._log_direct(msg)
            self.after(0, lambda: messagebox.showinfo("백테스트 결과", msg))
        except Exception as e:
            self._log_direct(f"[백테스트] 오류: {e}")

    def _log_direct(self, text: str) -> None:
        self.log_queue.put_nowait(f"{datetime.now().strftime('%H:%M:%S')} {text}")

    # ---------- 리포트 ----------

    def _on_open_report(self) -> None:
        reports_dir = BASE_DIR / "reports"
        files = sorted(reports_dir.glob("report_*.md"), key=lambda p: p.stat().st_mtime)
        if not files:
            messagebox.showinfo("리포트 없음", "아직 생성된 리포트가 없습니다. 봇을 먼저 실행해보세요.")
            return
        latest = files[-1]
        try:
            if sys.platform.startswith("win"):
                os.startfile(latest)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(latest)], check=False)
            else:
                subprocess.run(["xdg-open", str(latest)], check=False)
        except Exception:
            messagebox.showinfo("리포트 위치", f"다음 경로에서 직접 열어주세요:\n{latest}")

    # ---------- 종료 ----------

    def _on_close(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            ok = messagebox.askyesno("종료 확인", "봇이 실행 중입니다. 중지하고 종료할까요?")
            if not ok:
                return
            if self.stop_event is not None:
                self.stop_event.set()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
