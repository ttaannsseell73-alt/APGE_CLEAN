# APGE-05 — Paper / Sandbox Validation

## Purpose

APGE-05 provides a deterministic, no-credential, no-network paper-exchange path for validating the real APGE execution, persistence, reconciliation, and risk lifecycle before authenticated Binance Futures TESTNET validation.

This stage is deliberately separate from live or TESTNET exchange proof. Passing paper validation is not evidence of profitability or live-trading safety.

## Implemented

### `src/apge/paper_adapter.py`

A deterministic in-memory exchange adapter implementing the subset of the exchange contract used by `ExecutionEngine` and `Reconciler`:

- LIMIT GTC submission
- deterministic client-order lifecycle
- authoritative cancel response with cumulative `executedQty`
- order query
- open-order snapshot
- signed position snapshot
- closed-candle fill simulation
- no network access
- no credentials
- no leverage or margin behavior

### Conservative fill policy

OHLC bars do not reveal intrabar path. If both BUY and SELL grid levels are touched inside one candle, the simulator does not grant a synthetic two-sided round trip. It deterministically selects only one side using the same conservative rule used by the backtest engine.

### Real APGE lifecycle integration

Paper fills are emitted as parsed order-update events and are processed through the production-path components:

`PaperAdapter -> ExecutionEngine -> Persistence -> RiskEngine -> Reconciler`

The paper adapter does not bypass RiskEngine approval, persistence-before-submit semantics, signed position accounting, or reconciliation.

## Regression coverage

Validated commit:
`bf6c747a40cd0f9f7f513e3dddec1aedbd1ebc2e`

GitHub Actions:
- collected: 143
- passed: 143
- failed: 0
- skipped: 0

New APGE-05 tests prove:

1. Paper LIMIT submission reserves risk and authoritative cancel releases it.
2. BUY fill updates paper exchange position, persistence, and signed RiskEngine position consistently.
3. Reconciliation rebuilds the RiskEngine from the paper exchange snapshot and returns `OPERATIONAL`.
4. Dual-touch OHLC cannot create a synthetic two-sided same-bar round trip.
5. Malformed candle input is rejected without mutating order/risk state.

All APGE-03 and APGE-04 tests present on this branch also remain passing at the validated checkout.

## Status

**APGE-05 PAPER IMPLEMENTATION PASS / NOT CANONICAL**

Not canonical because:
- APGE-03 authenticated Binance Futures TESTNET controlled-grid proof is still pending.
- APGE-04/APGE-05 are intentionally downstream and must not be promoted over that missing exchange gate.

No real-money endpoint is authorized.
