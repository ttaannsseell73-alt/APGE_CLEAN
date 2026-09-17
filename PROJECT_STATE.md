# APGE PROJECT STATE

Last updated: 2026-09-17

## Active repository delivery — APGE-07

User-authorized continuation branch: `apge-repository-completion-v1`.

Starting HEAD: `c9e2ccf2c720151ae918d58b6d6927c156e4fb30`.
Starting tree: `fdab742722ca54541d5b59b9b2b1d1f945feed25`.

Verified at start:
- Offline run `35220917934`: SUCCESS, 166 collected / 166 passed / 0 failed / 0 skipped.
- Repository release run `35220917769`: all seven jobs SUCCESS.
- Fixed historical research run `35220917750`: execution SUCCESS; strategy gate NOT_PASSED.
- `main` remains `0c94e8a75e09a8cec0a57179c154f057f83cce04`.
- Draft PRs #12 and #13 remain unmerged; no open standalone issues.

APGE-07 status: **REVIEW — local adaptive runner / packaging PASS; final GitHub CI pending**.

Acceptance criteria:
- The normal runner uses AdaptiveGridDecision through the existing controller,
  ExecutionEngine and RiskEngine, with configured regime/policy parameters.
- Authenticated TESTNET reads closed candles and a validated funding snapshot;
  incomplete, stale, malformed or inconsistent data cannot submit new orders.
- The default dry runner stays offline and explicitly identifies synthetic data.
- Regression tests prove regime gates, configured spacing, cancellation before
  replacement, restart isolation and truthful fail-closed shutdown evidence.
- Package installation/entrypoint and full offline suite pass on the delivered tree;
  GitHub CI is verified at the exact resulting commit.

Open gates remain APGE-03 authenticated controlled TESTNET validation and APGE-06
strategy robustness. Completing repository code does not pass either gate.

Implemented and directly validated:
- normal runner uses the configured adaptive controller; static NEUTRAL was removed;
- closed/contiguous/fresh kline and funding boundary, including calendar months;
- actual WebSocket freshness, listen-key expiry and event serialization;
- cancel-fill inventory changes and account/trade mismatches block replacement;
- mode/symbol database isolation and clean/unclean shutdown evidence;
- 205 offline tests PASS / 0 failed / 0 errors / 0 skips / 0 warnings;
- isolated wheel build, installation and two-cycle synthetic executable smoke PASS;
- unified release workflow now includes an isolated wheel job.

No new authenticated exchange gate was run. In this environment both TESTNET
credential variables are unset; available Binance tools are public read-only.
Details: `docs/APGE_07_ADAPTIVE_RUNNER.md`.

| Open work | Current status | Required evidence |
| --- | --- | --- |
| APGE-03 authenticated controlled grid | BLOCKED / #12 DRAFT | TESTNET credentials and exact-checkout controlled validation |
| APGE-06 strategy robustness | NOT_PASSED | Frozen revised hypothesis / separate training / genuinely unseen validation |
| APGE-07 adaptive runner delivery | REVIEW | Final GitHub CI and durable commit |
| Android monitoring / later operations | DEFERRED | Earlier promotion and strategy/risk gates |

The allowed independent continuation is APGE-06 research design. Previously
observed July and September holdouts must not become fresh holdouts by renaming
them or by changing parameters after inspecting their results.

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
