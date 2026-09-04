"""Shared HTTP client. Read-only: no credentials are ever attached."""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Mapping

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


class Http:
    def __init__(
        self,
        timeout_s: float = 20.0,
        max_retries: int = 4,
        backoff_base_s: float = 1.0,
        user_agent: str = "polydrift/0.1",
    ) -> None:
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.session = requests.Session()
        # Required: Cloudflare 403s the default agent on clob.polymarket.com.
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "Http":
        h = cfg.get("http", {})
        return cls(
            timeout_s=float(h.get("timeout_s", 20)),
            max_retries=int(h.get("max_retries", 4)),
            backoff_base_s=float(h.get("backoff_base_s", 1.0)),
            user_agent=str(h.get("user_agent", "polydrift/0.1")),
        )

    def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
                if resp.status_code in RETRYABLE_STATUS:
                    raise HttpError(resp.status_code, resp.url, resp.text)
                if resp.status_code >= 400:
                    # Non-retryable: fail loudly rather than silently recording nulls.
                    raise HttpError(resp.status_code, resp.url, resp.text)
                return resp.json()
            except (requests.RequestException, HttpError, ValueError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                sleep_s = self.backoff_base_s * (2 ** attempt) + random.uniform(0, 0.3)
                log.warning("GET %s failed (%s), retry %d/%d in %.1fs",
                            url, exc, attempt + 1, self.max_retries, sleep_s)
                time.sleep(sleep_s)
        assert last_exc is not None
        raise last_exc

    def close(self) -> None:
        self.session.close()
