from polydrift.models import KalshiMarket, KalshiSnapshot, PolyMarket, PolySnapshot
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
