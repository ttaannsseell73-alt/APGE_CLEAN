# Adaptive Perpetual Grid Engine

APGE is a deterministic perpetual grid, inventory and risk engine. Strategy
decisions pass through the existing ExecutionEngine and RiskEngine. AI does not
manage funds or bypass risk limits.

Current development branch: `apge-repository-completion-v1`.
Canonical promotion, evidence and open work are recorded in [PROJECT_STATE.md](PROJECT_STATE.md).

The default executable is an **offline synthetic dry run**. Authenticated order
mutation is restricted to Binance Futures TESTNET. Strategy robustness and the
authenticated controlled-grid acceptance gate remain open; this repository is
not a real-capital release.

## Install and run offline

Python 3.12 or newer:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
APGE_DB_PATH=apge_dry_state.sqlite3 .venv/bin/apge-bot --dry-run --cycles 5
.venv/bin/python -m pytest -q
```

On Windows, use `.venv\Scripts\python.exe` / `.venv\Scripts\apge-bot.exe`
and set `APGE_DB_PATH` in the process environment before running.

The dry runner makes no network requests. Its fixed synthetic candles and book
exercise the same adaptive controller and order lifecycle, then cancel the local
synthetic orders. A successful dry run is software evidence, not a paper PnL or
exchange-execution result.

## Adaptive runtime

The normal runner uses closed-candle regime assessment, volatility-based spacing,
bounded size reduction, capped inventory/funding bias and deterministic grid
diffing. Configured values are shown in [.env.example](.env.example); that file
is a reference, and `.env` is not automatically loaded.

`APGE_ADAPTIVE_INTERVAL` and `APGE_REGIME_LOOKBACK` control the input window.
`APGE_BASE_SPACING_BPS`, spacing bounds and
`APGE_VOLATILITY_SPACING_MULTIPLIER` control spacing.
`APGE_GRID_SPACING` is retained for the static controlled validation path and does
not select the normal adaptive runner's spacing.

In authenticated TESTNET mode:
- only completed, contiguous and fresh candles are used;
- malformed, stale or mismatched funding has no zero-rate fallback;
- actual market WebSocket freshness and user-stream health gate new risk;
- STRONG_TREND, BREAKOUT and SHOCK remove normal exposure and permit only bounded
  inventory reduction in the operational state;
- account/trade discrepancies and cancellation fills require authoritative
  reconciliation before replacement;
- unresolved outcomes preserve an unclean recovery checkpoint and exit code 2.

Each database is bound to one mode and symbol. Use a separate `APGE_DB_PATH` for
authenticated TESTNET. The dry adapter cannot recover active persisted orders.
Audit records identify `SYNTHETIC_OFFLINE` or `BINANCE_FUTURES_TESTNET` and store
decisions, created/canceled IDs, blocked cycles and recovery checkpoints.

## TESTNET and release gates

See [the runbook](docs/APGE_TESTNET_V1_RUNBOOK.md) and
[controlled-grid acceptance](docs/APGE_03_CONTROLLED_GRID_VALIDATION.md).
`--allow-testnet-orders` is the explicit mutation switch and is incompatible
with `--dry-run`; `--testnet` alone does not authorize mutation. No market order,
leverage change or margin-mode change is provided by the runner.

CI checks the deterministic suite, tracked-file credential scan, isolated wheel
installation, offline executable and read-only historical research. Research
execution can pass while the strategy gate reports `NOT_PASSED`.
See [strategy research](docs/APGE_06_STRATEGY_RESEARCH.md) for the measured losses.
