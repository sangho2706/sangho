"""
데이터 적재 계층 (SQLite).

- price_ticks: 주기적으로 수집한 시세 (딥러닝 학습용 원자재)
- trades: 봇이 실제/모의로 실행한 매매 기록
- ledger_snapshots: 매 루프마다 자동매매 풀 잔고 스냅샷
- daily_reports: 생성된 리포트 메타데이터
- run_sessions: 봇을 켜고 끈 구간 (구동 시간/구간 손익 집계용)
- rise_events: 구동 중에 감시 종목이 상승한 기록
- patterns: 상승 패턴 학습 표본 (특징 + 이후 수익률 + 라벨)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market TEXT NOT NULL,
    price REAL NOT NULL,
    volume_24h REAL,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_ticks_market_ts ON price_ticks(market, ts);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,               -- 'buy' | 'sell'
    price REAL NOT NULL,
    volume REAL NOT NULL,
    krw_amount REAL NOT NULL,
    fee_krw REAL NOT NULL DEFAULT 0,
    reason TEXT,                      -- 전략이 남긴 매매 사유
    mode TEXT NOT NULL,               -- 'paper' | 'live'
    strategy TEXT,
    realized_pnl_krw REAL             -- 매도 시에만 채워짐
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS ledger_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cash_krw REAL NOT NULL,
    holdings_value_krw REAL NOT NULL,
    total_value_krw REAL NOT NULL,
    budget_krw REAL NOT NULL,
    pnl_krw REAL NOT NULL,
    pnl_pct REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_snapshots_ts ON ledger_snapshots(ts);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    file_path TEXT NOT NULL,
    total_value_krw REAL,
    pnl_krw REAL,
    pnl_pct REAL,
    num_trades INTEGER
);

-- 봇을 켜고 끈 구간. "얼마나 오래 돌았고 그동안 얼마 벌었나" 를 계산한다.
CREATE TABLE IF NOT EXISTS run_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,                    -- NULL 이면 아직 구동 중 (또는 비정상 종료)
    mode TEXT NOT NULL,               -- 'paper' | 'live'
    budget_krw REAL NOT NULL,
    start_value_krw REAL,
    end_value_krw REAL,
    realized_pnl_krw REAL DEFAULT 0,  -- 이 구간에 확정된 매매 손익
    num_trades INTEGER DEFAULT 0,
    stop_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_sessions_started ON run_sessions(started_at);

-- 구동 중 감시 종목이 상승한 기록. "돌아가는 동안 뭐가 올랐나" 를 남긴다.
CREATE TABLE IF NOT EXISTS rise_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    market TEXT NOT NULL,
    price_from REAL NOT NULL,
    price_to REAL NOT NULL,
    change_pct REAL NOT NULL,
    window_min INTEGER NOT NULL,
    held INTEGER NOT NULL DEFAULT 0   -- 1 = 그때 봇이 보유 중이었음 (먹은 상승)
);
CREATE INDEX IF NOT EXISTS idx_rise_events_ts ON rise_events(ts);

-- 학습 규칙(급등 기준/관찰 기간/특징 목록)을 기억해 둔다.
-- 규칙이 바뀌면 예전 표본은 라벨 자체가 달라져 그대로 쓰면 안 되므로,
-- 이 값을 비교해 달라졌으면 표본을 비우고 다시 모은다.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 상승 패턴 학습 표본.
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                 -- 패턴이 관측된 캔들 시각
    market TEXT NOT NULL,
    interval TEXT NOT NULL,
    features_json TEXT NOT NULL,
    future_return_pct REAL NOT NULL,  -- 이 패턴 이후 실제 수익률
    label INTEGER NOT NULL,           -- 1 = 상승 패턴, 0 = 아님
    source TEXT NOT NULL              -- 'backfill'(과거 데이터) | 'live'(구동 중 수집)
);
-- 같은 종목/주기/시각을 두 번 학습하지 않도록 막는다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_unique
    ON patterns(market, interval, ts);
CREATE INDEX IF NOT EXISTS idx_patterns_label ON patterns(label);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- writes ----------

    def log_price(self, market: str, price: float, volume_24h: Optional[float] = None,
                  extra_json: Optional[str] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO price_ticks (ts, market, price, volume_24h, extra_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), market, price, volume_24h, extra_json),
            )

    def log_trade(self, market: str, side: str, price: float, volume: float,
                  krw_amount: float, fee_krw: float, reason: str, mode: str,
                  strategy: str, realized_pnl_krw: Optional[float] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO trades (ts, market, side, price, volume, krw_amount, "
                "fee_krw, reason, mode, strategy, realized_pnl_krw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_now_iso(), market, side, price, volume, krw_amount, fee_krw,
                 reason, mode, strategy, realized_pnl_krw),
            )

    def log_ledger_snapshot(self, cash_krw: float, holdings_value_krw: float,
                             budget_krw: float) -> None:
        total = cash_krw + holdings_value_krw
        pnl = total - budget_krw
        pnl_pct = (pnl / budget_krw * 100) if budget_krw else 0.0
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ledger_snapshots (ts, cash_krw, holdings_value_krw, "
                "total_value_krw, budget_krw, pnl_krw, pnl_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now_iso(), cash_krw, holdings_value_krw, total, budget_krw, pnl, pnl_pct),
            )

    def log_report(self, period_start: str, period_end: str, file_path: str,
                    total_value_krw: float, pnl_krw: float, pnl_pct: float,
                    num_trades: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO daily_reports (ts, period_start, period_end, file_path, "
                "total_value_krw, pnl_krw, pnl_pct, num_trades) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_now_iso(), period_start, period_end, file_path, total_value_krw,
                 pnl_krw, pnl_pct, num_trades),
            )

    # ---------- reads ----------

    def trades_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM trades WHERE ts >= ? ORDER BY ts ASC", (since_iso,)
            )
            return cur.fetchall()

    def latest_ledger_snapshot(self) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM ledger_snapshots ORDER BY ts DESC LIMIT 1"
            )
            return cur.fetchone()

    def ledger_snapshots_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM ledger_snapshots WHERE ts >= ? ORDER BY ts ASC", (since_iso,)
            )
            return cur.fetchall()

    def all_trades(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM trades ORDER BY ts ASC")
            return cur.fetchall()

    # ---------- 구동 세션 (구동 시간 / 구간 손익) ----------

    def start_session(self, mode: str, budget_krw: float,
                      start_value_krw: float) -> int:
        """봇 구동 시작을 기록하고 세션 id 를 돌려준다."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO run_sessions (started_at, mode, budget_krw, start_value_krw) "
                "VALUES (?, ?, ?, ?)",
                (_now_iso(), mode, budget_krw, start_value_krw),
            )
            return int(cur.lastrowid)

    def end_session(self, session_id: int, end_value_krw: float,
                    realized_pnl_krw: float, num_trades: int,
                    stop_reason: str = "정상 종료") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_sessions SET ended_at = ?, end_value_krw = ?, "
                "realized_pnl_krw = ?, num_trades = ?, stop_reason = ? WHERE id = ?",
                (_now_iso(), end_value_krw, realized_pnl_krw, num_trades,
                 stop_reason, session_id),
            )

    def close_orphan_sessions(self) -> int:
        """끝나지 않은 채 남은 세션을 정리한다.

        프로그램이 비정상 종료(강제 종료/정전)되면 ended_at 이 NULL 로 남아
        구동 시간이 무한정 늘어난 것처럼 보인다. 다음 시작 때 그 세션의
        마지막 잔고 스냅샷 시각을 종료 시각으로 삼아 닫는다.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, started_at FROM run_sessions WHERE ended_at IS NULL"
            )
            rows = cur.fetchall()
            for row in rows:
                snap = conn.execute(
                    "SELECT ts FROM ledger_snapshots WHERE ts >= ? ORDER BY ts DESC LIMIT 1",
                    (row["started_at"],),
                ).fetchone()
                ended = snap["ts"] if snap else row["started_at"]
                conn.execute(
                    "UPDATE run_sessions SET ended_at = ?, stop_reason = ? WHERE id = ?",
                    (ended, "비정상 종료 (다음 실행 시 자동 정리)", row["id"]),
                )
            return len(rows)

    def all_sessions(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM run_sessions ORDER BY started_at DESC")
            return cur.fetchall()

    # ---------- 상승 기록 ----------

    def log_rise_event(self, session_id: Optional[int], market: str,
                       price_from: float, price_to: float, window_min: int,
                       held: bool) -> None:
        change_pct = ((price_to - price_from) / price_from * 100) if price_from else 0.0
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rise_events (ts, session_id, market, price_from, price_to, "
                "change_pct, window_min, held) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_now_iso(), session_id, market, price_from, price_to, change_pct,
                 window_min, 1 if held else 0),
            )

    def rise_events(self, limit: int = 500) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM rise_events ORDER BY ts DESC LIMIT ?", (limit,)
            )
            return cur.fetchall()

    # ---------- 패턴 학습 표본 ----------

    def insert_patterns(self, rows: list[tuple]) -> int:
        """(ts, market, interval, features_json, future_return_pct, label, source)
        튜플 목록을 한 번에 넣는다. 이미 있는 (market, interval, ts) 는 건너뛴다.
        실제로 새로 들어간 개수를 돌려준다."""
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.execute("SELECT COUNT(*) AS c FROM patterns").fetchone()["c"]
            conn.executemany(
                "INSERT OR IGNORE INTO patterns (ts, market, interval, features_json, "
                "future_return_pct, label, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            after = conn.execute("SELECT COUNT(*) AS c FROM patterns").fetchone()["c"]
            return after - before

    def count_patterns(self, label: Optional[int] = None) -> int:
        with self._connect() as conn:
            if label is None:
                cur = conn.execute("SELECT COUNT(*) AS c FROM patterns")
            else:
                cur = conn.execute(
                    "SELECT COUNT(*) AS c FROM patterns WHERE label = ?", (label,)
                )
            return int(cur.fetchone()["c"])

    def all_patterns(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM patterns ORDER BY ts ASC")
            return cur.fetchall()

    def get_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def clear_patterns(self) -> int:
        """학습 표본을 전부 비운다. 급등 기준 등 규칙이 바뀌었을 때 쓴다."""
        with self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM patterns").fetchone()["c"]
            conn.execute("DELETE FROM patterns")
            return int(n)

    def pattern_markets(self) -> list[str]:
        with self._connect() as conn:
            cur = conn.execute("SELECT DISTINCT market FROM patterns")
            return [r["market"] for r in cur.fetchall()]
