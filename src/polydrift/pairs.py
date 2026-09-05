"""Cross-venue pair registry with a human review gate.

A pair is a (Polymarket market, Kalshi market) that LOOKS like the same
question. Looking alike is cheap; resolving alike is the whole game. The
canonical trap, caught live in July 2026 and encoded below as a regression
test:

    Polymarket: "Will Maduro be the leader of Venezuela end of 2026?"  (82c)
    Kalshi:     "...de facto hold head of state..."                    (1.5c)

An 81-point gap that looked like the trade of the year. Polymarket resolves on
who OFFICIALLY holds the title (with an explicit carve-out that de facto control
doesn't count against the official holder); Kalshi resolves on who ACTUALLY
exercises power. Two different bets that normally agree and fork hardest during
a live regime crisis — the exact moment a naive matcher is most confidently
wrong.

So: every candidate is written to the registry, including the bad ones. A pair
becomes USABLE only when it auto-clears (high score + dates agree + no wording
conflict) or a human approves it after reading both venues' rules side by side.
The conflict detector is defence in depth, not a substitute for reading.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .kalshi import KalshiClient
from .matching import title_score, year_tokens
from .storage import Storage

log = logging.getLogger(__name__)

# Phrases that indicate the resolution BASIS. Opposing families on the two
# sides of a pair = conflict. Order within a family doesn't matter.
OFFICIAL_MARKERS = (
    "official", "officially", "de jure", "formally", "formal title",
    "holds the title", "hold the title", "official holder",
)
DEFACTO_MARKERS = (
    "de facto", "defacto", "effectively", "in practice", "actually",
    "exercises power", "exercise power", "holds power", "hold power",
    "actual control", "actually controls", "actually exercises",
)

# Phrases to strip BEFORE scanning: known false positives where a marker word
# describes something other than the resolution basis. Kalshi's Venezuela rules
# say "official seat of power" about the capital city, not about who wins.
DENYLIST_PHRASES = (
    # negated official-basis phrases: these ASSERT de facto, not official
    "regardless of formal title",
    "regardless of official title",
    "regardless of the official",
    "regardless of official",
    "irrespective of formal title",
    "irrespective of official",
    "official seat of power",
    "official capital",
    "official website",
    "official source",
    "official sources",
    "official announcement",
    "official results",
    "official result",
    "officially announced",
    "officially announces",
    "officially reported",
)


# Outcome-verb families for TITLES. "Will Spain win Euro 2028?" and "Will Spain
# qualify for Euro 2028?" score identically on token overlap and are always
# different bets. Observed on 7 of the first 87 live candidates (2026-09-05).
WIN_MARKERS = ("win", "wins", "winner", "champion", "champions", "title")
PARTIAL_MARKERS = ("qualify", "qualifies", "qualif", "advance", "advances", "runoff",
                   "run-off", "reach", "reaches", "make the", "top 4", "top four",
                   "podium", "finish in", "semifinal", "semi-final", "final four",
                   "playoff", "playoffs")


def title_outcome_conflict(poly_question: str, kalshi_title: str) -> WordingCheck:
    """One title asks about WINNING, the other about getting PART of the way."""
    p, k = (poly_question or "").lower(), (kalshi_title or "").lower()
    p_win, p_part = _has_any(p, WIN_MARKERS), _has_any(p, PARTIAL_MARKERS)
    k_win, k_part = _has_any(k, WIN_MARKERS), _has_any(k, PARTIAL_MARKERS)
    p_kind = "win" if (p_win and not p_part) else ("partial" if p_part else None)
    k_kind = "win" if (k_win and not k_part) else ("partial" if k_part else None)
    if p_kind and k_kind and p_kind != k_kind:
        return WordingCheck(True, f"polymarket asks '{p_kind}' ({', '.join(p_win or p_part)}); "
                                  f"kalshi asks '{k_kind}' ({', '.join(k_win or k_part)})")
    return WordingCheck(False, "")


@dataclass(frozen=True)
class WordingCheck:
    conflict: bool
    note: str


def _scrub(text: str) -> str:
    text = (text or "").lower()
    for phrase in DENYLIST_PHRASES:
        text = text.replace(phrase, " ")
    return text


def _has_any(text: str, markers: Sequence[str]) -> list[str]:
    return [m for m in markers if re.search(r"(?<![a-z])" + re.escape(m) + r"(?![a-z])", text)]


def wording_conflict(poly_text: str, kalshi_text: str) -> WordingCheck:
    """Flag when one side resolves on OFFICIAL status and the other on DE FACTO
    status. A conflict blocks auto-approval; it never blocks a human who has
    read both texts and disagrees."""
    p, k = _scrub(poly_text), _scrub(kalshi_text)
    p_off, p_df = _has_any(p, OFFICIAL_MARKERS), _has_any(p, DEFACTO_MARKERS)
    k_off, k_df = _has_any(k, OFFICIAL_MARKERS), _has_any(k, DEFACTO_MARKERS)

    # Each side is classified by which family it leans on MORE. A side that
    # mentions both is usually defining one against the other ("even if another
    # group exercises de facto control" — an official-basis rule that names the
    # other family once). Majority wins; a tie is unclassified. Symmetric on
    # purpose: the first version applied opposite tie-breaks to the two venues.
    def _basis(off: list[str], df: list[str]) -> str | None:
        if len(off) > len(df):
            return "official"
        if len(df) > len(off):
            return "defacto"
        return None

    p_basis, k_basis = _basis(p_off, p_df), _basis(k_off, k_df)

    if p_basis and k_basis and p_basis != k_basis:
        return WordingCheck(
            True,
            f"polymarket resolves on {p_basis} ({', '.join(p_off or p_df)}); "
            f"kalshi resolves on {k_basis} ({', '.join(k_df or k_off)})",
        )
    return WordingCheck(False, "")


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def dates_agree(poly_end: str | None, kalshi_close: str | None,
                tolerance_days: int) -> tuple[bool, int | None]:
    a, b = _parse_iso(poly_end), _parse_iso(kalshi_close)
    if a is None or b is None:
        return False, None
    days = abs(a - b).days
    return days <= tolerance_days, days


FetchEventMarkets = Callable[[str], tuple[dict[str, Any], list[dict[str, Any]]]]


def refresh_pairs(
    storage: Storage,
    cfg: Mapping[str, Any],
    now_ts: int,
    fetch_event_markets: FetchEventMarkets | None = None,
) -> dict[str, int]:
    """Two-stage re-match.

    Stage 1 — cheap: score every recorded Polymarket question against every
    Kalshi EVENT title in the local index (8,000+ events, no network).
    Stage 2 — targeted: for the top candidate events per market, fetch that
    event's legs (one request each), score the legs, and write the best ones
    to the registry with date + wording checks.

    Writes ALL candidates above the floor, including bad ones. Human decisions
    on existing pairs survive (see Storage.upsert_pair). `fetch_event_markets`
    is injectable so the whole thing runs offline in tests.
    """
    pcfg = cfg.get("pairs", {})
    event_min = float(pcfg.get("event_min_score", 0.25))
    min_score = float(pcfg.get("min_candidate_score", 0.30))
    auto_score = float(pcfg.get("auto_ok_score", 0.75))
    tol_days = int(pcfg.get("close_date_tolerance_days", 45))
    per_market = int(pcfg.get("max_candidates_per_market", 3))
    events_per_market = int(pcfg.get("max_events_per_market", 3))

    polys = storage.conn.execute(
        "SELECT condition_id, question, description, end_date FROM poly_markets"
    ).fetchall()
    events = storage.conn.execute(
        "SELECT event_ticker, series_ticker, title, sub_title FROM kalshi_events"
    ).fetchall()

    counts = {"events_scanned": len(events), "events_fetched": 0, "candidates": 0,
              "auto_ok": 0, "needs_review": 0, "kept_decision": 0}
    fetched: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    for pm in polys:
        p_years = year_tokens(pm["question"])

        # ---- stage 1: events
        ev_scored: list[tuple[float, Any]] = []
        for ev in events:
            text = f"{ev['title']} {ev['sub_title'] or ''}"
            e_years = year_tokens(text) | year_tokens(ev["event_ticker"])
            if p_years and e_years and not (p_years & e_years):
                continue
            sc = title_score(pm["question"], ev["title"], ev["sub_title"] or "")
            if sc >= event_min:
                ev_scored.append((sc, ev))
        ev_scored.sort(key=lambda t: -t[0])

        # ---- stage 2: legs of the top events
        leg_scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for _, ev in ev_scored[:events_per_market]:
            et = ev["event_ticker"]
            if et not in fetched:
                if fetch_event_markets is None:
                    continue
                try:
                    fetched[et] = fetch_event_markets(et)
                    counts["events_fetched"] += 1
                except Exception as exc:  # one bad event must not kill the refresh
                    log.warning("event fetch failed for %s: %s", et, exc)
                    fetched[et] = ({}, [])
            event_obj, legs = fetched[et]
            for leg in legs:
                sc = title_score(pm["question"], f"{ev['title']} {ev['sub_title'] or ''}",
                                 leg.get("yes_sub_title") or "")
                if sc >= min_score:
                    leg_scored.append((sc, event_obj or dict(ev), leg))
        leg_scored.sort(key=lambda t: -t[0])

        for score, event_obj, leg in leg_scored[:per_market]:
            km = KalshiClient.parse_market(event_obj, leg)
            if km is None:
                continue
            storage.upsert_kalshi_markets([km], now_ts)
            ok_dates, days = dates_agree(pm["end_date"], km.close_time, tol_days)
            check = wording_conflict(pm["description"] or "",
                                     f"{km.rules_primary} {km.rules_secondary}")
            if not check.conflict:
                check = title_outcome_conflict(pm["question"], f"{km.title} {km.yes_sub_title}")
            proposed = ("auto_ok" if (score >= auto_score and ok_dates and not check.conflict)
                        else "needs_review")
            final = storage.upsert_pair(
                pm["condition_id"], km.ticker, score=score, dates_ok=ok_dates,
                days_apart=days, wording_conflict=check.conflict, wording_note=check.note,
                proposed_status=proposed, now_ts=now_ts,
            )
            counts["candidates"] += 1
            if final in ("approved", "rejected"):
                counts["kept_decision"] += 1
            else:
                counts[final] += 1

    storage.set_meta("pairs_last_refresh_ts", str(now_ts))
    log.info("pairs refresh: %s", counts)
    return counts


def refresh_due(storage: Storage, cfg: Mapping[str, Any], now_ts: int) -> bool:
    interval = int(cfg.get("pairs", {}).get("refresh_interval_s", 6 * 3600))
    last = storage.get_meta("pairs_last_refresh_ts")
    return last is None or now_ts - int(last) >= interval
