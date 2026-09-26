"""The poll cycle. Read-only: fetches, records, never trades."""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Mapping

from .divergence import evaluate
from .games import refresh_game_pairs
from .http import Http
from .kalshi import KalshiClient
from .models import PolySnapshot
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

        # Per-cycle price = live CLOB midpoint where available; Gamma's cached
        # outcomePrices only as a fallback. Liquidity/volume still come from Gamma.
        # Paired markets that rotated out of the universe stay on the list until
        # they end, so a pair never goes one-sided.
        selected_ids = {m.condition_id for m in selected}
        sticky = [r for r in storage.paired_poly_markets() if r["condition_id"] not in selected_ids]
        mids: dict[str, float] = {}
        try:
            mids = poly.midpoints([m.yes_token_id for m in selected if m.yes_token_id]
                                  + [r["yes_token_id"] for r in sticky])
        except Exception as exc:
            log.warning("clob midpoints failed, falling back to gamma prices: %s", exc)
        snapshots = [
            PolySnapshot(r["condition_id"], now_ts, mids[r["yes_token_id"]],
                         round(1.0 - mids[r["yes_token_id"]], 6), None, None, "clob_mid")
            for r in sticky if r["yes_token_id"] in mids
        ]
        if sticky:
            log.info("polymarket: %d paired markets kept beyond the universe", len(snapshots))
        for market in selected:
            snap = poly.parse_snapshot(raw_by_condition[market.condition_id], now_ts)
            if snap is None:
                continue
            mid = mids.get(market.yes_token_id or "")
            if mid is not None:
                snap = PolySnapshot(snap.condition_id, snap.ts, mid, round(1.0 - mid, 6),
                                    snap.liquidity_usd, snap.volume_usd, "clob_mid")
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
                pruned = storage.prune_stale_events(now_ts)
                log.info("kalshi: indexed %d open events, pruned %d stale", n_events, pruned)
                pairs_counts = refresh_pairs(storage, cfg, now_ts,
                                             fetch_event_markets=kalshi.event_markets)

            # Game markets pair deterministically from identifiers; new games
            # appear daily, so this runs every cycle (it only fetches legs for
            # markets that have no pair yet).
            refresh_game_pairs(storage, cfg, now_ts, fetch_event_markets=kalshi.event_markets)

            # Version-controlled decisions land on every cycle, not just on the
            # 6-hourly sweep, so a deploy that ships a new decisions.yaml takes
            # effect within minutes. Idempotent and cheap.
            apply_decisions(storage, load_decisions(cfg.get("pairs", {}).get("decisions_file")), now_ts)

            tickers = storage.pair_tickers(
                min_review_score=cfg["kalshi"].get("tracked_min_review_score", 0.5))
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


def log_report(cfg: Mapping[str, Any], storage: Storage) -> None:
    """Emit the divergence report to the logs. The Railway volume is only
    reachable over SSH, which a project token cannot set up, so the recorder
    reports on itself instead and a check-in only needs `railway logs`."""
    try:
        st = storage.status()
        log.info("REPORT usable_pairs=%s needs_review=%s poly_points=%s kalshi_points=%s",
                 st.get("pairs_usable"), st.get("pairs_review"),
                 st.get("poly_snapshots"), st.get("kalshi_snapshots"))
        for h in cfg.get("divergence", {}).get("horizons_s", [3600]):
            r = evaluate(storage, cfg, horizon_s=int(h))
            for name, b in r.providers.items():
                t = f"{b.t_stat:.2f}" if b.t_stat is not None else "-"
                hit = f"{b.hit_rate:.3f}" if b.hit_rate is not None else "-"
                net = f"{b.mean_net:+.4f}" if b.mean_net is not None else "-"
                gross = f"{b.mean_gross:+.4f}" if b.mean_gross is not None else "-"
                moved = f"{b.moved_rate:.3f}" if b.moved_rate is not None else "-"
                log.info("REPORT h=%dh provider=%s n=%d moved=%s hit=%s gross=%s net=%s t=%s pairs=%d ts=%d",
                         int(h) // 3600, name, b.n, moved, hit, gross, net, t,
                         r.pairs_evaluated, r.timestamps_evaluated)
    except Exception:  # a report bug must never stop the recorder
        log.exception("report failed")


def poll_loop(cfg: Mapping[str, Any], storage: Storage, http: Http) -> None:
    from polydrift.web import start_if_port_set

    start_if_port_set(storage, dict(cfg))
    interval = int(cfg.get("poll", {}).get("interval_s", 300))
    report_every = int(cfg.get("poll", {}).get("report_every_cycles", 72))   # 6h at 300s
    log.info("poll loop starting, interval=%ds", interval)
    cycle = 0
    while True:
        started = time.time()
        result = poll_once(cfg, storage, http)
        log.info("cycle done: %s", dict(result))
        cycle += 1
        if cycle == 1 or (report_every > 0 and cycle % report_every == 0):
            log_report(cfg, storage)
        elapsed = time.time() - started
        time.sleep(max(5.0, interval - elapsed))
