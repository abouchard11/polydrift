import time

from polydrift.config import DEFAULTS
from polydrift.http import Http


def test_throttle_spaces_out_requests():
    """Regression: Kalshi 429'd every paged /events burst in production on
    2026-09-05. Tripping a public rate limit every 5 minutes for two weeks is
    how a keyless research feed gets blocked outright."""
    h = Http(min_interval_s=0.05)
    start = time.monotonic()
    for _ in range(4):
        h._throttle()
    assert time.monotonic() - start >= 0.15
    h.close()


def test_throttle_is_a_noop_when_disabled():
    h = Http(min_interval_s=0.0)
    start = time.monotonic()
    for _ in range(50):
        h._throttle()
    assert time.monotonic() - start < 0.05
    h.close()


def test_from_config_reads_the_throttle_and_user_agent():
    h = Http.from_config(DEFAULTS)
    assert h.min_interval_s == DEFAULTS["http"]["min_interval_s"]
    # Cloudflare 403s the stock python agent on clob.polymarket.com.
    assert "polydrift" in h.session.headers["User-Agent"]
    h.close()


def test_default_throttle_is_enabled():
    assert DEFAULTS["http"]["min_interval_s"] > 0
