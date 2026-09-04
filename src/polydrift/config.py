"""Configuration loading: defaults <- config.yaml <- environment overrides.

The shallow-merge bug is the one to avoid here: a top-level key present in
config.yaml must not wipe the nested defaults underneath it.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULTS: dict[str, Any] = {
    "db_path": "data/polydrift.db",
    "poll": {
        "interval_s": 300,
        "backfill_on_first_seen": True,
    },
    "universe": {
        "min_liquidity_usd": 25_000.0,
        "max_markets": 60,
        "scan_pages": 8,
        "require_end_date_future": True,
        "max_days_to_resolution": 400,
        "price_band": [0.05, 0.95],
        "exclude_question_patterns": [],
    },
    "kalshi": {
        "enabled": True,
        "max_markets": 400,
        "scan_pages": 8,
        "max_days_to_resolution": 400,
        "series_allowlist": [],
    },
    "http": {
        "timeout_s": 20,
        "max_retries": 4,
        "backoff_base_s": 1.0,
        # Polymarket's CLOB sits behind Cloudflare and returns 403 to the
        # default Python-urllib/requests agent. Verified 2026-09-04. Do not
        # remove this default.
        "user_agent": "polydrift/0.1 (read-only research harness)",
    },
}


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge `over` into `base`. Dicts merge; everything else replaces."""
    out = copy.deepcopy(dict(base))
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)

    candidate = Path(path) if path else Path(os.environ.get("POLYDRIFT_CONFIG", "config.yaml"))
    if candidate.is_file():
        loaded = yaml.safe_load(candidate.read_text()) or {}
        if not isinstance(loaded, Mapping):
            raise ValueError(f"{candidate} must contain a YAML mapping at the top level")
        cfg = deep_merge(cfg, loaded)

    # Environment always wins — this is how Railway injects the volume path.
    if env_db := os.environ.get("POLYDRIFT_DB_PATH"):
        cfg["db_path"] = env_db
    if env_interval := os.environ.get("POLYDRIFT_POLL_INTERVAL_S"):
        cfg["poll"]["interval_s"] = int(env_interval)

    return cfg
