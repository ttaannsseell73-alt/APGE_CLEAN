"""APGE V1 offline deterministic simulator.

Implements the Risk Engine state machine, approval registry, order lifecycle,
and input validation as specified in the design documents:
  - docs/APGE_V1_STATE_MACHINE.md
  - docs/APGE_V1_RISK_ENGINE_CONTRACT.md

Thread-safety: all public mutating methods are serialised behind a single
reentrant lock (_lock).  The lock scope is deliberately coarse to keep the
implementation simple and auditable at this stage.
"""

from decimal import Decimal, InvalidOperation
import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, List, Set, Dict, Tuple


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SystemState(Enum):
    OPERATIONAL = auto()
    CONNECTION_LOST = auto()
    RECONCILING = auto()
    HALTED = auto()


class ApprovalStatus(Enum):
    VALID = auto()
    CONSUMED = auto()
    EXPIRED = auto()
    REJECTED = auto()


class OrderState(Enum):
    UNKNOWN = auto()          # submission fired, outcome not yet confirmed
    OPEN = auto()             # exchange acknowledged
    PARTIALLY_FILLED = auto()
    FILLED = auto()
    CANCELED = auto()


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class RiskApproval:
    """An approval token created *exclusively* by a RiskEngine instance.

    Fields are set at creation and must not be mutated externally.
    The engine tracks every live approval in its internal registry
    (_approval_registry) keyed by a unique approval_id.

    IMPORTANT: The engine does NOT trust the mutable fields on this
    object for capacity accounting.  It keeps a separate immutable
    internal record (_ApprovalRecord) and uses that for all
    reservation arithmetic and payload validation.
    """

    _counter = 0

    def __init__(self, approval_id: int, amount: Decimal, expires_at: float,
                 version: int, *, symbol: str = "", side: str = ""):
        self.approval_id = approval_id
        self.amount = amount
        self.expires_at = expires_at
        self.version = version
        self.status = ApprovalStatus.VALID
        # Immutable order payload bound at approval time
        self.symbol = symbol
        self.side = side


@dataclass(frozen=True)
class _ApprovalRecord:
    """Engine-internal immutable snapshot of an approval's authoritative data.

    This is what the engine uses for ALL capacity arithmetic and payload
    validation, regardless of what the caller may have mutated on the
    external RiskApproval object.
    """
    approval_id: int
    amount: Decimal
    expires_at: float
    version: int
    symbol: str
    side: str


class Order:
    """Local record of an order submitted to the exchange."""

    def __init__(self, order_id: str, amount: Decimal, *, symbol: str = "", side: str = ""):
        self.order_id = order_id
        self.initial_amount = amount
        self.symbol = symbol
        self.side = str(side).upper()
        self.filled_amount = Decimal('0')         # sum of individually seen fills
        self.cancel_confirmed_total: Optional[Decimal] = None
        self.state = OrderState.UNKNOWN
        self.cancel_requested = False
        # Track individual fill IDs seen for this order, to distinguish
        # truly new late fills from already-counted ones after cancel sync.
        self.seen_fill_ids: List[str] = []
        self.seen_fills_sum = Decimal('0')        # sum of amounts from seen_fill_ids


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------

