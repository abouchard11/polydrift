"""Plain data carriers. Deliberately dumb — parsing lives in the clients."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolyMarket:
    condition_id: str
    gamma_id: str
    question: str
    slug: str
    description: str
    end_date: str | None
    yes_token_id: str | None
    no_token_id: str | None
    active: bool
    closed: bool


@dataclass(frozen=True)
class PolySnapshot:
    condition_id: str
    ts: int                  # unix seconds, UTC
    yes_price: float | None
    no_price: float | None
    liquidity_usd: float | None
    volume_usd: float | None
    source: str              # 'gamma' | 'clob_history' | 'clob_midpoint'


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    yes_sub_title: str
    rules_primary: str
    rules_secondary: str
    close_time: str | None
    market_type: str
    status: str


@dataclass(frozen=True)
class KalshiSnapshot:
    ticker: str
    ts: int
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    open_interest: float | None
    liquidity: float | None
