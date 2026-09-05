"""Fee models for both venues.

Numbers verified against primary sources on 2026-09-05:
  Polymarket — https://docs.polymarket.com/trading/fees
  Kalshi     — https://kalshi.com/docs/kalshi-fee-schedule.pdf ("Fee Schedule
               for July 2026 - 7.7.26 Update") and
               https://docs.kalshi.com/getting_started/fee_rounding

Fee regimes on both venues have changed more than once in 2026. Re-verify
before trusting any net-of-fees conclusion; every rate here is overridable from
config so a change is a config edit, not a code change.

The shape of both fee curves is the same and it matters: fee is proportional to
p*(1-p), so it is near zero at the extremes and MAXIMAL at 0.50. A divergence
signal on a market trading near even money pays the most to act on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

# --- Polymarket -------------------------------------------------------------
# fee = C * rate * p * (1 - p).  Makers are never charged.
POLYMARKET_TAKER_RATES: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.05,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,   # geopolitical / world-event markets are fee-free
}
POLYMARKET_DEFAULT_RATE = 0.05

# Share of taker fees rebated to makers, by category (context for the
# maker-side graduation step; not used in taker cost math).
POLYMARKET_MAKER_REBATE: dict[str, float] = {
    "crypto": 0.20, "sports": 0.15, "finance": 0.25, "politics": 0.25,
    "economics": 0.25, "culture": 0.25, "weather": 0.25, "other": 0.25,
    "mentions": 0.25, "tech": 0.25,
}

# --- Kalshi -----------------------------------------------------------------
# taker: round_up(M * 0.07   * C * P * (1-P)), default taker multiplier 1
# maker: round_up(M * 0.0175 * C * P * (1-P)), default maker multiplier 0
KALSHI_TAKER_RATE = 0.07
KALSHI_MAKER_RATE = 0.0175
KALSHI_DEFAULT_TAKER_MULTIPLIER = 1.0
KALSHI_DEFAULT_MAKER_MULTIPLIER = 0.0


@dataclass(frozen=True)
class FeeQuote:
    venue: str
    side: str            # 'taker' | 'maker'
    contracts: float
    price: float
    rate: float
    fee_usd: float
    note: str = ""


def _validate(contracts: float, price: float) -> None:
    if contracts < 0:
        raise ValueError("contracts must be non-negative")
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0, 1], got {price}")


def polymarket_fee(
    contracts: float,
    price: float,
    category: str | None = None,
    side: str = "taker",
    rates: Mapping[str, float] | None = None,
) -> FeeQuote:
    _validate(contracts, price)
    if side == "maker":
        return FeeQuote("polymarket", "maker", contracts, price, 0.0, 0.0,
                        "makers are never charged on Polymarket")
    table = dict(POLYMARKET_TAKER_RATES)
    if rates:
        table.update({k.lower(): float(v) for k, v in rates.items()})
    rate = table.get((category or "").strip().lower(), POLYMARKET_DEFAULT_RATE)
    fee = contracts * rate * price * (1.0 - price)
    note = "fee-free category" if rate == 0.0 else ""
    return FeeQuote("polymarket", "taker", contracts, price, rate, fee, note)


def kalshi_fee(
    contracts: float,
    price: float,
    side: str = "taker",
    multiplier: float | None = None,
) -> FeeQuote:
    """Kalshi rounds fees UP to the next whole cent, per trade. At small
    contract counts that rounding is the dominant term, not the rate."""
    _validate(contracts, price)
    if side == "maker":
        rate = KALSHI_MAKER_RATE
        mult = KALSHI_DEFAULT_MAKER_MULTIPLIER if multiplier is None else multiplier
    else:
        rate = KALSHI_TAKER_RATE
        mult = KALSHI_DEFAULT_TAKER_MULTIPLIER if multiplier is None else multiplier
    raw = mult * rate * contracts * price * (1.0 - price)
    fee = math.ceil(raw * 100.0) / 100.0 if raw > 0 else 0.0
    note = "rounded up to the cent" if raw > 0 else "zero multiplier"
    return FeeQuote("kalshi", side, contracts, price, rate * mult, fee, note)


def round_trip_cost(
    venue: str,
    contracts: float,
    entry_price: float,
    exit_price: float | None,
    category: str | None = None,
    side: str = "taker",
) -> float:
    """Total fee cost of entering and (optionally) exiting a position.

    exit_price=None means held to resolution, which incurs no exit fee on
    either venue — that is why the hold-to-resolution play survives fees at
    extreme prices while an in-and-out trade at 0.50 often does not.
    """
    fee_fn = polymarket_fee if venue == "polymarket" else kalshi_fee
    kwargs = {"category": category} if venue == "polymarket" else {}
    total = fee_fn(contracts, entry_price, side=side, **kwargs).fee_usd  # type: ignore[arg-type]
    if exit_price is not None:
        total += fee_fn(contracts, exit_price, side=side, **kwargs).fee_usd  # type: ignore[arg-type]
    return total


def breakeven_move(
    venue: str,
    entry_price: float,
    category: str | None = None,
    exit_price: float | None = None,
    contracts: float = 100.0,
) -> float:
    """Per-contract favourable price move needed to break even after fees —
    the bar a divergence has to clear before it is worth acting on.

    `contracts` matters on Kalshi and only on Kalshi: fees there are rounded UP
    to the whole cent per trade, so evaluating at 1 contract charges $0.02
    against a $0.0175 true cost and overstates the bar by ~14%. Quote this at a
    realistic size, not at 1.
    """
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    exit_at = entry_price if exit_price is None else exit_price
    return round_trip_cost(venue, contracts, entry_price, exit_at,
                           category=category) / contracts
