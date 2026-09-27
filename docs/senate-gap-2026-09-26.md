# The live Senate-control gap, classified: real divergence vs semantic mismatch

**As of September 26, 2026, ~23:45 UTC. Written by the event-window divergence
radar build (task t_4060b487). Every number below was pulled live from both
venues' public APIs during the build and re-run through polydrift's actual
classification code (`radar.semantic_mismatch`, `radar.candidate_costs`,
`radar.classify_gap`) — see the reproduction block at the end.**

## The question

The 90-day dislocation calendar recorded a **4-point Democratic-Senate gap**
in late September: Polymarket 59% vs Kalshi ~55% (calendar doc, Nov 3 row and
footnotes 33/34/43). Is that a real cross-venue mispricing or a measurement
artifact? The calendar itself warned that the earlier comparison "constructed
Kalshi Senate control from multiple contracts with a different settlement
horizon, while Polymarket had a direct control contract."

## What the venues actually list today

**Kalshi — `CONTROLS-2026-D`** (event `CONTROLS-2026`, "Which party will win
the U.S. Senate?", leg "Democratic Party"):

- Book: **bid 0.61 / ask 0.62, mid 0.615**; ~4.8M contracts volume on the D leg.
- This is a **direct control contract**: "If the Democratic Party has won
  control of the U.S. Senate in 2026, then the market resolves to Yes."
- Settlement: may resolve **early on a consensus of media calls**; otherwise
  "victory will be determined by the party identification of the President
  pro tempore of the Senate on **February 1, 2027**." (`close_time`
  2027-02-01T15:00:00Z.)

**Polymarket — `which-party-will-win-the-senate-in-2026`, Democratic Party leg**
(condition `0x307a1ed8…f3`):

- Live CLOB midpoint: **0.625**; ~$682k liquidity, ~$3.1M volume on the D leg.
- Also a **direct control contract**, but with different mechanics: majority
  of voting seats; if no majority, **half the seats + the Vice Presidency**;
  if still ambiguous, the party of the **first selected President Pro
  Tempore** of the new Congress. Seat counting is spelled out (regular +
  special elections as of Oct 31, 2026; runoffs count even after November).
- `endDate` **2027-01-05T04:59:00Z** on the D leg.

Sibling contracts found and pre-mapped for the event graph: Kalshi
`CONTROLH-2026` (House control), `KXBALANCEPOWERCOMBO-27FEB` (balance of
power, a combo that resolves off the two CONTROL markets),
`KXD/KXRSENATEWON-26NOV03` + `KXD/KXRSENATESEATS-27` (seat totals),
`SENATETX/MI/ME/NC/GA-26` (state races); Polymarket
`which-party-will-win-the-house-in-2026`, `balance-of-power-2026-midterms`,
`texas/maine/michigan/ohio-senate-election-winner`,
`texas/michigan-governor-winner-2026`, and family.

## Classification

**1. The historical 4-point gap was predominantly a semantic mismatch.**
The September comparison measured Polymarket's direct control contract
against a **synthetic Kalshi construction** (seat-count contracts with a
different settlement horizon). That is the calendar's own footnote 43, and it
is now confirmable: Kalshi lists a direct control contract, and against it
the gap is not 4 points.

**2. The direct-vs-direct gap today is ~1.0 cent — Kalshi cheaper
(61.5¢ vs 62.5¢) — below the 2.0¢ alert threshold.** The radar classifies it
`tier: None` (no alert).

**3. What gap remains between the direct contracts is compensation, not
mispricing**, on two dimensions the semantic checker flags:

- **Settlement horizon (flagged: "horizon: settlement 27d apart").** Kalshi
  can hold capital to Feb 1 2027; Polymarket's D leg ends Jan 5 2027. The
  venue locking capital ~4 weeks longer trades *cheaper* — exactly the sign
  observed, and exactly the TUM settlement-discount mechanism the calendar
  cites. A 1¢ discount on a ~2-month lock-up is in the sane range.
- **Resolution path.** Kalshi: early media-call consensus, else PPT party on
  a fixed date. Polymarket: seat-count criteria with a VP tiebreak, ambiguous
  cases to the first-selected PPT. Mostly-aligned outcomes, different edge
  cases (e.g. a 50-50 chamber where the VP's party and the elected PPT
  diverge). One-sided source naming, so the checker does not flag `source`
  — correctly, since flagging every Kalshi-vs-Polymarket pair on "media
  calls" would be noise.

**4. Even at a hypothetical 2.5¢ gap this pair is not executable.** Live
costs through `candidate_costs` at these prices: Kalshi taker ~1.7¢ +
Polymarket politics taker ~0.95¢ + slippage 1.0¢ (1¢ Kalshi spread →
half-spread + default) + adverse-selection buffer 0.3¢ = **3.9¢ total, plus
a 1.0¢ semantic reserve on this flagged pair**. Candidate score at the
current 1¢ gap: **−2.9¢**. The fee curve peaks near even money, which is
where this market sits.

**Verdict: semantic mismatch (synthetic construction + settlement-horizon
compensation), not real divergence.** The live 1¢ direct-vs-direct gap is
consistent with lock-up compensation and sits under the alert band.

## What this changes in polydrift

- `radar.semantic_mismatch` runs in the pair review gate: a seat-count or
  combo leg paired against a direct control leg is flagged `granularity`
  and can never auto-approve (regression:
  `test_seat_count_synthetic_vs_direct_control_is_blocked`). A >21-day
  settlement-horizon difference is flagged `horizon`. Both are stored on the
  pair (`semantic_conflict`, `semantic_note`) via an in-place migration.
- The radar's execution-candidate tier requires a positive fee-adjusted
  score AND no semantic flag; a flagged pair can tier at most `alert`.
- The Senate pair itself should stay `needs_review` on the registry with the
  horizon note until a human reads both rule sets and decides the edge cases
  (VP tiebreak vs PPT identification) are acceptable.

## Reproduction

Scratch DB built from the live API pulls above
(`kalshi.CONTROLS-2026-D` book 0.61/0.62; PM CLOB mid 0.625; both venues'
full rules text), then run through the shipped code:

```
gap_cents_direct_vs_direct: -1.0
semantic:  mismatch=true  dimensions=[horizon]  note="horizon: settlement 27d apart"
costs_cents: {fees: 2.6, slippage: 1.0, adverse: 0.3, semantic_reserve: 0.0, total: 3.9}
candidate_score_cents: -2.9
tier: null
graph: senate-control node pre-maps to CONTROLS-2026-D (kalshi) and 0x307a1ed8… (poly)
```

API endpoints used (read-only): Kalshi `GET /trade-api/v2/events/CONTROLS-2026`,
`GET /markets?tickers=CONTROLS-2026-D,CONTROLS-2026-R`; Polymarket Gamma
`GET /events?slug=which-party-will-win-the-senate-in-2026`, CLOB
`POST /midpoints`, `GET /book`.
