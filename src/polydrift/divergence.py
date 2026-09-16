"""Does divergence predict convergence, net of fees?

For every usable pair and every Kalshi snapshot at time t:

    divergence d_t  = kalshi_mid(t) - poly_yes(t)
    forward move    = poly_yes(t + h) - poly_yes(t)
    gross           = sign(d_t) * forward move        (points captured if you
                                                       trade Polymarket toward
                                                       Kalshi's price)
    net             = gross - round-trip taker fee     (entry at poly_yes(t),
                                                       exit at poly_yes(t+h))

A "signal" is any t where |d_t| clears a threshold. The provider is judged on
its signals' mean net and hit rate against baselines evaluated on the SAME
timestamps. A provider that cannot beat "the price will drift back toward its
own recent mean" is not a forecast, it is noise with a subscription fee.

All arithmetic is in probability points per contract; multiply by size later.
"""
from __future__ import annotations

import bisect
import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .fees import polymarket_fee
from .storage import Storage

log = logging.getLogger(__name__)

Series = tuple[list[int], list[float]]   # sorted timestamps, values


def _series(rows: Iterable[Any], ts_key: str, val_key: str) -> Series:
    pts = sorted((int(r[ts_key]), float(r[val_key])) for r in rows if r[val_key] is not None)
    return [t for t, _ in pts], [v for _, v in pts]


def value_at(series: Series, t: int, max_stale_s: int) -> float | None:
    """Last value at or before t, if within max_stale_s."""
    ts, vs = series
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > max_stale_s:
        return None
    return vs[i]


def value_after(series: Series, t: int, max_ahead_s: int) -> float | None:
    """First value at or after t, if within max_ahead_s."""
    ts, vs = series
    i = bisect.bisect_left(ts, t)
    if i >= len(ts) or ts[i] - t > max_ahead_s:
        return None
    return vs[i]


@dataclass
class Bucket:
    n: int = 0
    hits: int = 0
    moved: int = 0        # windows where the Polymarket price changed at all
    gross: float = 0.0
    net: float = 0.0
    abs_div: float = 0.0
    nets: list[float] = field(default_factory=list)

    def add(self, gross: float, net: float, abs_div: float) -> None:
        self.n += 1
        self.hits += int(gross > 0)
        self.moved += int(gross != 0.0)
        self.gross += gross
        self.net += net
        self.abs_div += abs_div
        self.nets.append(net)

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def moved_rate(self) -> float | None:
        """Share of windows with any price change. Near zero means the panel
        is dead and every other number in this bucket is fee noise."""
        return self.moved / self.n if self.n else None

    @property
    def mean_net(self) -> float | None:
        return self.net / self.n if self.n else None

    @property
    def mean_gross(self) -> float | None:
        return self.gross / self.n if self.n else None

    @property
    def mean_abs_div(self) -> float | None:
        return self.abs_div / self.n if self.n else None

    @property
    def t_stat(self) -> float | None:
        """Mean net / standard error. Below ~2 means 'could easily be zero'."""
        if self.n < 3:
            return None
        m = self.net / self.n
        var = sum((x - m) ** 2 for x in self.nets) / (self.n - 1)
        se = math.sqrt(var / self.n) if var > 0 else 0.0
        return m / se if se > 0 else None


@dataclass
class Result:
    horizon_s: int
    providers: dict[str, Bucket]
    pairs_evaluated: int
    timestamps_evaluated: int


def _round_trip(entry: float, exit_: float, rate: float) -> float:
    e = polymarket_fee(1.0, max(0.0, min(1.0, entry)), rates={"x": rate}, category="x").fee_usd
    x = polymarket_fee(1.0, max(0.0, min(1.0, exit_)), rates={"x": rate}, category="x").fee_usd
    return e + x


def evaluate(storage: Storage, cfg: Mapping[str, Any], horizon_s: int) -> Result:
    dcfg = cfg.get("divergence", {})
    threshold = float(dcfg.get("signal_threshold_pts", 0.03))
    max_stale = int(dcfg.get("max_stale_s", 1800))
    max_ahead = int(dcfg.get("max_ahead_s", 1800))
    fee_rate = float(dcfg.get("fee_rate", 0.05))
    lookback_s = int(dcfg.get("reversion_lookback_s", 6 * 3600))
    # Overlapping windows are not independent observations: one repricing
    # event lands in every 5-minute timestamp whose forward window contains
    # it, inflating n by ~horizon/cadence and making t-stats fiction. Sample
    # timestamps at least one horizon apart per pair (day-7 lesson, 2026-09-16).
    non_overlapping = bool(dcfg.get("non_overlapping", True))

    providers: dict[str, Bucket] = {
        "kalshi": Bucket(),
        "baseline:reversion": Bucket(),
        "baseline:random": Bucket(),
    }
    pairs = storage.usable_pairs()
    n_ts = 0

    for pair in pairs:
        poly_rows = storage.conn.execute(
            "SELECT ts, yes_price FROM poly_snapshots WHERE condition_id=? ORDER BY ts",
            (pair["condition_id"],),
        ).fetchall()
        k_rows = storage.conn.execute(
            """SELECT ts, COALESCE((yes_bid + yes_ask) / 2.0, last_price) AS mid
               FROM kalshi_snapshots WHERE ticker=? ORDER BY ts""",
            (pair["ticker"],),
        ).fetchall()
        poly = _series(poly_rows, "ts", "yes_price")
        kal = _series(k_rows, "ts", "mid")
        if not poly[0] or not kal[0]:
            continue

        # deterministic pseudo-random sign per timestamp so the baseline is
        # reproducible without seeding global state
        next_allowed = -1
        for t, k_mid in zip(*kal):
            if non_overlapping and t < next_allowed:
                continue
            p_now = value_at(poly, t, max_stale)
            p_fwd = value_after(poly, t + horizon_s, max_ahead)
            if p_now is None or p_fwd is None:
                continue
            n_ts += 1
            next_allowed = t + horizon_s
            move = p_fwd - p_now
            fee = _round_trip(p_now, p_fwd, fee_rate)

            # --- provider: Kalshi as the forecast
            d = k_mid - p_now
            if abs(d) >= threshold:
                g = math.copysign(1.0, d) * move
                providers["kalshi"].add(g, g - fee, abs(d))

            # --- baseline: bet the last `lookback` move reverses
            p_prev = value_at(poly, t - lookback_s, max_stale)
            if p_prev is not None:
                r = p_prev - p_now       # positive => price fell, bet it recovers
                if abs(r) >= threshold:
                    g = math.copysign(1.0, r) * move
                    providers["baseline:reversion"].add(g, g - fee, abs(r))

            # --- baseline: coin flip, same fee drag
            sign = 1.0 if (hash((pair["id"], t)) & 1) else -1.0
            providers["baseline:random"].add(sign * move, sign * move - fee, 0.0)

    return Result(horizon_s=horizon_s, providers=providers,
                  pairs_evaluated=len(pairs), timestamps_evaluated=n_ts)
