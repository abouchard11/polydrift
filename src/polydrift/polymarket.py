"""Polymarket read-only clients: Gamma (market metadata + current prices) and
CLOB (historical price series for backfill).

Field shapes verified live 2026-09-04:
  Gamma market: clobTokenIds / outcomes / outcomePrices arrive as JSON-ENCODED
  STRINGS, not arrays. volumeNum / liquidityNum are floats.
  CLOB /prices-history returns {"history": [{"t": <unix>, "p": <float>}, ...]}
  and 403s any request without a real User-Agent (see http.py).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Sequence

from .http import Http
from .models import PolyMarket, PolySnapshot

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def _json_array(raw: Any) -> list[Any]:
    """Gamma double-encodes arrays as strings. Tolerate both shapes."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _as_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class PolymarketClient:
    def __init__(self, http: Http) -> None:
        self.http = http

    def iter_markets(self, page_size: int = 100, max_pages: int = 40,
                     stop_below_liquidity: float | None = None) -> Iterator[dict[str, Any]]:
        """Page open markets ordered by LIQUIDITY.

        Deliberately not ordered by volume: Polymarket volume is heavily gamed
        (bot wash trading — see the research doc's base rates). Verified
        2026-09-04, the top-volume open markets are joke markets with six-figure
        volume and no real book.
        """
        offset = 0
        for _ in range(max_pages):
            batch = self.http.get_json(
                f"{GAMMA_BASE}/markets",
                params={
                    "limit": page_size,
                    "offset": offset,
                    "closed": "false",
                    "order": "liquidityNum",
                    "ascending": "false",
                },
            )
            if not isinstance(batch, list) or not batch:
                return
            for raw in batch:
                yield raw
            # Results are sorted by liquidity descending, so once a full page
            # ends below the floor every later page is below it too.
            if stop_below_liquidity is not None:
                tail = _as_float(batch[-1].get("liquidityNum")) or 0.0
                if tail < stop_below_liquidity:
                    return
            if len(batch) < page_size:
                return
            offset += page_size

    @staticmethod
    def parse_market(raw: dict[str, Any]) -> PolyMarket | None:
        condition_id = raw.get("conditionId")
        question = raw.get("question")
        if not condition_id or not question:
            return None
        tokens = _json_array(raw.get("clobTokenIds"))
        return PolyMarket(
            condition_id=str(condition_id),
            gamma_id=str(raw.get("id", "")),
            question=str(question),
            slug=str(raw.get("slug", "")),
            description=str(raw.get("description", "") or ""),
            end_date=raw.get("endDate"),
            yes_token_id=str(tokens[0]) if len(tokens) > 0 else None,
            no_token_id=str(tokens[1]) if len(tokens) > 1 else None,
            active=bool(raw.get("active", False)),
            closed=bool(raw.get("closed", False)),
        )

    @staticmethod
    def parse_snapshot(raw: dict[str, Any], ts: int) -> PolySnapshot | None:
        condition_id = raw.get("conditionId")
        if not condition_id:
            return None
        outcomes = [str(o).strip().lower() for o in _json_array(raw.get("outcomes"))]
        prices = [_as_float(p) for p in _json_array(raw.get("outcomePrices"))]

        yes_price = no_price = None
        if "yes" in outcomes:
            idx = outcomes.index("yes")
            yes_price = prices[idx] if idx < len(prices) else None
        if "no" in outcomes:
            idx = outcomes.index("no")
            no_price = prices[idx] if idx < len(prices) else None
        # Non-binary or unlabelled outcomes: fall back to positional, but only
        # when there are exactly two — never guess on a multi-outcome market.
        if yes_price is None and no_price is None and len(prices) == 2:
            yes_price, no_price = prices[0], prices[1]

        return PolySnapshot(
            condition_id=str(condition_id),
            ts=ts,
            yes_price=yes_price,
            no_price=no_price,
            liquidity_usd=_as_float(raw.get("liquidityNum")),
            volume_usd=_as_float(raw.get("volumeNum")),
            source="gamma",
        )

    def midpoints(self, token_ids: Sequence[str], chunk: int = 100) -> dict[str, float]:
        """Live order-book midpoints, batched. Gamma's list endpoint serves a
        cached outcomePrices that can lag a fast-moving game by minutes
        (observed 0.615 vs a 0.895 book on a live MLB game, 2026-09-16); a
        convergence study cannot be built on a lagging price."""
        out: dict[str, float] = {}
        ids = [t for t in token_ids if t]
        for i in range(0, len(ids), chunk):
            payload = self.http.post_json(f"{CLOB_BASE}/midpoints",
                                          [{"token_id": t} for t in ids[i:i + chunk]])
            if isinstance(payload, dict):
                for tok, mid in payload.items():
                    val = _as_float(mid)
                    if val is not None:
                        out[str(tok)] = val
        return out

    def price_history(self, condition_id: str, token_id: str,
                      interval: str = "max", fidelity: int = 60) -> list[PolySnapshot]:
        """One-time backfill of a market's price series from the CLOB."""
        payload = self.http.get_json(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        history = payload.get("history", []) if isinstance(payload, dict) else []
        out: list[PolySnapshot] = []
        for point in history:
            ts = point.get("t")
            price = _as_float(point.get("p"))
            if ts is None or price is None:
                continue
            out.append(PolySnapshot(
                condition_id=condition_id,
                ts=int(ts),
                yes_price=price,
                no_price=round(1.0 - price, 6),
                liquidity_usd=None,
                volume_usd=None,
                source="clob_history",
            ))
        return out
