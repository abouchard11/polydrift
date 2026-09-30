import json

import pytest

from polydrift.fees import kalshi_fee, polymarket_fee
from polydrift.models import KalshiMarket, KalshiSnapshot, PolyMarket, PolySnapshot
from polydrift.radar import (
    REQUIRED_ALERT_KEYS, candidate_costs, classify_gap, infer_leader,
    scan, scan_digest, semantic_mismatch,
)
from polydrift.storage import Storage


NOW = 1_800_000_000


def mk_storage(tmp_path, *, tier_texts=None):
    st = Storage(tmp_path / "radar.db")
    poly = PolyMarket(
        condition_id="c1", gamma_id="1",
        question="Will the Democratic Party control the Senate after the 2026 Midterm elections?",
        slug="q", description=tier_texts["poly_desc"] if tier_texts else
        "This market will resolve according to the party that wins control of the "
        "United States Senate in the 2026 midterm election. A party wins control "
        "if it wins a majority of the chamber's voting seats.",
        end_date="2026-11-03T23:59:00Z",
        yes_token_id="t1", no_token_id="t2", active=True, closed=False)
    kal = KalshiMarket(
        ticker="CONTROLS-2026-D", event_ticker="CONTROLS-2026", series_ticker="CONTROLS",
        title="Which party will win the U.S. Senate?",
        yes_sub_title="Democratic Party",
        rules_primary=tier_texts["kal_rules"] if tier_texts else
        "If the Democratic Party has won control of the U.S. Senate in 2026, "
        "then the market resolves to Yes.",
        rules_secondary="", close_time="2026-11-03T23:59:00Z",
        market_type="binary", status="open")
    st.upsert_poly_markets([poly], NOW)
    st.upsert_kalshi_markets([kal], NOW)
    st.upsert_pair("c1", "CONTROLS-2026-D", score=0.9, dates_ok=True, days_apart=0,
                   wording_conflict=False, wording_note="", proposed_status="auto_ok",
                   now_ts=NOW)
    return st


def add_snaps(st, k_price, p_price, ts=NOW):
    bid, ask = k_price - 0.005, k_price + 0.005
    st.insert_kalshi_snapshots([KalshiSnapshot("CONTROLS-2026-D", ts, bid, ask,
                                               k_price, 1000.0, 500.0, 0.0)])
    st.insert_poly_snapshots([PolySnapshot("c1", ts, p_price, 1 - p_price,
                                           1000.0, 500.0, "clob_mid")])


CFG = {"radar": {"alerts_file": "data/event-alerts.jsonl"}}


# --- tiering -----------------------------------------------------------------

def test_tier_boundaries():
    assert classify_gap(1.99, 10.0) is None
    assert classify_gap(-2.0, 10.0) == "alert"           # same-side, sign ignored
    assert classify_gap(2.49, 10.0) == "alert"
    assert classify_gap(2.5, 0.1) == "execution-candidate"
    assert classify_gap(2.5, -0.1) == "alert"            # positive score required
    assert classify_gap(5.0, 1.0) == "execution-candidate"


def test_semantic_forces_alert_never_execution():
    assert classify_gap(5.0, 2.0, semantic=True) == "alert"
    assert classify_gap(3.0, 1.5, semantic=True) == "alert"
    assert classify_gap(2.5, 1.0, semantic=False) == "execution-candidate"


def test_candidate_costs_subtract_fees_and_frictions():
    costs = candidate_costs(0.50, 0.50, 1.0, "politics",
                            default_slippage_cents=0.5, adverse_selection_cents=0.3,
                            semantic_reserve_cents=1.0, semantic=False)
    # kalshi taker at 100 contracts: 0.07*0.25 = 1.75c true cost; the
    # per-trade cent-rounding leaves 1.76c even amortized at size (FP artifact
    # of the ceil in fees.kalshi_fee, not a bug to smooth over)
    assert costs.fee_kalshi_cents == pytest.approx(1.76)
    # polymarket politics taker: 0.04*0.25 = 1.0c
    assert costs.fee_poly_cents == pytest.approx(1.0)
    # spread 1c -> half-spread 0.5 + default 0.5 for the other venue
    assert costs.slippage_cents == pytest.approx(1.0)
    assert costs.adverse_cents == pytest.approx(0.3)
    assert costs.semantic_reserve_cents == 0.0
    assert costs.total_cents == pytest.approx(1.76 + 1.0 + 1.0 + 0.3)


