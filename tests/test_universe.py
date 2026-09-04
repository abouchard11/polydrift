import datetime as dt

from polydrift.config import DEFAULTS, deep_merge
from polydrift.models import PolyMarket
from polydrift.universe import is_zombie, select_poly_universe

NOW = dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)


def mk(question, end_date, closed=False):
    return PolyMarket(
        condition_id=question, gamma_id="1", question=question, slug="s",
        description="", end_date=end_date, yes_token_id="t1", no_token_id="t2",
        active=True, closed=closed,
    )


def test_rejects_zombie_markets():
    """Live case 2026-09-04: the Ethiopia PM markets report active=true with an
    endDate of 2026-06-01, three months in the past."""
    zombie = mk("Will Adanech Abiebie be the next Prime Minister of Ethiopia?",
                "2026-06-01T00:00:00Z")
    assert is_zombie(zombie, NOW)
    kept = select_poly_universe([(zombie, 500_000.0, 0.5)], DEFAULTS, now=NOW)
    assert kept == []


def test_keeps_a_live_liquid_market():
    live = mk("Will the Fed cut rates in September?", "2026-09-18T00:00:00Z")
    kept = select_poly_universe([(live, 500_000.0, 0.5)], DEFAULTS, now=NOW)
    assert [m.question for m in kept] == [live.question]


def test_liquidity_floor_filters_thin_books():
    thin = mk("Thin market", "2026-12-01T00:00:00Z")
    assert select_poly_universe([(thin, 100.0, 0.5)], DEFAULTS, now=NOW) == []


def test_ranks_by_liquidity_not_volume():
    a = mk("A", "2026-12-01T00:00:00Z")
    b = mk("B", "2026-12-01T00:00:00Z")
    kept = select_poly_universe([(a, 30_000.0, 0.5), (b, 900_000.0, 0.5)], DEFAULTS, now=NOW)
    assert [m.question for m in kept] == ["B", "A"]


def test_pattern_denylist_drops_farm_markets():
    cfg = deep_merge(DEFAULTS, {"universe": {"exclude_question_patterns": ["(?i)jesus christ"]}})
    farm = mk("Will Jesus Christ return before 2027?", "2027-01-01T00:00:00Z")
    assert select_poly_universe([(farm, 586_460.0, 0.5)], cfg, now=NOW) == []


def test_horizon_cap_drops_the_2070_tail():
    far = mk("Will Elon visit Mars in his lifetime?", "2070-01-01T00:00:00Z")
    assert select_poly_universe([(far, 900_000.0, 0.5)], DEFAULTS, now=NOW) == []


def test_price_band_drops_dead_longshots():
    """Live 2026-09-04: ranking on liquidity alone returned six markets all
    priced 0.0005-0.0075 — the dead legs of multi-outcome election markets.
    Nothing converges from half a basis point."""
    longshot = mk("Will Aldo Rebelo win the 2026 Brazilian presidential election?",
                  "2026-10-04T00:00:00Z")
    assert select_poly_universe([(longshot, 2_076_107.0, 0.0005)], DEFAULTS, now=NOW) == []


def test_price_band_keeps_a_live_contested_market():
    contested = mk("Will the Fed cut in September?", "2026-09-18T00:00:00Z")
    kept = select_poly_universe([(contested, 300_000.0, 0.62)], DEFAULTS, now=NOW)
    assert len(kept) == 1


def test_missing_price_does_not_silently_drop_a_market():
    unknown = mk("No price yet", "2026-12-01T00:00:00Z")
    assert len(select_poly_universe([(unknown, 300_000.0, None)], DEFAULTS, now=NOW)) == 1
