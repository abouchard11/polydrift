"""The poll cycle. Read-only: fetches, records, never trades."""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Mapping

from .http import Http
from .kalshi import KalshiClient
from .polymarket import PolymarketClient
from .storage import Storage
from .universe import select_poly_universe

log = logging.getLogger(__name__)


def _within_horizon(close_time: str | None, now: dt.datetime, max_days: Any) -> bool:
    """Kalshi lists markets resolving in 2045/2050. Those never converge inside
    a measurement window and would dominate a volume-ranked sample."""
    if max_days is None or not close_time:
        return True
    try:
        close = dt.datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (close - now).days <= int(max_days)


class PollResult(dict):
    """Plain dict with attribute-ish access for logging convenience."""


def poll_once(cfg: Mapping[str, Any], storage: Storage, http: Http) -> PollResult:
    now = dt.datetime.now(dt.timezone.utc)
    now_ts = int(now.timestamp())
    run_id = storage.start_run(now_ts)

    poly_markets = poly_snaps = kalshi_markets = kalshi_snaps = 0
    backfilled = 0
    error: str | None = None

    try:
        # ---- Polymarket ----
        poly = PolymarketClient(http)
        raw_by_condition: dict[str, dict[str, Any]] = {}
        candidates: list[tuple[Any, float | None]] = []

        uni_cfg = cfg.get("universe", {})
        floor = float(uni_cfg.get("min_liquidity_usd", 0) or 0)
        for raw in poly.iter_markets(
            max_pages=int(uni_cfg.get("scan_pages", 8)),
            stop_below_liquidity=floor or None,
        ):
            market = poly.parse_market(raw)
            if market is None:
                continue
            raw_by_condition[market.condition_id] = raw
            liquidity = raw.get("liquidityNum")
            snap = poly.parse_snapshot(raw, now_ts)
            candidates.append((
                market,
                float(liquidity) if liquidity is not None else None,
                snap.yes_price if snap else None,
            ))

        selected = select_poly_universe(candidates, cfg, now=now)
        log.info("polymarket: %d candidates -> %d selected", len(candidates), len(selected))

        poly_markets = storage.upsert_poly_markets(selected, now_ts)

        snapshots = []
        for market in selected:
            snap = poly.parse_snapshot(raw_by_condition[market.condition_id], now_ts)
            if snap is not None:
                snapshots.append(snap)
        poly_snaps = storage.insert_poly_snapshots(snapshots)

        # One-time historical backfill per market, so a fresh deploy is not
        # starting from a cold zero-history baseline.
        if cfg.get("poll", {}).get("backfill_on_first_seen", True):
            for market in selected:
                if not market.yes_token_id or storage.has_history(market.condition_id):
                    continue
                try:
                    history = poly.price_history(market.condition_id, market.yes_token_id)
                    written = storage.insert_poly_snapshots(history)
                    backfilled += written
                    log.info("backfilled %d points for %s", written, market.question[:50])
                except Exception as exc:  # one bad market must not kill the cycle
                    log.warning("backfill failed for %s: %s", market.condition_id, exc)

        # ---- Kalshi ----
        if cfg.get("kalshi", {}).get("enabled", True):
            kalshi = KalshiClient(http)
            limit = int(cfg["kalshi"].get("max_markets", 400))
            k_max_days = cfg["kalshi"].get("max_days_to_resolution")
            allowlist = cfg["kalshi"].get("series_allowlist", []) or []
            markets, snaps = [], []
            for event, raw in kalshi.iter_event_markets(
                max_pages=int(cfg["kalshi"].get("scan_pages", 8)),
                series_allowlist=allowlist,
            ):
                market = kalshi.parse_market(event, raw)
                snap = kalshi.parse_snapshot(raw, now_ts)
                if market is None or snap is None:
                    continue
                if not _within_horizon(market.close_time, now, k_max_days):
                    continue
                markets.append(market)
                snaps.append(snap)
                if len(markets) >= limit:
                    break
            kalshi_markets = storage.upsert_kalshi_markets(markets, now_ts)
            kalshi_snaps = storage.insert_kalshi_snapshots(snaps)
            log.info("kalshi: %d markets, %d snapshots", kalshi_markets, kalshi_snaps)

    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("poll cycle failed")

    storage.finish_run(
        run_id, int(time.time()),
        poly_markets=poly_markets, poly_snaps=poly_snaps,
        kalshi_markets=kalshi_markets, kalshi_snaps=kalshi_snaps,
        error=error,
    )
    return PollResult(
        run_id=run_id, poly_markets=poly_markets, poly_snapshots=poly_snaps,
        backfilled=backfilled, kalshi_markets=kalshi_markets,
        kalshi_snapshots=kalshi_snaps, error=error,
    )


def poll_loop(cfg: Mapping[str, Any], storage: Storage, http: Http) -> None:
    interval = int(cfg.get("poll", {}).get("interval_s", 300))
    log.info("poll loop starting, interval=%ds", interval)
    while True:
        started = time.time()
        result = poll_once(cfg, storage, http)
        log.info("cycle done: %s", dict(result))
        elapsed = time.time() - started
        time.sleep(max(5.0, interval - elapsed))
