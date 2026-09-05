"""The poll cycle. Read-only: fetches, records, never trades."""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Mapping

from .http import Http
from .kalshi import KalshiClient
from .pairs import apply_decisions, load_decisions, refresh_due, refresh_pairs
from .polymarket import PolymarketClient
from .storage import Storage
from .universe import select_poly_universe

log = logging.getLogger(__name__)


class PollResult(dict):
    """Plain dict; exists so the type reads as 'a poll result' in logs."""


def poll_once(cfg: Mapping[str, Any], storage: Storage, http: Http) -> PollResult:
    now = dt.datetime.now(dt.timezone.utc)
    now_ts = int(now.timestamp())
    run_id = storage.start_run(now_ts)

    poly_markets = poly_snaps = kalshi_markets = kalshi_snaps = 0
    backfilled = 0
    pairs_counts: dict[str, int] | None = None
    error: str | None = None

    try:
        # ---- Polymarket ----
        poly = PolymarketClient(http)
        raw_by_condition: dict[str, dict[str, Any]] = {}
        candidates: list[tuple[Any, float | None, float | None]] = []

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
        # Two costs, deliberately separated:
        #   (a) when due (default 6h): sweep the full open-event index (titles
        #       only) and re-run the matcher. This is what discovers overlap.
        #   (b) every cycle: a cheap batched price refresh for ONLY the tickers
        #       already in the pair registry. Recording cost scales with the
        #       overlap we care about, not with Kalshi's 8k-event catalog.
        if cfg.get("kalshi", {}).get("enabled", True):
            kalshi = KalshiClient(http)
            if refresh_due(storage, cfg, now_ts):
                n_events = storage.upsert_kalshi_events(
                    kalshi.iter_events(max_pages=int(cfg["kalshi"].get("event_scan_pages", 60))),
                    now_ts,
                )
                log.info("kalshi: indexed %d open events", n_events)
                pairs_counts = refresh_pairs(storage, cfg, now_ts,
                                             fetch_event_markets=kalshi.event_markets)

            # Version-controlled decisions land on every cycle, not just on the
            # 6-hourly sweep, so a deploy that ships a new decisions.yaml takes
            # effect within minutes. Idempotent and cheap.
            apply_decisions(storage, load_decisions(cfg.get("pairs", {}).get("decisions_file")), now_ts)

            tickers = storage.pair_tickers()
            markets, snaps = [], []
            for raw in kalshi.markets_by_tickers(tickers):
                ev = {"event_ticker": raw.get("event_ticker", ""), "series_ticker": ""}
                market = kalshi.parse_market(ev, raw)
                snap = kalshi.parse_snapshot(raw, now_ts)
                if market is None or snap is None:
                    continue
                markets.append(market)
                snaps.append(snap)
            kalshi_markets = storage.upsert_kalshi_markets(markets, now_ts) if markets else 0
            kalshi_snaps = storage.insert_kalshi_snapshots(snaps)
            log.info("kalshi: refreshed %d paired tickers, %d snapshots", len(tickers), kalshi_snaps)

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
        kalshi_snapshots=kalshi_snaps, pairs=pairs_counts, error=error,
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
