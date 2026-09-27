"""Hierarchical event graph for the 2026 midterm cycle.

Levels (the calendar's scanner design):
    state race -> chamber seat count -> chamber control -> balance of power

The point of the hierarchy: a venue can LEAD the atomic state race while
LAGGING the aggregate. So lead/lag is measured per NODE (cross-venue) and
per EDGE (does the child node's move show up in the parent node, and with
what delay, on each venue) — never as one global "Kalshi leads" flag.

Series are pre-mapped by pattern against whatever the recorder has already
indexed (kalshi_markets / poly_markets), so the graph fills in as the panel
records. Live tickers/slugs verified 2026-09-26:

  Kalshi:  CONTROLS-2026 (Senate control)      CONTROLH-2026 (House control)
           KXBALANCEPOWERCOMBO-27FEB (balance)  KXD/KXRSENATEWON-26NOV03,
           KXD/KXRHOUSEWON-26NOV03 (seat totals), SENATETX/MI/ME-26,
           SENATENC/GA-26 (state races)
  PM:      which-party-will-win-the-senate-in-2026,
           which-party-will-win-the-house-in-2026,
           balance-of-power-2026-midterms,
           texas/maine/michigan/ohio-senate-election-winner,
           texas/michigan-governor-winner-2026, ...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .storage import Storage

__all__ = ["NodeSpec", "MIDTERM_GRAPH", "find_series", "cross_venue_leadlag",
           "graph_report", "graph_digest"]


@dataclass(frozen=True)
class NodeSpec:
    key: str
    level: str            # 'state_race' | 'seat_count' | 'chamber_control' | 'balance'
    title: str
    kalshi: tuple[str, ...] = ()   # regexes over "title / yes_sub_title"
    poly: tuple[str, ...] = ()     # regexes over the question
    priority: str = ""


State = tuple[str, str]        # (event-or-condition id, label)

MIDTERM_GRAPH: tuple[NodeSpec, ...] = (
    # Kalshi patterns match "ticker title / subtitle", lowercased before the
    # match (so patterns must be lowercase); the ticker pins the cycle so a
    # 2028 race with an identical title cannot match a 2026 node. Polymarket
    # patterns match the market question.
    NodeSpec("senate-control", "chamber_control", "Senate control",
             (r"controls-2026\b",), (r"win the senate in 2026", r"control the senate")),
    NodeSpec("house-control", "chamber_control", "House control",
             (r"controlh-2026\b",), (r"win the house in 2026", r"control the house")),
    NodeSpec("congress-balance", "balance", "Congress balance of power",
             (r"balancepower", r"balance of power"), (r"balance of power",)),
    NodeSpec("senate-seat-total", "seat_count", "Senate party seat totals",
             (r"[dr]senatewon-26nov03", r"[dr]senateseats-2[67]",
              r"senate seats? .*(win|hold)"), (r"senate seats",)),
    NodeSpec("house-seat-total", "seat_count", "House party seat totals",
             (r"[dr]housewon-26nov03", r"[dr]houseseats-2[67]",
              r"house seats? .*(win|hold)"), (r"house seats",)),
    NodeSpec("state-senate-tx", "state_race", "Texas Senate winner",
             (r"senatetx-26\b",), (r"texas senate election winner",)),
    NodeSpec("state-senate-me", "state_race", "Maine Senate winner",
             (r"senateme-26\b",), (r"maine (?:us )?senate election winner",)),
    NodeSpec("state-senate-mi", "state_race", "Michigan Senate winner",
             (r"senatemi-26\b",), (r"michigan senate election winner",)),
    NodeSpec("state-senate-oh", "state_race", "Ohio Senate winner",
             (r"senateoh-26\b",), (r"ohio senate election winner",)),
    NodeSpec("governors", "state_race", "Governor winners (TX/ME/MI/OH)",
             (r"gov[a-z]*-(?:tx|me|mi|oh)-26\b", r"governor winner\? \(2026\)"),
             (r"governor (?:election )?winner",)),
)

# aggregation edges: child level feeds parent level
EDGES: tuple[tuple[str, str], ...] = (
    ("state_race", "seat_count"),
    ("seat_count", "chamber_control"),
    ("chamber_control", "balance"),
)


# --- series discovery -------------------------------------------------------

def _matches(text: str, patterns: Sequence[str]) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in patterns)


def find_series(storage: Storage, node: NodeSpec) -> dict[str, list[State]]:
    """Recorded markets matching this node's patterns, per venue. Kalshi
    patterns are matched against "ticker title / subtitle" so they can pin
    the cycle by ticker (SENATETX-26 vs SENATEOH-28); Polymarket patterns
    against the question text."""
    out: dict[str, list[State]] = {"kalshi": [], "poly": []}
    if node.kalshi:
        rows = storage.conn.execute(
            "SELECT ticker, title, yes_sub_title FROM kalshi_markets").fetchall()
        for r in rows:
            if _matches(f"{r['ticker']} {r['title']} / {r['yes_sub_title'] or ''}", node.kalshi):
                out["kalshi"].append((r["ticker"], f"{r['title']} / {r['yes_sub_title'] or ''}"))
    if node.poly:
        rows = storage.conn.execute(
            "SELECT condition_id, question FROM poly_markets").fetchall()
        for r in rows:
            if _matches(r["question"], node.poly):
                out["poly"].append((r["condition_id"], r["question"]))
    return out


def node_series(storage: Storage, node: NodeSpec, since_ts: int) -> dict[str, dict[str, list[tuple[int, float]]]]:
    """(ts, price) series per venue per matched market, from `since_ts`."""
    found = find_series(storage, node)
    out: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for venue, ids in found.items():
        out[venue] = {}
        for id_, _label in ids:
            if venue == "kalshi":
                out[venue][id_] = storage.recent_kalshi_series(id_, since_ts)
            else:
                out[venue][id_] = storage.recent_poly_series(id_, since_ts)
    return out


# --- lead/lag ---------------------------------------------------------------

def _returns(series: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    prev: float | None = None
    for ts, v in series:
        if prev is not None and v is not None:
            out.append((ts, v - prev))
        prev = v
    return out


def _corr(a: Sequence[float], b: Sequence[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[:n], b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / (va * vb) ** 0.5


def cross_venue_leadlag(
    kalshi: Sequence[tuple[int, float]],
    poly: Sequence[tuple[int, float]],
    max_lag_steps: int = 6,
    cadence_s: int = 300,
) -> tuple[str | None, int, float]:
    """Cross-correlation of returns at integer lags of the poll cadence.

    Returns (leader, lag_seconds, correlation). lag > 0 means the kalshi
    series leads by that much; leader is None when no lag beats the
    zero-lag correlation enough to call (guard: |corr| < 0.2 is noise).

    Series are resampled to the cadence grid by last-value so the two
    venues' timestamps do not have to agree exactly.
    """
    def grid(series: Sequence[tuple[int, float]]) -> dict[int, float]:
        g: dict[int, float] = {}
        for ts, v in series:
            g[(ts // cadence_s) * cadence_s] = v   # last value in the bucket
        return g

    gk, gp = grid(kalshi), grid(poly)
    stamps = sorted(set(gk) & set(gp))
    if len(stamps) < 4:
        return None, 0, 0.0

    def shifted(lag_steps: int) -> tuple[list[float], list[float]]:
        """kalshi returns at t, poly returns at t + lag*cadence."""
        a, b = [], []
        for t in stamps:
            t2 = t + lag_steps * cadence_s
            if t in gk and t2 in gp and t - cadence_s in gk and t2 - cadence_s in gp:
                a.append(gk[t] - gk[t - cadence_s])
                b.append(gp[t2] - gp[t2 - cadence_s])
        return a, b

    best_lag, best_corr = 0, -1.0
    for lag in range(-max_lag_steps, max_lag_steps + 1):
        a, b = shifted(lag)
        c = _corr(a, b)
        if abs(c) > best_corr:
            best_corr, best_lag = abs(c), lag
    if best_corr < 0.2:
        return None, 0, round(best_corr, 3)
    if best_lag == 0:
        return None, 0, round(best_corr, 3)
    leader = "kalshi" if best_lag > 0 else "polymarket"
    return leader, abs(best_lag) * cadence_s, round(best_corr, 3)


# --- report -----------------------------------------------------------------

@dataclass
class EdgeReport:
    child_level: str
    parent_level: str
    notes: list[str] = field(default_factory=list)


def graph_report(storage: Storage, lookback_s: int = 7 * 86400,
                 now_ts: int | None = None) -> dict:
    """Pre-map status + cross-venue lead/lag per node + per-edge notes.

    Honest about what is measurable: a node with only one venue recorded, or
    too few overlapping stamps, reports 'insufficient' rather than a guess."""
    import time as _time
    now_ts = now_ts if now_ts is not None else int(_time.time())
    since = now_ts - lookback_s

    nodes: dict[str, dict] = {}
    for node in MIDTERM_GRAPH:
        found = find_series(storage, node)
        series = node_series(storage, node, since)
        entry: dict = {
            "key": node.key, "level": node.level, "title": node.title,
            "kalshi": [lbl for _, lbl in found["kalshi"]],
            "poly": [lbl for _, lbl in found["poly"]],
            "leadlag": [],
        }
        for venue in ("kalshi", "poly"):
            for id_, s in series.get(venue, {}).items():
                if len(s) >= 4:
                    entry["leadlag"].append({"venue": venue, "id": id_, "points": len(s)})
        # cross-venue lead/lag on the first matched pair of markets
        k_series = next(iter(series.get("kalshi", {}).values()), None)
        p_series = next(iter(series.get("poly", {}).values()), None)
        if k_series and p_series:
            leader, lag_s, corr = cross_venue_leadlag(k_series, p_series)
            entry["cross_venue"] = {"leader": leader, "lag_s": lag_s, "corr": corr,
                                    "kalshi_points": len(k_series), "poly_points": len(p_series)}
        else:
            entry["cross_venue"] = {"leader": None, "lag_s": 0, "corr": 0.0,
                                    "note": "missing one venue"}
        nodes[node.key] = entry

    edges = [{"child": c, "parent": p,
              "note": "child moves propagate to parent; measure per venue once "
                      "both levels have recorded series"}
             for c, p in EDGES]
    return {"nodes": nodes, "edges": edges, "lookback_s": lookback_s}


def graph_digest(storage: Storage, lookback_s: int = 7 * 86400) -> dict:
    """Compact form for logs / web: counts + the leaders we can actually call."""
    rep = graph_report(storage, lookback_s=lookback_s)
    mapped = {k: v for k, v in rep["nodes"].items() if v["kalshi"] or v["poly"]}
    both = {k: v["cross_venue"] for k, v in rep["nodes"].items()
            if v["cross_venue"].get("leader")}
    return {
        "nodes_total": len(rep["nodes"]),
        "nodes_mapped": len(mapped),
        "nodes_both_venues": sum(1 for v in rep["nodes"].values() if v["kalshi"] and v["poly"]),
        "leaders": {k: {"leader": v["leader"], "lag_s": v["lag_s"], "corr": v["corr"]}
                    for k, v in both.items()},
    }
