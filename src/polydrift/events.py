"""T-minus event windows for the Q4 dislocation calendar.

Hardcoded from docs/event-calendar-90d-2026-09-26.md (kalshi-lane). The dates
are the doc's dates; if a release moves, it moves here too — this is config
that happens to live in code, on purpose: a bad date in YAML fails silently,
a bad date here fails a test.

DST: the calendar spans the Nov 1 fall-back. The slim production image has no
tzdata, so ET offsets are computed from the US DST rule (second Sunday in
March -> first Sunday in November) instead of zoneinfo.

Modes, from the calendar's "Thresholds and windows" section:
  baseline  poll normally
  t-24h     snapshot rule text; freshness checks tighten
  t-60m     book-depth capture + freshness checks
  high      T-5m -> T+15m for BLS/BEA, FOMC windows, election night

What a mode actually does here is deliberately narrow: it sets the poll
interval, the quote-freshness bound the radar will accept, and flags that
rule text should be snapshotted (the recorder already stores rules on every
cycle). Full order-book depth capture needs a schema + client change and is
NOT in this drop; the flag is recorded so the radar can tell "we were in
t-60m" from the data alone.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "EventWindow", "MODES", "CapturePlan", "EVENT_CALENDAR",
    "mode_for", "capture_plan", "next_event", "et", "et_offset",
]

MODES = ("baseline", "t24h", "t60m", "high")
_MODE_RANK = {"baseline": 0, "t24h": 1, "t60m": 2, "high": 3}


# --- Eastern Time without tzdata -------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 7 * (n - 1))


def et_offset(when: dt.datetime) -> dt.timedelta:
    """UTC offset of Eastern Time at a naive ET local time (US DST rule)."""
    dst_start = dt.datetime.combine(_nth_weekday(when.year, 3, 6, 2), dt.time(2, 0))
    dst_end = dt.datetime.combine(_nth_weekday(when.year, 11, 6, 1), dt.time(2, 0))
    if dst_start <= when < dst_end:
        return dt.timedelta(hours=-4)
    return dt.timedelta(hours=-5)


def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A naive ET wall-clock time as an aware UTC datetime."""
    local = dt.datetime(year, month, day, hour, minute)
    return (local - et_offset(local)).replace(tzinfo=dt.timezone.utc)


# --- calendar ---------------------------------------------------------------

@dataclass(frozen=True)
class EventWindow:
    key: str
    name: str
    category: str                       # 'bls_bea' | 'fomc' | 'election'
    anchor_utc: dt.datetime             # release / decision / poll-close moment
    high_windows_utc: tuple[tuple[dt.datetime, dt.datetime], ...]
    priority: str                       # doc's priority grade
    series: tuple[str, ...]             # series families to pre-map


def _bls_bea(key: str, name: str, y: int, m: int, d: int, priority: str,
             series: tuple[str, ...]) -> EventWindow:
    """8:30am ET release; high-frequency capture T-5m -> T+15m."""
    anchor = et(y, m, d, 8, 30)
    return EventWindow(key, name, "bls_bea", anchor,
                       ((et(y, m, d, 8, 25), et(y, m, d, 8, 45)),),
                       priority, series)


def _fomc(key: str, name: str, y: int, m: int, d: int) -> EventWindow:
    """2:00pm ET statement; high-frequency 1:55-2:10pm and 2:28-3:15pm
    (statement, then press conference)."""
    anchor = et(y, m, d, 14, 0)
    return EventWindow(key, name, "fomc", anchor,
                       ((et(y, m, d, 13, 55), et(y, m, d, 14, 10)),
                        (et(y, m, d, 14, 28), et(y, m, d, 15, 15))),
                       "A+",
                       ("fed-decision", "rate-thresholds", "dissents", "rate-path"))


