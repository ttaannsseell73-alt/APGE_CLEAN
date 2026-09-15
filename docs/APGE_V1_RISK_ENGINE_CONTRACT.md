# APGE V1 Risk Engine Contract

The Risk Engine is the highest authority in APGE V1. The strategy layer proposes orders, but the Risk Engine determines if they can be sent to the exchange. The strategy layer cannot bypass the Risk Engine.

## 1. Audit Ordering

The order flow strictly follows this sequence:
1. **Strategy Layer:** Evaluates market regime and generates an `OrderProposal`.
2. **Risk Engine:** Receives `OrderProposal`, evaluates current risk, exposure limits, and operational states.
3. **Risk Decision:** Risk Engine attaches a `RiskDecision` (Approved / Rejected) to the proposal.
4. **Order Layer:** Only executes orders that have an explicit, valid `Approved` `RiskDecision`.

## 2. Inputs to the Risk Engine

The Risk Engine requires the following inputs to make a decision:
* **OrderProposal:** Symbol, Side, Quantity, Price, Order Type, Intent (Entry vs. Reduce-Only).
* **Current Exposure:** Total open position size, unrealized PnL, margin utilization.
* **Pending Exposure:** Aggregate size of all open/unfilled orders.
* **Operational State:** Current state from the State Machine (e.g., OPERATIONAL, RECONCILING, DATA_STALL).
* **Market Regime:** Current market state (e.g., NEUTRAL, BREAKOUT).
* **Data Health:** Stream freshness, sequence/gap check status, and reconciliation status.

## 3. Decision Outputs, Validation, and Atomic Reservation

The Risk Engine outputs a `RiskDecision` object containing:
* **Status:** `APPROVED` or `REJECTED`.
* **Rationale:** A clear, deterministic reason code.
* **Timestamp:** The exact time the decision was made.
* **Proposal ID & Decision ID:** Unique, single-use identifiers linking the decision strictly to an immutable order payload.
* **Snapshot Version:** The specific state and risk snapshot version that justified the approval.

### Proposed Contract Rules (Review Suggestions)
* **Risk Approval Validity at Submission:** Before dispatch, the Order Layer MUST validate the approval at an atomic/serial execution boundary. The validation must check: TTL, immutable order payload, single-use Decision ID, current operational state, current market regime, and the specific risk snapshot version. Any concurrent change in position, open orders, or risk limits invalidates the approval and requires re-evaluation.
* **Atomic Reservation:** To prevent multiple concurrent approvals from exceeding risk capacity, an `APPROVED` decision creates an atomic risk reservation.
* **Reservation Lifecycle:** 
  1. **Approved but not sent:** Risk capacity is locked.
  2. **Sending started / Outcome UNKNOWN:** Order is in flight or connection dropped during submission. Reservation is HELD.
  3. **Accepted and Open:** Exchange acknowledged the order. Reservation transfers to pending exposure.
  4. **Partially filled:** The filled amount transfers to position risk. The remaining amount stays in reservation/pending exposure. (No double counting).
  5. **Fully filled:** Entire reservation transfers to position risk.
  6. **Remaining amount cancelled or explicitly rejected:** Only the unexecuted remaining amount is released from reservation.

## 4. Error Behavior and Data Integrity

The Risk Engine must fail closed to protect capital.

| Scenario | Risk Engine Behavior | Order Layer Action |
| :--- | :--- | :--- |
| **Missing Account Data** | Reject all new entries. Reject reduce-only. | Block all orders. |
| **Market Data Stall** | Reject all new entries. Allow reduce-only ONLY if Account/Position data passes integrity checks. | Block entries. Process reduce-only if exchange supports it safely. |
| **Indeterminate Data Mismatch** | Block new entries. Transition to `RECONCILING`. | Block new orders. Attempt reconciliation. |
| **Unresolvable Data Conflict** | Halt system. Force transition to `HALTED`. | Block all automated orders and cancels. |
| **API/Connectivity Uncertainty** | Reject all orders. | Block all orders. |
| **Unknown Order Outcome** | Lock risk reservation. Halt new risk-increasing orders. | Enter `RECONCILING`. Do NOT send blind retries. |

### Proposed Contract Rules (Review Suggestions)
* **Reduce-Only Safety:** Reduce-only orders require verifiable position and order data (sequence/gap checks passed), verification of supported exchange position mode semantics, and valid risk approval. If these conditions are unverified, order submission is blocked.
* **Belirsiz Emir (UNKNOWN) Behavior:** A network timeout during order submission does NOT mean "order rejected". For any `UNKNOWN` outcome, new risk-increasing orders are stopped. Blind resends are forbidden. The system MUST transition to `RECONCILING` and use the unique Client Order ID to fetch the true status. Pending exposure and reservations must be held until resolved.

## 5. Short Acceptance Scenarios (Risk Engine)

* **Onaydan sonra pozisyon değişmesi (Position change after approval):** 
  * *Expected State:* Normal operation (but validation fails).
  * *Allowed Action:* Order dispatch is blocked at the atomic boundary due to snapshot version mismatch.
  * *Reservation Outcome:* Reservation is released, order must be re-evaluated.
* **Aynı onayın ikinci kez kullanılması (Double use of approval):** 
  * *Expected State:* Normal operation (but validation fails).
  * *Allowed Action:* Second dispatch attempt blocked at the atomic boundary due to single-use ID constraint.
  * *Reservation Outcome:* Rejected, no new reservation created.
* **Kısmi gerçekleşmeden sonra iptal teyidi (Cancel confirm after partial fill):** 
  * *Expected State:* Normal operation.
  * *Allowed Action:* Local state update.
  * *Reservation Outcome:* Partial fill is already in position risk. Cancel confirmation releases ONLY the remaining unexecuted reservation.
* **Gönderilmemiş onayın süresinin dolması (Expired unsent approval):** 
  * *Expected State:* Normal operation.
  * *Allowed Action:* Discard order.
  * *Reservation Outcome:* Atomically verified as never sent; reservation is completely released.
* **UNKNOWN emrin TTL süresinin dolması (Expired UNKNOWN approval):** 
  * *Expected State:* `RECONCILING`.
  * *Allowed Action:* Wait for reconciliation via Client Order ID. Blind retries blocked.
  * *Reservation Outcome:* TTL expiration does NOT release the reservation. Exposure is held until explicitly resolved by the exchange.

## 6. Locked Constraints vs. Suggestions

### Locked Decisions (Enforced)
* V1 is 100% deterministic; AI/LLM does not make risk or sizing decisions.
* Risk Engine has absolute veto power over strategy.
* No new entries during `BREAKOUT`, `SHOCK`, or data/API uncertainty.
* Reconnection requires full reconciliation before new entries.

### Open Decisions (Pending Values)
* **Risk Decision TTL:** How many milliseconds a `RiskDecision` remains valid before it expires (used alongside state invalidation).
* **Max Exposure Limits:** Maximum gross and net exposure limits per symbol and globally.
* **Drawdown Limits:** Daily or global drawdown limits that trigger a system halt.
