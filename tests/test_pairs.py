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


def test_decisions_file_applies_and_is_idempotent(tmp_path):
    from polydrift.pairs import apply_decisions, load_decisions
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will Spain win the UEFA EURO 2028?", poly_rules="", poly_end="2028-07-15T00:00:00Z",
          k_ticker="KXUEFAEURO-28-ESP", k_title="Euro 2028 champion?",
          k_sub="Spain", k_rules="", k_close="2028-08-15T00:00:00Z")
    from polydrift.config import deep_merge
    isolated = deep_merge(DEFAULTS, {"pairs": {"decisions_file": str(tmp_path / "none.yaml")}})
    refresh_pairs(st, isolated, now_ts=2000, fetch_event_markets=fetch)
    assert st.list_pairs()[0]["status"] == "needs_review"      # score 0.50: needs a human

    dec_path = tmp_path / "decisions.yaml"
    dec_path.write_text("approve:\n  - ticker: KXUEFAEURO-28-ESP\n    reason: same bet\n"
                        "reject:\n  - ticker: KXNOPE-1\n    reason: not in registry\n")
    dec = load_decisions(str(dec_path))
    counts = apply_decisions(st, dec, now_ts=3000)
    assert counts == {"approved": 1, "rejected": 0, "unmatched": 1, "ambiguous": 0}
    assert st.usable_pairs()[0]["ticker"] == "KXUEFAEURO-28-ESP"
    assert apply_decisions(st, dec, now_ts=4000) == {"approved": 0, "rejected": 0, "unmatched": 1, "ambiguous": 0}
    st.close()


def test_decisions_are_reapplied_on_refresh(tmp_path, monkeypatch):
    """Decisions ship with the code. A fresh volume + a refresh must land them
    without anyone typing --approve."""
    from polydrift.config import deep_merge
    st = Storage(tmp_path / "t.db")
    fetch = _seed(st,
          poly_q="Will Spain win the UEFA EURO 2028?", poly_rules="", poly_end="2028-07-15T00:00:00Z",
          k_ticker="KXUEFAEURO-28-ESP", k_title="Will Spain win the 2028 UEFA Euros?",
          k_sub="Spain", k_rules="", k_close="2028-08-15T00:00:00Z")
    dec_path = tmp_path / "d.yaml"
    dec_path.write_text("approve:\n  - ticker: KXUEFAEURO-28-ESP\n")
    cfg = deep_merge(DEFAULTS, {"pairs": {"decisions_file": str(dec_path)}})
    counts = refresh_pairs(st, cfg, now_ts=2000, fetch_event_markets=fetch)
    assert counts["decisions_applied"] == 1
    assert st.list_pairs()[0]["status"] == "approved"
    st.close()


def test_missing_decisions_file_is_not_an_error(tmp_path):
    from polydrift.pairs import load_decisions
    assert load_decisions(str(tmp_path / "nope.yaml")) == {"approve": [], "reject": []}


def test_ambiguous_approve_fails_closed(tmp_path):
    """REGRESSION 2026-09-05: a Kalshi leg that matched two Polymarket markets
    was approved by ticker, blessing the cross pair (a wrong bet)."""
    from polydrift.pairs import apply_decisions
    st = Storage(tmp_path / "t.db")
    for cid, q in (("cA", "Will Gukesh win the 2026 World Chess Championship?"),
                   ("cB", "Will Sindarov win the 2026 World Chess Championship?")):
        st.upsert_poly_markets([PolyMarket(condition_id=cid, gamma_id="1", question=q, slug="s",
            description="", end_date="2027-01-01T00:00:00Z", yes_token_id="t", no_token_id="n",
            active=True, closed=False)], 1)
    st.upsert_kalshi_markets([KalshiMarket(ticker="KXCHESS-GDOM", event_ticker="E", series_ticker="S",
        title="World Chess Champion", yes_sub_title="Gukesh", rules_primary="", rules_secondary="",
        close_time="2028-12-30T00:00:00Z", market_type="binary", status="active")], 1)
    for cid, sc in (("cA", 0.55), ("cB", 0.31)):
        st.upsert_pair(cid, "KXCHESS-GDOM", score=sc, dates_ok=False, days_apart=700,
                       wording_conflict=False, wording_note="", proposed_status="needs_review", now_ts=1)
    counts = apply_decisions(st, {"approve": [{"ticker": "KXCHESS-GDOM"}]}, 2)
    assert counts["ambiguous"] == 1 and counts["approved"] == 0
    assert st.usable_pairs() == []
    counts = apply_decisions(st, {"approve": [{"ticker": "KXCHESS-GDOM", "condition_id": "cA"}]}, 3)
    assert counts["approved"] == 1
    assert [(r["condition_id"], r["status"]) for r in st.list_pairs()] == [("cA", "approved"), ("cB", "needs_review")]
    st.close()


