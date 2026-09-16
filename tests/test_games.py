"""Shapes below are verbatim from live Gamma / Kalshi responses on 2026-09-16."""
import datetime as dt

from polydrift.config import DEFAULTS, deep_merge
from polydrift.games import parse_poly_game, pick_leg, refresh_game_pairs
from polydrift.models import PolyMarket
from polydrift.storage import Storage
from polydrift.universe import select_poly_universe

NOW = dt.datetime(2026, 9, 15, 20, 0, tzinfo=dt.timezone.utc)


def test_parse_polymarket_game_slug():
    k = parse_poly_game("mlb-sea-laa-2026-09-15", "Seattle Mariners vs. Los Angeles Angels")
    assert (k.league, k.team0, k.team1) == ("mlb", "sea", "laa")
    assert k.date == dt.date(2026, 9, 15)
    assert k.team0_name == "Seattle Mariners"
    assert k.kalshi_prefix == "KXMLBGAME-26SEP15"


def test_non_game_markets_are_ignored():
    assert parse_poly_game("will-the-fed-cut", "Will the Fed cut?") is None
    assert parse_poly_game("mlb-sea-laa-2026-09-15", "Mariners to win the World Series?") is None


def test_pick_leg_by_ticker_suffix_then_by_subtitle():
    k = parse_poly_game("mlb-sea-laa-2026-09-15", "Seattle Mariners vs. Los Angeles Angels")
    legs = [{"ticker": "KXMLBGAME-26SEP151838SEALAA-SEA", "yes_sub_title": "Seattle"},
            {"ticker": "KXMLBGAME-26SEP151838SEALAA-LAA", "yes_sub_title": "Los Angeles A"}]
    assert pick_leg(k, legs)["ticker"].endswith("-SEA")
    # Venues abbreviate the A's differently: Polymarket 'oak', Kalshi 'ATH'.
    k2 = parse_poly_game("mlb-oak-tb-2026-09-15", "Athletics vs. Tampa Bay Rays")
    legs2 = [{"ticker": "KXMLBGAME-26SEP151840ATHTB-ATH", "yes_sub_title": "Athletics"},
             {"ticker": "KXMLBGAME-26SEP151840ATHTB-TB", "yes_sub_title": "Tampa Bay"}]
    assert pick_leg(k2, legs2)["ticker"].endswith("-ATH")
    assert pick_leg(k, legs2) is None


def _poly(cid, slug, q, end="2026-09-16T01:38:00Z"):
    return PolyMarket(condition_id=cid, gamma_id="1", question=q, slug=slug, description="",
                      end_date=end, yes_token_id="t", no_token_id="n", active=True, closed=False)


