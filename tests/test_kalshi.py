from polydrift.kalshi import KalshiClient, is_combo

# Shape captured from a live Kalshi trade-api v2 response, 2026-09-04.
EVENT = {"event_ticker": "KXNEWPOPE-70", "series_ticker": "KXNEWPOPE",
         "title": "Who will the next Pope be?"}
MARKET = {
    "ticker": "KXNEWPOPE-70-PPIZ",
    "event_ticker": "KXNEWPOPE-70",
    "title": "Who will the next Pope be?",
    "yes_sub_title": "Pierbattista Pizzaballa",
    "rules_primary": "If Pierbattista Pizzaballa becomes the first person elected Pope...",
    "rules_secondary": "",
    "close_time": "2070-01-01T15:00:00Z",
    "market_type": "binary",
    "status": "active",
    "yes_bid_dollars": "0.0300",
    "yes_ask_dollars": "0.0450",
    "last_price_dollars": "0.0450",
    "volume_fp": 19586.62,
    "open_interest_fp": 2945.75,
    "liquidity_dollars": "0.0000",
}


def test_reads_the_dollar_suffixed_fields():
    snap = KalshiClient.parse_snapshot(MARKET, ts=1000)
    assert snap.yes_bid == 0.03
    assert snap.yes_ask == 0.045
    assert snap.last_price == 0.045
    assert snap.volume == 19586.62
    assert snap.open_interest == 2945.75


def test_legacy_field_names_are_absent_and_must_not_be_relied_on():
    """Regression: Kalshi dropped yes_bid/yes_ask/volume. A client written
    against those names records NULLs forever without ever erroring."""
    assert "yes_bid" not in MARKET and "volume" not in MARKET
    legacy_only = {"ticker": "T", "yes_bid": 3, "yes_ask": 5, "volume": 100}
    snap = KalshiClient.parse_snapshot(legacy_only, ts=1)
    assert snap.yes_bid is None and snap.volume is None


def test_combo_markets_are_excluded():
    assert is_combo("KXMVECROSSCATEGORY-SHARD1-S2026C5003BABA00-C3349EF9713")
    assert is_combo("KXMVE-FOO")
    assert not is_combo("KXNEWPOPE-70-PPIZ")


def test_parse_market_prefers_per_leg_subtitle():
    m = KalshiClient.parse_market(EVENT, MARKET)
    assert m.series_ticker == "KXNEWPOPE"
    assert m.yes_sub_title == "Pierbattista Pizzaballa"
    assert m.rules_primary
