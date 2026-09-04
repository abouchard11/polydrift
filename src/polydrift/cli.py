"""polydrift CLI."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from .config import load_config
from .http import Http
from .runner import poll_loop, poll_once
from .storage import Storage


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polydrift", description=__doc__)
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_poll = sub.add_parser("poll", help="run one poll cycle (or loop)")
    p_poll.add_argument("--loop", action="store_true", help="poll forever on the configured interval")

    sub.add_parser("status", help="show what has been recorded so far")

    p_list = sub.add_parser("list-markets", help="show the current selected universe")
    p_list.add_argument("--limit", type=int, default=25)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    cfg = load_config(args.config)
    storage = Storage(cfg["db_path"])
    http = Http.from_config(cfg)

    try:
        if args.command == "poll":
            if args.loop:
                poll_loop(cfg, storage, http)
            else:
                result = poll_once(cfg, storage, http)
                print(json.dumps(dict(result), indent=2))
                return 1 if result.get("error") else 0

        elif args.command == "status":
            st = storage.status()
            span = st.pop("poly_span", (None, None))
            print(f"db                 {st['db_path']}")
            print(f"polymarket markets {st['poly_markets']}")
            print(f"polymarket points  {st['poly_snapshots']}")
            print(f"kalshi markets     {st['kalshi_markets']}")
            print(f"kalshi points      {st['kalshi_snapshots']}")
            print(f"runs               {st['runs_total']} ({st['runs_failed']} failed)")
            print(f"last run           {_fmt_ts(st.get('last_run_ts'))} "
                  f"{'ok' if st.get('last_run_ok') else 'FAILED'}")
            if st.get("last_run_error"):
                print(f"last error         {st['last_run_error']}")
            print(f"recorded span      {_fmt_ts(span[0])} -> {_fmt_ts(span[1])}")

        elif args.command == "list-markets":
            rows = storage.conn.execute(
                """SELECT m.question, m.end_date, s.yes_price, s.liquidity_usd
                   FROM poly_markets m
                   LEFT JOIN poly_snapshots s ON s.condition_id = m.condition_id
                        AND s.id = (SELECT id FROM poly_snapshots
                                    WHERE condition_id = m.condition_id AND source='gamma'
                                    ORDER BY ts DESC LIMIT 1)
                   ORDER BY s.liquidity_usd DESC NULLS LAST LIMIT ?""",
                (args.limit,),
            ).fetchall()
            for r in rows:
                price = f"{r['yes_price']:.3f}" if r["yes_price"] is not None else "  -  "
                liq = f"{r['liquidity_usd']:>10,.0f}" if r["liquidity_usd"] is not None else "         -"
                end = (r["end_date"] or "")[:10]
                print(f"  {price}  liq={liq}  end={end}  {r['question'][:64]}")
    finally:
        http.close()
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
