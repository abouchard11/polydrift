import math

import pytest

from polydrift.fees import (
    KALSHI_TAKER_RATE, POLYMARKET_TAKER_RATES,
    breakeven_move, kalshi_fee, polymarket_fee, round_trip_cost,
)


def test_polymarket_formula_matches_published_rate_table():
    """fee = C * rate * p * (1-p); docs.polymarket.com/trading/fees, 2026-09-05."""
    q = polymarket_fee(100, 0.50, "politics")
    assert q.rate == 0.04
    assert q.fee_usd == pytest.approx(100 * 0.04 * 0.5 * 0.5)


def test_polymarket_geopolitics_is_fee_free():
    assert polymarket_fee(1000, 0.5, "geopolitics").fee_usd == 0.0


def test_polymarket_makers_are_never_charged():
    assert polymarket_fee(1000, 0.5, "crypto", side="maker").fee_usd == 0.0


def test_unknown_category_falls_back_to_the_general_rate():
    assert polymarket_fee(100, 0.5, "nonsense").rate == 0.05


def test_fee_peaks_at_even_money_and_vanishes_at_the_extremes():
    """p*(1-p) shape: a divergence signal near 0.50 is the most expensive to act on."""
    mid = polymarket_fee(100, 0.50, "sports").fee_usd
    edge = polymarket_fee(100, 0.99, "sports").fee_usd
    assert mid > edge
    assert polymarket_fee(100, 1.0, "sports").fee_usd == pytest.approx(0.0)


def test_kalshi_rounds_up_to_the_whole_cent():
    """round up(M x 0.07 x C x P x (1-P)); kalshi.com fee schedule, 7.7.26."""
    raw = 1 * KALSHI_TAKER_RATE * 1 * 0.5 * 0.5   # 0.0175
    q = kalshi_fee(1, 0.5)
    assert raw == pytest.approx(0.0175)
    assert q.fee_usd == 0.02
    assert q.fee_usd == math.ceil(raw * 100) / 100


def test_kalshi_maker_multiplier_defaults_to_zero():
    assert kalshi_fee(100, 0.5, side="maker").fee_usd == 0.0
    assert kalshi_fee(100, 0.5, side="maker", multiplier=1.0).fee_usd > 0


def test_hold_to_resolution_pays_no_exit_fee():
    entry_only = round_trip_cost("polymarket", 100, 0.9, None, category="politics")
    round_trip = round_trip_cost("polymarket", 100, 0.9, 0.9, category="politics")
    assert round_trip == pytest.approx(2 * entry_only)


def test_breakeven_at_one_contract_overstates_kalshi_cost():
    """Regression: quoting breakeven at 1 contract charges the rounded-up cent
    against the true 0.0175 cost and inflates the bar."""
    at_one = breakeven_move("kalshi", 0.5, contracts=1)
    at_size = breakeven_move("kalshi", 0.5, contracts=1000)
    assert at_one > at_size
    assert at_size == pytest.approx(2 * 0.0175, rel=1e-3)


def test_breakeven_rejects_zero_size():
    with pytest.raises(ValueError):
        breakeven_move("kalshi", 0.5, contracts=0)


def test_price_outside_probability_range_is_rejected():
    with pytest.raises(ValueError):
        polymarket_fee(10, 1.5, "sports")
