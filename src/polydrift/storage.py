"""SQLite persistence.

Chosen over parquet deliberately: at ~60 markets on a 5-minute cycle this is
kilobytes a day. SQLite gives transactions, a single portable file, and zero
ops. Revisit only if we start recording full order-book snapshots.

Snapshot inserts are idempotent (UNIQUE on the natural key) so a backfill can
be re-run without duplicating history.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .models import KalshiMarket, KalshiSnapshot, PolyMarket, PolySnapshot

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS poly_markets (
    condition_id  TEXT PRIMARY KEY,
    gamma_id      TEXT,
    question      TEXT NOT NULL,
    slug          TEXT,
    description   TEXT,
    end_date      TEXT,
    yes_token_id  TEXT,
    no_token_id   TEXT,
    active        INTEGER,
    closed        INTEGER,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS poly_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id  TEXT NOT NULL REFERENCES poly_markets(condition_id),
    ts            INTEGER NOT NULL,
    yes_price     REAL,
    no_price      REAL,
    liquidity_usd REAL,
    volume_usd    REAL,
    source        TEXT NOT NULL,
    UNIQUE(condition_id, ts, source)
);
CREATE INDEX IF NOT EXISTS ix_poly_snap_cid_ts ON poly_snapshots(condition_id, ts);

CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker         TEXT PRIMARY KEY,
    event_ticker   TEXT,
    series_ticker  TEXT,
    title          TEXT NOT NULL,
    yes_sub_title  TEXT,
    rules_primary  TEXT,
    rules_secondary TEXT,
    close_time     TEXT,
    market_type    TEXT,
    status         TEXT,
    first_seen_ts  INTEGER NOT NULL,
    last_seen_ts   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kalshi_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL REFERENCES kalshi_markets(ticker),
    ts            INTEGER NOT NULL,
    yes_bid       REAL,
    yes_ask       REAL,
    last_price    REAL,
    volume        REAL,
    open_interest REAL,
    liquidity     REAL,
    UNIQUE(ticker, ts)
);
CREATE INDEX IF NOT EXISTS ix_kalshi_snap_tk_ts ON kalshi_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS poll_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts         INTEGER NOT NULL,
    finished_ts        INTEGER,
    poly_markets_seen  INTEGER DEFAULT 0,
    poly_snapshots     INTEGER DEFAULT 0,
    kalshi_markets_seen INTEGER DEFAULT 0,
    kalshi_snapshots   INTEGER DEFAULT 0,
    ok                 INTEGER DEFAULT 0,
    error              TEXT
);
"""


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.journal_mode = self._set_journal_mode()
        self.conn.executescript(SCHEMA)

    def _set_journal_mode(self) -> str:
        """WAL is faster, but it needs shared memory the filesystem may not
        provide. Network/FUSE mounts and some container volumes raise
        'disk I/O error' on WAL and would otherwise take the whole recorder
        down on start. Degrade to a rollback journal instead of dying."""
        for mode in ("WAL", "TRUNCATE", "DELETE"):
            try:
                row = self.conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
            except sqlite3.OperationalError:
                continue
            if row and str(row[0]).upper() == mode:
                if mode != "WAL":
                    log.warning("SQLite WAL unavailable on %s; using journal_mode=%s",
                                self.db_path.parent, mode)
                return mode
        return "unknown"

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------- Polymarket ----------

    def upsert_poly_markets(self, markets: Iterable[PolyMarket], now_ts: int) -> int:
        rows = [
            (m.condition_id, m.gamma_id, m.question, m.slug, m.description, m.end_date,
             m.yes_token_id, m.no_token_id, int(m.active), int(m.closed), now_ts, now_ts)
            for m in markets
        ]
        if not rows:
            return 0
        with self.tx() as c:
            c.executemany(
                """INSERT INTO poly_markets
                   (condition_id, gamma_id, question, slug, description, end_date,
                    yes_token_id, no_token_id, active, closed, first_seen_ts, last_seen_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(condition_id) DO UPDATE SET
                     question=excluded.question, slug=excluded.slug,
                     description=excluded.description, end_date=excluded.end_date,
                     yes_token_id=excluded.yes_token_id, no_token_id=excluded.no_token_id,
                     active=excluded.active, closed=excluded.closed,
                     last_seen_ts=excluded.last_seen_ts""",
                rows,
            )
        return len(rows)

    def insert_poly_snapshots(self, snaps: Iterable[PolySnapshot]) -> int:
        rows = [(s.condition_id, s.ts, s.yes_price, s.no_price,
                 s.liquidity_usd, s.volume_usd, s.source) for s in snaps]
        if not rows:
            return 0
        with self.tx() as c:
            before = c.execute("SELECT COUNT(*) FROM poly_snapshots").fetchone()[0]
            c.executemany(
                """INSERT OR IGNORE INTO poly_snapshots
                   (condition_id, ts, yes_price, no_price, liquidity_usd, volume_usd, source)
                   VALUES (?,?,?,?,?,?,?)""",
                rows,
            )
            after = c.execute("SELECT COUNT(*) FROM poly_snapshots").fetchone()[0]
        return after - before

    def has_history(self, condition_id: str, source: str = "clob_history") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM poly_snapshots WHERE condition_id=? AND source=? LIMIT 1",
            (condition_id, source),
        ).fetchone()
        return row is not None

    # ---------- Kalshi ----------

    def upsert_kalshi_markets(self, markets: Iterable[KalshiMarket], now_ts: int) -> int:
        rows = [
            (m.ticker, m.event_ticker, m.series_ticker, m.title, m.yes_sub_title,
             m.rules_primary, m.rules_secondary, m.close_time, m.market_type,
             m.status, now_ts, now_ts)
            for m in markets
        ]
        if not rows:
            return 0
        with self.tx() as c:
            c.executemany(
                """INSERT INTO kalshi_markets
                   (ticker, event_ticker, series_ticker, title, yes_sub_title,
                    rules_primary, rules_secondary, close_time, market_type, status,
                    first_seen_ts, last_seen_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     title=excluded.title, yes_sub_title=excluded.yes_sub_title,
                     rules_primary=excluded.rules_primary,
                     rules_secondary=excluded.rules_secondary,
                     close_time=excluded.close_time, status=excluded.status,
                     last_seen_ts=excluded.last_seen_ts""",
                rows,
            )
        return len(rows)

    def insert_kalshi_snapshots(self, snaps: Iterable[KalshiSnapshot]) -> int:
        rows = [(s.ticker, s.ts, s.yes_bid, s.yes_ask, s.last_price,
                 s.volume, s.open_interest, s.liquidity) for s in snaps]
        if not rows:
            return 0
        with self.tx() as c:
            before = c.execute("SELECT COUNT(*) FROM kalshi_snapshots").fetchone()[0]
            c.executemany(
                """INSERT OR IGNORE INTO kalshi_snapshots
                   (ticker, ts, yes_bid, yes_ask, last_price, volume, open_interest, liquidity)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
            after = c.execute("SELECT COUNT(*) FROM kalshi_snapshots").fetchone()[0]
        return after - before

    # ---------- run bookkeeping / status ----------

    def start_run(self, now_ts: int) -> int:
        cur = self.conn.execute("INSERT INTO poll_runs (started_ts) VALUES (?)", (now_ts,))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now_ts: int, *, poly_markets: int, poly_snaps: int,
                   kalshi_markets: int, kalshi_snaps: int, error: str | None) -> None:
        self.conn.execute(
            """UPDATE poll_runs SET finished_ts=?, poly_markets_seen=?, poly_snapshots=?,
               kalshi_markets_seen=?, kalshi_snapshots=?, ok=?, error=? WHERE id=?""",
            (now_ts, poly_markets, poly_snaps, kalshi_markets, kalshi_snaps,
             0 if error else 1, error, run_id),
        )

    def status(self) -> dict[str, object]:
        q = self.conn.execute
        out: dict[str, object] = {
            "db_path": str(self.db_path),
            "poly_markets": q("SELECT COUNT(*) FROM poly_markets").fetchone()[0],
            "poly_snapshots": q("SELECT COUNT(*) FROM poly_snapshots").fetchone()[0],
            "kalshi_markets": q("SELECT COUNT(*) FROM kalshi_markets").fetchone()[0],
            "kalshi_snapshots": q("SELECT COUNT(*) FROM kalshi_snapshots").fetchone()[0],
            "runs_total": q("SELECT COUNT(*) FROM poll_runs").fetchone()[0],
            "runs_failed": q("SELECT COUNT(*) FROM poll_runs WHERE ok=0 AND finished_ts IS NOT NULL").fetchone()[0],
        }
        row = q("SELECT started_ts, ok, error FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            out["last_run_ts"] = row["started_ts"]
            out["last_run_ok"] = bool(row["ok"])
            out["last_run_error"] = row["error"]
        span = q("SELECT MIN(ts), MAX(ts) FROM poly_snapshots").fetchone()
        out["poly_span"] = (span[0], span[1])
        return out

    def close(self) -> None:
        self.conn.close()
