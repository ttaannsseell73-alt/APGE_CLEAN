"""Regression tests for gaps in the APGE simulator.

These tests target specific weaknesses identified in the current
implementation.  They are designed to FAIL before the corresponding
fixes are applied, and PASS afterwards.
"""

import threading
import unittest
from decimal import Decimal

from apge.simulator import (
    RiskApproval,
    RiskEngine,
    SystemState,
    ApprovalStatus,
    OrderState,
)


# ---------------------------------------------------------------------------
# GAP 1 – Thread-safety: real concurrent capacity requests
# ---------------------------------------------------------------------------

class TestConcurrentThreadSafety(unittest.TestCase):
    """Capacity control must hold under real thread contention."""

    def test_threaded_approval_respects_limit(self):
        """Two threads each requesting 6 units against a limit of 10.
        At most one may succeed."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        results = [None, None]
        barrier = threading.Barrier(2)

        def worker(idx):
            barrier.wait()
            results[idx] = engine.request_approval(Decimal('6.0'))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        approvals = [r for r in results if r is not None]
        self.assertLessEqual(
            len(approvals), 1,
            "Both threads got an approval – capacity overrun!")
        self.assertLessEqual(
            engine.reservations, Decimal('6.0'),
            "Reservation exceeds what a single approval can reserve")


# ---------------------------------------------------------------------------
# GAP 2 – UNKNOWN order vs. in-flight submission distinction;
#          reconciliation gate; HALTED immutability
# ---------------------------------------------------------------------------

class TestUnknownOrderBlocksEntry(unittest.TestCase):
    """An UNKNOWN outcome must prevent new risk-increasing orders."""

    def test_unknown_order_blocks_new_approval(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('3.0'))
        engine.consume_approval_and_submit(app, "order_1")
        self.assertEqual(engine.orders["order_1"].state, OrderState.UNKNOWN)
        # New risk-increasing approval should be denied
        app2 = engine.request_approval(Decimal('3.0'))
        self.assertIsNone(app2, "New entry allowed while UNKNOWN order exists")

    def test_connection_lost_overrides_reconciling(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        engine.restore_connection()              # RECONCILING
        engine.lose_connection()                 # should override
        self.assertEqual(engine.system_state, SystemState.CONNECTION_LOST)

    def test_complete_reconciliation_blocked_with_unknown_orders(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")
        engine.lose_connection()
        engine.restore_connection()
        engine.complete_reconciliation()
        self.assertNotEqual(
            engine.system_state, SystemState.OPERATIONAL,
            "Reconciliation completed despite unresolved UNKNOWN order")

    def test_halted_cannot_be_auto_cleared(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        engine.system_state = SystemState.HALTED
        engine.restore_connection()
        self.assertEqual(engine.system_state, SystemState.HALTED)
        engine.complete_reconciliation()
        self.assertEqual(engine.system_state, SystemState.HALTED)


# ---------------------------------------------------------------------------
# GAP 3 – Approval provenance: registry, immutable binding, duplicate order_id
# ---------------------------------------------------------------------------

class TestApprovalProvenance(unittest.TestCase):
    """Approvals must be verified against the engine's internal registry."""

    def test_forged_approval_rejected(self):
        """An approval object not created by the engine must be rejected
        without affecting reservations."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        fake = RiskApproval(
            approval_id=99999,
            amount=Decimal('5.0'),
            expires_at=engine.get_time() + 10.0,
            version=engine.risk_version)
        result = engine.consume_approval_and_submit(fake, "order_x")
        self.assertFalse(result, "Forged approval was accepted")
        self.assertEqual(engine.reservations, Decimal('0.0'),
                         "Forged approval changed reservation")

    def test_duplicate_order_id_rejected(self):
        """Submitting a second order with an already-used order_id must fail
        and must not overwrite the existing order record."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        a1 = engine.request_approval(Decimal('3.0'))
        engine.consume_approval_and_submit(a1, "order_1")
        original = engine.orders["order_1"]

        # Resolve order_1 so the UNKNOWN gate doesn't block the second
        # approval request – we're testing order_id collision, not UNKNOWN.
        engine.on_fill("order_1", "fill_1", Decimal('3.0'))

        a2 = engine.request_approval(Decimal('2.0'))
        self.assertIsNotNone(a2, "Approval denied unexpectedly")
        result = engine.consume_approval_and_submit(a2, "order_1")
        self.assertFalse(result, "Duplicate order_id was accepted")
        self.assertIs(engine.orders["order_1"], original,
                      "Existing order record was overwritten")

    def test_approval_bound_to_immutable_payload(self):
        """Approval must carry immutable order parameters; changing them
        after creation must be detectable."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(
            Decimal('5.0'), ttl_seconds=10.0,
            symbol="BTCUSDT", side="BUY")
        # Submit with different symbol
        result = engine.consume_approval_and_submit(
            app, "order_1", symbol="ETHUSDT", side="BUY",
            amount=Decimal('5.0'))
        self.assertFalse(result, "Tampered payload was accepted")


# ---------------------------------------------------------------------------
# GAP 4 – Input validation: amounts, fill overflow, cancel consistency
# ---------------------------------------------------------------------------

class TestInputValidation(unittest.TestCase):
    """Amounts must be finite and positive; fills must not exceed order
    quantity; cancel totals must be consistent."""

    def test_reject_zero_amount(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('0'))
        self.assertIsNone(app, "Zero-amount approval was granted")

    def test_reject_negative_amount(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('-5.0'))
        self.assertIsNone(app, "Negative-amount approval was granted")

    def test_fill_exceeding_remaining_triggers_reconciliation(self):
        """A fill that exceeds the remaining order quantity is inconsistent
        data – should not silently accept."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")
        engine.on_fill("order_1", "fill_big", Decimal('7.0'))
        self.assertNotEqual(
            engine.system_state, SystemState.OPERATIONAL,
            "Over-fill was silently accepted in OPERATIONAL state")

    def test_cancel_total_less_than_observed_fills_triggers_reconciliation(self):
        """Cancel confirmation claiming less filled than we already observed
        is inconsistent – must not silently subtract."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('10.0'))
        engine.consume_approval_and_submit(app, "order_1")
        engine.on_fill("order_1", "fill_1", Decimal('6.0'))
        engine.on_cancel_confirmed("order_1", final_filled_amount=Decimal('4.0'))
        # Must NOT reduce position from 6 to 4
        self.assertEqual(engine.current_position, Decimal('6.0'),
                         "Cancel downward revision silently deleted risk")
        self.assertNotEqual(
            engine.system_state, SystemState.OPERATIONAL,
            "Conflicting cancel data was silently accepted")


# ---------------------------------------------------------------------------
# GAP 5 – Position / risk changes invalidate pending approvals
# ---------------------------------------------------------------------------

class TestPositionChangeInvalidation(unittest.TestCase):
    """Any fill or position change must bump the risk version, invalidating
    pending (unconsumed) approvals.  Consumed/UNKNOWN order reservations
    must NOT be released by this mechanism."""

    def test_fill_invalidates_pending_approval(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        a1 = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(a1, "order_1")

        # Request a2 BEFORE the fill (while order_1 is UNKNOWN, new approval
        # is blocked).  So we resolve order_1 first, request a2, then
        # get another fill that should invalidate a2.
        engine.on_fill("order_1", "fill_1", Decimal('5.0'))
        # order_1 is now FILLED – UNKNOWN gate cleared.
        a2 = engine.request_approval(Decimal('4.0'))
        self.assertIsNotNone(a2, "Approval denied unexpectedly")

        # Create order_2 to generate a fill that will bump risk_version
        engine.consume_approval_and_submit(a2, "order_2")
        # order_2 is UNKNOWN – need another approval for testing.
        # Actually, let's restructure: we want to show that a fill arriving
        # after a2 is created makes a2 stale.
        # Reset approach: two orders, approve both while OPERATIONAL.
        engine2 = RiskEngine(position_limit=Decimal('20.0'))
        a_x = engine2.request_approval(Decimal('5.0'))
        a_y = engine2.request_approval(Decimal('4.0'))
        engine2.consume_approval_and_submit(a_x, "ox")
        # Fill on ox – bumps risk version, a_y should become stale
        engine2.on_fill("ox", "fx", Decimal('5.0'))
        result = engine2.consume_approval_and_submit(a_y, "oy")
        self.assertFalse(result, "Stale approval accepted after position change")

    def test_fill_does_not_release_other_order_reservation(self):
        """Risk version bump must not release reservations held by
        in-flight orders.

        Restructured for UNKNOWN gate: order_1 is resolved to OPEN
        before order_2 is submitted, so the UNKNOWN gate doesn't block.
        The security property is the same: a fill on order_1 must NOT
        release order_2's reservation.
        """
        engine = RiskEngine(position_limit=Decimal('20.0'))
        a1 = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(a1, "order_1")
        # Resolve order_1 to OPEN so UNKNOWN gate clears
        engine.lose_connection()
        engine.restore_connection()
        engine.resolve_order("order_1", OrderState.OPEN, Decimal('0'))
        engine.complete_reconciliation()
        # Now submit order_2
        a2 = engine.request_approval(Decimal('3.0'))
        self.assertIsNotNone(a2, "Approval denied unexpectedly")
        engine.consume_approval_and_submit(a2, "order_2")
        # Resolve order_2 to OPEN too
        engine.lose_connection()
        engine.restore_connection()
        engine.resolve_order("order_2", OrderState.OPEN, Decimal('0'))
        engine.complete_reconciliation()
        # Fill on order_1
        engine.on_fill("order_1", "fill_1", Decimal('5.0'))
        # order_2 reservation (3.0) must still be held
        self.assertEqual(engine.reservations, Decimal('3.0'),
                         "Order reservation was released by risk version bump")


# ---------------------------------------------------------------------------
# GAP 6 – Late / delayed fills after cancel: distinguish covered vs. excess
# ---------------------------------------------------------------------------

class TestLateFillAfterCancel(unittest.TestCase):
    """After cancel confirmation, a late fill that is within the confirmed
    total should be safe to ignore (already counted).  A late fill that
    would push the total BEYOND the confirmed amount is inconsistent."""

    def test_late_fill_within_cancel_total_ignored(self):
        """Cancel said total filled = 5.  We saw 3 before cancel.
        A late fill_2 for 2 arrives – covered by cancel total, ignore."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('10.0'))
        engine.consume_approval_and_submit(app, "order_1")
        engine.on_fill("order_1", "fill_1", Decimal('3.0'))
        engine.on_cancel_confirmed("order_1", final_filled_amount=Decimal('5.0'))
        engine.on_fill("order_1", "fill_2", Decimal('2.0'))
        self.assertEqual(engine.current_position, Decimal('5.0'))
        self.assertEqual(engine.reservations, Decimal('0.0'))

    def test_late_fill_exceeding_cancel_total_triggers_reconciliation(self):
        """Cancel said total filled = 3.  A late fill_2 for 2 arrives
        that would make total 5, contradicting cancel.  Must not silently
        accept; require reconciliation."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('10.0'))
        engine.consume_approval_and_submit(app, "order_1")
        engine.on_fill("order_1", "fill_1", Decimal('3.0'))
        engine.on_cancel_confirmed("order_1", final_filled_amount=Decimal('3.0'))
        self.assertEqual(engine.current_position, Decimal('3.0'))
        # Late fill that contradicts the cancel total
        engine.on_fill("order_1", "fill_2", Decimal('2.0'))
        self.assertEqual(engine.current_position, Decimal('3.0'),
                         "Excess late fill changed position silently")
        self.assertNotEqual(
            engine.system_state, SystemState.OPERATIONAL,
            "Excess late fill was silently accepted")


if __name__ == '__main__':
    unittest.main()
