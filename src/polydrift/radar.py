"""Event-window divergence radar: tiered alerts, semantic flags, JSONL ledger.

Read-only. An alert is a signal, never an order.

Tiering (from the 90-day dislocation calendar):
  same-side gap >= 2.0c                -> alert
  same-side gap >= 2.5c AND positive
  fee-and-friction-adjusted score      -> execution-candidate
  semantic mismatch on the pair        -> alert at most, never execution

candidate score = gross edge - taker fees both venues - expected slippage
                  - adverse-selection buffer - semantic-risk reserve
(all in cents per contract)

Semantic mismatch is the calendar's own warning, live-confirmed on the Senate
pair (2026-09-26): Kalshi's direct control contract settles on the President
pro tempore's party on Feb 1 2027 with early media-call resolution;
Polymarket's settles on seat counts with a VP tiebreak and a Jan 5 2027 end.
Same question at the headline level, different bets at the edges — a displayed
gap between them is partly compensation, not mispricing.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .fees import kalshi_fee, polymarket_fee
from .storage import Storage

log = logging.getLogger(__name__)

ALERT_CENTS = 2.0
EXECUTION_CENTS = 2.5

# --- semantic mismatch ------------------------------------------------------

SOURCE_PATTERNS = (
    "associated press", " ap ", "reuters", "bloomberg", "fox news", "cnn",
    "nbc", "abc news", "cbs", "secretary of state", "uma ", "umа oracle",
    "media call", "consensus of media",
)
# "how many seats" / threshold-ladder language = a count market, not control
COUNT_MARKERS = (
    "how many", "seat count", "seats will", "seats did", "hold after",
    "more than", "at least", "or more", "or fewer", "or less",
    "number of seats",
)
CONTROL_MARKERS = ("control", "win the senate", "win the house", "majority",
                   "which party will win")
COMBO_MARKERS = ("all of the following", "combination market", "if all of")
ROUNDING_MARKERS = ("round up", "round down", "rounded", "rounding", "nearest")


@dataclass(frozen=True)
class SemanticCheck:
    mismatch: bool
    dimensions: tuple[str, ...] = ()
    note: str = ""

    def __bool__(self) -> bool:  # `if check:` reads better at call sites
        return self.mismatch


def _find(text: str, markers: Sequence[str]) -> list[str]:
    t = " " + (text or "").lower() + " "
    return [m for m in markers if m in t]


def _horizon_days(poly_end: str | None, kalshi_close: str | None) -> int | None:
    def parse(v: str | None) -> dt.datetime | None:
        if not v:
            return None
        try:
            return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    a, b = parse(poly_end), parse(kalshi_close)
    if a is None or b is None:
        return None
    return abs(a - b).days


def semantic_mismatch(
    poly_text: str,
    kalshi_text: str,
    poly_end: str | None = None,
    kalshi_close: str | None = None,
    horizon_tol_days: int = 21,
) -> SemanticCheck:
    """Compare resolution terms between pair legs.

    Dimensions (the calendar's list): resolution source, cutoff/settlement
    horizon, strike, rounding, granularity. Every check is text-level and
    deliberately conservative where false positives would be common — e.g. a
    source is only a mismatch when BOTH sides name sources and the sets are
    disjoint (one-sided source mentions are ubiquitous).
    """
    dims: list[str] = []
    notes: list[str] = []

    p, k = (poly_text or "").lower(), (kalshi_text or "").lower()

    # horizon / cutoff: settlement dates far apart mean different lock-up,
    # which is compensation for time, not mispricing.
    days = _horizon_days(poly_end, kalshi_close)
    if days is not None and days > horizon_tol_days:
        dims.append("horizon")
        notes.append(f"settlement {days}d apart")

    # granularity: count/combo market paired against a direct control market
    p_count, k_count = _find(p, COUNT_MARKERS), _find(k, COUNT_MARKERS)
    p_ctrl, k_ctrl = _find(p, CONTROL_MARKERS), _find(k, CONTROL_MARKERS)
    p_combo, k_combo = _find(p, COMBO_MARKERS), _find(k, COMBO_MARKERS)
    p_is_count = bool(p_count) or bool(p_combo)
    k_is_count = bool(k_count) or bool(k_combo)
    if p_is_count != k_is_count and (p_ctrl or k_ctrl or p_is_count or k_is_count):
        which = "poly" if p_is_count else "kalshi"
        dims.append("granularity")
        notes.append(f"{which} leg is count/combo, other is direct control")

    # source: both sides name resolution sources and the sets are disjoint
    p_src, k_src = set(_find(p, SOURCE_PATTERNS)), set(_find(k, SOURCE_PATTERNS))
    if p_src and k_src and not (p_src & k_src):
        dims.append("source")
        notes.append(f"sources {sorted(p_src)} vs {sorted(k_src)}")

    # rounding: one side specifies rounding, the other does not
    p_rnd, k_rnd = _find(p, ROUNDING_MARKERS), _find(k, ROUNDING_MARKERS)
    if bool(p_rnd) != bool(k_rnd):
        dims.append("rounding")
        notes.append("rounding rules on one leg only")

    # strike: seat-threshold numbers present on both sides and different
    p_strike = set(re.findall(r"\b(2\d[05]\b|5[01]\b|4[3-7]\b)", p))
    k_strike = set(re.findall(r"\b(2\d[05]\b|5[51]\b|4[3-7]\b)", k))
    if p_strike and k_strike and p_strike != k_strike:
        dims.append("strike")
        notes.append(f"thresholds {sorted(p_strike)} vs {sorted(k_strike)}")

    return SemanticCheck(bool(dims), tuple(dims),
                         "; ".join(f"{d}: {n}" for d, n in zip(dims, notes)))


# --- candidate score --------------------------------------------------------

@dataclass(frozen=True)
class CostBreakdown:
    fee_kalshi_cents: float
    fee_poly_cents: float
    slippage_cents: float
    adverse_cents: float
    semantic_reserve_cents: float
    total_cents: float


def candidate_costs(
    kalshi_price: float,
    poly_price: float,
    kalshi_spread_cents: float | None,
    poly_category: str = "politics",
    *,
    default_slippage_cents: float = 0.5,
    adverse_selection_cents: float = 0.3,
    semantic_reserve_cents: float = 1.0,
    semantic: bool = False,
    contracts: float = 100.0,
) -> CostBreakdown:
    """Per-contract round of frictions, in cents.

    Fees: one taker fill per venue (the two arb legs), priced at the
    fee-and-slippage-adjusted venue at its own current price. Kalshi fees are
    rounded up per trade, so they are quoted at a realistic size and divided
    back — at 1 contract the rounding IS the fee (see fees.breakeven_move).
    Slippage: half the observed Kalshi spread when we have a book; otherwise
    a configured default per venue pair (conservative: both venues).
    """
    fee_k = kalshi_fee(contracts, kalshi_price).fee_usd / contracts * 100.0
    fee_p = polymarket_fee(1.0, poly_price, category=poly_category).fee_usd * 100.0
    if kalshi_spread_cents is not None:
        slip = kalshi_spread_cents / 2.0 + default_slippage_cents
    else:
        slip = 2.0 * default_slippage_cents
    reserve = semantic_reserve_cents if semantic else 0.0
    total = fee_k + fee_p + slip + adverse_selection_cents + reserve
    return CostBreakdown(fee_k, fee_p, slip, adverse_selection_cents, reserve, total)


def classify_gap(
    gap_cents: float,
    score_cents: float,
    semantic: bool = False,
    *,
    alert_cents: float = ALERT_CENTS,
    execution_cents: float = EXECUTION_CENTS,
) -> str | None:
    """'alert' | 'execution-candidate' | None. Same-side gap only (|gap|)."""
    gap = abs(gap_cents)
    if gap < alert_cents:
        return None
    if semantic:
        return "alert"          # compensation risk: never an execution candidate
    if gap >= execution_cents and score_cents > 0.0:
        return "execution-candidate"
    return "alert"


# --- leader inference -------------------------------------------------------

def infer_leader(
    kalshi_series: Sequence[tuple[int, float]],
    poly_series: Sequence[tuple[int, float]],
) -> tuple[str | None, str]:
    """Which venue's price moved most recently. The measurable proxy for
    'who incorporated the information first' available from snapshot data:
    the venue whose last price CHANGE is newer led this move. If exactly one
    venue moved at all, that venue is the leader ('only-mover'); 'flat' when
    neither moved, 'insufficient-history' when the data cannot say."""
    def last_change(series: Sequence[tuple[int, float]]) -> int | None:
        if len(series) < 2:
            return None
        prev = series[0][1]
        for ts, v in series[1:]:
            if v is not None and prev is not None and v != prev:
                return ts
            prev = v
        return None
    kc, pc = last_change(kalshi_series), last_change(poly_series)
    if kc is None and pc is None:
        return None, "flat" if (kalshi_series and poly_series) else "insufficient-history"
    if kc is None:
        return "polymarket", "only-mover"
    if pc is None:
        return "kalshi", "only-mover"
    if kc == pc:
        return None, "simultaneous"
    if kc > pc:
        return "kalshi", "last-change"
    return "polymarket", "last-change"


# --- the scan ---------------------------------------------------------------

REQUIRED_ALERT_KEYS = (
    "ts", "mode", "pair_id", "ticker", "condition_id", "question",
    "gap_cents", "tier", "semantic_flag", "leader", "leader_basis",
    "kalshi_mid", "poly_yes", "candidate_score_cents", "cost_cents",
)


@dataclass
class Alert:
    row: dict[str, Any] = field(default_factory=dict)


def scan(
    storage: Storage,
    cfg: Mapping[str, Any],
    now_ts: int,
    mode: str = "baseline",
) -> list[dict[str, Any]]:
    """Scan usable pairs for tiered divergence alerts; append to the JSONL
    ledger. Fail-safe per pair: one broken pair never stops the scan."""
    rcfg = cfg.get("radar", {})
    alerts_file = Path(rcfg.get("alerts_file", "data/event-alerts.jsonl"))
    alert_cents = float(rcfg.get("alert_cents", ALERT_CENTS))
    execution_cents = float(rcfg.get("execution_cents", EXECUTION_CENTS))
    freshness = int(rcfg.get("max_stale_s", 900))
    cooldown = int(rcfg.get("cooldown_s", 1800))
    if mode == "high":
        cooldown = int(rcfg.get("high_cooldown_s", 300))
    poly_category = rcfg.get("poly_category", "politics")

    recent = _recent_alert_keys(alerts_file, now_ts, cooldown)
    out: list[dict[str, Any]] = []

    for pair in storage.usable_pairs():
        try:
            kal = storage.latest_kalshi(pair["ticker"], max_stale_s=freshness)
            poly = storage.latest_poly(pair["condition_id"], max_stale_s=freshness)
            if kal is None or poly is None or poly["yes_price"] is None:
                continue
            k_mid = (kal["yes_bid"] + kal["yes_ask"]) / 2.0 if (
                kal["yes_bid"] is not None and kal["yes_ask"] is not None
            ) else kal["last_price"]
            if k_mid is None:
                continue
            gap_cents = (k_mid - poly["yes_price"]) * 100.0
            if abs(gap_cents) < alert_cents:
                continue

            sem = semantic_mismatch(
                f"{pair['question']} {pair['poly_rules'] or ''}",
                f"{pair['k_title']} {pair['yes_sub_title'] or ''} "
                f"{pair['rules_primary'] or ''} {pair['rules_secondary'] or ''}",
                pair["end_date"], pair["close_time"],
                horizon_tol_days=int(rcfg.get("horizon_tol_days", 21)),
            )
            spread = ((kal["yes_ask"] - kal["yes_bid"]) * 100.0
                      if kal["yes_bid"] is not None and kal["yes_ask"] is not None else None)
            costs = candidate_costs(
                k_mid, poly["yes_price"], spread, poly_category,
                default_slippage_cents=float(rcfg.get("default_slippage_cents", 0.5)),
                adverse_selection_cents=float(rcfg.get("adverse_selection_cents", 0.3)),
                semantic_reserve_cents=float(rcfg.get("semantic_reserve_cents", 1.0)),
                semantic=sem.mismatch,
            )
            score = abs(gap_cents) - costs.total_cents
            tier = classify_gap(gap_cents, score, sem.mismatch,
                                alert_cents=alert_cents, execution_cents=execution_cents)
            if tier is None:
                continue

            leader, basis = infer_leader(
                storage.recent_kalshi_series(pair["ticker"], since_ts=now_ts - 2 * freshness),
                storage.recent_poly_series(pair["condition_id"], since_ts=now_ts - 2 * freshness),
            )
            key = (pair["ticker"], tier)
            if key in recent:
                continue
            row = {
                "ts": now_ts,
                "mode": mode,
                "pair_id": pair["id"],
                "ticker": pair["ticker"],
                "condition_id": pair["condition_id"],
                "question": (pair["question"] or "")[:120],
                "gap_cents": round(gap_cents, 2),
                "tier": tier,
                "semantic_flag": sem.mismatch,
                "semantic_dimensions": list(sem.dimensions),
                "semantic_note": sem.note,
                "leader": leader,
                "leader_basis": basis,
                "kalshi_mid": round(k_mid, 4),
                "poly_yes": round(poly["yes_price"], 4),
                "candidate_score_cents": round(score, 2),
                "cost_cents": round(costs.total_cents, 2),
                "fee_kalshi_cents": round(costs.fee_kalshi_cents, 2),
                "fee_poly_cents": round(costs.fee_poly_cents, 2),
                "slippage_cents": round(costs.slippage_cents, 2),
                "adverse_buffer_cents": round(costs.adverse_cents, 2),
                "semantic_reserve_cents": round(costs.semantic_reserve_cents, 2),
            }
            out.append(row)
            recent.add(key)
        except Exception:  # noqa: BLE001 - one bad pair must not kill the scan
            log.exception("radar: pair %s failed", pair["ticker"])
    if out:
        _append_alerts(alerts_file, out)
    return out


def _append_alerts(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _recent_alert_keys(path: Path, now_ts: int, cooldown_s: int) -> set[tuple[str, str]]:
    """(ticker, tier) pairs alerted within the cooldown, read from the ledger
    tail. Ledger corrupt/unreadable -> empty set (re-alert is the safe side)."""
    if not path.is_file():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-500:]
    except OSError:
        return set()
    out: set[tuple[str, str]] = set()
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if now_ts - int(row.get("ts", 0)) <= cooldown_s:
            out.add((str(row.get("ticker")), str(row.get("tier"))))
    return out


def scan_digest(alerts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One-line summary for the report logs and the web /report."""
    return {
        "alerts": len(alerts),
        "execution_candidates": sum(1 for a in alerts if a["tier"] == "execution-candidate"),
        "semantic_flags": sum(1 for a in alerts if a["semantic_flag"]),
        "pairs": sorted({a["ticker"] for a in alerts}),
    }
