# APGE-07 — Adaptive runner delivery

The completion branch exposed adaptive market reads and configurable policy
parameters, but the actual executable still used fixed spacing with a constant
NEUTRAL regime. The normal runner now routes both its explicitly synthetic
offline mode and authenticated TESTNET mode through the existing adaptive
controller, ExecutionEngine, persistence and RiskEngine.

## Starting point

- branch: `apge-repository-completion-v1`;
- HEAD: `c9e2ccf2c720151ae918d58b6d6927c156e4fb30`;
- tree: `fdab742722ca54541d5b59b9b2b1d1f945feed25`;
- offline CI `35220917934`: 166/166;
- release CI `35220917769`: seven jobs passed;
- canonical main remains `0c94e8a75e09a8cec0a57179c154f057f83cce04`.

## Resulting behavior

Configured interval/lookback determine the closed-candle information set.
Completed candles are selected by timestamps, with continuity and freshness
validation. Funding requires matching symbol, valid mark/index prices, a finite
signed rate and a current server-adjusted timestamp. There is no last-good-data
or zero-funding fallback. Calendar-month intervals use UTC month boundaries.

The same runtime event lock serializes market/account/trade events with adaptive
diff application. REST book refreshes cannot mask a silent/disconnected market
WebSocket. Expired listen keys block activity. The RiskEngine's validated position
is used for decisions; conflicting account position events require REST
reconciliation. If an authoritative cancel reveals a fill, replacement based on
the previous inventory is blocked until reconciliation/recalculation.

Each database is bound to a mode and symbol. A synthetic dry adapter cannot
cancel/recover existing exchange orders or replace TESTNET recovery state.
Conflicting `--dry-run --allow-testnet-orders` arguments are rejected.

Audit events contain source labels, regime, spacing, size, inventory target,
funding, input timestamps, created/canceled IDs and blocked outcomes. Failed
preflight or unresolved data/shutdown preserves an unclean checkpoint and exit
code 2. A clean shutdown requires authoritative cancellation, stopped event
ingestion and final exchange reconciliation. It does not claim a flat position
or positive expectancy.

## Direct local evidence

Linux / Python 3.12.14 / pytest 9.1.1, isolated virtual environment:

```bash
python -m pytest --collect-only -q
python -m pytest -q --junitxml=<outside-repository-evidence-path>
python -m build --wheel
```

- full suite: **205 collected / 205 passed / 0 failed / 0 errors / 0 skipped / 0 warnings**;
- 39 new offline regressions cover completed/invalid/stale/future/gapped input,
  calendar months, configured spacing/repricing, shock inventory gates, stream
  loss/expiry, account disagreement, cancel-fill race, database isolation and
  shutdown failure;
- wheel installed into a separate virtual environment, outside the source tree;
- installed executable: two adaptive synthetic cycles, zero active intents and
  clean shutdown, spacing 80.0004 from the default 8 bps policy;
- build dependencies observed: setuptools 84.0.0 / wheel 0.48.0;
- source hashes and environment summary: `validation/apge_07_local.json`.

Authenticated-path regression uses a stateful offline HTTP fixture and the real
core components. It is not an authenticated Binance acceptance result. No TESTNET
keys were available, and the connected Binance tools support public reads only.

The release workflow includes an isolated wheel build/install/adaptive smoke job
and uploads the wheel. Exact final commit/CI evidence is recorded in
`PROJECT_STATE.md`; no main integration is claimed by this report.

## Remaining gates

APGE-03 authenticated controlled-grid acceptance remains pending. APGE-06 remains
NOT_PASSED, including the fixed July holdout documented in the research report.
No live-capital release or promotion is authorized by these software checks.