def test_unquoted_hex_condition_id_is_rejected_loudly(tmp_path):
    """PyYAML (YAML 1.1) scans 0x... as an int. Silent mismatch otherwise."""
    import pytest
    from polydrift.pairs import load_decisions
    f = tmp_path / "d.yaml"
    f.write_text("approve:\n  - ticker: T\n    condition_id: 0xb8b5ba72\n")
    with pytest.raises(ValueError, match="quoted string"):
        load_decisions(str(f))
    f.write_text("approve:\n  - ticker: T\n    condition_id: \"0xb8b5ba72\"\n")
    assert load_decisions(str(f))["approve"][0]["condition_id"] == "0xb8b5ba72"


def test_tracked_tickers_exclude_weak_unreviewed_candidates(tmp_path):
    """2026-09-08: tracking every candidate grew 51 -> 516 tickers in 3 days
    and tripped Kalshi's burst limit every cycle."""
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([PolyMarket(condition_id="c1", gamma_id="1", question="Q?", slug="s",
        description="", end_date="2027-01-01T00:00:00Z", yes_token_id="t", no_token_id="n",
        active=True, closed=False)], 1)
    for tk, sc, conflict, status in (("K-STRONG", 0.6, False, "needs_review"),
                                     ("K-WEAK", 0.31, False, "needs_review"),
                                     ("K-CONFLICT", 0.9, True, "needs_review"),
                                     ("K-APPROVED", 0.4, False, "approved"),
                                     ("K-AUTO", 0.8, False, "auto_ok")):
        st.upsert_kalshi_markets([KalshiMarket(ticker=tk, event_ticker="E", series_ticker="S",
            title="T", yes_sub_title="", rules_primary="", rules_secondary="",
            close_time="2027-01-01T00:00:00Z", market_type="binary", status="active")], 1)
        st.upsert_pair("c1", tk, score=sc, dates_ok=True, days_apart=0, wording_conflict=conflict,
                       wording_note="", proposed_status=status, now_ts=1)
    assert sorted(st.pair_tickers()) == ["K-APPROVED", "K-AUTO", "K-STRONG"]
    assert len(st.pair_tickers(min_review_score=None)) == 5
    st.close()


def test_stale_events_are_pruned_after_a_sweep(tmp_path):
    st = Storage(tmp_path / "t.db")
    st.upsert_kalshi_events([{"event_ticker": "OLD", "title": "old"}], now_ts=100)
    st.upsert_kalshi_events([{"event_ticker": "NEW", "title": "new"}], now_ts=200)
    assert st.prune_stale_events(seen_after_ts=200) == 1
    rows = [r[0] for r in st.conn.execute("SELECT event_ticker FROM kalshi_events")]
    assert rows == ["NEW"]
    st.close()


def test_sweep_fetch_budget_goes_to_the_strongest_events(tmp_path):
    from polydrift.config import deep_merge
    st = Storage(tmp_path / "t.db")
    st.upsert_poly_markets([PolyMarket(condition_id="c1", gamma_id="1",
        question="Will Spain win the UEFA EURO 2028?", slug="s", description="",
        end_date="2028-07-15T00:00:00Z", yes_token_id="t", no_token_id="n", active=True, closed=False)], 1)
    st.upsert_kalshi_events([
        {"event_ticker": "STRONG", "title": "Will Spain win the 2028 UEFA Euros?", "sub_title": ""},
        {"event_ticker": "WEAK", "title": "Euro 2028 top scorer", "sub_title": "Spain"},
    ], now_ts=1)
    calls = []
    def fetch(et):
        calls.append(et)
        return {"event_ticker": et, "title": "x"}, []
    cfg = deep_merge(DEFAULTS, {"pairs": {"max_event_fetches_per_sweep": 1,
                                          "decisions_file": str(tmp_path / "none.yaml")}})
    counts = refresh_pairs(st, cfg, now_ts=2, fetch_event_markets=fetch)
    assert calls == ["STRONG"]
    assert counts["fetch_capped"] == 1
    st.close()
