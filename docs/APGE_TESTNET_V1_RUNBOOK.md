# APGE TESTNET V1 RUNBOOK

This document describes how to execute and monitor the APGE Testnet V1 Integration.

## Environment Variables

For any commands interacting with the live TESTNET API (Stages B, C, or `--allow-testnet-orders`), you must export your testnet API credentials:

```bash
export BINANCE_TESTNET_API_KEY="your_testnet_key_here"
export BINANCE_TESTNET_API_SECRET="your_testnet_secret_here"
```

*Note: Never place real production credentials in these variables. This bot is strictly hardcoded to only connect to `testnet.binancefuture.com` and `fstream.binancefuture.com`. No production endpoints are allowed.*

## TESTNET Account Preparation

1. Navigate to the [Binance Futures Testnet](https://testnet.binancefuture.com/).
2. Log in and generate API Keys for the Testnet environment.
3. Ensure you have transferred dummy USDT from the testnet faucet to your USD-M Futures account so that you have the margin required for order smoke tests.

## 1. Testnet Validation Smoke Tool

Before running the full bot, run the incremental smoke tool to verify network and credentials.

### Stage A: Public Connectivity Only
Validates that you can reach the testnet server and fetch server time / exchange info without authentication.
```bash
PYTHONPATH=src python3 scripts/testnet_smoke.py --stage A
```

### Stage B: Authenticated Read-Only
Validates that your API keys work for fetching position risk and open orders. Does not place orders.
```bash
PYTHONPATH=src python3 scripts/testnet_smoke.py --stage B
```

### Stage C: Explicit TESTNET Order Smoke
**DANGER:** This will place a tiny LIMIT BUY order (e.g. 0.001 BTC @ 10000 USDT) and then immediately attempt to cancel it. It requires interactive confirmation.
```bash
PYTHONPATH=src python3 scripts/testnet_smoke.py --stage C
```

## 2. Bot Execution

### Dry Run Command
The bot defaults to an offline synthetic DRY RUN if no mutation flag is specified.
It does not connect to Binance. It uses synthetic candles/book/filters with the
real adaptive controller, persistence and risk/execution lifecycle, then cancels
its local synthetic orders. Audit source: `SYNTHETIC_OFFLINE`.
```bash
PYTHONPATH=src python3 -m apge.bot_runner
```

### Live Testnet Orders Command
To allow the bot to place limit orders on the Testnet, you must explicitly pass the `--allow-testnet-orders` flag.
Use a separate `APGE_DB_PATH` from your dry run; each database is bound to its
runner mode and symbol. `--dry-run --allow-testnet-orders` is rejected.
```bash
APGE_DB_PATH=apge_testnet_state.sqlite3 PYTHONPATH=src python3 -m apge.bot_runner --allow-testnet-orders
```

## Expected Outputs

Upon successful startup in Live Mode, you should see logs similar to:
1. `Server time synchronized. Offset: Xms`
2. `Loaded exchange info for BTCUSDT. TickSize: 0.1, StepSize: 0.001`
3. `Reconciling state...`
4. `System is OPERATIONAL. Starting grid loop.`
5. Once both WebSocket streams are healthy and the actual market stream is fresh,
   the bot reads completed klines and a current funding snapshot, computes the
   configured adaptive decision, and cancels obsolete orders before replacement.

`APGE_GRID_SPACING` belongs to the static controlled validation path. The normal
runner uses the adaptive variables in `.env.example`, including the configured
interval, lookback, spacing bounds, volatility multiplier and inventory/funding
caps. No `.env` file is automatically loaded.

Malformed/stale/incomplete candle or funding input cannot submit new orders.
An input failure exits 2 and preserves a dirty checkpoint and any unresolved
exchange intents for subsequent reconciliation. REST book updates cannot mark a
disconnected or silent market WebSocket healthy. Listen-key expiry blocks new
risk. Shutdown requires stopping event ingestion and a final authoritative
snapshot before a clean checkpoint is written.

## HALT Conditions

The Risk Engine and Reconciler will transition the system to `HALTED` (refusing to place new orders) under the following conditions:
* **Corrupt State:** The local SQLite database (`apge_testnet.db`) contains open orders that the exchange firmly rejects as non-existent (e.g., `-2013` error), indicating a missed cancel or local database write failure.
* **Exchange-Only Orders:** The exchange has open orders that the local database knows nothing about. The bot refuses to take them over to prevent unintended risk exposure.
* **API Gateway Errors:** Unrecoverable or unexpected 5xx responses during reconciliation.

## Restart / Reconciliation Procedure

If the bot halts or is manually stopped, restart it by simply running the boot command again.
On startup, the `Reconciler` will:
1. Fetch all local `UNKNOWN` intents and explicitly query Binance for their final state, applying any missing fills to the local DB.
2. Fetch all local `OPEN` intents and query Binance to synchronize partially filled quantities or mark them canceled.
3. Compare the known local CIDs against the live exchange orders.
4. Only if the states match perfectly will it transition to `OPERATIONAL`.

If the bot remains `HALTED` on restart due to Exchange-Only Orders:
1. Manually cancel the unknown orders via the Binance Testnet UI.
2. Ensure you have no unintended open positions.
3. Restart the bot.

## Verifying Production URLs are Impossible

The adapter (`src/apge/binance_adapter.py`) enforces the testnet at the constructor level. Look at `__init__`:
```python
if parsed_http.hostname != "testnet.binancefuture.com":
    raise ValueError(f"Invalid HTTP URL: {http_url}. TESTNET ONLY.")
if parsed_ws.hostname != "fstream.binancefuture.com":
    raise ValueError(f"Invalid WS URL: {ws_url}. TESTNET ONLY.")
```
Any attempt to pass `fapi.binance.com` will throw a `ValueError` immediately.
