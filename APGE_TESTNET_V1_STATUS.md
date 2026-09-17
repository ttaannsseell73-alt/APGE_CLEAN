# APGE TESTNET V1 STATUS

## Current canonical APGE-03 state

Base: `0c94e8a75e09a8cec0a57179c154f057f83cce04`

Branch: `apge-03-controlled-grid-validation`

Draft PR: `#12`

Truthful status: **OFFLINE VALIDATED / AUTHENTICATED BINANCE FUTURES TESTNET GATE PENDING**.

## Offline validated

The current APGE V1 path is validated offline with the real project modules and deterministic mocks:

- deterministic grid proposal generation
- signed BUY/SELL position accounting
- side-aware worst-case exposure limits
- immutable order side/symbol tracking
- authoritative cancel -> RiskEngine lifecycle
- missing/invalid cancel fill totals fail closed into reconciliation
- duplicate fill and duplicate cancel idempotency
- cancel/fill race handling
- persistence/risk reconciliation rebuild
- UNKNOWN submission fail-closed behavior
- deterministic desired-vs-live grid diffing
- cancel-before-create reprice behavior
- 100-cycle create/cancel/recreate reservation-leak regression
- server-time offset propagation into signed requests
- strict TESTNET-only HTTP/WS endpoint validation

## Offline regression gate

GitHub Actions workflow: `.github/workflows/apge-offline.yml`

Current suite:

- collected: 118
- passed: 118
- failed: 0
- skipped: 0

`TestnetRuntime` pytest collection noise is handled explicitly in `tests/conftest.py`; warnings are not globally suppressed.

## Not yet validated against authenticated Binance Futures TESTNET

The following require an execution environment with Binance Futures TESTNET credentials and authenticated order/account endpoints:

- authenticated position/open-order preflight
- real LIMIT order placement
- real exchange acknowledgement and clientOrderId correlation
- duplicate-cycle proof against live exchange state
- real cancellation confirmation with authoritative cumulative filled quantity
- unexpected-fill handling against live TESTNET events
- final exchange/local/risk reconciliation
- authenticated user-data/WebSocket behavior over a real session

The connected Binance integration available in this workspace is read-only market data. It cannot perform the authenticated TESTNET order mutation required for this gate.

## Merge policy

PR #12 remains draft.

Do not label APGE-03 complete and do not merge it as a completed controlled-grid milestone until the authenticated TESTNET proof is attached and verified.

No real-money endpoint is authorized.