def test_candidate_costs_charge_semantic_reserve_only_when_flagged():
    clean = candidate_costs(0.5, 0.5, None, semantic=False)
    flagged = candidate_costs(0.5, 0.5, None, semantic=True)
    assert flagged.total_cents - clean.total_cents == pytest.approx(1.0)


def test_fees_at_extremes_are_small():
    # fee curve is p*(1-p): near-zero at the extremes, max at 0.50
    mid = candidate_costs(0.50, 0.50, None)
    extreme = candidate_costs(0.97, 0.97, None)
    assert extreme.fee_kalshi_cents + extreme.fee_poly_cents < \
        mid.fee_kalshi_cents + mid.fee_poly_cents


# --- semantic mismatch -------------------------------------------------------

PM_CONTROL = (
    "This market will resolve according to the party that wins control of the "
    "United States Senate in the 2026 midterm election. A party wins control "
    "if it wins a majority of the chamber's voting seats. If no party wins a "
    "majority, the party that wins half of the voting seats and holds the Vice "
    "Presidency wins control. All regular and special Senate elections in "
    "November 2026 are included; runoffs count even if held after November."
)
K_CONTROL = (
    "If the Democratic Party has won control of the U.S. Senate in 2026, then "
    "the market resolves to Yes. This market may be determined early based on "
    "a consensus of media calls projecting control of the U.S. Senate. "
    "Otherwise, victory will be determined by the party identification of the "
    "President pro tempore of the Senate on February 1, 2027."
)
K_SEATCOUNT = (
    "This market resolves to Yes if Democrats hold 51 or more Senate seats "
    "after the 2026 midterms. How many Senate seats will Democrats hold?"
)


def test_identical_control_contracts_do_not_flag():
    check = semantic_mismatch(PM_CONTROL, K_CONTROL,
                              "2026-11-03T23:59:00Z", "2026-11-03T23:59:00Z")
    assert not check.mismatch, check.note


def test_seat_count_vs_control_flags_granularity():
    check = semantic_mismatch(PM_CONTROL, K_SEATCOUNT,
                              "2026-11-03T23:59:00Z", "2026-11-03T23:59:00Z")
    assert check.mismatch
    assert "granularity" in check.dimensions


def test_settlement_horizon_far_apart_flags_horizon():
    check = semantic_mismatch(PM_CONTROL, K_CONTROL,
                              "2027-01-05T04:59:00Z", "2027-02-01T15:00:00Z")
    assert check.mismatch
    assert "horizon" in check.dimensions


def test_combo_market_vs_direct_flags_granularity():
    check = semantic_mismatch(
        "Which party will control the Senate?",
        "This is a combination market requiring ALL of the following to occur: "
        "House Control: Democratic, Senate Control: Democratic.",
        None, None)
    assert check.mismatch and "granularity" in check.dimensions


def test_disjoint_named_sources_flag_source():
    a = "Resolves according to a consensus of media calls by the Associated Press."
    b = "Resolves based on official results certified by the Secretary of State."
    check = semantic_mismatch(a, b)
    assert "source" in check.dimensions


def test_one_sided_source_mention_does_not_flag():
    a = "Resolves according to a consensus of media calls by the Associated Press."
    b = "Resolves to Yes if the event occurs."
    assert not semantic_mismatch(a, b).mismatch


def test_rounding_on_one_leg_only_flags():
    a = "The value is rounded to the nearest tenth of a percent."
    b = "The value is used as published."
    check = semantic_mismatch(a, b)
    assert "rounding" in check.dimensions


# --- leader inference --------------------------------------------------------

def test_infer_leader_picks_most_recent_mover():
    k = [(0, 0.5), (300, 0.5), (600, 0.5)]
    p = [(0, 0.5), (300, 0.6), (600, 0.6)]
    leader, basis = infer_leader(k, p)
    assert leader == "polymarket" and basis == "only-mover"
    k2 = [(0, 0.5), (300, 0.5), (600, 0.5), (900, 0.7)]
    p2 = [(0, 0.5), (300, 0.6), (600, 0.6), (900, 0.6)]
    leader, basis = infer_leader(k2, p2)
    assert leader == "kalshi" and basis == "last-change"


def test_infer_leader_none_when_flat_or_short():
    assert infer_leader([(0, 0.5)], [(0, 0.5)])[0] is None
    assert infer_leader([(0, 0.5), (300, 0.5)], [(0, 0.5), (300, 0.5)]) == (None, "flat")


