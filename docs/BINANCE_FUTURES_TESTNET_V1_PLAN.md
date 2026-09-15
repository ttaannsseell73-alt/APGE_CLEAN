# Binance Futures TESTNET V1 Implementation Plan

## 1. Overview and Constraints
This document defines the shortest practical path to a working Binance USD-M Futures TESTNET bot based on the APGE V1 architecture.
**Strict Constraints:**
- TESTNET ONLY. No live capital, no real API keys.
- Do not redesign APGE; the Risk Engine (`src/apge/simulator.py`) remains the highest authority.
- Preserve 100% deterministic behavior. No AI/LLM decision-making in the trading logic.
- Fail-closed defaults on any data mismatch or API uncertainty.

## 2. Architecture & Components

### 2.1 Minimum Exchange Adapter Responsibilities
The Exchange Adapter must strictly translate APGE V1 state machine commands and Risk Engine decisions to/from the Binance Testnet API.
- **REST API:** Establish initial state, fetch account balances, fetch open orders, submit/cancel orders.
- **WebSocket Streams:** Maintain User Data Stream (execution reports, account updates) and Market Data Stream (ticker/book).
- **Latency & Ping:** Respond to Ping/Pong to maintain WebSocket connections and handle reconnects deterministically.
- **Data Translation:** Map Binance `ClientOrderId` to internal APGE unique order IDs for tracking.

### 2.2 Market Data Inputs
- **Ticker/Book Stream:** Subscribe to `<symbol>@bookTicker` for best bid/ask (BBA) to drive the grid logic.
- **Staleness Tracking:** Monitor local receipt time against `E` (Event time). If latency exceeds `Stale Data Threshold`, signal `DATA_STALL`.

### 2.3 Testnet Credentials/Secrets Handling
- Use `.env` file for `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET`.
- Hardcode URL to `https://testnet.binancefuture.com` and WSS to `wss://stream.binancefuture.com`.
- **Pre-flight check:** The initialization sequence must verify the endpoint URL contains "testnet" and assert failure if any production endpoint is detected.

## 3. Core Trading Logic

### 3.1 Neutral Adaptive Grid Loop
- Triggered only in `NEUTRAL` Market Regime and `OPERATIONAL` Security State.
- Reads BBA (Best Bid/Ask) and proposes staggered limit orders (bids below BBA, asks above BBA).
- Submits an `OrderProposal` to the Risk Engine. Only proceeds if an `APPROVED` token is received.
- Adjusts bid/ask spreads and sizing dynamically based on inventory, but within deterministic boundaries without AI override.

### 3.2 Order Lifecycle
1. **Proposal:** Strategy generates an entry or reduce-only limit order proposal.
2. **Approval:** Risk Engine checks limits, states, and grants an atomic reservation with a single-use approval ID.
3. **Dispatch:** Adapter validates the single-use ID and submits the order to Binance using `ClientOrderId`. Order state becomes `UNKNOWN`.
4. **Acknowledgment:** WebSocket User Data Stream returns `NEW`. Order state becomes `OPEN`. Risk reservation converts to pending exposure.
5. **Fill:** Execution reports update `PARTIALLY_FILLED` or `FILLED`.
6. **Cancel:** Order cancelled via adapter. Confirmation releases risk reservation.

### 3.3 Cancel/Replace Logic
- Binance Testnet does not support atomic cancel/replace safely under high latency without race conditions.
- **Implementation:** Two-step explicit process.
  1. Submit `CANCEL`. Wait for explicit cancellation confirmation.
  2. Release risk reservation.
  3. Submit `NEW` order via the Risk Engine approval loop.

### 3.4 Inventory Accounting
- Local tracking of `current_position` and `reservations` handled completely by the Risk Engine.
- Exchange Adapter parses `ACCOUNT_UPDATE` WebSocket events.
- Independent observed fills are summed and verified against total position data during reconciliation.

### 3.5 Fill Handling
- Inbound `ORDER_TRADE_UPDATE` events (Execution reports).
- Extract filled amount and parse it using `Decimal` for precision.
- Invoke `RiskEngine.on_fill(order_id, fill_id, amount)`. The Risk Engine automatically tracks duplicate `fill_id`s to ensure idempotency.
- If total fills exceed expected order size, the adapter triggers `RECONCILING` state.

