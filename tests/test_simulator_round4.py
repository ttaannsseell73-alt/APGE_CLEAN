import unittest
from decimal import Decimal
from apge.simulator import RiskEngine, SystemState, OrderState, RiskApproval

class TestRound4_1_PreserveRiskOnConflict(unittest.TestCase):
    """Bug 1: Contradictory result must preserve risk.
    Limit 10, order 5, fill 0. on_cancel_confirmed(order, 7).
    Reservation should NOT become 0. Unresolved risk must be preserved,
    and new entry blocked.
    """
    def test_cancel_conflict_preserves_reservation(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")
        
        # Conflicting cancel confirm: says 7 filled, but initial was 5, and we saw 0.
        engine.on_cancel_confirmed("order_1", Decimal('7.0'))
        
        self.assertEqual(engine.system_state, SystemState.RECONCILING)
        
        # Force complete_reconciliation - shouldn't work if conflict unresolved
        engine.complete_reconciliation()
        
        # Should still be RECONCILING or HALTED, not OPERATIONAL
        self.assertNotEqual(engine.system_state, SystemState.OPERATIONAL)
        
        # Reservations should NOT be cleared to 0. We should assume the worst case risk.
        # Since it was a 5 unit order, we must reserve at least 5 units of risk (or position 5).
        total_exposure = engine.current_position + engine.reservations
        self.assertEqual(total_exposure, Decimal('5.0'), "Worst case risk not preserved on conflict")
        
        # New entry should be blocked
        self.assertIsNone(engine.request_approval(Decimal('1.0')))

class TestRound4_2_ApprovalOwnership(unittest.TestCase):
    """Bug 2: Engine must verify approval object identity/ownership."""
    def test_forged_object_with_valid_id_rejected(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        
        # Forge an object with same ID
        forged_app = RiskApproval(
            approval_id=app.approval_id,
            amount=Decimal('5.0'),
            expires_at=app.expires_at,
            version=app.version
        )
        
        result = engine.consume_approval_and_submit(forged_app, "order_1")
        self.assertFalse(result, "Forged approval object was accepted")

    def test_other_engine_approval_rejected(self):
        engine1 = RiskEngine(position_limit=Decimal('10.0'))
        engine2 = RiskEngine(position_limit=Decimal('10.0'))
        
        app1 = engine1.request_approval(Decimal('5.0'))
        app2 = engine2.request_approval(Decimal('5.0'))
        
        # Assuming ID sequences might overlap (e.g., both ID 1)
        # Try to use engine2's approval on engine1
        result = engine1.consume_approval_and_submit(app2, "order_1")
        self.assertFalse(result, "Approval from different engine was accepted")

class TestRound4_3_PersistentConflictAndResolution(unittest.TestCase):
    """Bug 3: Persistent data conflict must block complete_reconciliation until explicitly solved."""
    def test_late_fill_conflict_blocks_reconciliation(self):
        engine = RiskEngine(position_limit=Decimal('10.0'))
        app = engine.request_approval(Decimal('5.0'))
        engine.consume_approval_and_submit(app, "order_1")
        
        # 3 units fill, then cancel confirmed at 3
        engine.on_fill("order_1", "fill_1", Decimal('3.0'))
        engine.on_cancel_confirmed("order_1", Decimal('3.0'))
        
        # Late fill of 2 units with new ID
        engine.on_fill("order_1", "fill_2", Decimal('2.0'))
        
        self.assertEqual(engine.system_state, SystemState.RECONCILING)
        
        # Trying to clear it normally shouldn't work
        engine.complete_reconciliation()
        self.assertNotEqual(engine.system_state, SystemState.OPERATIONAL, "System reopened with unresolved conflict")
        
        # Valid resolution logic
        engine.resolve_conflict("order_1", Decimal('5.0'))
        
        # Now complete_reconciliation should work
        engine.complete_reconciliation()
        self.assertEqual(engine.system_state, SystemState.OPERATIONAL, "System didn't reopen after resolution")
        
        # Check positions/reservations:
        # initial: 5
        # filled: 3 + 2 = 5 (from seen_fills_sum and final_filled is 5)
        # So current_position should be 5, reservations 0
        self.assertEqual(engine.current_position, Decimal('5.0'))
        self.assertEqual(engine.reservations, Decimal('0'))