class RiskEngine:
    """Deterministic Risk Engine with thread-safe serialisation.

    Design invariants
    -----------------
    * **Capacity:**  ``current_position + reservations <= position_limit``
      at all times.
    * **Registry:**  Only approvals stored in ``_approval_registry`` are
      honoured by ``consume_approval_and_submit``.
    * **HALTED lock:**  Once ``system_state`` is ``HALTED`` it can only be
      cleared by ``manual_reset`` (simulating human intervention).
    * **UNKNOWN gate:**  While any order is in ``UNKNOWN`` state, new
      risk-increasing approvals are denied.
    """

    def __init__(self, position_limit: Decimal, *, require_explicit_side: bool = False):
        self._lock = threading.RLock()

        self.position_limit = position_limit
        self.require_explicit_side = require_explicit_side
        self.current_position = Decimal('0')
        self.reservations = Decimal('0')
        self.system_state = SystemState.OPERATIONAL
        self.risk_version: int = 1

        # Internal registries -------------------------------------------------
        self.active_approvals: List[RiskApproval] = []
        self._approval_registry: Dict[int, _ApprovalRecord] = {}
        self._next_approval_id: int = 1
        self.orders: Dict[str, Order] = {}
        self.processed_fills: Set[str] = set()
        self.unresolved_conflicts: Set[str] = set()
        self.mock_time: float = 0.0

    # -- Clock (test-only) ---------------------------------------------------

    def get_time(self) -> float:
        return self.mock_time

    def advance_time(self, seconds: float):
        with self._lock:
            self.mock_time += seconds

    # -- Capacity query ------------------------------------------------------

    def get_available_capacity(self) -> Decimal:
        # Legacy scalar view retained for compatibility with older simulator tests.
        return self.position_limit - (self.current_position + self.reservations)

    def _pending_for_side(self, side: str) -> Decimal:
        side = self._normalize_side(side)
        total = Decimal('0')
        for record in self._approval_registry.values():
            if record.side == side:
                total += record.amount
        for order in self.orders.values():
            if order.side != side:
                continue
            if order.state in (OrderState.FILLED, OrderState.CANCELED):
                continue
            remaining = order.initial_amount - order.filled_amount
            if remaining > 0:
                total += remaining
        return total

    def get_available_capacity_for_side(self, side: str) -> Decimal:
        """Worst-case signed capacity without netting opposite pending orders."""
        side = self._normalize_side(side)
        if side == 'BUY':
            value = self.position_limit - (self.current_position + self._pending_for_side('BUY'))
        elif side == 'SELL':
            value = self.position_limit + self.current_position - self._pending_for_side('SELL')
        else:
            return Decimal('0')
        return max(Decimal('0'), value)

    # -- Internal helpers ----------------------------------------------------

    def _has_unknown_orders(self) -> bool:
        """Return True if any tracked order has an unresolved UNKNOWN state."""
        return any(o.state == OrderState.UNKNOWN for o in self.orders.values())

    def _has_data_conflicts(self) -> bool:
        """Return True if any order has a data conflict requiring resolution."""
        if self.unresolved_conflicts:
            return True
        for o in self.orders.values():
            if o.cancel_confirmed_total is not None:
                if o.seen_fills_sum > o.cancel_confirmed_total:
                    return True
        return False

    def _bump_risk_version(self):
        """Increment risk version so all pending VALID approvals become stale.

        This does NOT release order reservations — only unconsumed approval
        reservations will be released when the stale approval is next
        presented for consumption (or cleaned up).
        """
        self.risk_version += 1

    def _enter_reconciling(self):
        """Transition to RECONCILING unless a higher-priority state is active."""
        if self.system_state in (SystemState.HALTED, SystemState.CONNECTION_LOST):
            return
        self.system_state = SystemState.RECONCILING

    @staticmethod
    def _is_valid_amount(val: Decimal) -> bool:
        """Return True if val is finite and positive."""
        if not isinstance(val, Decimal):
            return False
        if val.is_nan() or val.is_infinite():
            return False
        return val > Decimal('0')

    @staticmethod
    def _is_valid_non_negative(val: Decimal) -> bool:
        """Return True if val is finite and >= 0."""
        if not isinstance(val, Decimal):
            return False
        if val.is_nan() or val.is_infinite():
            return False
        return val >= Decimal('0')

    @staticmethod
    def _normalize_side(side: str) -> str:
        return str(side or "").upper()

    def _position_delta(self, order: Order, amount: Decimal) -> Decimal:
        """Translate an absolute fill delta into signed inventory movement.

        Production runtimes require explicit BUY/SELL side. Legacy simulator
        tests may omit side; in that compatibility mode only, the historical
        positive-position behavior is retained. Explicit invalid sides are
        rejected before an Order can be created.
        """
        if order.side == "BUY":
            return amount
        if order.side == "SELL":
            return -amount
        if self.require_explicit_side:
            self.unresolved_conflicts.add(order.order_id)
            self._enter_reconciling()
            return Decimal('0')
        return amount

    # -- Approval lifecycle --------------------------------------------------

    def request_approval(self, amount: Decimal, ttl_seconds: float = 5.0,
                         *, symbol: str = "", side: str = ""
                         ) -> Optional[RiskApproval]:
        """Create a risk reservation and return an approval token.

        Returns None if:
        - system is not OPERATIONAL
        - any UNKNOWN orders exist (no new risk while uncertain)
        - amount is not positive / finite
        - insufficient capacity
        """
        with self._lock:
            normalized_side = self._normalize_side(side)
            if normalized_side and normalized_side not in ("BUY", "SELL"):
                return None
            if self.require_explicit_side and normalized_side not in ("BUY", "SELL"):
                return None
            side = normalized_side

            if self.system_state != SystemState.OPERATIONAL:
                return None

            if self._has_unknown_orders():
                return None

            # Input validation
            if not self._is_valid_amount(amount):
                return None

            if self.require_explicit_side:
                has_capacity = self.get_available_capacity_for_side(side) >= amount
            else:
                has_capacity = self.get_available_capacity() >= amount

            if has_capacity:
                aid = self._next_approval_id
                self._next_approval_id += 1
                expires_at = self.get_time() + ttl_seconds

                approval = RiskApproval(
                    aid, amount, expires_at,
                    self.risk_version, symbol=symbol, side=side)
                # Store an immutable internal record — the source of truth
                record = _ApprovalRecord(
                    approval_id=aid, amount=amount,
                    expires_at=expires_at, version=self.risk_version,
                    symbol=symbol, side=side)
                self.reservations += amount
                self.active_approvals.append(approval)
                self._approval_registry[aid] = record
                return approval
            return None

    def cleanup_expired_approvals(self):
        """Expire approvals whose TTL has passed (only unconsumed ones)."""
        with self._lock:
            now = self.get_time()
            for app in list(self.active_approvals):
                if app.status == ApprovalStatus.VALID:
                    record = self._approval_registry.get(app.approval_id)
                    if record and record.expires_at < now:
                        app.status = ApprovalStatus.EXPIRED
                        self.reservations -= record.amount
                        del self._approval_registry[app.approval_id]
            self.active_approvals = [
                a for a in self.active_approvals
                if a.status == ApprovalStatus.VALID
            ]

    def _reject_approval(self, approval: RiskApproval,
                         record: _ApprovalRecord,
                         status: ApprovalStatus) -> bool:
        """Reject an approval, release its reservation using the
        authoritative record amount, and clean up registries."""
        approval.status = status
        self.reservations -= record.amount
        self.active_approvals = [
            a for a in self.active_approvals if a is not approval]
        if record.approval_id in self._approval_registry:
            del self._approval_registry[record.approval_id]
        return False

    def consume_approval_and_submit(self, approval: RiskApproval,
                                    order_id: str, *,
                                    symbol: str = "", side: str = "",
                                    amount: Optional[Decimal] = None
                                    ) -> bool:
        """Atomically validate, consume an approval, and record the order.

        Checks performed inside the lock:
        1. System is OPERATIONAL
        2. No UNKNOWN orders exist (UNKNOWN gate at submission)
        3. Approval is in our registry (not forged / from another engine)
        4. Approval status is VALID
        5. Approval has not expired (using record's TTL)
        6. Risk version matches (using record's version)
        7. Immutable payload matches (symbol, side, amount vs record)
        8. order_id is not already in use
        """
        with self._lock:
            if self.system_state != SystemState.OPERATIONAL:
                return False

            # --- UNKNOWN gate at submission (Bug 2) ---
            if self._has_unknown_orders():
                # Release the approval reservation and reject
                record = self._approval_registry.get(
                    getattr(approval, 'approval_id', None))
                if record is not None and approval.status == ApprovalStatus.VALID:
                    return self._reject_approval(
                        approval, record, ApprovalStatus.REJECTED)
                return False

            # --- Registry check: use internal record, not the external object ---
            # Also verify exact object ownership (Bug 2)
            if approval not in self.active_approvals:
                return False
                
            record = self._approval_registry.get(
                getattr(approval, 'approval_id', None))
            if record is None:
                # Not in our registry — reject without touching reservations
                return False

            if approval.status != ApprovalStatus.VALID:
                return False

            if record.side and record.side not in ("BUY", "SELL"):
                return self._reject_approval(
                    approval, record, ApprovalStatus.REJECTED)
            if self.require_explicit_side and record.side not in ("BUY", "SELL"):
                return self._reject_approval(
                    approval, record, ApprovalStatus.REJECTED)

            # --- TTL check (from record, not the mutable object) ---
            if self.get_time() > record.expires_at:
                return self._reject_approval(
                    approval, record, ApprovalStatus.EXPIRED)

            # --- Risk version check (from record) ---
            if record.version != self.risk_version:
                return self._reject_approval(
                    approval, record, ApprovalStatus.REJECTED)

            # --- Immutable payload check against record (Bug 1) ---
            submit_symbol = symbol if symbol else record.symbol
            submit_side = side if side else record.side
            submit_amount = amount if amount is not None else record.amount
            if (submit_symbol != record.symbol or
                    submit_side != record.side or
                    submit_amount != record.amount):
                return self._reject_approval(
                    approval, record, ApprovalStatus.REJECTED)

            # --- Duplicate order_id check ---
            if order_id in self.orders:
                return self._reject_approval(
                    approval, record, ApprovalStatus.REJECTED)

            # --- Consume atomically ---
            approval.status = ApprovalStatus.CONSUMED
            self.active_approvals = [
                a for a in self.active_approvals if a is not approval]
            del self._approval_registry[record.approval_id]

            # Reservation transitions from "Approval" to "Order".
            # Use record.amount (the original), not approval.amount.
            self.orders[order_id] = Order(
                order_id, record.amount, symbol=record.symbol, side=record.side)
            return True

    # -- Fill handling -------------------------------------------------------

    def on_fill(self, order_id: str, fill_id: str, amount: Decimal):
        """Process an exchange fill event.

        Duplicate fill_ids are idempotent.  Invalid amounts (non-positive,
        NaN, Infinity) are silently dropped to protect risk integrity.
        Fills for CANCELED orders are checked against the cancel
        confirmation total using the *independently observed* fill sum
        (seen_fills_sum), not the synced filled_amount.
        Fills exceeding the remaining order quantity trigger RECONCILING.
        """
        with self._lock:
            # --- Input validation (Bug 6) ---
            if not self._is_valid_amount(amount):
                return

            if fill_id in self.processed_fills:
                return

            if order_id not in self.orders:
                return

            order = self.orders[order_id]

            # --- Canceled order: late fill handling (Bug 4) ---
            if order.state == OrderState.CANCELED:
                self.processed_fills.add(fill_id)
                if order.cancel_confirmed_total is not None:
                    # Check using independently observed fills, not the
                    # synced filled_amount (which includes the cancel
                    # sync adjustment).
                    hypothetical_observed = order.seen_fills_sum + amount
                    if hypothetical_observed > order.cancel_confirmed_total:
                        # This fill pushes observed total beyond what the
                        # cancel confirmation reported — real inconsistency.
                        self.unresolved_conflicts.add(order_id)
                        self._enter_reconciling()
                    else:
                        # Within the cancel total — this was a delayed
                        # delivery of a fill already accounted for by sync.
                        order.seen_fills_sum += amount
                        order.seen_fill_ids.append(fill_id)
                # No cancel_confirmed_total → can't evaluate, ignore
                return

            # --- Over-fill check ---
            if order.filled_amount + amount > order.initial_amount:
                self.processed_fills.add(fill_id)
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                return

            # --- Normal fill application ---
            order.filled_amount += amount
            order.seen_fills_sum += amount
            order.seen_fill_ids.append(fill_id)
            self.current_position += self._position_delta(order, amount)
            self.reservations -= amount
            self.processed_fills.add(fill_id)

            if order.filled_amount >= order.initial_amount:
                order.state = OrderState.FILLED
            else:
                order.state = OrderState.PARTIALLY_FILLED

            # Position changed → bump risk version
            self._bump_risk_version()

    # -- Cancel handling -----------------------------------------------------

    def request_cancel(self, order_id: str):
        """Request cancellation.  Does NOT release any risk."""
        with self._lock:
            if order_id in self.orders:
                self.orders[order_id].cancel_requested = True

    def on_cancel_confirmed(self, order_id: str, final_filled_amount: Decimal) -> bool:
        """Apply an authoritative cancel total. Returns True only when consistent."""
        with self._lock:
            if not self._is_valid_non_negative(final_filled_amount):
                self._enter_reconciling()
                return False

            if order_id not in self.orders:
                self._enter_reconciling()
                return False
            order = self.orders[order_id]

            if order.state == OrderState.CANCELED:
                if order.cancel_confirmed_total == final_filled_amount:
                    return True
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                return False

            if order.state == OrderState.FILLED:
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                return False

            if final_filled_amount > order.initial_amount:
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                self._bump_risk_version()
                return False

            if final_filled_amount < order.filled_amount:
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                self._bump_risk_version()
                return False

            order.state = OrderState.CANCELED
            order.cancel_confirmed_total = final_filled_amount

            diff = final_filled_amount - order.filled_amount
            if diff > 0:
                order.filled_amount += diff
                self.current_position += self._position_delta(order, diff)
                self.reservations -= diff

            remaining = order.initial_amount - order.filled_amount
            if remaining > 0:
                self.reservations -= remaining

            if self.reservations < 0:
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                return False

            self._bump_risk_version()
            return True

    def rebuild_from_authoritative_state(self, current_position: Decimal, intents: List[dict]) -> bool:
        """Rebuild in-memory risk state from an exchange/persistence reconciliation snapshot."""
        with self._lock:
            if not isinstance(current_position, Decimal) or current_position.is_nan() or current_position.is_infinite():
                self._enter_reconciling()
                return False

            rebuilt: Dict[str, Order] = {}
            reservations = Decimal('0')
            buy_pending = Decimal('0')
            sell_pending = Decimal('0')

            for intent in intents:
                status_name = str(intent.get('status', ''))
                if status_name == 'REJECTED':
                    continue
                try:
                    state = OrderState[status_name]
                    qty = Decimal(str(intent['quantity']))
                    filled = Decimal(str(intent.get('filled_quantity', '0')))
                    observed = Decimal(str(intent.get('observed_filled_quantity', filled)))
                except Exception:
                    self._enter_reconciling()
                    return False

                side = self._normalize_side(intent.get('side', ''))
                symbol = str(intent.get('symbol', ''))
                cid = str(intent.get('client_order_id', ''))
                if not cid or not symbol or side not in ('BUY', 'SELL'):
                    self._enter_reconciling()
                    return False
                if not self._is_valid_amount(qty):
                    self._enter_reconciling()
                    return False
                if not self._is_valid_non_negative(filled) or filled > qty:
                    self._enter_reconciling()
                    return False
                if not self._is_valid_non_negative(observed) or observed > filled:
                    self._enter_reconciling()
                    return False
                if state == OrderState.FILLED and filled != qty:
                    self._enter_reconciling()
                    return False
                if state == OrderState.OPEN and filled != 0:
                    self._enter_reconciling()
                    return False
                if state == OrderState.PARTIALLY_FILLED and not (Decimal('0') < filled < qty):
                    self._enter_reconciling()
                    return False

                order = Order(cid, qty, symbol=symbol, side=side)
                order.filled_amount = filled
                order.seen_fills_sum = observed
                order.state = state
                if state == OrderState.CANCELED:
                    order.cancel_requested = True
                    order.cancel_confirmed_total = filled

                rebuilt[cid] = order
                if state not in (OrderState.FILLED, OrderState.CANCELED):
                    remaining = qty - filled
                    reservations += remaining
                    if side == 'BUY':
                        buy_pending += remaining
                    else:
                        sell_pending += remaining

            if current_position + buy_pending > self.position_limit:
                self._enter_reconciling()
                return False
            if current_position - sell_pending < -self.position_limit:
                self._enter_reconciling()
                return False

            self.current_position = current_position
            self.reservations = reservations
            self.orders = rebuilt
            self.active_approvals = []
            self._approval_registry = {}
            self.unresolved_conflicts = set()
            self.processed_fills = set()
            self._bump_risk_version()
            return True

    # -- Connection / state transitions --------------------------------------

    def lose_connection(self):
        """Transition to CONNECTION_LOST.  Overrides everything except HALTED."""
        with self._lock:
            if self.system_state == SystemState.HALTED:
                return
            self.system_state = SystemState.CONNECTION_LOST

    def restore_connection(self):
        """Transition to RECONCILING.  Blocked if HALTED."""
        with self._lock:
            if self.system_state == SystemState.HALTED:
                return
            self.system_state = SystemState.RECONCILING

    def complete_reconciliation(self):
        """Transition to OPERATIONAL only if:
        - Current state is RECONCILING (not CONNECTION_LOST or HALTED)
        - All UNKNOWN orders are resolved
        - No unresolved data conflicts exist
        """
        with self._lock:
            if self.system_state != SystemState.RECONCILING:
                return  # Bug 3: only valid from RECONCILING
            if self._has_unknown_orders():
                return  # cannot go OPERATIONAL with unresolved orders
            if self._has_data_conflicts():
                return  # cannot go OPERATIONAL with unresolved data issues
            self.system_state = SystemState.OPERATIONAL
            self._bump_risk_version()

    def manual_reset(self):
        """Simulate manual intervention to clear HALTED state.

        Transitions to RECONCILING so that full verification is required
        before the system becomes OPERATIONAL again.
        """
        with self._lock:
            if self.system_state == SystemState.HALTED:
                self.system_state = SystemState.RECONCILING
                self._bump_risk_version()

    # -- Order state helpers (for reconciliation use) ------------------------

    def resolve_order(self, order_id: str, final_state: OrderState,
                      final_filled: Decimal):
        """Resolve an UNKNOWN order during reconciliation.

        This is called when the exchange confirms the true status of an
        order via Client Order ID lookup.
        Validates that final_filled is within [0, initial_amount].
        """
        with self._lock:
            if not self._is_valid_non_negative(final_filled):
                self._enter_reconciling()
                return

            if order_id not in self.orders:
                return
            order = self.orders[order_id]
            if order.state != OrderState.UNKNOWN:
                return

            # --- Bounds check (Bug 5) ---
            if final_filled > order.initial_amount:
                # Inconsistent data — do not consider this a definitive resolution
                # and do NOT release any reservations. Preserve worst-case risk.
                self.unresolved_conflicts.add(order_id)
                self._enter_reconciling()
                self._bump_risk_version()
                return

            if final_state == OrderState.FILLED:
                diff = final_filled - order.filled_amount
                if diff > 0:
                    order.filled_amount += diff
                    self.current_position += self._position_delta(order, diff)
                    self.reservations -= diff
                order.state = OrderState.FILLED
            elif final_state == OrderState.CANCELED:
                order.state = OrderState.CANCELED
                order.cancel_confirmed_total = final_filled
                diff = final_filled - order.filled_amount
                if diff > 0:
                    order.filled_amount += diff
                    self.current_position += self._position_delta(order, diff)
                    self.reservations -= diff
                remaining = order.initial_amount - order.filled_amount
                if remaining > 0:
                    self.reservations -= remaining
            elif final_state == OrderState.OPEN:
                order.state = OrderState.OPEN

            self._bump_risk_version()

    def resolve_conflict(self, order_id: str, final_filled: Decimal):
        """Resolve a persistent data conflict with verified reconciliation data.
        
        Removes the order from unresolved_conflicts and applies the verified
        final_filled amount. This allows the system to exit RECONCILING state
        if all conflicts are resolved.
        """
        with self._lock:
            if order_id not in self.unresolved_conflicts:
                return
            if not self._is_valid_non_negative(final_filled):
                return
            if order_id not in self.orders:
                return
                
            order = self.orders[order_id]
            if final_filled > order.initial_amount:
                # Still contradictory, can't resolve with this data
                return
                
            # Calculate how much reservation this order currently holds
            if order.state in (OrderState.CANCELED, OrderState.FILLED):
                old_held = Decimal('0')
            else:
                old_held = order.initial_amount - order.filled_amount

            # Apply the resolution
            diff = final_filled - order.filled_amount
            order.filled_amount += diff
            self.current_position += self._position_delta(order, diff)
            
            # The order is now definitively resolved and considered CANCELED,
            # so it holds 0 reservations. Release whatever it was holding before.
            self.reservations -= old_held
            
            order.state = OrderState.CANCELED
            order.cancel_confirmed_total = final_filled
                
            self.unresolved_conflicts.remove(order_id)
            self._bump_risk_version()
