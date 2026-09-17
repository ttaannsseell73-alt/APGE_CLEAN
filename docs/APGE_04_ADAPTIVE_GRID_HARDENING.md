# APGE-04 — Adaptive Grid Hardening

## Scope

APGE-04 extends the APGE-03 execution/risk/reconciliation foundation with a deterministic adaptive layer. The adaptive layer does **not** bypass `RiskEngine`, does not use LLM/RL for money management, and does not independently authorize risk.

## Implemented modules

- `src/apge/market_regime.py`
  - deterministic candle-based regime classification
  - separates data validity from market regime
  - emits `NEUTRAL`, `SLIGHT_UP`, `SLIGHT_DOWN`, `STRONG_TREND`, `BREAKOUT`, `SHOCK`

- `src/apge/adaptive_policy.py`
  - volatility-aware grid spacing
  - volatility-aware base-size reduction
  - bounded slight-trend inventory target
  - bounded funding-rate modifier
  - funding cannot re-enable risk in strong-trend/breakout/shock regimes
  - target inventory is capped and deterministic

- `src/apge/grid_lifecycle.py`
  - deterministic desired-vs-live lifecycle application
  - cancel-before-create behavior
  - fail-closed behavior if risk state leaves `OPERATIONAL`

- `src/apge/adaptive_controller.py`
  - regime -> policy -> proposal -> exchange-filter -> lifecycle pipeline
  - all proposals still pass through the existing `ExecutionEngine` and `RiskEngine`
  - malformed market/filter input fails closed into reconciliation

- `src/apge/backtest.py`
  - deterministic candle-level simulation
  - fee, funding, turnover, inventory and drawdown accounting
  - closed-bar-only regime inputs to avoid look-ahead in classification
  - conservative dual-touch handling to avoid granting synthetic same-bar round-trip profit
  - explicitly does not model liquidation/margin mechanics and is not live-safety proof

## Safety invariants

1. `STRONG_TREND`, `BREAKOUT`, and `SHOCK` cannot create new risk-increasing exposure.
2. Funding is only a bounded inventory-target modifier.
3. Volatility can widen spacing and reduce size; it cannot increase base size above the nominal configured amount.
4. Inventory targets remain bounded by configured maximum inventory.
5. Invalid/insufficient data fails closed.
6. Duplicate local grid intent state forces reconciliation instead of guessing.
7. Adaptive execution remains subordinate to the existing RiskEngine state machine.
8. Backtest inventory is hard-bounded by `max_inventory`.

## Deterministic regression gate

Branch: `apge-prelive-completion-v1`

Validated commit before this report:
`0315963eea64c938852ec9190710828f9f7ef6e2`

GitHub Actions result:
- collected: 139
- passed: 139
- failed: 0
- skipped: 0

Coverage includes:
- regime classification boundaries
- trend/breakout/shock risk gating
- funding and volatility policy bounds
- inventory-target skew behavior
- adaptive controller fail-closed behavior
- duplicate-local-intent reconciliation
- deterministic backtest repeatability
- hard inventory limit
- conservative dual-touch rule
- fee monotonicity
- funding sign convention
- malformed input rejection
- all APGE-03 tests preserved

## Status

**APGE-04 IMPLEMENTATION PASS / NOT CANONICAL**

Reason it is not yet canonical:
- APGE-03 authenticated Binance Futures TESTNET controlled-grid gate is still pending.
- This branch intentionally remains downstream of the APGE-03 branch and must not be promoted over an unvalidated exchange milestone.

No real-money endpoint is authorized.