EVENT_CALENDAR: tuple[EventWindow, ...] = (
    # --- BLS / BEA 8:30am ET releases (doc: Oct 14, Oct 29, Nov 6, Nov 10,
    #     Nov 25, Dec 4, Dec 10, Dec 23) ---
    _bls_bea("cpi-2026-09", "September CPI (Oct 14)", 2026, 10, 14, "A",
             ("cpi-headline-brackets", "cpi-core-brackets", "fed-decision")),
    _bls_bea("pce-gdp-2026-09", "September PCE + Q3 advance GDP (Oct 29)", 2026, 10, 29, "B+",
             ("pce-thresholds", "gdp-brackets", "fed-decision")),
    _bls_bea("jobs-2026-10", "October Employment Situation (Nov 6)", 2026, 11, 6, "B+",
             ("payroll-brackets", "unemployment-brackets", "fed-decision")),
    _bls_bea("cpi-2026-10", "October CPI (Nov 10)", 2026, 11, 10, "A",
             ("cpi-headline-brackets", "cpi-core-brackets", "fed-decision")),
    _bls_bea("pce-gdp-2026-10", "October PCE + Q3 second GDP (Nov 25)", 2026, 11, 25, "B+",
             ("pce-thresholds", "gdp-revisions", "fed-decision")),
    _bls_bea("jobs-2026-11", "November Employment Situation (Dec 4)", 2026, 12, 4, "B+",
             ("payroll-brackets", "unemployment-brackets", "fed-decision")),
    _bls_bea("cpi-2026-11", "November CPI (Dec 10)", 2026, 12, 10, "A",
             ("cpi-headline-brackets", "cpi-core-brackets", "rate-path")),
    _bls_bea("pce-gdp-2026-11", "November PCE + Q3 third GDP (Dec 23)", 2026, 12, 23, "B+",
             ("pce-thresholds", "gdp-revisions", "rate-path")),
    # --- FOMC (Oct 27-28, Dec 8-9; statement + presser on day two) ---
    _fomc("fomc-2026-10", "FOMC decision (Oct 28)", 2026, 10, 28),
    _fomc("fomc-2026-12", "FOMC decision + SEP (Dec 9)", 2026, 12, 9),
    # --- Election night: first poll close 6pm ET Nov 3; high-frequency until
    #     national-call convergence. The end is an assumption: the doc says
    #     "until the national control markets converge"; noon ET Nov 4 is the
    #     working bound, after which T-60m-style cadence is pointless anyway. ---
    EventWindow(
        "election-2026", "U.S. midterm elections (Nov 3)", "election",
        et(2026, 11, 3, 18, 0),
        ((et(2026, 11, 3, 18, 0), et(2026, 11, 4, 12, 0)),),
        "A++",
        ("senate-control", "house-control", "congress-balance", "seat-totals",
         "state-senate-tx", "state-senate-me", "state-senate-mi", "state-senate-oh",
         "governors"),
    ),
)


# --- mode resolution --------------------------------------------------------

@dataclass(frozen=True)
class ActiveMode:
    mode: str
    event: EventWindow | None = None

    @property
    def rank(self) -> int:
        return _MODE_RANK[self.mode]


def mode_for(now: dt.datetime) -> ActiveMode:
    """Most intense active mode across the calendar. T-24h and T-60m windows
    are [anchor - lead, anchor): once the release has landed the only special
    treatment left is the explicit high-frequency window around it."""
    now = now.astimezone(dt.timezone.utc) if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
    best = ActiveMode("baseline")
    for ev in EVENT_CALENDAR:
        if any(a <= now < b for a, b in ev.high_windows_utc):
            cand = ActiveMode("high", ev)
            if cand.rank > best.rank:
                best = cand
        elif et_leadin := _leadin(ev, now):
            if et_leadin.rank > best.rank:
                best = et_leadin
    return best


def _leadin(ev: EventWindow, now: dt.datetime) -> ActiveMode | None:
    if now >= ev.anchor_utc:
        return None
    if ev.anchor_utc - now <= dt.timedelta(minutes=60):
        return ActiveMode("t60m", ev)
    if ev.anchor_utc - now <= dt.timedelta(hours=24):
        return ActiveMode("t24h", ev)
    return None


def next_event(now: dt.datetime) -> EventWindow | None:
    """Next calendar anchor strictly after `now` (for digests / logs)."""
    now = now.astimezone(dt.timezone.utc) if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
    upcoming = [e for e in EVENT_CALENDAR if e.anchor_utc > now]
    return min(upcoming, key=lambda e: e.anchor_utc) if upcoming else None


# --- what a mode changes ----------------------------------------------------

@dataclass(frozen=True)
class CapturePlan:
    mode: str
    interval_s: int | None      # None -> keep the configured poll interval
    freshness_max_s: int        # quotes older than this are stale for alerts
    book_depth: bool            # t-60m+: book-depth capture requested
    rules_snapshot: bool        # t-24h+: snapshot rule text every cycle


def capture_plan(mode: str, cfg: Mapping[str, float] | None = None) -> CapturePlan:
    cfg = cfg or {}
    if mode == "high":
        return CapturePlan("high", interval_s=int(cfg.get("high_interval_s", 30)),
                           freshness_max_s=int(cfg.get("high_freshness_s", 120)),
                           book_depth=True, rules_snapshot=True)
    if mode == "t60m":
        return CapturePlan("t60m", interval_s=int(cfg.get("t60m_interval_s", 60)),
                           freshness_max_s=int(cfg.get("t60m_freshness_s", 300)),
                           book_depth=True, rules_snapshot=True)
    if mode == "t24h":
        return CapturePlan("t24h", interval_s=None,
                           freshness_max_s=int(cfg.get("t24h_freshness_s", 900)),
                           book_depth=False, rules_snapshot=True)
    return CapturePlan("baseline", interval_s=None,
                       freshness_max_s=int(cfg.get("baseline_freshness_s", 1800)),
                       book_depth=False, rules_snapshot=False)