def test_game_pairs_auto_clear_from_identifiers(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([_poly("c1", "mlb-sea-laa-2026-09-15", "Seattle Mariners vs. Los Angeles Angels")], 1)
    st.upsert_kalshi_events([
        {"event_ticker": "KXMLBGAME-26SEP151838SEALAA", "title": "Seattle vs Los Angeles A"},
        {"event_ticker": "KXMLBGAME-26SEP151840ATHTB", "title": "A's vs Tampa Bay"},
    ], now_ts=1)
    calls = []
    def fetch(et):
        calls.append(et)
        return ({"event_ticker": et, "series_ticker": "KXMLBGAME", "title": "Seattle vs Los Angeles A"},
                [{"ticker": et + "-SEA", "event_ticker": et, "yes_sub_title": "Seattle",
                  "rules_primary": "If Seattle wins ... resolves to Yes.", "close_time": "2026-09-18T22:40:00Z",
                  "market_type": "binary", "status": "active"},
                 {"ticker": et + "-LAA", "event_ticker": et, "yes_sub_title": "Los Angeles A",
                  "rules_primary": "If Los Angeles A wins ...", "close_time": "2026-09-18T22:40:00Z",
                  "market_type": "binary", "status": "active"}])
    counts = refresh_game_pairs(st, DEFAULTS, now_ts=2, fetch_event_markets=fetch)
    assert counts["paired"] == 1 and calls == ["KXMLBGAME-26SEP151838SEALAA"]
    pair = st.usable_pairs()[0]
    assert pair["ticker"] == "KXMLBGAME-26SEP151838SEALAA-SEA" and pair["status"] == "auto_ok"
    # idempotent: already paired, no refetch
    counts = refresh_game_pairs(st, DEFAULTS, now_ts=3, fetch_event_markets=fetch)
    assert counts["already_paired"] == 1 and len(calls) == 1
    st.close()


def test_game_slice_joins_the_universe_at_a_lower_floor():
    general = _poly("g", "will-the-fed-cut", "Will the Fed cut in September?", end="2026-09-18T00:00:00Z")
    game = _poly("m", "mlb-sd-col-2026-09-15", "San Diego Padres vs. Colorado Rockies")
    cfg = deep_merge(DEFAULTS, {"universe": {"max_markets": 1, "max_games": 5}})
    kept = select_poly_universe([(general, 300_000.0, 0.5), (game, 15_000.0, 0.6)], cfg, now=NOW)
    assert [m.condition_id for m in kept] == ["g", "m"]        # game kept despite < 25k
    cfg0 = deep_merge(DEFAULTS, {"universe": {"max_markets": 1, "max_games": 0}})
    assert [m.condition_id for m in select_poly_universe([(general, 300_000.0, 0.5), (game, 15_000.0, 0.6)], cfg0, now=NOW)] == ["g"]


def test_ended_games_stop_being_tracked(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([_poly("old", "mlb-sea-laa-2026-09-01", "Seattle Mariners vs. Los Angeles Angels",
                                  end="2026-09-02T01:00:00Z"),
                            _poly("new", "mlb-sea-laa-2099-09-01", "Seattle Mariners vs. Los Angeles Angels",
                                  end="2099-09-02T01:00:00Z")], 1)
    from polydrift.models import KalshiMarket
    for cid, tk in (("old", "K-OLD"), ("new", "K-NEW")):
        st.upsert_kalshi_markets([KalshiMarket(ticker=tk, event_ticker="E", series_ticker="S", title="t",
            yes_sub_title="", rules_primary="", rules_secondary="", close_time=None,
            market_type="binary", status="active")], 1)
        st.upsert_pair(cid, tk, score=1.0, dates_ok=True, days_apart=0, wording_conflict=False,
                       wording_note="", proposed_status="auto_ok", now_ts=1)
    assert st.pair_tickers() == ["K-NEW"]
    st.close()


def test_a_stale_fuzzy_candidate_does_not_block_the_deterministic_pair(tmp_path):
    from polydrift.models import KalshiMarket
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([_poly("c1", "mlb-sea-laa-2026-09-15", "Seattle Mariners vs. Los Angeles Angels")], 1)
    st.upsert_kalshi_markets([KalshiMarket(ticker="KXMLBGAME-26SEP141838SEALAA-SEA", event_ticker="E",
        series_ticker="S", title="t", yes_sub_title="Seattle", rules_primary="", rules_secondary="",
        close_time=None, market_type="binary", status="active")], 1)
    st.upsert_pair("c1", "KXMLBGAME-26SEP141838SEALAA-SEA", score=0.4, dates_ok=False, days_apart=1,
                   wording_conflict=False, wording_note="", proposed_status="needs_review", now_ts=1)  # wrong day
    st.upsert_kalshi_events([{"event_ticker": "KXMLBGAME-26SEP151838SEALAA", "title": "x"}], now_ts=1)
    def fetch(et):
        return ({"event_ticker": et, "series_ticker": "KXMLBGAME", "title": "x"},
                [{"ticker": et + "-SEA", "event_ticker": et, "yes_sub_title": "Seattle", "rules_primary": "",
                  "close_time": "2026-09-18T22:40:00Z", "market_type": "binary", "status": "active"}])
    counts = refresh_game_pairs(st, DEFAULTS, now_ts=2, fetch_event_markets=fetch)
    assert counts["paired"] == 1
    assert [r["ticker"] for r in st.usable_pairs()] == ["KXMLBGAME-26SEP151838SEALAA-SEA"]
    st.close()