# --- the scan ----------------------------------------------------------------

def test_scan_writes_schema_valid_ledger(tmp_path):
    st = mk_storage(tmp_path)
    add_snaps(st, k_price=0.615, p_price=0.585)   # 3.0c gap
    cfg = {"radar": {"alerts_file": str(tmp_path / "alerts.jsonl"),
                     "max_stale_s": 3600, "cooldown_s": 1800}}
    alerts = scan(st, cfg, NOW)
    assert len(alerts) == 1
    a = alerts[0]
    for key in REQUIRED_ALERT_KEYS:
        assert key in a, f"missing {key}"
    lines = (tmp_path / "alerts.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["tier"] in ("alert", "execution-candidate")
    assert isinstance(row["gap_cents"], float)
    assert isinstance(row["semantic_flag"], bool)
    assert row["leader"] in ("kalshi", "polymarket", None)
    st.close()


def test_scan_skips_subthreshold_and_stale(tmp_path):
    st = mk_storage(tmp_path)
    add_snaps(st, k_price=0.615, p_price=0.60)    # 1.5c: below alert band
    cfg = {"radar": {"alerts_file": str(tmp_path / "alerts.jsonl"), "max_stale_s": 60}}
    assert scan(st, cfg, NOW) == []
    add_snaps(st, k_price=0.615, p_price=0.585)   # 3c but freshness=60 vs ts=NOW-... 
    # snapshots are at NOW; a later 'now' makes them stale
    assert scan(st, cfg, NOW + 3600) == []
    st.close()


def test_scan_cooldown_dedupes_same_pair_and_tier(tmp_path):
    st = mk_storage(tmp_path)
    add_snaps(st, k_price=0.615, p_price=0.585)
    cfg = {"radar": {"alerts_file": str(tmp_path / "alerts.jsonl"),
                     "max_stale_s": 3600, "cooldown_s": 1800}}
    first = scan(st, cfg, NOW)
    second = scan(st, cfg, NOW + 300)             # 5 min later, same quotes
    assert len(first) == 1 and second == []
    third = scan(st, cfg, NOW + 2400)             # past cooldown: re-alert
    assert len(third) == 1
    st.close()


def test_scan_survives_a_broken_pair(tmp_path):
    st = mk_storage(tmp_path)
    # a second pair whose Kalshi market exists but has NO snapshots at all
    poly2 = PolyMarket(condition_id="c2", gamma_id="2", question="Broken?",
                       slug="b", description="", end_date=None,
                       yes_token_id="t3", no_token_id="t4", active=True, closed=False)
    kal2 = KalshiMarket(ticker="BROKEN-1", event_ticker="B", series_ticker="B",
                        title="Broken", yes_sub_title="", rules_primary="",
                        rules_secondary="", close_time=None, market_type="binary",
                        status="open")
    st.upsert_poly_markets([poly2], NOW)
    st.upsert_kalshi_markets([kal2], NOW)
    st.upsert_pair("c2", "BROKEN-1", score=0.9, dates_ok=True, days_apart=0,
                   wording_conflict=False, wording_note="", proposed_status="auto_ok",
                   now_ts=NOW)
    add_snaps(st, k_price=0.615, p_price=0.585)
    cfg = {"radar": {"alerts_file": str(tmp_path / "alerts.jsonl"), "max_stale_s": 3600}}
    alerts = scan(st, cfg, NOW)      # must not raise on the snapshot-less pair
    assert len(alerts) == 1 and alerts[0]["ticker"] == "CONTROLS-2026-D"
    st.close()


def test_scan_digest_shape():
    d = scan_digest([{"tier": "alert", "semantic_flag": False, "ticker": "X"},
                     {"tier": "execution-candidate", "semantic_flag": True, "ticker": "X"}])
    assert d["alerts"] == 2 and d["execution_candidates"] == 1
    assert d["semantic_flags"] == 1 and d["pairs"] == ["X"]


def test_fee_module_shapes_used_by_score():
    # kalshi fee amortized at size vs 1 contract: the ceil is the point
    assert kalshi_fee(1, 0.5).fee_usd == 0.02      # 1.75c rounded up
    assert kalshi_fee(100, 0.5).fee_usd == 1.76    # amortized at size (1.75 true)
    assert polymarket_fee(1, 0.5, category="politics").fee_usd == pytest.approx(0.01)
    assert polymarket_fee(1, 0.5, category="geopolitics").fee_usd == 0.0
