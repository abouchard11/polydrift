"""polydrift CLI."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from .config import load_config
from .divergence import evaluate
from .http import Http
from .kalshi import KalshiClient
from .pairs import refresh_pairs
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


def _wrap(text: str, width: int = 88, indent: str = "      ") -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text or "(no rules text)", width=width,
                                    initial_indent=indent, subsequent_indent=indent))


def _cmd_pairs(args, cfg, storage: Storage, http: Http) -> None:
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())

    if args.refresh:
        kalshi = KalshiClient(http)
        n_events = storage.upsert_kalshi_events(
            kalshi.iter_events(max_pages=int(cfg["kalshi"].get("event_scan_pages", 60))), now_ts)
        counts = refresh_pairs(storage, cfg, now_ts, fetch_event_markets=kalshi.event_markets)
        print(f"refresh: indexed {n_events} events;", json.dumps(counts))

    for flag, status in (("approve", "approved"), ("reject", "rejected"),
                         ("unapprove", "needs_review")):
        pid = getattr(args, flag)
        if pid is not None:
            ok = storage.set_pair_status(pid, status, now_ts)
            print(f"pair {pid}: {'-> ' + status if ok else 'NOT FOUND'}")

    if args.export_decisions:
        import yaml
        out = {"approve": [], "reject": []}
        for r in storage.list_pairs(("approved", "rejected")):
            out["approve" if r["status"] == "approved" else "reject"].append(
                {"ticker": r["ticker"], "condition_id": r["condition_id"],
                 "reason": f"{r['question'][:60]} <> {r['k_title'][:40]} / {r['yes_sub_title']}"})
        print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
        return

    if args.show is not None:
        r = storage.get_pair(args.show)
        if r is None:
            print(f"pair {args.show}: NOT FOUND")
            return
        flag = "  !! WORDING CONFLICT: " + r["wording_note"] if r["wording_conflict"] else ""
        days = "unknown" if r["days_apart"] is None else f"{r['days_apart']}d apart"
        print(f"pair {r['id']}  status={r['status']}  score={r['score']:.2f}  "
              f"dates_ok={bool(r['dates_ok'])} ({days}){flag}")
        if r["days_apart"] is not None and r["days_apart"] > 365:
            print("  note: Kalshi close_time this far out is usually an early-close placeholder, "
                  "not the resolution date — judge the pair on the rules text.")
        print(f"\n  POLYMARKET  {r['question']}\n    ends {r['end_date']}")
        print(_wrap(r["poly_rules"]))
        print(f"\n  KALSHI      {r['k_title']}  /  {r['yes_sub_title']}  [{r['ticker']}]\n    closes {r['close_time']}")
        print(_wrap(r["rules_primary"]))
        if r["rules_secondary"]:
            print(_wrap(r["rules_secondary"]))
        print("\n  Read both. Same bet? -> `pairs --approve", r["id"], "`   Different bet? -> `pairs --reject", r["id"], "`")
        return

    statuses = None if args.all else ("auto_ok", "approved", "needs_review")
    rows = storage.list_pairs(statuses)
    if not rows:
        print("no pairs yet — run `pairs --refresh` after at least one poll cycle")
        return
    print(f"  {'id':>4}  {'status':12} {'score':>5}  {'dates':5}  conflict  polymarket -> kalshi")
    for r in rows:
        print(f"  {r['id']:>4}  {r['status']:12} {r['score']:>5.2f}  "
              f"{'ok' if r['dates_ok'] else 'NO':5}  {'YES' if r['wording_conflict'] else '-':8}  "
              f"{r['question'][:44]:44} -> {(r['k_title'] or '')[:30]} / {(r['yes_sub_title'] or '')[:18]}")
    print(f"\n  usable={sum(1 for r in rows if r['status'] in ('auto_ok', 'approved'))}  "
          f"needs_review={sum(1 for r in rows if r['status'] == 'needs_review')}   "
          f"`pairs --show ID` to review one")


def _fmt(v, pct=False) -> str:
    if v is None:
        return "    -  "
    return f"{v * 100:6.1f}%" if pct else f"{v * 100:+6.2f}"


def _cmd_report(args, cfg, storage: Storage) -> None:
    horizons = args.horizon or cfg.get("divergence", {}).get("horizons_s", [3600])
    st = storage.status()
    span = st.get("poly_span") or (None, None)
    print(f"recorded span   {_fmt_ts(span[0])} -> {_fmt_ts(span[1])}")
    print(f"usable pairs    {st['pairs_usable']}   (needs_review: {st['pairs_review']})")
    print(f"fee assumption  taker, general rate {cfg['divergence']['fee_rate']}, round trip; "
          f"signal threshold {cfg['divergence']['signal_threshold_pts'] * 100:.0f} pts")
    print()
    print("  units: probability points per contract.  net = gross - fees.  "
          "t-stat < 2 means 'could easily be zero'.")
    for h in horizons:
        r = evaluate(storage, cfg, horizon_s=int(h))
        print(f"\n  horizon {int(h) // 3600}h   pairs={r.pairs_evaluated}  timestamps={r.timestamps_evaluated}")
        print(f"    {'provider':22} {'n':>5}  {'moved':>7}  {'hit':>7}  {'gross':>7}  {'net':>7}  {'|div|':>7}  {'t':>5}")
        for name, b in r.providers.items():
            t = f"{b.t_stat:5.1f}" if b.t_stat is not None else "    -"
            print(f"    {name:22} {b.n:>5}  {_fmt(b.moved_rate, pct=True)}  {_fmt(b.hit_rate, pct=True)}  "
                  f"{_fmt(b.mean_gross)}  {_fmt(b.mean_net)}  {_fmt(b.mean_abs_div)}  {t}")
    print("\n  A provider is only interesting if its net beats BOTH baselines with t >= 2 "
          "on a horizon you could actually trade.")


def _cmd_radar(args, cfg, storage: Storage) -> None:
    """Run one event-window divergence scan right now (read-only)."""
    import time as _time
    from .events import mode_for, next_event
    from .radar import scan, scan_digest

    now = dt.datetime.now(dt.timezone.utc)
    active = mode_for(now)
    nxt = next_event(now)
    print(f"t-minus mode    {active.mode}"
          + (f"  (event {active.event.key})" if active.event else ""))
    print(f"next event      {nxt.key if nxt else '-'}"
          + (f"  {nxt.anchor_utc:%Y-%m-%d %H:%MZ}  priority {nxt.priority}" if nxt else ""))
    print()
    alerts = scan(storage, cfg, int(_time.time()), active.mode)
    if not alerts:
        print("no pairs over the alert threshold right now — ledger unchanged")
        return
    if args.json:
        print(json.dumps({"mode": active.mode, "digest": scan_digest(alerts),
                          "alerts": alerts}, indent=1))
        return
    print(f"  {'tier':20} {'gap':>7} {'score':>7}  {'sem':4}  {'leader':10}  pair")
    for a in alerts:
        print(f"  {a['tier']:20} {a['gap_cents']:+7.2f} {a['candidate_score_cents']:+7.2f}  "
              f"{'YES' if a['semantic_flag'] else '-':4}  {a['leader'] or '-':10}  {a['ticker']}  "
              f"{a['question'][:48]}")
        if a["semantic_flag"]:
            print(f"      semantic: {a['semantic_note']}")
    print(f"\n  {scan_digest(alerts)}   ledger: {cfg.get('radar', {}).get('alerts_file', 'data/event-alerts.jsonl')}")


def _cmd_graph(args, cfg, storage: Storage) -> None:
    """Midterm event graph: pre-mapped series + per-node cross-venue lead/lag."""
    from .graph import graph_report

    rep = graph_report(storage, lookback_s=args.lookback_hours * 3600)
    print("hierarchy: state race -> seat count -> chamber control -> balance of power\n")
    for key, node in rep["nodes"].items():
        k = ", ".join(node["kalshi"]) or "-"
        p = ", ".join(node["poly"]) or "-"
        cv = node["cross_venue"]
        lead = cv.get("leader")
        if lead:
            ll = f"{lead} leads by {cv['lag_s']}s (corr {cv['corr']})"
        elif "note" in cv:
            ll = f"({cv['note']})"
        else:
            ll = "(no callable lag)"
        print(f"  [{node['level']:15}] {node['title']}")
        print(f"      kalshi: {k[:100]}")
        print(f"      poly:   {p[:100]}")
        print(f"      lead/lag: {ll}\n")
    for e in rep["edges"]:
        print(f"  edge {e['child']} -> {e['parent']}: {e['note']}")


def _cmd_tminus(args, cfg) -> None:
    """Show the T-minus calendar and the mode each window is in right now."""
    from .events import EVENT_CALENDAR, mode_for
    now = dt.datetime.now(dt.timezone.utc)
    active = mode_for(now)
    print(f"now: {now:%Y-%m-%d %H:%MZ}   active mode: {active.mode}"
          + (f" ({active.event.key})" if active.event else ""))
    print()
    for ev in EVENT_CALENDAR:
        print(f"  {ev.anchor_utc:%Y-%m-%d %H:%MZ}  {ev.priority:4}  {ev.name}")
        for a, b in ev.high_windows_utc:
            print(f"      high-freq window {a:%m-%d %H:%MZ} -> {b:%m-%d %H:%MZ}")
        print(f"      series: {', '.join(ev.series)}")


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

    p_pairs = sub.add_parser("pairs", help="cross-venue pair registry and review gate")
    p_pairs.add_argument("--refresh", action="store_true", help="re-run the matcher now")
    p_pairs.add_argument("--show", type=int, metavar="ID", help="print both venues' rules side by side")
    p_pairs.add_argument("--approve", type=int, metavar="ID")
    p_pairs.add_argument("--reject", type=int, metavar="ID")
    p_pairs.add_argument("--unapprove", type=int, metavar="ID", help="send a decided pair back to review")
    p_pairs.add_argument("--all", action="store_true", help="include rejected pairs in the listing")
    p_pairs.add_argument("--export-decisions", action="store_true",
                         help="print current approved/rejected pairs in decisions.yaml shape")

    p_rep = sub.add_parser("report", help="does divergence predict convergence, net of fees?")
    p_rep.add_argument("--horizon", type=int, action="append", metavar="SECONDS",
                       help="evaluate this horizon (repeatable); default from config")

    p_radar = sub.add_parser("radar", help="event-window divergence scan (tiered alerts, read-only)")
    p_radar.add_argument("--json", action="store_true", help="machine-readable output")

    p_graph = sub.add_parser("graph", help="midterm event graph: pre-map + lead/lag per edge")
    p_graph.add_argument("--lookback-hours", type=int, default=24 * 7,
                         help="history window for lead/lag (default 168h)")

    sub.add_parser("tminus", help="show the T-minus event calendar and current mode")

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

        elif args.command == "pairs":
            _cmd_pairs(args, cfg, storage, http)

        elif args.command == "report":
            _cmd_report(args, cfg, storage)

        elif args.command == "radar":
            _cmd_radar(args, cfg, storage)

        elif args.command == "graph":
            _cmd_graph(args, cfg, storage)

        elif args.command == "tminus":
            _cmd_tminus(args, cfg)

        elif args.command == "list-markets":
            rows = storage.conn.execute(
                """SELECT m.question, m.end_date, s.yes_price, s.liquidity_usd
                   FROM poly_markets m
                   LEFT JOIN poly_snapshots s ON s.condition_id = m.condition_id
                        AND s.id = (SELECT id FROM poly_snapshots
                                    WHERE condition_id = m.condition_id AND source IN ('gamma','clob_mid')
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
