import pytest

from polydrift.config import DEFAULTS, deep_merge
from polydrift.divergence import evaluate, value_after, value_at
from polydrift.models import KalshiMarket, KalshiSnapshot, PolyMarket, PolySnapshot
from polydrift.storage import Storage

H = 3600


def _pair(st: Storage) -> None:
    st.upsert_poly_markets([PolyMarket(
        condition_id="c1", gamma_id="1", question="Q?", slug="q", description="",
        end_date="2027-01-01T00:00:00Z", yes_token_id="t1", no_token_id="t2",
        active=True, closed=False)], 0)
    st.upsert_kalshi_markets([KalshiMarket(
        ticker="K1", event_ticker="E", series_ticker="S", title="Q?", yes_sub_title="",
        rules_primary="", rules_secondary="", close_time="2027-01-01T00:00:00Z",
        market_type="binary", status="active")], 0)
    st.upsert_pair("c1", "K1", score=0.9, dates_ok=True, days_apart=0,
                   wording_conflict=False, wording_note="", proposed_status="auto_ok", now_ts=0)


def test_series_lookup_respects_staleness():
    s = ([100, 200, 300], [0.1, 0.2, 0.3])
    assert value_at(s, 250, max_stale_s=100) == 0.2
    assert value_at(s, 250, max_stale_s=10) is None
    assert value_after(s, 250, max_ahead_s=100) == 0.3
    assert value_after(s, 350, max_ahead_s=10) is None


def test_kalshi_divergence_that_converges_is_scored_positive(tmp_path):
    """Kalshi says 0.60, Polymarket is at 0.50. An hour later Polymarket is at
    0.58: the provider captured +0.08 gross, minus a round-trip taker fee."""
    st = Storage(tmp_path / "t.db"); _pair(st)
    st.insert_poly_snapshots([
        PolySnapshot("c1", 1000,     0.50, 0.50, None, None, "gamma"),
        PolySnapshot("c1", 1000 + H, 0.58, 0.42, None, None, "gamma"),
    ])
    st.insert_kalshi_snapshots([KalshiSnapshot("K1", 1000, 0.59, 0.61, 0.60, 1, 1, 0)])
    r = evaluate(st, DEFAULTS, horizon_s=H)
    k = r.providers["kalshi"]
    assert k.n == 1 and k.hits == 1
    assert k.mean_gross == pytest.approx(0.08)
    assert k.mean_net < k.mean_gross           # fees were charged
    assert k.mean_net == pytest.approx(0.08 - (0.05 * 0.5 * 0.5 + 0.05 * 0.58 * 0.42))
    assert r.timestamps_evaluated == 1
    st.close()


def test_divergence_that_widens_is_scored_negative(tmp_path):
    st = Storage(tmp_path / "t.db"); _pair(st)
    st.insert_poly_snapshots([
        PolySnapshot("c1", 1000,     0.50, 0.50, None, None, "gamma"),
        PolySnapshot("c1", 1000 + H, 0.44, 0.56, None, None, "gamma"),
    ])
    st.insert_kalshi_snapshots([KalshiSnapshot("K1", 1000, 0.59, 0.61, 0.60, 1, 1, 0)])
    k = evaluate(st, DEFAULTS, horizon_s=H).providers["kalshi"]
    assert k.n == 1 and k.hits == 0 and k.mean_gross == pytest.approx(-0.06)
    st.close()


def test_below_threshold_is_not_a_signal(tmp_path):
    st = Storage(tmp_path / "t.db"); _pair(st)
    st.insert_poly_snapshots([
        PolySnapshot("c1", 1000,     0.50, 0.50, None, None, "gamma"),
        PolySnapshot("c1", 1000 + H, 0.51, 0.49, None, None, "gamma"),
    ])
    st.insert_kalshi_snapshots([KalshiSnapshot("K1", 1000, 0.51, 0.52, 0.515, 1, 1, 0)])
    r = evaluate(st, DEFAULTS, horizon_s=H)
    assert r.providers["kalshi"].n == 0
    assert r.providers["baseline:random"].n == 1     # baselines still evaluated
    st.close()


def test_missing_forward_price_skips_the_timestamp(tmp_path):
    st = Storage(tmp_path / "t.db"); _pair(st)
    st.insert_poly_snapshots([PolySnapshot("c1", 1000, 0.50, 0.50, None, None, "gamma")])
    st.insert_kalshi_snapshots([KalshiSnapshot("K1", 1000, 0.59, 0.61, 0.60, 1, 1, 0)])
    r = evaluate(st, DEFAULTS, horizon_s=H)
    assert r.timestamps_evaluated == 0
    st.close()


def test_only_usable_pairs_are_evaluated(tmp_path):
    st = Storage(tmp_path / "t.db"); _pair(st)
    pid = st.list_pairs()[0]["id"]
    st.set_pair_status(pid, "rejected", 1)
    st.insert_poly_snapshots([
        PolySnapshot("c1", 1000,     0.50, 0.50, None, None, "gamma"),
        PolySnapshot("c1", 1000 + H, 0.58, 0.42, None, None, "gamma"),
    ])
    st.insert_kalshi_snapshots([KalshiSnapshot("K1", 1000, 0.59, 0.61, 0.60, 1, 1, 0)])
    r = evaluate(st, DEFAULTS, horizon_s=H)
    assert r.pairs_evaluated == 0 and r.timestamps_evaluated == 0
    st.close()


def test_log_report_never_raises_and_emits_lines(tmp_path, caplog):
    """The recorder reports on itself in the logs; a report bug must never
    stop the poll loop."""
    import logging
    from polydrift.runner import log_report
    st = Storage(tmp_path / "t.db"); _pair(st)
    with caplog.at_level(logging.INFO, logger="polydrift.runner"):
        log_report(DEFAULTS, st)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("REPORT")]
    assert any("usable_pairs=1" in l for l in lines)
    assert sum(1 for l in lines if "provider=kalshi" in l) == len(DEFAULTS["divergence"]["horizons_s"])
    st.close()


def test_tokens_are_memoised():
    from polydrift.matching import _tokens_cached, tokens
    _tokens_cached.cache_clear()
    for _ in range(50):
        tokens("Will Spain win the UEFA EURO 2028?")
    info = _tokens_cached.cache_info()
    assert info.misses == 1 and info.hits == 49
