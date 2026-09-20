"""
tkinter Canvas 로 직접 그리는 간단한 차트 위젯.

matplotlib 을 쓰지 않는 이유: 단일 exe 에 matplotlib 을 넣으면 용량이 수백 MB
늘어나고 빌드도 느려진다. 자산 추이선과 손익 막대 정도는 Canvas 로 충분히
그릴 수 있어서 표준 라이브러리만으로 해결했다.

공통 동작
- 창 크기가 바뀌면 자동으로 다시 그린다.
- 데이터가 없으면 안내 문구를 가운데 표시한다.
- 값 축(왼쪽)에 눈금과 숫자를 넣고, 가로축에는 라벨을 겹치지 않을 만큼만 찍는다.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# 색상
COLOR_BG = "#ffffff"
COLOR_GRID = "#e6e6e6"
COLOR_AXIS = "#9a9a9a"
COLOR_TEXT = "#333333"
COLOR_MUTED = "#8a8a8a"
COLOR_LINE = "#1f6feb"
COLOR_FILL = "#dbe9fb"
COLOR_UP = "#d93025"      # 한국 관습: 상승/이익 = 빨강
COLOR_DOWN = "#1a73e8"    # 하락/손실 = 파랑
COLOR_BASE = "#b0b0b0"

PAD_LEFT = 74
PAD_RIGHT = 16
PAD_TOP = 16
PAD_BOTTOM = 34


def _nice_num(value: float, round_it: bool) -> float:
    """축 눈금 간격을 1/2/5 × 10^n 형태의 '보기 좋은 수' 로 맞춘다."""
    import math
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    frac = value / (10 ** exp)
    if round_it:
        nf = 1.0 if frac < 1.5 else 2.0 if frac < 3.0 else 5.0 if frac < 7.0 else 10.0
    else:
        nf = 1.0 if frac <= 1.0 else 2.0 if frac <= 2.0 else 5.0 if frac <= 5.0 else 10.0
    return nf * (10 ** exp)


def _fmt_krw(v: float) -> str:
    """축 숫자를 짧게. 1,234,567 -> 123만"""
    a = abs(v)
    if a >= 100_000_000:
        return f"{v/100_000_000:,.1f}억"
    if a >= 10_000:
        return f"{v/10_000:,.0f}만"
    return f"{v:,.0f}"


class _BaseChart(tk.Canvas):
    def __init__(self, parent, height: int = 220, empty_text: str = "표시할 데이터가 없습니다.",
                 **kw):
        super().__init__(parent, height=height, bg=COLOR_BG,
                         highlightthickness=1, highlightbackground="#d5d5d5", **kw)
        self.empty_text = empty_text
        self._data: list = []
        self.bind("<Configure>", lambda e: self.redraw())

    def set_data(self, data: list) -> None:
        self._data = list(data or [])
        self.redraw()

    # --- 공통 그리기 도구 ---

    def _plot_area(self):
        w = max(int(self.winfo_width()), 1)
        h = max(int(self.winfo_height()), 1)
        return PAD_LEFT, PAD_TOP, w - PAD_RIGHT, h - PAD_BOTTOM

    def _draw_empty(self) -> None:
        w = max(int(self.winfo_width()), 1)
        h = max(int(self.winfo_height()), 1)
        self.create_text(w / 2, h / 2, text=self.empty_text, fill=COLOR_MUTED,
                         font=("", 10))

    def _draw_y_axis(self, lo: float, hi: float, fmt=_fmt_krw) -> tuple[float, float]:
        """가로 눈금선과 왼쪽 숫자를 그리고, 실제 사용할 (lo, hi) 를 돌려준다."""
        x0, y0, x1, y1 = self._plot_area()
        if hi - lo < 1e-9:
            hi = lo + 1.0
        span = _nice_num(hi - lo, False)
        step = _nice_num(span / 4, True)
        lo = step * (lo // step)
        hi = step * ((hi // step) + 1)

        v = lo
        while v <= hi + step * 0.5:
            ratio = (v - lo) / (hi - lo) if hi > lo else 0
            y = y1 - ratio * (y1 - y0)
            self.create_line(x0, y, x1, y, fill=COLOR_GRID)
            self.create_text(x0 - 6, y, text=fmt(v), anchor="e",
                             fill=COLOR_MUTED, font=("", 8))
            v += step
        self.create_line(x0, y0, x0, y1, fill=COLOR_AXIS)
        self.create_line(x0, y1, x1, y1, fill=COLOR_AXIS)
        return lo, hi

    def _draw_x_labels(self, labels: list[str], x_of) -> None:
        """가로축 라벨. 폭이 부족하면 균등하게 솎아낸다."""
        x0, y0, x1, y1 = self._plot_area()
        n = len(labels)
        if n == 0:
            return
        max_labels = max(int((x1 - x0) / 74), 2)
        stride = max(1, -(-n // max_labels))
        for i in range(0, n, stride):
            self.create_text(x_of(i), y1 + 12, text=labels[i], anchor="n",
                             fill=COLOR_MUTED, font=("", 8))


class LineChart(_BaseChart):
    """시간에 따른 값 변화. data = [(라벨, 값), ...]"""

    def __init__(self, parent, baseline: float | None = None, **kw):
        super().__init__(parent, **kw)
        self.baseline = baseline

    def set_baseline(self, value: float | None) -> None:
        self.baseline = value
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        data = self._data
        if len(data) < 1:
            self._draw_empty()
            return

        values = [float(v) for _, v in data]
        lo, hi = min(values), max(values)
        if self.baseline is not None:
            lo, hi = min(lo, self.baseline), max(hi, self.baseline)
        margin = (hi - lo) * 0.12 or max(abs(hi) * 0.02, 1.0)
        lo, hi = self._draw_y_axis(lo - margin, hi + margin)

        x0, y0, x1, y1 = self._plot_area()
        n = len(values)

        def x_of(i: int) -> float:
            return x0 if n == 1 else x0 + (x1 - x0) * i / (n - 1)

        def y_of(v: float) -> float:
            return y1 - (v - lo) / (hi - lo) * (y1 - y0)

        pts = [(x_of(i), y_of(v)) for i, v in enumerate(values)]
        if len(pts) >= 2:
            # 선 아래를 옅게 채워 추이를 눈에 띄게
            poly = [x0, y1] + [c for p in pts for c in p] + [pts[-1][0], y1]
            self.create_polygon(poly, fill=COLOR_FILL, outline="")
            self.create_line([c for p in pts for c in p], fill=COLOR_LINE,
                             width=2, smooth=False)
        else:
            self.create_oval(pts[0][0] - 3, pts[0][1] - 3, pts[0][0] + 3,
                             pts[0][1] + 3, fill=COLOR_LINE, outline="")

        # 기준선 (예: 배정 예산). 이 위면 이익, 아래면 손실.
        # 채움색에 가리지 않도록 선을 그린 뒤에 올린다.
        if self.baseline is not None:
            by = y_of(self.baseline)
            self.create_line(x0, by, x1, by, fill=COLOR_BASE, dash=(4, 3))
            self.create_text(x0 + 4, by - 8, text="기준(예산)", anchor="w",
                             fill=COLOR_MUTED, font=("", 8))

        # 마지막 값 강조. 글자가 점·선과 겹치지 않도록 점 왼쪽에 둔다.
        lx, ly = pts[-1]
        self.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill=COLOR_LINE, outline="white")
        self.create_text(lx - 8, max(ly - 12, y0 + 6),
                         text=_fmt_krw(values[-1]) + "원", anchor="e",
                         fill=COLOR_TEXT, font=("", 9, "bold"))

        self._draw_x_labels([str(lbl) for lbl, _ in data], x_of)


class BarChart(_BaseChart):
    """양수/음수 막대. data = [(라벨, 값), ...]  이익은 빨강, 손실은 파랑."""

    def redraw(self) -> None:
        self.delete("all")
        data = self._data
        if not data:
            self._draw_empty()
            return

        values = [float(v) for _, v in data]
        lo = min(min(values), 0.0)
        hi = max(max(values), 0.0)
        margin = (hi - lo) * 0.15 or 1.0
        lo, hi = self._draw_y_axis(lo - margin, hi + margin)

        x0, y0, x1, y1 = self._plot_area()
        n = len(values)
        slot = (x1 - x0) / n
        bar_w = max(min(slot * 0.62, 46), 3)

        def y_of(v: float) -> float:
            return y1 - (v - lo) / (hi - lo) * (y1 - y0)

        zero_y = y_of(0.0)
        self.create_line(x0, zero_y, x1, zero_y, fill=COLOR_AXIS)

        def x_of(i: int) -> float:
            return x0 + slot * (i + 0.5)

        for i, v in enumerate(values):
            cx = x_of(i)
            top, bottom = (y_of(v), zero_y) if v >= 0 else (zero_y, y_of(v))
            if abs(top - bottom) < 1:      # 0 에 가까워도 보이게
                top = bottom - 1
            self.create_rectangle(cx - bar_w / 2, top, cx + bar_w / 2, bottom,
                                  fill=COLOR_UP if v >= 0 else COLOR_DOWN, outline="")
            if slot > 34:
                self.create_text(cx, top - 7 if v >= 0 else bottom + 7,
                                 text=_fmt_krw(v), anchor="s" if v >= 0 else "n",
                                 fill=COLOR_TEXT, font=("", 8))

        self._draw_x_labels([str(lbl) for lbl, _ in data], x_of)


class ChartPanel(ttk.Frame):
    """제목 + 차트를 묶은 카드."""

    def __init__(self, parent, title: str, chart: _BaseChart | None = None,
                 kind: str = "line", height: int = 200, baseline=None,
                 empty_text: str = "표시할 데이터가 없습니다."):
        super().__init__(parent)
        self.title_var = tk.StringVar(value=title)
        ttk.Label(self, textvariable=self.title_var,
                  font=("", 10, "bold")).pack(anchor="w", pady=(0, 4))
        if chart is None:
            if kind == "bar":
                chart = BarChart(self, height=height, empty_text=empty_text)
            else:
                chart = LineChart(self, height=height, baseline=baseline,
                                  empty_text=empty_text)
        self.chart = chart
        self.chart.pack(fill="both", expand=True)

    def set_title(self, text: str) -> None:
        self.title_var.set(text)

    def set_data(self, data: list) -> None:
        self.chart.set_data(data)
