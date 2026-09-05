import datetime as dt

from polydrift.config import DEFAULTS
from polydrift.models import KalshiMarket, PolyMarket
from polydrift.pairs import dates_agree, refresh_pairs, wording_conflict
from polydrift.storage import Storage

# --- The Maduro trap, verbatim in spirit from the July 2026 live catch --------

POLY_MADURO_RULES = (
    "This market will resolve to Yes if Nicolas Maduro is the official President "
    "of Venezuela on December 31, 2026. For the purposes of this market, the "
    "individual who officially holds the title of President will be considered "
    "the leader, even if another person or group exercises de facto control."
)
KALSHI_MADURO_RULES = (
    "If Nicolas Maduro holds de facto head of state of Venezuela on Dec 31, 2026, "
    "then the market resolves Yes. Caracas is the official seat of power of "
    "Venezuela. The person who effectively exercises power will be considered "
    "head of state regardless of formal title."
)


def test_maduro_official_vs_de_facto_is_a_conflict():
    """REGRESSION. An 81-point gap that was two different bets. Must never
    auto-approve."""
    check = wording_conflict(POLY_MADURO_RULES, KALSHI_MADURO_RULES)
    assert check.conflict, check.note
    assert "official" in check.note and "defacto" in check.note


def test_denylisted_official_seat_of_power_does_not_flip_kalshi_to_official():
    """REGRESSION. Kalshi's own text says 'official seat of power' about the
    CAPITAL, not the resolution basis. The first version of the detector read
    that as an official-basis marker and cleared the pair."""
    kalshi_only_defacto = "If X holds de facto head of state. Caracas is the official seat of power."
    poly_official = "Resolves on whoever officially holds the title."
    assert wording_conflict(poly_official, kalshi_only_defacto).conflict


def test_clean_pair_has_no_conflict():
    poly = "Resolves Yes if the Republican Party holds a majority of House seats."
    kalshi = "Resolves Yes if Republicans control the House after the midterms."
    assert not wording_conflict(poly, kalshi).conflict


def test_both_sides_official_is_fine():
    assert not wording_conflict("official title holder", "officially recognized president").conflict


def test_dates_agree_within_tolerance():
    ok, days = dates_agree("2026-12-31T00:00:00Z", "2027-01-15T15:00:00Z", 45)
    assert ok and days == 15
    ok, days = dates_agree("2026-12-31T00:00:00Z", "2028-01-01T00:00:00Z", 45)
    assert not ok and days > 300
    assert dates_agree(None, "2027-01-01T00:00:00Z", 45) == (False, None)


# --- Registry behaviour -----------------------------------------------------

def _seed(st: Storage, *, poly_q: str, poly_rules: str, poly_end: str,
          k_ticker: str, k_title: str, k_sub: str, k_rules: str, k_close: str):
    """Mirror production: the matcher sees an EVENT title in the index, then
    fetches that event's legs. Returns the fetcher to pass to refresh_pairs."""
    st.upsert_poly_markets([PolyMarket(
        condition_id="c1", gamma_id="1", question=poly_q, slug="s", description=poly_rules,
        end_date=poly_end, yes_token_id="t1", no_token_id="t2", active=True, closed=False,
    )], 1000)
    event_ticker = k_ticker.rsplit("-", 1)[0] if "-" in k_ticker else k_ticker
    st.upsert_kalshi_events([{"event_ticker": event_ticker, "series_ticker": "S",
                              "title": k_title, "sub_title": "", "category": "Politics"}], 1000)
    leg = {"ticker": k_ticker, "event_ticker": event_ticker, "title": k_title,
           "yes_sub_title": k_sub, "rules_primary": k_rules, "rules_secondary": "",
           "close_time": k_close, "market_type": "binary", "status": "active"}
    calls = []

    def fetch(et: str):
        calls.append(et)
        assert et == event_ticker
        return {"event_ticker": event_ticker, "series_ticker": "S", "title": k_title}, [leg]
    fetch.calls = calls
    return fetch


def test_maduro_pair_lands_in_needs_review_not_auto_ok(tmp_path):
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will Maduro be the leader of Venezuela end of 2026?",
          poly_rules=POLY_MADURO_RULES, poly_end="2026-12-31T00:00:00Z",
          k_ticker="KXVEN-26-MAD", k_title="Who will hold de facto head of state of Venezuela end of 2026?",
          k_sub="Nicolas Maduro", k_rules=KALSHI_MADURO_RULES, k_close="2026-12-31T23:59:00Z")
    counts = refresh_pairs(st, DEFAULTS, now_ts=2000, fetch_event_markets=fetch)
    assert counts["candidates"] == 1
    assert counts["events_fetched"] == 1
    pair = st.list_pairs()[0]
    assert pair["status"] == "needs_review"
    assert pair["wording_conflict"] == 1
    assert st.usable_pairs() == []
    st.close()


