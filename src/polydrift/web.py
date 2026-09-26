"""Minimal read-only HTTP surface: /status and /report.

Starts only when PORT is set (Railway sets it for services with a domain).
Runs in a daemon thread beside the poll loop — never blocks, never writes.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from polydrift.divergence import evaluate
from polydrift.storage import Storage


def _report_payload(storage: Storage, cfg: dict[str, Any]) -> dict[str, Any]:
    horizons = cfg.get("divergence", {}).get("horizons_s", [3600])
    st = storage.status()
    out: dict[str, Any] = {"status": st, "horizons": []}
    for h in horizons:
        r = evaluate(storage, cfg, horizon_s=int(h))
        providers = {}
        for name, b in r.providers.items():
            providers[name] = {
                "n": b.n,
                "hit_rate": b.hit_rate,
                "mean_gross": b.mean_gross,
                "mean_net": b.mean_net,
                "mean_abs_div": b.mean_abs_div,
                "t_stat": b.t_stat,
            }
        out["horizons"].append({
            "horizon_s": int(h),
            "pairs_evaluated": r.pairs_evaluated,
            "timestamps_evaluated": r.timestamps_evaluated,
            "providers": providers,
        })
    return out


def make_handler(storage: Storage, cfg: dict[str, Any]) -> type:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                if self.path.startswith("/status"):
                    payload = storage.status()
                elif self.path.startswith("/report"):
                    payload = _report_payload(storage, cfg)
                elif self.path in ("/", "/health"):
                    payload = {"ok": True, "service": "polydrift", "read_only": True}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(payload, indent=1).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:  # noqa: BLE001
                msg = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

    return H


def start_if_port_set(storage: Storage, cfg: dict[str, Any]) -> None:
    port = os.environ.get("PORT")
    if not port:
        return
    srv = ThreadingHTTPServer(("0.0.0.0", int(port)), make_handler(storage, cfg))
    t = threading.Thread(target=srv.serve_forever, name="polydrift-web", daemon=True)
    t.start()
    print(f"polydrift web: serving /status /report on :{port}", flush=True)
