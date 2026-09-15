"""Round-3 regression tests — six specific bugs found via independent audit.

Each test is expected to FAIL against the current simulator, then PASS
after the corresponding fix is applied.
"""
import math
import unittest
from decimal import Decimal

from apge.simulator import (
    RiskEngine,
    SystemState,
    ApprovalStatus,
    OrderState,
)


class TestBug1_MutableApprovalAmount(unittest.TestCase):
    """Bug 1: Caller mutates approval.amount after request_approval.
    consume_approval_and_submit uses the tampered value for the Order,
    creating a 30-unit order from a 3-unit reservation."""

    def test_tampered_amount_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('3.0'))
        self.assertIsNotNone(app)
        self.assertEqual(engine.reservations, Decimal('3.0'))

        # Caller tampers with the external object
        app.amount = Decimal('30.0')

        result = engine.consume_approval_and_submit(app, "order_1")

        # Must reject OR create the order at 3 (the original amount)
        if result:
            # If accepted, the order must use the ORIGINAL 3, not 30
            order = engine.orders["order_1"]
            self.assertEqual(
                order.initial_amount, Decimal('3.0'),
                f"Order was created with tampered amount {order.initial_amount}")
            self.assertLessEqual(
                engine.reservations, Decimal('3.0'),
                f"Reservation became {engine.reservations} after tampered submit")
        else:
            # Rejected is also acceptable — reservation should be
            # released using the ORIGINAL 3
            self.assertEqual(
                engine.reservations, Decimal('0.0'),
                f"Reservation is {engine.reservations}, expected 0 after rejection")

    def test_tampered_symbol_on_external_object(self):
        """Even if the caller mutates approval.symbol directly, the engine
        must use its own record."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(
            Decimal('5.0'), symbol="BTCUSDT", side="BUY")
        app.symbol = "ETHUSDT"  # tamper

        result = engine.consume_approval_and_submit(app, "order_1")
        if result:
            # Accepted — engine must have used the original symbol
            pass  # (we can't inspect the order symbol yet, but at minimum
                  # the reservation math must be correct)
            self.assertLessEqual(engine.reservations, Decimal('5.0'))
        # Either outcome is fine as long as the engine doesn't blindly
        # trust the mutated field for capacity accounting.


class TestBug2_UnknownGateAtSubmission(unittest.TestCase):
    """Bug 2: Two approvals taken while OPERATIONAL (no UNKNOWN yet).
    First consumed → order_1 UNKNOWN.  Second consume_approval_and_submit
    should be blocked but currently succeeds."""

    def test_second_submit_blocked_when_unknown_exists(self):
        engine = RiskEngine(position_limit=Decimal('20.0'))
        a1 = engine.request_approval(Decimal('5.0'))
        a2 = engine.request_approval(Decimal('3.0'))
        self.assertIsNotNone(a1)
        self.assertIsNotNone(a2)

        engine.consume_approval_and_submit(a1, "order_1")
        self.assertEqual(engine.orders["order_1"].state, OrderState.UNKNOWN)

        # a2 was obtained before order_1 existed, but at submission time
        # there IS an UNKNOWN order — must be blocked.
        result = engine.consume_approval_and_submit(a2, "order_2")
        self.assertFalse(
            result,
            "Second order submitted while UNKNOWN order exists")

        # Reservation from the rejected a2 should be released
        # (only order_1's 5.0 should remain)
        self.assertEqual(
            engine.reservations, Decimal('5.0'),
            f"Expected 5.0, got {engine.reservations}")

    def test_new_entry_blocked_after_submit(self):
        """Verify request_approval is also still blocked (existing test
        but included here for completeness)."""
        engine = RiskEngine(position_limit=Decimal('20.0'))
        a1 = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(a1, "order_1")
        a2 = engine.request_approval(Decimal('3.0'))
        self.assertIsNone(a2)


class TestBug3_CompleteReconciliationFromConnectionLost(unittest.TestCase):
    """Bug 3: lose_connection() then complete_reconciliation() skips
    RECONCILING and goes straight to OPERATIONAL."""

    def test_complete_reconciliation_requires_reconciling_state(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        engine.lose_connection()
        self.assertEqual(engine.system_state, SystemState.CONNECTION_LOST)

        engine.complete_reconciliation()
        self.assertNotEqual(
            engine.system_state, SystemState.OPERATIONAL,
            "complete_reconciliation succeeded from CONNECTION_LOST")

    def test_complete_reconciliation_from_operational_is_noop(self):
        """Calling complete_reconciliation when already OPERATIONAL
        should not cause issues (but shouldn't be the normal path)."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        self.assertEqual(engine.system_state, SystemState.OPERATIONAL)
        old_version = engine.risk_version
        engine.complete_reconciliation()
        # Should either be a no-op or remain OPERATIONAL
        self.assertEqual(engine.system_state, SystemState.OPERATIONAL)


class TestBug4_LateFillDoubleCount(unittest.TestCase):
    """Bug 4: 10-unit order, seen fill 3, cancel total 5, late fill 2.
    Current code: order.filled_amount was synced to 5 by cancel,
    then late fill check: 5+2=7 > 5 → RECONCILING.  But the fill_2
    is the SAME fill that was covered by the cancel sync."""

    def test_late_fill_covered_by_cancel_sync_no_reconciliation(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('10.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_fill("order_1", "fill_1", Decimal('3.0'))
        self.assertEqual(engine.current_position, Decimal('3.0'))

        engine.on_cancel_confirmed(
            "order_1", final_filled_amount=Decimal('5.0'))
        self.assertEqual(engine.current_position, Decimal('5.0'))
        self.assertEqual(engine.reservations, Decimal('0.0'))

        # This late fill_2 for 2 is part of the gap that cancel sync
        # already covered (3→5).  It should be safe — NOT trigger
        # RECONCILING.
        prev_state = engine.system_state
        engine.on_fill("order_1", "fill_2", Decimal('2.0'))

        self.assertEqual(
            engine.current_position, Decimal('5.0'),
            "Position changed after covered late fill")
        self.assertEqual(
            engine.reservations, Decimal('0.0'),
            "Reservation changed after covered late fill")
        # System must NOT have entered RECONCILING for a covered fill
        # (it may already have been in RECONCILING from the cancel bump,
        # but the fill itself shouldn't trigger it)
        # Actually let's check: after a clean cancel with consistent data,
        # system should still be OPERATIONAL (cancel bumps version but
        # doesn't change state).
        # The issue is: current code DOES trigger RECONCILING here.
        # We want it NOT to.
        self.assertNotEqual(
            engine.system_state, SystemState.RECONCILING,
            "Covered late fill incorrectly triggered RECONCILING")


class TestBug5_CancelTotalExceedsOrderAmount(unittest.TestCase):
    """Bug 5: 5-unit order, cancel total = 7.  Reservations go negative."""

    def test_cancel_total_exceeding_order_triggers_reconciliation(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_cancel_confirmed(
            "order_1", final_filled_amount=Decimal('7.0'))

        # Reservations must NOT go negative
        self.assertGreaterEqual(
            engine.reservations, Decimal('0.0'),
            f"Reservations went negative: {engine.reservations}")

        # System should recognise this as inconsistent data
        self.assertNotEqual(
            engine.system_state, SystemState.OPERATIONAL,
            "Inconsistent cancel total was silently accepted")

    def test_resolve_order_with_excessive_fill(self):
        """resolve_order with final_filled > initial_amount must not
        produce negative reservations."""
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")
        engine.lose_connection()
        engine.restore_connection()

        engine.resolve_order(
            "order_1", OrderState.FILLED, Decimal('8.0'))

        self.assertGreaterEqual(
            engine.reservations, Decimal('0.0'),
            f"Reservations went negative: {engine.reservations}")


class TestBug6_NegativeAndInvalidFillAmounts(unittest.TestCase):
    """Bug 6: on_fill with negative amount decreases position and
    increases reservation."""

    def test_negative_fill_amount_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_fill("order_1", "fill_neg", Decimal('-2.0'))

        self.assertEqual(
            engine.current_position, Decimal('0.0'),
            f"Negative fill changed position to {engine.current_position}")
        self.assertEqual(
            engine.reservations, Decimal('5.0'),
            f"Negative fill changed reservation to {engine.reservations}")

    def test_zero_fill_amount_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_fill("order_1", "fill_zero", Decimal('0'))
        self.assertEqual(engine.current_position, Decimal('0.0'))

    def test_nan_fill_amount_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_fill("order_1", "fill_nan", Decimal('NaN'))
        self.assertEqual(
            engine.current_position, Decimal('0.0'),
            "NaN fill changed position")
        self.assertEqual(
            engine.reservations, Decimal('5.0'),
            "NaN fill changed reservation")

    def test_inf_fill_amount_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_fill("order_1", "fill_inf", Decimal('Infinity'))
        self.assertEqual(
            engine.current_position, Decimal('0.0'),
            "Infinity fill changed position")

    def test_negative_cancel_total_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_cancel_confirmed("order_1", Decimal('-3.0'))
        self.assertGreaterEqual(
            engine.reservations, Decimal('0.0'),
            f"Negative cancel total made reservations {engine.reservations}")

    def test_nan_cancel_total_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")

        engine.on_cancel_confirmed("order_1", Decimal('NaN'))
        self.assertGreaterEqual(
            engine.reservations, Decimal('0.0'),
            "NaN cancel total corrupted reservations")
        # Order should NOT be marked as canceled with invalid data
        self.assertNotEqual(
            engine.orders["order_1"].state, OrderState.CANCELED,
            "NaN cancel total was accepted as valid cancelation")


if __name__ == '__main__':
    unittest.main()
