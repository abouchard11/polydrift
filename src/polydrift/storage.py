"""SQLite persistence.

Chosen over parquet deliberately: at ~60 markets on a 5-minute cycle this is
kilobytes a day. SQLite gives transactions, a single portable file, and zero
ops. Revisit only if we start recording full order-book snapshots.

Snapshot inserts are idempotent (UNIQUE on the natural key) so a backfill can
be re-run without duplicating history.
"""
from __future__ import annotations

import datetime as dt
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

CREATE TABLE IF NOT EXISTS kalshi_events (
    event_ticker  TEXT PRIMARY KEY,
    series_ticker TEXT,
    title         TEXT NOT NULL,
    sub_title     TEXT,
    category      TEXT,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kalshi_pairs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id     TEXT NOT NULL REFERENCES poly_markets(condition_id),
    ticker           TEXT NOT NULL REFERENCES kalshi_markets(ticker),
    score            REAL NOT NULL,
    dates_ok         INTEGER NOT NULL,
    days_apart       INTEGER,
    wording_conflict INTEGER NOT NULL,
    wording_note     TEXT,
    status           TEXT NOT NULL,      -- auto_ok | needs_review | approved | rejected
    created_ts       INTEGER NOT NULL,
    updated_ts       INTEGER NOT NULL,
    UNIQUE(condition_id, ticker)
);
CREATE INDEX IF NOT EXISTS ix_pairs_status ON kalshi_pairs(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

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

# Columns added after first deploy; applied idempotently in _migrate() so an
# existing Railway volume upgrades in place instead of failing the CREATEs
# above (which are IF NOT EXISTS and silently no-op on an old table).
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("kalshi_pairs", "ALTER TABLE kalshi_pairs ADD COLUMN semantic_conflict INTEGER NOT NULL DEFAULT 0"),
    ("kalshi_pairs", "ALTER TABLE kalshi_pairs ADD COLUMN semantic_note TEXT"),
)


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.journal_mode = self._set_journal_mode()
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first deploy. try/except per
        statement: a duplicate column error means it already ran; anything
        else (e.g. a locked volume) must not take the recorder down."""
        for _table, stmt in MIGRATIONS:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    log.warning("migration failed: %s (%s)", stmt, exc)

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
                     series_ticker=CASE WHEN excluded.series_ticker != '' THEN excluded.series_ticker ELSE kalshi_markets.series_ticker END,
                     event_ticker=CASE WHEN excluded.event_ticker != '' THEN excluded.event_ticker ELSE kalshi_markets.event_ticker END,
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

    def upsert_kalshi_events(self, events: Iterable[dict], now_ts: int) -> int:
        rows = [(str(e.get("event_ticker")), str(e.get("series_ticker") or ""),
                 str(e.get("title") or ""), str(e.get("sub_title") or ""),
                 str(e.get("category") or ""), now_ts, now_ts)
                for e in events if e.get("event_ticker")]
        if not rows:
            return 0
        with self.tx() as c:
            c.executemany(
                """INSERT INTO kalshi_events
                   (event_ticker, series_ticker, title, sub_title, category, first_seen_ts, last_seen_ts)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(event_ticker) DO UPDATE SET
                     series_ticker=excluded.series_ticker, title=excluded.title,
                     sub_title=excluded.sub_title, category=excluded.category,
                     last_seen_ts=excluded.last_seen_ts""",
                rows,
            )
        return len(rows)

    def pair_tickers(self, include_rejected: bool = False,
                     min_review_score: float | None = 0.5) -> list[str]:
        """Kalshi tickers worth snapshotting every cycle.

        Usable pairs always. needs_review pairs only when they clear a score
        floor and carry no wording conflict — i.e. a human might plausibly
        approve them. Live lesson 2026-09-08: tracking every candidate grew the
        set from 51 to 516 tickers in three days and tripped Kalshi's burst
        limit on every cycle."""
        if include_rejected:
            sql, args = "SELECT DISTINCT ticker FROM kalshi_pairs", ()
        elif min_review_score is None:
            sql, args = "SELECT DISTINCT ticker FROM kalshi_pairs WHERE status != 'rejected'", ()
        else:
            # Games settle nightly; once the Polymarket side has ended there
            # is nothing left to pair against, so stop spending calls on it.
            sql = """SELECT DISTINCT p.ticker FROM kalshi_pairs p
                     JOIN poly_markets m ON m.condition_id = p.condition_id
                     WHERE (p.status IN ('auto_ok', 'approved')
                            OR (p.status = 'needs_review' AND p.score >= ? AND p.wording_conflict = 0))
                       AND (m.end_date IS NULL OR m.end_date >= ?)"""
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            args = (float(min_review_score), cutoff)
        return [r[0] for r in self.conn.execute(sql, args).fetchall()]

    def paired_poly_markets(self) -> list[sqlite3.Row]:
        """Polymarket markets with a usable pair that have not ended. Kept on
        the snapshot list even after they rotate out of the liquidity top-N:
        a game that leaves the universe mid-game (liquidity is pulled as it
        ends) must still be recorded, or its last price is a stale one and the
        Kalshi side keeps moving alone (2026-09-16)."""
        cutoff = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.conn.execute(
            """SELECT DISTINCT m.condition_id, m.yes_token_id
               FROM poly_markets m JOIN kalshi_pairs p ON p.condition_id = m.condition_id
               WHERE p.status IN ('auto_ok', 'approved') AND m.yes_token_id IS NOT NULL
                 AND (m.end_date IS NULL OR m.end_date >= ?)""", (cutoff,)).fetchall()

    def prune_stale_events(self, seen_after_ts: int) -> int:
        """Drop index entries not refreshed by the latest sweep. Kalshi closes
        thousands of events a week; without this the index only grows
        (23,186 scanned vs 12,171 open on 2026-09-08)."""
        cur = self.conn.execute("DELETE FROM kalshi_events WHERE last_seen_ts < ?", (seen_after_ts,))
        return cur.rowcount

    # ---------- pair registry ----------

    def upsert_pair(self, condition_id: str, ticker: str, *, score: float, dates_ok: bool,
                    days_apart: int | None, wording_conflict: bool, wording_note: str,
                    proposed_status: str, now_ts: int,
                    semantic_conflict: bool = False, semantic_note: str = "") -> str:
        """Insert or refresh a candidate. Human decisions (approved / rejected)
        SURVIVE a refresh of the same pair — a re-match must never silently
        flip a reviewed pair back. Returns the resulting status."""
        row = self.conn.execute(
            "SELECT status FROM kalshi_pairs WHERE condition_id=? AND ticker=?",
            (condition_id, ticker),
        ).fetchone()
        if row and row["status"] in ("approved", "rejected"):
            status = row["status"]
        else:
            status = proposed_status
        self.conn.execute(
            """INSERT INTO kalshi_pairs
               (condition_id, ticker, score, dates_ok, days_apart, wording_conflict,
                wording_note, semantic_conflict, semantic_note,
                status, created_ts, updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(condition_id, ticker) DO UPDATE SET
                 score=excluded.score, dates_ok=excluded.dates_ok,
                 days_apart=excluded.days_apart,
                 wording_conflict=excluded.wording_conflict,
                 wording_note=excluded.wording_note,
                 semantic_conflict=excluded.semantic_conflict,
                 semantic_note=excluded.semantic_note,
                 status=?, updated_ts=excluded.updated_ts""",
            (condition_id, ticker, score, int(dates_ok), days_apart, int(wording_conflict),
             wording_note, int(semantic_conflict), semantic_note,
             status, now_ts, now_ts, status),
        )
        return status

    def set_pair_status(self, pair_id: int, status: str, now_ts: int) -> bool:
        cur = self.conn.execute(
            "UPDATE kalshi_pairs SET status=?, updated_ts=? WHERE id=?",
            (status, now_ts, pair_id),
        )
        return cur.rowcount == 1

    def list_pairs(self, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        sql = """SELECT p.*, m.question, m.description AS poly_rules, m.end_date,
                        k.title AS k_title, k.yes_sub_title, k.rules_primary,
                        k.rules_secondary, k.close_time
                 FROM kalshi_pairs p
                 JOIN poly_markets m ON m.condition_id = p.condition_id
                 JOIN kalshi_markets k ON k.ticker = p.ticker"""
        args: tuple = ()
        if statuses:
            sql += " WHERE p.status IN (%s)" % ",".join("?" * len(statuses))
            args = statuses
        sql += " ORDER BY p.status, p.score DESC"
        return self.conn.execute(sql, args).fetchall()

    def get_pair(self, pair_id: int) -> sqlite3.Row | None:
        rows = [r for r in self.list_pairs() if r["id"] == pair_id]
        return rows[0] if rows else None

    def usable_pairs(self) -> list[sqlite3.Row]:
        return self.list_pairs(("auto_ok", "approved"))

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------- radar snapshot queries ----------

    def latest_kalshi(self, ticker: str, max_stale_s: int = 900,
                      now_ts: int | None = None) -> sqlite3.Row | None:
        """Most recent Kalshi snapshot for a ticker, if fresher than the bound."""
        row = self.conn.execute(
            "SELECT * FROM kalshi_snapshots WHERE ticker=? ORDER BY ts DESC LIMIT 1",
            (ticker,)).fetchone()
        now_ts = now_ts if now_ts is not None else int(dt.datetime.now(dt.timezone.utc).timestamp())
        if row is None or int(row["ts"]) < now_ts - max_stale_s:
            return None
        return row

    def latest_poly(self, condition_id: str, max_stale_s: int = 900,
                    now_ts: int | None = None) -> sqlite3.Row | None:
        """Most recent live Polymarket snapshot (book midpoint or gamma, never
        the historical backfill source)."""
        row = self.conn.execute(
            """SELECT * FROM poly_snapshots
               WHERE condition_id=? AND source IN ('gamma', 'clob_mid')
               ORDER BY ts DESC LIMIT 1""",
            (condition_id,)).fetchone()
        now_ts = now_ts if now_ts is not None else int(dt.datetime.now(dt.timezone.utc).timestamp())
        if row is None or int(row["ts"]) < now_ts - max_stale_s:
            return None
        return row

    def recent_kalshi_series(self, ticker: str, since_ts: int) -> list[tuple[int, float]]:
        rows = self.conn.execute(
            """SELECT ts, COALESCE((yes_bid + yes_ask) / 2.0, last_price) AS mid
               FROM kalshi_snapshots WHERE ticker=? AND ts>=? ORDER BY ts""",
            (ticker, since_ts)).fetchall()
        return [(int(r["ts"]), float(r["mid"])) for r in rows if r["mid"] is not None]

    def recent_poly_series(self, condition_id: str, since_ts: int) -> list[tuple[int, float]]:
        rows = self.conn.execute(
            """SELECT ts, yes_price FROM poly_snapshots
               WHERE condition_id=? AND ts>=? AND source IN ('gamma', 'clob_mid')
               ORDER BY ts""",
            (condition_id, since_ts)).fetchall()
        return [(int(r["ts"]), float(r["yes_price"])) for r in rows if r["yes_price"] is not None]

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
            "kalshi_events": q("SELECT COUNT(*) FROM kalshi_events").fetchone()[0],
            "kalshi_markets": q("SELECT COUNT(*) FROM kalshi_markets").fetchone()[0],
            "kalshi_snapshots": q("SELECT COUNT(*) FROM kalshi_snapshots").fetchone()[0],
            "pairs_total": q("SELECT COUNT(*) FROM kalshi_pairs").fetchone()[0],
            "pairs_usable": q("SELECT COUNT(*) FROM kalshi_pairs WHERE status IN ('auto_ok','approved')").fetchone()[0],
            "pairs_review": q("SELECT COUNT(*) FROM kalshi_pairs WHERE status='needs_review'").fetchone()[0],
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
