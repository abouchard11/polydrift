import datetime as dt

from polydrift.events import (
    EVENT_CALENDAR, capture_plan, et, et_offset, mode_for, next_event,
)


def T(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc)


# --- Eastern Time / DST ------------------------------------------------------

def test_et_offsets_span_the_fall_back():
    assert et_offset(dt.datetime(2026, 10, 14, 8, 30)) == dt.timedelta(hours=-4)  # EDT
    assert et_offset(dt.datetime(2026, 11, 10, 8, 30)) == dt.timedelta(hours=-5)  # EST
    assert et_offset(dt.datetime(2026, 11, 1, 1, 30)) == dt.timedelta(hours=-4)   # 1:30am still EDT
    assert et_offset(dt.datetime(2026, 11, 1, 2, 30)) == dt.timedelta(hours=-5)   # 2:30am EST


def test_release_anchors_convert_with_dst():
    by_key = {e.key: e for e in EVENT_CALENDAR}
    assert by_key["cpi-2026-09"].anchor_utc == T(2026, 10, 14, 12, 30)   # 8:30 EDT
    assert by_key["cpi-2026-10"].anchor_utc == T(2026, 11, 10, 13, 30)   # 8:30 EST
    assert by_key["fomc-2026-10"].anchor_utc == T(2026, 10, 28, 18, 0)   # 2pm EDT
    assert by_key["fomc-2026-12"].anchor_utc == T(2026, 12, 9, 19, 0)    # 2pm EST
    assert by_key["election-2026"].anchor_utc == T(2026, 11, 3, 23, 0)   # 6pm EST poll close


def test_calendar_has_the_docs_dates():
    keys = {e.key for e in EVENT_CALENDAR}
    assert keys == {
        "cpi-2026-09", "pce-gdp-2026-09", "jobs-2026-10", "cpi-2026-10",
        "pce-gdp-2026-10", "jobs-2026-11", "cpi-2026-11", "pce-gdp-2026-11",
        "fomc-2026-10", "fomc-2026-12", "election-2026",
    }
    assert max(len(e.high_windows_utc) for e in EVENT_CALENDAR) == 2  # FOMC has two


# --- mode switching ----------------------------------------------------------

def test_modes_around_a_bls_release():
    # Oct 14 CPI, 8:30 ET = 12:30 UTC; high window 12:25 -> 12:45 UTC
    assert mode_for(T(2026, 10, 13, 11, 0)).mode == "baseline"   # T-25h
    assert mode_for(T(2026, 10, 13, 14, 0)).mode == "t24h"       # T-22.5h
    assert mode_for(T(2026, 10, 14, 11, 45)).mode == "t60m"      # T-45m
    assert mode_for(T(2026, 10, 14, 12, 24)).mode == "t60m"      # T-6m
    assert mode_for(T(2026, 10, 14, 12, 25)).mode == "high"      # window opens
    assert mode_for(T(2026, 10, 14, 12, 44)).mode == "high"
    assert mode_for(T(2026, 10, 14, 12, 45)).mode == "baseline"  # window closed


def test_fomc_double_window():
    # Oct 28: 1:55-2:10pm EDT (17:55-18:10 UTC) and 2:28-3:15pm (18:28-19:15 UTC)
    assert mode_for(T(2026, 10, 28, 17, 55)).mode == "high"
    # between the two windows the mode drops below high — it may legitimately
    # read t24h because the next morning's PCE release is <24h away
    assert mode_for(T(2026, 10, 28, 18, 11)).mode != "high"
    assert mode_for(T(2026, 10, 28, 18, 28)).mode == "high"      # presser window
    assert mode_for(T(2026, 10, 28, 19, 16)).mode != "high"
    assert mode_for(T(2026, 10, 28, 17, 10)).mode == "t60m"      # 50m before statement
    assert mode_for(T(2026, 12, 9, 18, 29)).event.key == "fomc-2026-12"


def test_election_night_window():
    assert mode_for(T(2026, 11, 3, 23, 0)).mode == "high"       # first poll close
    assert mode_for(T(2026, 11, 4, 3, 0)).mode == "high"        # vote dumps
    assert mode_for(T(2026, 11, 4, 17, 1)).mode != "high"       # after noon ET bound
    assert mode_for(T(2026, 11, 2, 23, 30)).mode == "t24h"      # day before


def test_high_wins_over_t60m_when_windows_overlap():
    # 12:25-12:45 UTC Oct 14 is inside high AND after t-60m lead-in
    m = mode_for(T(2026, 10, 14, 12, 30))
    assert m.mode == "high" and m.event.key == "cpi-2026-09"


def test_baseline_far_from_any_event():
    # mid-September 2027: calendar exhausted
    assert mode_for(T(2027, 9, 15, 0, 0)).mode == "baseline"
    assert next_event(T(2027, 9, 15, 0, 0)) is None


def test_next_event_picks_nearest_anchor():
    assert next_event(T(2026, 10, 1, 0, 0)).key == "cpi-2026-09"
    assert next_event(T(2026, 11, 4, 0, 0)).key == "jobs-2026-10"


# --- capture plans -----------------------------------------------------------

def test_capture_plans_tighten_with_mode():
    base = capture_plan("baseline")
    t24 = capture_plan("t24h")
    t60 = capture_plan("t60m")
    high = capture_plan("high")
    assert base.interval_s is None and not base.book_depth and not base.rules_snapshot
    assert t24.rules_snapshot and t24.interval_s is None
    assert t60.book_depth and t60.interval_s == 60
    assert high.book_depth and high.interval_s == 30 and high.freshness_max_s < t60.freshness_max_s


def test_capture_plan_respects_config_overrides():
    plan = capture_plan("high", {"high_interval_s": 15, "high_freshness_s": 60})
    assert plan.interval_s == 15 and plan.freshness_max_s == 60


def test_et_helper_returns_utc():
    when = et(2026, 10, 14, 8, 30)
    assert when.tzinfo == dt.timezone.utc
    assert when == T(2026, 10, 14, 12, 30)
