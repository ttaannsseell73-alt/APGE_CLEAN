# APGE PROJECT STATE

Last updated: 2026-09-17

## Canonical main

`main`: `0c94e8a75e09a8cec0a57179c154f057f83cce04`

This remains the last canonical checkpoint until the authenticated APGE-03 Binance Futures TESTNET gate passes and the controlled-grid PR is merged.

## APGE-03 — Controlled Grid Validation

Branch: `apge-03-controlled-grid-validation`

Draft PR: `#12` -> `main`

Current implementation head: `de994dc66fe1b54917d2c0c447c5ad022a1f6887`

Offline CI at that checkout:
- collected: 127
- passed: 127
- failed: 0
- skipped: 0

Validated repository-side capabilities:
- signed BUY/SELL position accounting
- immutable side/symbol order metadata
- side-aware worst-case exposure limits
- authoritative cancel lifecycle
- fail-closed missing/invalid cumulative fill totals
- fill/cancel race and duplicate-event hardening
- persistence/risk/exchange reconciliation rebuild
- signed request server-time offset propagation
- deterministic desired-vs-live grid diff
- 100-cycle reprice/reservation leak regression
- TESTNET-only adapter host restrictions
- controlled-grid validator with unexpected-fill handling and no replacement exposure

Status: **OFFLINE IMPLEMENTATION PASS / AUTHENTICATED TESTNET GATE PENDING / NOT CANONICAL**

External blocker:
- authenticated Binance Futures TESTNET account/order execution. The connected Binance tool currently exposes public market data only and cannot mutate TESTNET orders.

## APGE-04 — Adaptive Grid Hardening

Working branch: `apge-prelive-completion-v1`

Draft PR: `#13` -> `apge-03-controlled-grid-validation`

Implemented:
- deterministic market-regime classifier
- adaptive volatility-based spacing
- bounded size reduction
- slight-trend inventory target
- bounded funding modifier
- STRONG_TREND/BREAKOUT/SHOCK no-new-risk gate
- adaptive controller routed through existing RiskEngine/ExecutionEngine
- deterministic lifecycle diffing
- deterministic candle backtest with fee/funding/turnover/drawdown accounting

Status: **IMPLEMENTATION PASS / NOT CANONICAL**

## APGE-05 — Paper / Sandbox Validation

Validated paper-stage commit: `bf6c747a40cd0f9f7f513e3dddec1aedbd1ebc2e`

CI:
- collected: 143
- passed: 143
- failed: 0
- skipped: 0

Implemented:
- deterministic in-memory exchange adapter
- real ExecutionEngine/Persistence/RiskEngine/Reconciler lifecycle
- authoritative paper cancel
- signed paper fills
- conservative OHLC dual-touch handling
- post-fill reconciliation
- malformed-input fail-closed tests

Status: **PAPER IMPLEMENTATION PASS / NOT CANONICAL**

## Promotion order

1. Run authenticated APGE-03 controlled Binance Futures TESTNET validation.
2. Verify generated evidence against the exact APGE-03 checkout.
3. Only then mark PR #12 ready and merge to `main`.
4. Reconcile/rebase downstream APGE-04/05 work onto the new canonical `main`.
5. Re-run the full offline/paper CI gate on the exact promoted checkout.
6. Promote adaptive/paper work only after those gates remain green.
7. Proceed to extended forward paper validation and, only after explicit statistical/risk gates, a separate very-small-capital live-validation milestone.

## Safety locks

- No real-money endpoint is authorized by repository defaults.
- No market-order requirement is introduced by APGE-03.
- No leverage or margin-mode change is part of the controlled TESTNET validator.
- AI/LLM/RL does not control V1 money management or bypass RiskEngine.
- Unknown exchange outcomes fail closed into reconciliation.
- Test PASS is not treated as canonical completion without durable commit/branch, reproducibility, and explicit merge state.
