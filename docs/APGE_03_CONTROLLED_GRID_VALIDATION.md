# APGE-03 Controlled Grid Validation

## Canonical base

`0c94e8a75e09a8cec0a57179c154f057f83cce04`

## Current implementation branch

`apge-03-controlled-grid-validation`

Current offline head at report creation:

`13439dab035c5a4f165eab3f9c36ea124b434fbe`

## Offline validation status

- Signed position accounting: implemented and covered for BUY/SELL, partial fills, duplicates, and overfill fail-closed behavior.
- Cancel -> RiskEngine lifecycle: authoritative cumulative fill handling, idempotent cancel confirmation, missing/invalid authoritative totals fail closed into reconciliation.
- Grid lifecycle: deterministic diffing, cancel-before-create behavior, duplicate-CID protection, signed inventory behavior, repeated reprice reservation invariants.
- Reconciliation: exchange/persistence/risk rebuild hardening and signed exposure checks.
- Server time: synchronized offset propagated to signed Binance requests.
- Offline regression gate: GitHub Actions `APGE Offline Gate` PASS on branch head.
- Test count: 118 collected / 118 passed / 0 failed.
- Pytest collection warning suppression: applied on `TestnetRuntime` via `__test__ = False`.

## Testnet gate

**NOT YET CLAIMED AS PASS.**

The remaining APGE-03 gate requires authenticated Binance Futures TESTNET access for exactly one controlled 1-level grid validation (maximum one BUY + one SELL), duplicate-cycle verification, APGE-only cleanup, and final exchange/local/risk reconciliation.

No real-money endpoint is authorized for APGE-03.

Until that authenticated TESTNET proof exists, APGE-03 remains **OFFLINE VALIDATED / TESTNET PENDING** and must not be labeled `CONTROLLED TESTNET GRID VALIDATED`.
