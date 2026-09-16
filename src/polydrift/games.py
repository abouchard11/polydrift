"""Deterministic pairing for game (moneyline) markets.

Why this exists: the day-7 read (2026-09-16) showed the fuzzy-matched, human-
approved panel — Euro 2028 winners, Champions League, chess — is dead water:
Polymarket's price did not move in 92-98% of 1h-24h windows, so there was
nothing to measure. Game markets are the opposite: identical moneyline rules
on both venues, prices that move all day, resolution every night, dozens of
new pairs a week.

Both venues encode the match key in their identifiers, so no Jaccard is
involved and the pair auto-clears:

    Polymarket  slug  mlb-sea-laa-2026-09-15   (league-away-home-localdate)
                outcomes ["Seattle Mariners", "Los Angeles Angels"] — the
                FIRST outcome is the recorded yes_price (positional fallback)
    Kalshi      event KXMLBGAME-26SEP151840SEALAA, legs -SEA / -LAA
                rules "If Seattle wins the ... game originally scheduled for
                Sep 15, 2026 ... resolves to Yes"

Pair = Polymarket market <-> the Kalshi leg for Polymarket's first-listed team.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .kalshi import KalshiClient
from .matching import tokens
from .storage import Storage

log = logging.getLogger(__name__)

LEAGUE_SERIES: dict[str, str] = {
    "mlb": "KXMLBGAME", "nfl": "KXNFLGAME", "nba": "KXNBAGAME", "nhl": "KXNHLGAME",
    "ncaaf": "KXNCAAFGAME", "epl": "KXEPLGAME",
}
_SLUG_RX = re.compile(r"^(?P<league>[a-z]+)-(?P<t0>[a-z0-9]+)-(?P<t1>[a-z0-9]+)-(?P<date>\d{4}-\d{2}-\d{2})$")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


@dataclass(frozen=True)
class GameKey:
    league: str
    team0: str          # Polymarket's first-listed team code (its yes side)
    team1: str
    date: dt.date
    team0_name: str     # from the question, e.g. "Seattle Mariners"

    @property
    def kalshi_prefix(self) -> str | None:
        series = LEAGUE_SERIES.get(self.league)
        if not series:
            return None
        return f"{series}-{self.date.strftime('%y')}{_MONTHS[self.date.month - 1]}{self.date.day:02d}"


def parse_poly_game(slug: str, question: str) -> GameKey | None:
    m = _SLUG_RX.match(slug or "")
    if not m or " vs. " not in (question or ""):
        return None
    try:
        date = dt.date.fromisoformat(m["date"])
    except ValueError:
        return None
    return GameKey(m["league"], m["t0"], m["t1"], date, question.split(" vs. ")[0].strip())


def is_game_market(slug: str, question: str) -> bool:
    return parse_poly_game(slug, question) is not None


def pick_leg(key: GameKey, legs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The Kalshi leg for Polymarket's first-listed team. Ticker suffix first
    (SEA == sea); fall back to the leg subtitle being contained in the team
    name ("Seattle" in "Seattle Mariners") for venues that abbreviate
    differently (Kalshi ATH / Polymarket oak for the A's)."""
    for leg in legs:
        suffix = str(leg.get("ticker", "")).rsplit("-", 1)[-1].lower()
        if suffix == key.team0:
            return leg
    name_tokens = tokens(key.team0_name)
    for leg in legs:
        sub = tokens(str(leg.get("yes_sub_title") or ""))
        if sub and sub <= name_tokens:
            return leg
    return None


def refresh_game_pairs(
    storage: Storage,
    cfg: Mapping[str, Any],
    now_ts: int,
    fetch_event_markets: Callable[[str], tuple[dict[str, Any], list[dict[str, Any]]]] | None,
) -> dict[str, int]:
    """Pair every unpaired Polymarket game market with its Kalshi leg using
    the identifiers alone. Only fetches legs for markets with no pair yet, so
    it is cheap enough to run every cycle."""
    counts = {"game_markets": 0, "already_paired": 0, "no_kalshi_event": 0,
              "no_leg": 0, "paired": 0}
    if fetch_event_markets is None:
        return counts
    rows = storage.conn.execute(
        """SELECT m.condition_id, m.slug, m.question,
                  (SELECT COUNT(*) FROM kalshi_pairs p WHERE p.condition_id = m.condition_id AND p.score >= 1.0) AS n_pairs
           FROM poly_markets m WHERE m.question LIKE '% vs. %'"""
    ).fetchall()
    for r in rows:
        key = parse_poly_game(r["slug"], r["question"])
        if key is None or key.kalshi_prefix is None:
            continue
        counts["game_markets"] += 1
        if r["n_pairs"]:
            counts["already_paired"] += 1
            continue
        both = {key.team0.upper(), key.team1.upper()}
        events = storage.conn.execute(
            "SELECT event_ticker FROM kalshi_events WHERE event_ticker LIKE ?",
            (key.kalshi_prefix + "%",),
        ).fetchall()
        # The event ticker ends with the two team codes concatenated; require
        # both to appear so a doubleheader or a same-day game elsewhere can't
        # collide.
        hits = [e["event_ticker"] for e in events
                if all(code in e["event_ticker"].split("-")[1] for code in both)]
        if not hits:
            counts["no_kalshi_event"] += 1
            continue
        try:
            event, legs = fetch_event_markets(hits[0])
        except Exception as exc:
            log.warning("game event fetch failed for %s: %s", hits[0], exc)
            continue
        leg = pick_leg(key, legs)
        if leg is None:
            counts["no_leg"] += 1
            log.info("game %s: no leg matched %s among %s", r["slug"], key.team0,
                     [l.get("ticker") for l in legs])
            continue
        km = KalshiClient.parse_market(event, leg)
        if km is None:
            continue
        storage.upsert_kalshi_markets([km], now_ts)
        storage.upsert_pair(
            r["condition_id"], km.ticker, score=1.0, dates_ok=True, days_apart=0,
            wording_conflict=False, wording_note="moneyline: identifiers match on both venues",
            proposed_status="auto_ok", now_ts=now_ts,
        )
        counts["paired"] += 1
    if counts["paired"] or counts["no_leg"]:
        log.info("game pairs: %s", counts)
    return counts
