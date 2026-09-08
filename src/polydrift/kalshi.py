"""Kalshi read-only client (public trade-api v2, keyless).

SCHEMA WARNING — verified live 2026-09-04. Kalshi's market object no longer
exposes the widely-documented `yes_bid` / `yes_ask` / `last_price` / `volume`
integer-cent fields. They are absent, so any client written against them
silently records NULL prices forever. The live fields are decimal-dollar
STRINGS and fixed-point floats:

    yes_bid_dollars "0.0300"   yes_ask_dollars "0.0450"
    last_price_dollars         previous_price_dollars
    volume_fp                  volume_24h_fp        open_interest_fp
    liquidity_dollars

Also: /markets?status=open is polluted with auto-generated multivariate
"CROSSCATEGORY" / MVE combo tickers that carry no prices and no rules text
(200/200 of the first page were junk). Pull through /events instead, which
yields real markets with rules_primary populated, and filter the combos.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator, Sequence

from .http import Http
from .models import KalshiMarket, KalshiSnapshot

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Auto-generated combo/parlay markets. Not real standalone questions.
COMBO_MARKERS = ("CROSSCATEGORY", "KXMVE", "-MVE")


def _as_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def is_combo(ticker: str) -> bool:
    t = (ticker or "").upper()
    return any(marker in t for marker in COMBO_MARKERS)


class KalshiClient:
    def __init__(self, http: Http) -> None:
        self.http = http

    def iter_event_markets(
        self,
        page_size: int = 100,
        max_pages: int = 30,
        series_allowlist: Sequence[str] = (),
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """Yield (event, market) pairs for open events, combos excluded."""
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "limit": page_size,
                "status": "open",
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get_json(f"{KALSHI_BASE}/events", params=params)
            events = payload.get("events", []) if isinstance(payload, dict) else []
            if not events:
                return
            for event in events:
                series = str(event.get("series_ticker", "") or "")
                if series_allowlist and series not in series_allowlist:
                    continue
                for market in event.get("markets", []) or []:
                    if is_combo(str(market.get("ticker", ""))):
                        continue
                    yield event, market
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not cursor:
                return

    def iter_events(self, page_size: int = 200, max_pages: int = 60) -> Iterator[dict[str, Any]]:
        """Titles-only sweep of the open-event index. Kalshi had 8,000+ open
        events on 2026-09-05; nested markets on every page would be ~10x the
        payload for no matching benefit. Legs are fetched per candidate event."""
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size, "status": "open",
                                      "with_nested_markets": "false"}
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get_json(f"{KALSHI_BASE}/events", params=params)
            events = payload.get("events", []) if isinstance(payload, dict) else []
            if not events:
                return
            for event in events:
                yield event
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not cursor:
                return

    def event_markets(self, event_ticker: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload = self.http.get_json(f"{KALSHI_BASE}/events/{event_ticker}",
                                     params={"with_nested_markets": "true"})
        event = payload.get("event", payload) if isinstance(payload, dict) else {}
        markets = [m for m in (event.get("markets") or []) if not is_combo(str(m.get("ticker", "")))]
        return event, markets

    def markets_by_tickers(self, tickers: Sequence[str], chunk: int = 50,
                           chunk_pause_s: float = 1.0) -> Iterator[dict[str, Any]]:
        """Cheap per-cycle price refresh for a known ticker set. Consecutive
        chunks are spaced out: /markets?tickers= has a tighter burst limit than
        the global throttle assumes (429s on 11 back-to-back calls, 2026-09-08)."""
        import time as _time
        tickers = [t for t in tickers if t]
        for i in range(0, len(tickers), chunk):
            if i and chunk_pause_s > 0:
                _time.sleep(chunk_pause_s)
            payload = self.http.get_json(f"{KALSHI_BASE}/markets",
                                         params={"tickers": ",".join(tickers[i:i + chunk])})
            for m in (payload.get("markets", []) if isinstance(payload, dict) else []):
                yield m

    @staticmethod
    def parse_market(event: dict[str, Any], raw: dict[str, Any]) -> KalshiMarket | None:
        ticker = raw.get("ticker")
        if not ticker:
            return None
        # Prefer the per-market subtitle for the actual proposition; the event
        # title is shared across every leg ("Who will the next Pope be?").
        return KalshiMarket(
            ticker=str(ticker),
            event_ticker=str(raw.get("event_ticker", "") or event.get("event_ticker", "") or ""),
            series_ticker=str(event.get("series_ticker", "") or ""),
            title=str(raw.get("title") or event.get("title") or ""),
            yes_sub_title=str(raw.get("yes_sub_title", "") or ""),
            rules_primary=str(raw.get("rules_primary", "") or ""),
            rules_secondary=str(raw.get("rules_secondary", "") or ""),
            close_time=raw.get("close_time"),
            market_type=str(raw.get("market_type", "") or ""),
            status=str(raw.get("status", "") or ""),
        )

    @staticmethod
    def parse_snapshot(raw: dict[str, Any], ts: int) -> KalshiSnapshot | None:
        ticker = raw.get("ticker")
        if not ticker:
            return None
        return KalshiSnapshot(
            ticker=str(ticker),
            ts=ts,
            yes_bid=_as_float(raw.get("yes_bid_dollars")),
            yes_ask=_as_float(raw.get("yes_ask_dollars")),
            last_price=_as_float(raw.get("last_price_dollars")),
            volume=_as_float(raw.get("volume_fp")),
            open_interest=_as_float(raw.get("open_interest_fp")),
            liquidity=_as_float(raw.get("liquidity_dollars")),
        )
