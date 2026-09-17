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

## APGE-06 — Strategy Robustness Research

Research branch: `apge-prelive-completion-v1`

Latest train/holdout research checkout: `9ae45ab611c4ca1355d159da7fe83e09e82d13c5`

Software/regression evidence:
- fill-confirmation hardening checkout `541ef0ba057dcb65ab0a71e23064ed4c7198b71f`: 160/160 pytest PASS
- stress walk-forward workflow: PASS as a structural/risk execution
- train/holdout workflow: PASS as a reproducible research execution

Strategy evidence:
- 28-day baseline walk-forward: 1/4 positive weeks, aggregate +3.60887426
- 1 bp fill-confirmation stress: 1/4 positive, aggregate -3.48497202
- 2 bp fill-confirmation stress: 1/4 positive, aggregate -9.40008244
- 1 bp + doubled fee: 1/4 positive, aggregate -11.07364404

Train-only parameter search selected:
- base spacing 16 bps
- volatility spacing multiplier 2.0
- 5/6 positive training weeks
- aggregate train PnL +4.26467224
- median train week +0.98721201

Untouched two-week holdout:
- 1/2 positive weeks
- aggregate PnL -3.21227672
- 1 bp fill-confirmation holdout stress aggregate -1.73725250

Status: **STRATEGY ROBUSTNESS GATE NOT PASSED**

Meaning:
- engine/risk implementation remains technically validated in the tested scope;
- current adaptive strategy has not demonstrated robust positive expectancy;
- no real-money promotion is allowed from this evidence.

See `docs/APGE_06_STRATEGY_RESEARCH.md`.

## Promotion order

1. Run authenticated APGE-03 controlled Binance Futures TESTNET validation.
2. Verify generated evidence against the exact APGE-03 checkout.
3. Only then mark PR #12 ready and merge to `main`.
4. Reconcile/rebase downstream APGE-04/05/06 work onto the new canonical `main`.
5. Re-run the full offline/paper/public-data CI gate on the exact promoted checkout.
6. Continue strategy research on a genuinely unseen period; do not tune against the already-observed September holdout.
7. Require an explicit statistical robustness gate before any real-capital milestone.
8. Only after robust paper/holdout evidence and a separately approved risk gate may a very-small-capital live-validation milestone be designed.

## Safety locks

- No real-money endpoint is authorized by repository defaults.
- No market-order requirement is introduced by APGE-03.
- No leverage or margin-mode change is part of the controlled TESTNET validator.
- AI/LLM/RL does not control V1 money management or bypass RiskEngine.
- Unknown exchange outcomes fail closed into reconciliation.
- Test PASS is not treated as canonical completion without durable commit/branch, reproducibility, and explicit merge state.
- Positive aggregate backtest PnL is not treated as proof of positive expectancy.
