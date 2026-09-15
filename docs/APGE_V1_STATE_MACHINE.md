# APGE V1 State Machine

This document defines the operational security states and market regimes for APGE V1. The state machine enforces deterministic, safe behavior under all conditions.

## 1. Operational Security States

These states define the system's ability to operate safely, connect to exchanges, and maintain data integrity. Operational states override all market regimes. Risk Engine vetoes apply within these states but cannot override operational bans.

Instead of numerical priority, concurrency is handled by strict transition rules:
* **HALTED Lock:** `HALTED` blocks all automated order/cancel actions. It cannot auto-clear via new connection or data. It wins over all other states.
* **Connection Dominance:** A connection loss (`CONNECTION_LOST`) immediately disables all order and cancel submissions, even if the system was in `INITIALIZING` or `RECONCILING`.
* **Indeterminate Mismatch:** If conflicting data is detected and it is unknown whether it is resolvable, the system blocks new orders and defaults to `RECONCILING`. If reconciliation fails, it escalates to `HALTED`.
* **Operational Gate:** Transitioning to `OPERATIONAL` requires verifiable data health: confirmed stream freshness, sequence/gap check integrity, full position/order reconciliation, and zero unresolved UNKNOWN orders.

| State | Entry Condition | Exit Condition | Allowed Actions | Denied Actions |
| :--- | :--- | :--- | :--- | :--- |
| **HALTED** | Unresolvable integrity/security breach, manual halt, or Risk Engine panic. | Manual intervention and restart only. | Read-only queries, Alerting. | ALL automated new orders, ALL automated cancels. |
| **INITIALIZING** | Application start. | API connected AND verifiable data health checks passed. | Read-only queries, Connect to API. | ALL new orders, ALL cancels. |
| **RECONCILING** | Reconnection after drop, resolvable data desync, or UNKNOWN order outcome. | Full reconciliation of positions, trades, and open orders via unique Client Order IDs. | Read-only queries, Cancel existing orders (if account data is verifiable). | New entry orders, Reduce-only orders (until account data synced). |
| **CONNECTION_LOST** | WebSocket drop OR REST API timeout OR ping fail. | Connection restored (Transitions to RECONCILING). | Local state updates, Alerting. | ALL new orders, ALL cancels. |
| **DATA_STALL** | Market data older than `Stale Data Threshold`. | Full stream freshness and integrity verified. | Read-only queries, Cancel existing orders, Reduce-only orders (ONLY if account/position data is verifiable). | New entry orders. |
| **OPERATIONAL** | Verifiable data health checks passed (freshness, integrity, zero UNKNOWN orders). | Connection loss, Data stall, Fatal error, Manual stop. | All actions permitted by Market Regime & Risk Engine. | N/A |

### Proposed Contract Rules (Review Suggestions)
* **Safe Restart & Reconnection:** At startup or upon any reconnection, the system MUST enter `INITIALIZING` or `RECONCILING`. No new entries until verifiable data health checks pass.
* **Account Data vs. Market Data Staleness:** A `DATA_STALL` in market data allows reduce-only orders, but ONLY if account, position, and order state have passed sequence/gap checks. If account data is stale or unverifiable, it transitions to `RECONCILING` or `HALTED` and blocks reduce-only orders.
* **Existing Orders Management:** Entering `DATA_STALL` triggers the cancellation of pending risk-increasing orders. A cancel request does NOT release exposure until confirmation is received.

## 2. Market Regimes

These states define the market condition and dictate the trading strategy's allowed actions, subject to Operational Security States and Risk Engine approval.

| Regime | Entry Condition | Exit Condition | Allowed Actions | Denied Actions |
| :--- | :--- | :--- | :--- | :--- |
| **NEUTRAL** | Volatility and trend indicators within normal bounds. | Trend detected, Volatility spike, Breakout. | Neutral adaptive grid placing, normal inventory management. | Aggressive directional trades. |
| **SLIGHT_UP / SLIGHT_DOWN** | Mild trend detected. | Trend accelerates (Strong), or reverts to Neutral. | Grid trading with limited directional inventory target bias. | Aggressive long/short entries. |
| **STRONG_TREND** | Trend strength exceeds `Strong Trend Threshold`. | Trend weakens below threshold. | Reduce-only orders, Cancel existing risk-increasing orders. | *Proposed V1 Conservative Rule:* ALL new exposure increases and grid entries are strictly forbidden. |
| **BREAKOUT** | Price breaks significant support/resistance levels. | Volatility subsides, price stabilizes. | Reduce-only orders, Cancel existing risk-increasing orders. | ALL new entry orders. |
| **SHOCK** | Extreme volatility or sudden price gap detected. | Market normalizes and sustains stability for `Shock Cooldown Period`. | Emergency risk reduction (if connectivity allows), Cancel existing risk-increasing orders. | ALL new entry orders. |

### Proposed Contract Rules (Review Suggestions)
* **Pending Risk-Increasing Orders:** In `STRONG_TREND`, `BREAKOUT`, and `SHOCK`, merely blocking new entries is insufficient. The system MUST actively request cancellation of pending risk-increasing orders.
* **Cancel Confirmation:** Exposure calculation MUST hold the risk of pending orders until the cancel is confirmed by the exchange.

## 3. Short Acceptance Scenarios (State Machine)

* **Mutabakat sırasında bağlantı kopması (Connection loss during reconciliation):** 
  * *Expected State:* `CONNECTION_LOST`.
  * *Allowed Action:* Read-only queries, local state updates.
  * *Reservation Outcome:* All reservations held. Submissions and cancels are denied.
* **Giderilebilir veri uyuşmazlığı (Resolvable data mismatch):** 
  * *Expected State:* `RECONCILING`.
  * *Allowed Action:* Read-only queries, cancel existing orders (if account data is verifiable). New entries blocked.
  * *Reservation Outcome:* Existing reservations held until reconciliation is complete or orders are explicitly confirmed cancelled.

## 4. Open Decisions
* **Stale Data Threshold:** Time (in ms) before market/account data is considered stale.
* **Strong Trend Threshold:** The specific metric defining a strong trend.
* **Shock Cooldown Period:** Duration of stability required to exit `SHOCK`.
* **Reconciliation Strategy:** Exactly how missing/unfilled orders during a disconnect are handled.
