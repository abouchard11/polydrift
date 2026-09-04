# polydrift

Read-only measurement harness for Polymarket and Kalshi. It records prices over
time and is built to answer one question: **does a forecast that diverges from
the market price predict subsequent convergence, net of fees?**

It never trades. It holds no keys. Both APIs it uses are public and keyless.

## Why this exists first

Per the strategy research, the game on these venues is an execution game, not a
prediction game: ~69% of participants lose, and the winning cohort is the
passive-limit-order cohort. Before committing capital to any strategy, the cheap
move is to record everything for two weeks and find out whether a measurable
edge exists at all. That recording cannot be faked retroactively, so it is the
first thing that has to run — and it has to run *unbroken*.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml     # config.yaml is gitignored
polydrift poll                          # one cycle
polydrift status                        # what's been recorded
polydrift list-markets                  # current universe
polydrift poll --loop                   # run continuously
```

## Field notes — things verified live on 2026-09-04

These are the traps that cost real time. They are encoded as tests; do not
"simplify" them away.

**Kalshi silently changed its market schema.** The widely documented
`yes_bid` / `yes_ask` / `last_price` / `volume` integer-cent fields are gone.
The live fields are decimal-dollar strings and fixed-point floats:
`yes_bid_dollars`, `yes_ask_dollars`, `last_price_dollars`, `volume_fp`,
`open_interest_fp`, `liquidity_dollars`. A client written against the old names
does not error — it records NULL prices forever. Covered by
`tests/test_kalshi.py`.

**Polymarket's CLOB 403s the default Python user agent.** `clob.polymarket.com`
sits behind Cloudflare. `urllib`/`requests` with their stock agent get 403 on
`/prices-history`; the same request with a real `User-Agent` returns 200 from
the same IP. This looks exactly like a geo-block and is not one. See `http.py`.

**Gamma's `closed=false` includes zombies.** 7 of the first 100 markets returned
had an `endDate` already months in the past while still reporting
`active=true`. Recording them pollutes any convergence measurement with series
that can never converge. See `universe.py`.

**Volume is a gamed metric; rank on liquidity.** The top-volume open markets on
Polymarket are joke/farm markets — "Will Jesus Christ return before 2027?" at
$65M volume. Consistent with the ~5% of wallets that are bot-like generating 75%
of volume.

**Liquidity alone is not enough either.** Ranking purely on liquidity returned
six markets all priced 0.0005–0.0075 — the dead legs of large multi-outcome
election markets. Nothing converges from half a basis point, and fees dominate.
Hence `universe.price_band`, default `[0.05, 0.95]`.

**Kalshi lists markets resolving in 2045 and 2070.** They never converge inside
a measurement window. Hence `kalshi.max_days_to_resolution`.

**SQLite WAL is not available on every filesystem.** Network/FUSE mounts and
some container volumes raise `disk I/O error` on `PRAGMA journal_mode=WAL`,
which would take the recorder down at start. `storage.py` degrades to a
rollback journal and logs a warning.

## Storage

SQLite, deliberately. At ~40 markets on a 5-minute cycle this is kilobytes per
day; SQLite gives transactions, a single portable file, and zero ops. Parquet
earns its place only when recording full order-book snapshots — the graduation
step toward maker-side flow — and not before. Snapshot inserts are idempotent on
their natural key, so a backfill can be re-run without duplicating history.

## Deployment

Runs on Railway from the `Dockerfile`. **The persistent volume must be mounted
at `/data`** — without it the database is ephemeral and every redeploy silently
restarts the measurement clock from zero, which is the one failure mode that
wastes weeks instead of minutes.

## Status

Built and verified end-to-end 2026-09-04: 25 offline tests passing, live poll
cycle recording Polymarket + Kalshi, CLOB historical backfill working.

Not yet built: the cross-venue pair matcher with its human review gate, the fee
model, and the divergence/convergence report. Those can be built while data
accumulates — recording cannot.
