from polydrift.models import PolyMarket, PolySnapshot
from polydrift.storage import Storage


def mk_market():
    return PolyMarket(condition_id="c1", gamma_id="1", question="Q?", slug="q",
                      description="d", end_date="2027-01-01T00:00:00Z",
                      yes_token_id="t1", no_token_id="t2", active=True, closed=False)


def test_snapshot_inserts_are_idempotent(tmp_path):
    """A re-run backfill must not duplicate history."""
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([mk_market()], 1000)
    snaps = [PolySnapshot("c1", 1000, 0.4, 0.6, 1.0, 2.0, "clob_history")]
    assert st.insert_poly_snapshots(snaps) == 1
    assert st.insert_poly_snapshots(snaps) == 0
    st.close()


def test_same_ts_different_source_both_recorded(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([mk_market()], 1000)
    st.insert_poly_snapshots([PolySnapshot("c1", 1000, 0.4, 0.6, None, None, "gamma")])
    st.insert_poly_snapshots([PolySnapshot("c1", 1000, 0.4, 0.6, None, None, "clob_history")])
    assert st.status()["poly_snapshots"] == 2
    st.close()


def test_has_history_only_true_for_backfilled_source(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([mk_market()], 1000)
    st.insert_poly_snapshots([PolySnapshot("c1", 1000, 0.4, 0.6, None, None, "gamma")])
    assert not st.has_history("c1")
    st.insert_poly_snapshots([PolySnapshot("c1", 900, 0.4, 0.6, None, None, "clob_history")])
    assert st.has_history("c1")
    st.close()


def test_upsert_updates_rather_than_duplicating(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([mk_market()], 1000)
    st.upsert_poly_markets([mk_market()], 2000)
    assert st.status()["poly_markets"] == 1
    st.close()


def test_run_bookkeeping_records_failures(tmp_path):
    st = Storage(tmp_path / "t.db")
    rid = st.start_run(1000)
    st.finish_run(rid, 1010, poly_markets=0, poly_snaps=0,
                  kalshi_markets=0, kalshi_snaps=0, error="boom")
    s = st.status()
    assert s["runs_failed"] == 1 and s["last_run_ok"] is False
    st.close()


# --- Semantic-gate migration (2026-09-26) ------------------------------------

def test_old_volume_db_gains_semantic_columns(tmp_path):
    """A Railway volume created before the semantic gate has kalshi_pairs
    WITHOUT the new columns; opening it must upgrade in place, not fail."""
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE kalshi_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL, ticker TEXT NOT NULL,
            score REAL NOT NULL, dates_ok INTEGER NOT NULL, days_apart INTEGER,
            wording_conflict INTEGER NOT NULL, wording_note TEXT,
            status TEXT NOT NULL, created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL,
            UNIQUE(condition_id, ticker));
    """)
    raw.execute(
        "INSERT INTO kalshi_pairs (condition_id, ticker, score, dates_ok, wording_conflict,"
        " status, created_ts, updated_ts) VALUES ('c1','K1',0.9,1,0,'approved',1,1)")
    raw.commit()
    raw.close()

    st = Storage(path)   # migration runs on open
    cols = {r["name"] for r in st.conn.execute("PRAGMA table_info(kalshi_pairs)")}
    assert {"semantic_conflict", "semantic_note"} <= cols
    row = st.conn.execute("SELECT * FROM kalshi_pairs").fetchone()
    assert row["semantic_conflict"] == 0 and row["semantic_note"] is None
    assert row["status"] == "approved"          # existing decisions survive
    st.close()