## 4. Resilience & Error Handling

### 4.1 Restart/Reconciliation
- On startup or WebSocket drop, state defaults to `INITIALIZING` or `CONNECTION_LOST`, then `RECONCILING`.
- **Reconciliation sequence:**
  1. Fetch exchange position via REST API.
  2. Fetch all open orders via REST API.
  3. Match Binance `clientOrderId` with local `order_id`.
  4. Resolve any `UNKNOWN` orders using `RiskEngine.resolve_order()`.
  5. If discrepancies cannot be resolved (e.g. unknown local fills vs exchange), transition to `HALTED`.
  6. Transition to `OPERATIONAL` only if fully synchronized.

### 4.2 Fail-Closed Conditions
- **State Escalation:** Any unhandled exception, severe rate limit (HTTP 429), or unresolved balance discrepancy transitions the system immediately to `HALTED`.
- **Blocking Entries:** `HALTED` blocks all automated order/cancel actions. Requires manual restart.
- No blind retries on order submission timeouts.

### 4.3 API Uncertainty Handling
- If a REST order submission times out, the local order remains in `UNKNOWN` state.
- The Risk Engine gate `_has_unknown_orders()` blocks all new risk-increasing proposals.
- The system immediately transitions to `RECONCILING` to fetch the true state of the order from the exchange using its unique `ClientOrderId`.

## 5. Implementation Roadmap

### 5.1 Existing APGE Code Reused Unchanged
- `src/apge/simulator.py`: The entire Risk Engine, including state transitions, atomic reservations, fill handling, and conflict resolution will be imported and used identically as the production truth.

### 5.2 What Is Missing
- **`src/apge/binance_adapter.py`**: The actual asynchronous HTTP/WS client tailored to Binance Testnet.
- **`src/apge/grid_strategy.py`**: The neutral adaptive grid logic triggering off market data.
- **`src/apge/bot_runner.py`**: The async event loop orchestrating the adapter, strategy, and risk engine.

### 5.3 Exact Implementation Modules/Files to Add
1. `src/apge/binance_adapter.py` (Binance REST/WS integration)
2. `src/apge/grid_strategy.py` (Deterministic grid execution)
3. `src/apge/bot_runner.py` (Main entry point)
4. `tests/test_binance_adapter.py` (Mocked adapter tests)
5. `.env.example` (Template for testnet keys)

### 5.4 Exact Test Sequence Before Any Live Capital
Even though this is restricted to Testnet, the following testing sequence must be passed:
1. **Unit Tests:** Execute tests ensuring the adapter parses all Binance payloads correctly and interacts flawlessly with the immutable Risk Engine.
2. **Dry-Run Mode:** Run `bot_runner.py` with order execution disabled. Verify market data ingestion and proposal generation logging for 1 hour.
3. **Single Order Test:** Enable execution. Manually force the strategy to submit exactly 1 test order and verify its journey from `UNKNOWN` to `OPEN` to `CANCELED`.
4. **Reconciliation Test:** While running, physically disconnect the network or forcefully kill the process. Restart and verify it enters `RECONCILING`, recovers state, and cancels dangling orders without duplicating risk.
5. **Grid Test:** Let the bot run the full neutral adaptive grid for 24 hours on Testnet. Monitor for memory leaks and `DATA_STALL` recoveries.

### 5.5 Prioritized Implementation Order (Sized for One Working Day)
- **Phase 1 (2 hours):** Build `binance_adapter.py` focusing strictly on authentication, REST API for orders/positions, and data models. Hardcode testnet assertions.
- **Phase 2 (2 hours):** Implement WebSocket streams in the adapter. Hook up the BBA stream and User Data Stream.
- **Phase 3 (1.5 hours):** Write `bot_runner.py` to glue the adapter to the existing `RiskEngine` instance. Implement the startup `RECONCILING` flow.
- **Phase 4 (1.5 hours):** Implement `grid_strategy.py` leveraging the BBA stream and generating `OrderProposal`s.
- **Phase 5 (1 hour):** End-to-end local unit tests using mocked responses to simulate `UNKNOWN` timeouts and fill races.
