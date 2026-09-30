
from polydrift.graph import MIDTERM_GRAPH, cross_venue_leadlag, find_series, graph_digest, graph_report
from polydrift.models import KalshiMarket, KalshiSnapshot, PolyMarket, PolySnapshot
from polydrift.storage import Storage

NOW = 1_800_000_000


def mk_storage(tmp_path):
    return Storage(tmp_path / "graph.db")


def add_kalshi(st, ticker, title, sub=""):
    st.upsert_kalshi_markets([KalshiMarket(
        ticker=ticker, event_ticker="E", series_ticker="S", title=title,
        yes_sub_title=sub, rules_primary="", rules_secondary="",
        close_time=None, market_type="binary", status="open")], NOW)


def add_poly(st, cid, question):
    st.upsert_poly_markets([PolyMarket(
        condition_id=cid, gamma_id="1", question=question, slug="q",
        description="", end_date=None, yes_token_id="t1", no_token_id="t2",
        active=True, closed=False)], NOW)


# --- pre-map patterns --------------------------------------------------------

def test_senate_control_node_matches_the_real_contracts(tmp_path):
    st = mk_storage(tmp_path)
    add_kalshi(st, "CONTROLS-2026", "Which party will win the U.S. Senate?", "Democratic Party")
    add_poly(st, "c1", "Which party will win the Senate in 2026?")
    node = {n.key: n for n in MIDTERM_GRAPH}["senate-control"]
    found = find_series(st, node)
    assert [i for i, _ in found["kalshi"]] == ["CONTROLS-2026"]
    assert [i for i, _ in found["poly"]] == ["c1"]
    st.close()


def test_state_race_nodes_match_state_events(tmp_path):
    st = mk_storage(tmp_path)
    add_kalshi(st, "SENATETX-26", "Texas Senate winner?")
    add_poly(st, "tx", "Texas Senate Election Winner")
    nodes = {n.key: n for n in MIDTERM_GRAPH}
    assert find_series(st, nodes["state-senate-tx"])["kalshi"]
    assert find_series(st, nodes["state-senate-tx"])["poly"]
    # a 2028 race must NOT match the 2026 node pattern
    add_kalshi(st, "SENATEOH-28", "Ohio Senate winner? (2028)")
    assert "SENATEOH-28" not in [i for i, _ in find_series(st, nodes["state-senate-oh"])["kalshi"]]
    st.close()


def test_seat_total_and_balance_nodes(tmp_path):
    st = mk_storage(tmp_path)
    add_kalshi(st, "KXDSENATEWON-26NOV03", "How many Senate seats will Democrats win in the Midterms?")
    add_kalshi(st, "KXBALANCEPOWERCOMBO-27FEB", "2026 Midterms: Congress Balance of Power?")
    add_poly(st, "b1", "Balance of Power: 2026 Midterms")
    nodes = {n.key: n for n in MIDTERM_GRAPH}
    assert find_series(st, nodes["senate-seat-total"])["kalshi"]
    assert find_series(st, nodes["congress-balance"])["kalshi"]
    assert find_series(st, nodes["congress-balance"])["poly"]
    st.close()


# --- lead/lag ----------------------------------------------------------------

def _series(steps, start=0.5, cadence=300, move_at=None, move=0.1):
    out = []
    v = start
    for i in range(steps):
        if move_at is not None and i == move_at:
            v += move
        out.append((i * cadence, v))
    return out


def test_leadlag_detects_kalshi_leading():
    kalshi = _series(20, move_at=5, move=0.08)
    poly = _series(20, move_at=8, move=0.08)      # same move, 3 cadences later
    leader, lag_s, corr = cross_venue_leadlag(kalshi, poly)
    assert leader == "kalshi"
    assert lag_s == 3 * 300
    assert corr >= 0.2


def test_leadlag_detects_polymarket_leading():
    kalshi = _series(20, move_at=9, move=-0.06)
    poly = _series(20, move_at=4, move=-0.06)
    leader, lag_s, _ = cross_venue_leadlag(kalshi, poly)
    assert leader == "polymarket" and lag_s == 5 * 300


def test_leadlag_returns_none_when_uninformative():
    flat = [(i * 300, 0.5) for i in range(20)]
    other = _series(20, move_at=3, move=0.1)
    assert cross_venue_leadlag(flat, flat)[0] is None       # no variance
    assert cross_venue_leadlag(flat[:2], other[:2])[0] is None  # too few stamps


def test_leadlag_ignores_unalignable_moves():
    # one isolated move per venue, too far apart for any lag in range to
    # align them: no callable lead, deterministically
    kalshi = _series(20, move_at=5, move=0.08)
    poly = _series(20, move_at=13, move=0.08)
    leader, lag_s, corr = cross_venue_leadlag(kalshi, poly)
    assert leader is None and corr < 0.2


# --- report / digest ---------------------------------------------------------

def test_graph_report_structure(tmp_path):
    st = mk_storage(tmp_path)
    rep = graph_report(st, now_ts=NOW)
    assert set(rep["nodes"]) == {n.key for n in MIDTERM_GRAPH}
    assert {(e["child"], e["parent"]) for e in rep["edges"]} == {
        ("state_race", "seat_count"), ("seat_count", "chamber_control"),
        ("chamber_control", "balance")}
    # nothing recorded: no crash, no phantom leaders
    assert all("leader" in n["cross_venue"] for n in rep["nodes"].values())
    st.close()


def test_graph_digest_counts_and_leaders(tmp_path):
    st = mk_storage(tmp_path)
    add_kalshi(st, "CONTROLS-2026", "Which party will win the U.S. Senate?")
    add_poly(st, "c1", "Which party will win the Senate in 2026?")
    # record enough overlapping history for a callable lead/lag
    for i in range(20):
        ts = NOW - (20 - i) * 300
        kv = 0.5 + (0.08 if i >= 5 else 0.0)
        pv = 0.5 + (0.08 if i >= 9 else 0.0)
        st.insert_kalshi_snapshots([KalshiSnapshot("CONTROLS-2026", ts, kv - .005, kv + .005, kv, 1, 1, 1)])
        st.insert_poly_snapshots([PolySnapshot("c1", ts, pv, 1 - pv, 1, 1, "clob_mid")])
    d = graph_digest(st, lookback_s=24 * 3600)
    assert d["nodes_mapped"] >= 1
    assert d["nodes_both_venues"] >= 1
    assert "senate-control" in d["leaders"]
    assert d["leaders"]["senate-control"]["leader"] == "kalshi"
    st.close()