def test_clean_high_score_pair_auto_clears(tmp_path):
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will the Republican Party control the House after the 2026 Midterm Election?",
          poly_rules="Resolves Yes if Republicans hold a House majority.", poly_end="2026-11-03T00:00:00Z",
          k_ticker="KXHOUSE-26-R", k_title="Which party will control the House after the 2026 Midterm Election?",
          k_sub="Republican Party", k_rules="Resolves Yes if the Republican Party controls the House.",
          k_close="2026-11-10T00:00:00Z")
    refresh_pairs(st, DEFAULTS, now_ts=2000, fetch_event_markets=fetch)
    pair = st.list_pairs()[0]
    assert pair["status"] == "auto_ok", dict(pair)
    assert len(st.usable_pairs()) == 1
    st.close()


def test_human_approval_survives_a_refresh(tmp_path):
    """A re-match must never silently undo a decision a human made after
    reading the rules."""
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will Maduro be the leader of Venezuela end of 2026?",
          poly_rules=POLY_MADURO_RULES, poly_end="2026-12-31T00:00:00Z",
          k_ticker="KXVEN-26-MAD", k_title="Who will hold de facto head of state of Venezuela end of 2026?",
          k_sub="Nicolas Maduro", k_rules=KALSHI_MADURO_RULES, k_close="2026-12-31T23:59:00Z")
    refresh_pairs(st, DEFAULTS, now_ts=2000, fetch_event_markets=fetch)
    pid = st.list_pairs()[0]["id"]
    assert st.set_pair_status(pid, "rejected", 3000)
    counts = refresh_pairs(st, DEFAULTS, now_ts=4000, fetch_event_markets=fetch)
    assert counts["kept_decision"] == 1
    assert st.get_pair(pid)["status"] == "rejected"
    st.close()


def test_year_mismatch_is_never_a_candidate(tmp_path):
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will Real Madrid win the 2026-27 UEFA Champions League?",
          poly_rules="", poly_end="2027-06-01T00:00:00Z",
          k_ticker="KXUCL-28-RMA", k_title="Will Real Madrid win the 2028 UEFA Champions League?",
          k_sub="", k_rules="", k_close="2028-06-01T00:00:00Z")
    counts = refresh_pairs(st, DEFAULTS, now_ts=2000, fetch_event_markets=fetch)
    assert counts["candidates"] == 0
    assert fetch.calls == []        # never even fetched the legs
    st.close()


def test_rejected_pairs_are_not_snapshotted_but_others_are(tmp_path):
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will the Republican Party control the House after the 2026 Midterm Election?",
          poly_rules="", poly_end="2026-11-03T00:00:00Z",
          k_ticker="KXHOUSE-26-R", k_title="Which party will control the House after the 2026 Midterm Election?",
          k_sub="Republican Party", k_rules="", k_close="2026-11-10T00:00:00Z")
    refresh_pairs(st, DEFAULTS, now_ts=2000, fetch_event_markets=fetch)
    assert st.pair_tickers() == ["KXHOUSE-26-R"]
    st.set_pair_status(st.list_pairs()[0]["id"], "rejected", 3000)
    assert st.pair_tickers() == []
    assert st.pair_tickers(include_rejected=True) == ["KXHOUSE-26-R"]
    st.close()


def test_refresh_without_a_fetcher_writes_no_pairs(tmp_path):
    st = Storage(tmp_path / "t.db")
    _seed(st,
          poly_q="Will the Republican Party control the House after the 2026 Midterm Election?",
          poly_rules="", poly_end="2026-11-03T00:00:00Z",
          k_ticker="KXHOUSE-26-R", k_title="Which party will control the House after the 2026 Midterm Election?",
          k_sub="Republican Party", k_rules="", k_close="2026-11-10T00:00:00Z")
    counts = refresh_pairs(st, DEFAULTS, now_ts=2000)     # offline: no fetcher
    assert counts["candidates"] == 0 and counts["events_scanned"] == 1
    st.close()


def test_win_vs_qualify_is_a_conflict():
    """7 of the first 87 live candidates on 2026-09-05 were this family."""
    from polydrift.pairs import title_outcome_conflict
    assert title_outcome_conflict("Will Spain win the UEFA EURO 2028?",
                                  "Will Spain qualify for the 2028 UEFA Euros? Spain").conflict
    assert title_outcome_conflict(
        "Will Luiz Inácio Lula da Silva win the 2026 Brazilian presidential election?",
        "Will Lula qualify for the runoff in the 2026 Brazilian presidential election?").conflict
    assert not title_outcome_conflict("Will Spain win the UEFA EURO 2028?",
                                      "Will Spain win the 2028 UEFA Euros? Spain").conflict


def test_win_vs_qualify_blocks_auto_ok_in_the_registry(tmp_path):
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will Spain win the UEFA EURO 2028?", poly_rules="", poly_end="2028-07-15T00:00:00Z",
          k_ticker="KXEUROQ-28-ESP", k_title="Will Spain qualify for the 2028 UEFA Euros?",
          k_sub="Spain", k_rules="", k_close="2028-07-20T00:00:00Z")
    refresh_pairs(st, DEFAULTS, now_ts=2000, fetch_event_markets=fetch)
    pair = st.list_pairs()[0]
    assert pair["wording_conflict"] == 1 and pair["status"] == "needs_review"
    assert "partial" in pair["wording_note"]
    st.close()
