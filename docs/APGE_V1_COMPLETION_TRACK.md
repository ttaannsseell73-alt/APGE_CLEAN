# APGE V1 Completion Track

This document is the canonical repository-side completion track after APGE-03.

## Locked architecture

- Deterministic V1 only. AI/LLM/RL cannot approve, size, route, or manage money.
- RiskEngine is the highest authority and fails closed on uncertainty.
- Healthy range -> neutral adaptive grid.
- Slight trend -> inventory-target bias; trend does not directly open an aggressive directional position.
- Strong trend / breakout / shock -> no new risk-increasing exposure; reduce-only behavior only when state integrity allows it.
- Data/API uncertainty -> reconciliation or halt; no blind retries.
- No production trading endpoint is authorized by the V1 validation path.

## Repository-side completion layers

### 1. Execution safety — implemented

- immutable approval/order metadata
- signed BUY/SELL position accounting
- authoritative cumulative-fill cancel handling
- UNKNOWN outcome reconciliation
- reservation invariants
- deterministic create/cancel/reprice lifecycle
- persistence/exchange/risk reconciliation
- server-time offset propagation
- 100-cycle reprice leak regression

### 2. Adaptive policy — implemented on `apge-v1-completion`

- deterministic regime classification
- volatility/spread-aware spacing
- slight-trend inventory-target bias
- funding-cost bias
- strong-trend/breakout/shock defensive mode
- bounded target inventory and hard absolute exposure cap

### 3. Portfolio guard — implemented on `apge-v1-completion`

- daily loss circuit breaker
- global drawdown circuit breaker
- consecutive-error breaker
- manual hard halt
- explicit reset only after halt

### 4. Research backtest — implemented on `apge-v1-completion`

- next-bar OHLC crossing model
- maker-fee accounting
- signed funding accounting
- inventory hard cap
- drawdown and halt metrics
- deterministic repeatability
- Binance public kline loader CLI

The backtest is a research approximation. It is not an exchange queue simulator and does not establish profitable expectancy by itself.

## Validation gates still external to repository logic

### APGE-03 authenticated Binance Futures TESTNET controlled grid

Required before APGE-03 can be called fully complete:

- exactly one controlled BUY + one SELL LIMIT GTC grid
- no duplicate submit on identical second cycle
- cancel only APGE-created test orders
- authoritative cancel/fill processing
- final exchange APGE open orders = 0
- final local active intents = 0
- final reservations = 0
- exchange/local inventory reconciled
- final system state OPERATIONAL

This gate needs authenticated Binance Futures TESTNET credentials in an execution environment. The connected Binance plugin is public/read-only market data and cannot perform that mutation.

## Profitability gate

No profitability claim is made. Before any real-capital deployment, the project still requires statistically meaningful backtest, paper/sandbox observation, and then a separately authorized tiny-capital live validation. Positive expectancy and controlled drawdown must be demonstrated from evidence rather than assumed.
