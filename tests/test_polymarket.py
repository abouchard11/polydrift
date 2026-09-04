from polydrift.polymarket import PolymarketClient

# Shape captured from a live Gamma response, 2026-09-04.
RAW = {
    "id": "2063134",
    "conditionId": "0x7d0aaf81",
    "question": "Will X happen?",
    "slug": "will-x-happen",
    "description": "Resolution criteria...",
    "endDate": "2027-01-01T00:00:00Z",
    "clobTokenIds": '["27146956652877944551877724690365745048", "33216695217861742195941369663873573"]',
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.0035", "0.9965"]',
    "volumeNum": 83454457.9,
    "liquidityNum": 16711.7,
    "active": True,
    "closed": False,
}


def test_parses_double_encoded_json_string_arrays():
    m = PolymarketClient.parse_market(RAW)
    assert m is not None
    assert m.yes_token_id == "27146956652877944551877724690365745048"
    assert m.no_token_id == "33216695217861742195941369663873573"


def test_maps_prices_by_outcome_label_not_position():
    raw = dict(RAW, outcomes='["No", "Yes"]', outcomePrices='["0.9965", "0.0035"]')
    snap = PolymarketClient.parse_snapshot(raw, ts=1000)
    assert snap.yes_price == 0.0035
    assert snap.no_price == 0.9965


def test_refuses_to_guess_on_multi_outcome_markets():
    raw = dict(RAW, outcomes='["A", "B", "C"]', outcomePrices='["0.2", "0.3", "0.5"]')
    snap = PolymarketClient.parse_snapshot(raw, ts=1000)
    assert snap.yes_price is None and snap.no_price is None


def test_tolerates_already_decoded_arrays():
    raw = dict(RAW, outcomes=["Yes", "No"], outcomePrices=["0.4", "0.6"])
    snap = PolymarketClient.parse_snapshot(raw, ts=1000)
    assert snap.yes_price == 0.4
