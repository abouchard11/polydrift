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
from typing import Any, Callable, Mapping, Sequence

from .kalshi import KalshiClient
from .matching import title_score, year_tokens
from .radar import semantic_mismatch
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
    max_fetches = int(pcfg.get("max_event_fetches_per_sweep", 150))

    polys = storage.conn.execute(
        "SELECT condition_id, question, description, end_date FROM poly_markets"
    ).fetchall()
    events = storage.conn.execute(
        "SELECT event_ticker, series_ticker, title, sub_title FROM kalshi_events"
    ).fetchall()

    counts = {"events_scanned": len(events), "events_fetched": 0, "fetch_capped": 0,
              "candidates": 0, "auto_ok": 0, "needs_review": 0, "kept_decision": 0}
    fetched: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    # Pre-pass: rank every (market, event) candidate globally so the per-sweep
    # fetch budget goes to the strongest matches, not to whichever Polymarket
    # market happens to sort first. 453 fetches/sweep took 16 minutes and
    # left a gap in the Polymarket series every 6h (2026-09-08).
    global_best: dict[str, float] = {}
    for pm in polys:
        p_years = year_tokens(pm["question"])
        for ev in events:
            text = f"{ev['title']} {ev['sub_title'] or ''}"
            e_years = year_tokens(text) | year_tokens(ev["event_ticker"])
            if p_years and e_years and not (p_years & e_years):
                continue
            sc = title_score(pm["question"], ev["title"], ev["sub_title"] or "")
            if sc >= event_min and sc > global_best.get(ev["event_ticker"], 0.0):
                global_best[ev["event_ticker"]] = sc
    fetch_allowed = {et for et, _ in sorted(global_best.items(), key=lambda t: -t[1])[:max_fetches]}
    counts["fetch_capped"] = max(0, len(global_best) - len(fetch_allowed))

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
                if fetch_event_markets is None or et not in fetch_allowed:
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
            # Semantic gate (2026-09-26): resolution source / settlement
            # horizon / granularity differences. The Senate-control catch:
            # Kalshi's direct control contract vs a Polymarket seat-count
            # synthetic look identical in the title and are different bets.
            sem = semantic_mismatch(
                f"{pm['question']} {pm['description'] or ''}",
                f"{km.title} {km.yes_sub_title} {km.rules_primary} {km.rules_secondary}",
                pm["end_date"], km.close_time,
            )
            proposed = ("auto_ok" if (score >= auto_score and ok_dates
                                      and not check.conflict and not sem.mismatch)
                        else "needs_review")
            final = storage.upsert_pair(
                pm["condition_id"], km.ticker, score=score, dates_ok=ok_dates,
                days_apart=days, wording_conflict=check.conflict, wording_note=check.note,
                semantic_conflict=sem.mismatch, semantic_note=sem.note,
                proposed_status=proposed, now_ts=now_ts,
            )
            counts["candidates"] += 1
            if final in ("approved", "rejected"):
                counts["kept_decision"] += 1
            else:
                counts[final] += 1

    applied = apply_decisions(storage, load_decisions(cfg.get("pairs", {}).get("decisions_file")), now_ts)
    counts["decisions_applied"] = applied["approved"] + applied["rejected"]
    storage.set_meta("pairs_last_refresh_ts", str(now_ts))
    log.info("pairs refresh: %s", counts)
    return counts


def refresh_due(storage: Storage, cfg: Mapping[str, Any], now_ts: int) -> bool:
    interval = int(cfg.get("pairs", {}).get("refresh_interval_s", 6 * 3600))
    last = storage.get_meta("pairs_last_refresh_ts")
    return last is None or now_ts - int(last) >= interval


def load_decisions(path: str | None) -> dict[str, list[dict[str, Any]]]:
    import os
    import yaml
    candidate = path or os.environ.get("POLYDRIFT_DECISIONS", "decisions.yaml")
    try:
        with open(candidate) as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {"approve": [], "reject": []}
    if not isinstance(data, dict):
        raise ValueError(f"{candidate}: top level must be a mapping")
    out = {"approve": list(data.get("approve") or []), "reject": list(data.get("reject") or [])}
    for key in ("approve", "reject"):
        for d in out[key]:
            cid = d.get("condition_id")
            if cid is not None and not isinstance(cid, str):
                # PyYAML is YAML 1.1: an unquoted 0x... scans as a hex INTEGER and
                # the id silently becomes a decimal that matches nothing.
                raise ValueError(
                    f"{candidate}: condition_id for {d.get('ticker')} must be a quoted string "
                    f"(got {type(cid).__name__}); write condition_id: \"0x...\"")
    return out


def apply_decisions(storage: Storage, decisions: Mapping[str, Sequence[Mapping[str, Any]]],
                    now_ts: int) -> dict[str, int]:
    """Apply version-controlled human decisions to the registry. Idempotent.

    A decision names a Kalshi ticker, optionally pinned to a condition_id.
    APPROVE fails closed: if the ticker matched more than one Polymarket market
    and no condition_id is given, nothing is approved and it is counted as
    ambiguous. Live catch 2026-09-05: each World Chess Championship leg matched
    BOTH players' Polymarket markets; approving by ticker blessed the cross
    pairs (Gukesh's leg <-> Sindarov's market), which are wrong bets.
    REJECT applies to every matching pair — rejecting more is the safe side.
    """
    counts = {"approved": 0, "rejected": 0, "unmatched": 0, "ambiguous": 0}
    for status, key in (("approved", "approve"), ("rejected", "reject")):
        for d in decisions.get(key, []) or []:
            ticker = str(d.get("ticker", "")).strip()
            if not ticker:
                continue
            cond = str(d["condition_id"]) if d.get("condition_id") else None
            where, args = "ticker=?", [ticker]
            if cond:
                where += " AND condition_id=?"
                args.append(cond)
            matches = storage.conn.execute(
                f"SELECT id, condition_id, status FROM kalshi_pairs WHERE {where}", args).fetchall()
            if not matches:
                counts["unmatched"] += 1
                continue
            if status == "approved" and len(matches) > 1:
                counts["ambiguous"] += 1
                log.warning("decision for %s is ambiguous (%d pairs); pin a condition_id: %s",
                            ticker, len(matches), [m["condition_id"][:12] for m in matches])
                continue
            for m in matches:
                if m["status"] != status:
                    storage.conn.execute(
                        "UPDATE kalshi_pairs SET status=?, updated_ts=? WHERE id=?",
                        (status, now_ts, m["id"]))
                    counts[status] += 1
    if any(counts[k] for k in ("approved", "rejected", "ambiguous")):
        log.info("decisions applied: %s", counts)
    return counts
