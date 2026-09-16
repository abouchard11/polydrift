"""Market universe selection — which markets are worth recording.

Two filters that matter, both from live observation on 2026-09-04:

1. ZOMBIES. Gamma's `closed=false` is not trustworthy: 7 of the first 100
   markets had an endDate already in the past while still reporting
   active=true (the Ethiopia PM markets, endDate 2026-06-01). Recording those
   pollutes any convergence measurement with series that can never converge.

2. WASH VOLUME. Volume is a gamed metric. Rank on liquidity instead, and keep
   a small pattern denylist for the obvious farm markets.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable, Mapping, Sequence

from .games import is_game_market
from .models import PolyMarket


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_zombie(market: PolyMarket, now: dt.datetime) -> bool:
    """Open per the API, but its resolution date has already passed."""
    end = _parse_iso(market.end_date)
    return end is not None and end < now


def select_poly_universe(
    candidates: Iterable[tuple[PolyMarket, float | None, float | None]],
    cfg: Mapping[str, Any],
    now: dt.datetime | None = None,
) -> list[PolyMarket]:
    """candidates: (market, liquidity_usd, yes_price) triples. Returns the kept
    markets, highest liquidity first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    uni = cfg.get("universe", {})
    min_liq = float(uni.get("min_liquidity_usd", 0) or 0)
    max_markets = int(uni.get("max_markets", 60))
    require_future = bool(uni.get("require_end_date_future", True))
    max_days = uni.get("max_days_to_resolution")
    patterns: Sequence[str] = uni.get("exclude_question_patterns", []) or []
    band = uni.get("price_band") or [0.0, 1.0]
    lo, hi = float(band[0]), float(band[1])
    compiled = [re.compile(p) for p in patterns]

    kept: list[tuple[float, PolyMarket]] = []
    for market, liquidity, price in candidates:
        if market.closed:
            continue
        if require_future and is_zombie(market, now):
            continue
        if max_days is not None:
            end = _parse_iso(market.end_date)
            if end is not None and (end - now).days > int(max_days):
                continue
        if (liquidity or 0.0) < min_liq:
            continue
        if any(rx.search(market.question) for rx in compiled):
            continue
        # Deep longshots are near-useless for a convergence study: there is
        # no room to move and fees dominate whatever movement there is.
        # Ranking on liquidity alone pulls in the dead tail legs of big
        # multi-outcome election markets (observed live 2026-09-04: every
        # top-liquidity pick priced 0.0005-0.0075).
        if price is not None and not (lo <= price <= hi):
            continue
        kept.append((liquidity or 0.0, market))

    kept.sort(key=lambda pair: -pair[0])
    general = [market for _, market in kept[:max_markets]]

    # Game (moneyline) markets get their own slice. They are the only markets
    # whose prices reliably MOVE inside a 1h-24h window; the general top-N is
    # dominated by long-dated markets that sit still for weeks (day-7 read,
    # 2026-09-16). Lower liquidity floor: a $10k MLB book is plenty for a
    # measurement, and the pair on the other venue is deterministic.
    max_games = int(uni.get("max_games", 0) or 0)
    if max_games <= 0:
        return general
    game_floor = float(uni.get("game_min_liquidity_usd", 10_000) or 0)
    chosen = {m.condition_id for m in general}
    games: list[tuple[float, PolyMarket]] = []
    for market, liquidity, price in candidates:
        if market.condition_id in chosen or market.closed:
            continue
        if not is_game_market(market.slug, market.question):
            continue
        if require_future and is_zombie(market, now):
            continue
        if (liquidity or 0.0) < game_floor:
            continue
        if price is not None and not (lo <= price <= hi):
            continue
        games.append((liquidity or 0.0, market))
    games.sort(key=lambda pair: -pair[0])
    return general + [market for _, market in games[:max_games]]
