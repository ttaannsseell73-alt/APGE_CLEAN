import unittest
from decimal import Decimal
from apge.simulator import RiskEngine, SystemState, ApprovalStatus, OrderState

class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(position_limit=Decimal('10.0'))

    # 1. Limit 10 iken 6 birimlik iki eşzamanlı onay talebi limiti aşamasın.
    def test_concurrent_approvals_respect_limit(self):
        app1 = self.engine.request_approval(Decimal('6.0'))
        self.assertIsNotNone(app1)
        self.assertEqual(self.engine.reservations, Decimal('6.0'))
        
        app2 = self.engine.request_approval(Decimal('6.0'))
        self.assertIsNone(app2)
        self.assertEqual(self.engine.reservations, Decimal('6.0'))

    # 2. Aynı onay ikinci kez gönderilemesin.
    def test_single_use_approval(self):
        app = self.engine.request_approval(Decimal('5.0'))
        success1 = self.engine.consume_approval_and_submit(app, "order_1")
        self.assertTrue(success1)
        self.assertEqual(app.status, ApprovalStatus.CONSUMED)
        
        success2 = self.engine.consume_approval_and_submit(app, "order_2")
        self.assertFalse(success2)

    # 3. Onay sonrası ilgili risk durumu değişirse gönderim engellensin; yalnız hiç gönderilmediği doğrulanan rezervasyon çözülsün.
    def test_risk_version_change_blocks_submission(self):
        app = self.engine.request_approval(Decimal('5.0'))
        self.assertEqual(self.engine.reservations, Decimal('5.0'))
        
        # Risk state changes (e.g. via reconciliation or engine bump)
        self.engine.risk_version += 1
        
        success = self.engine.consume_approval_and_submit(app, "order_1")
        self.assertFalse(success)
        self.assertEqual(app.status, ApprovalStatus.REJECTED)
        # Reservation should be released
        self.assertEqual(self.engine.reservations, Decimal('0.0'))

    # 4. 10 birim emirde 4 gerçekleşme + iptal isteği: pozisyon 4, kalan risk 6. Kesin iptal sonrası pozisyon 4, kalan risk 0.
    def test_partial_fill_and_cancel(self):
        app = self.engine.request_approval(Decimal('10.0'))
        self.engine.consume_approval_and_submit(app, "order_1")
        
        self.engine.on_fill("order_1", "fill_1", Decimal('4.0'))
        self.assertEqual(self.engine.current_position, Decimal('4.0'))
        self.assertEqual(self.engine.reservations, Decimal('6.0'))
        
        self.engine.request_cancel("order_1")
        # Cancel request does not release risk
        self.assertEqual(self.engine.reservations, Decimal('6.0'))
        
        self.engine.on_cancel_confirmed("order_1", final_filled_amount=Decimal('4.0'))
        self.assertEqual(self.engine.current_position, Decimal('4.0'))
        self.assertEqual(self.engine.reservations, Decimal('0.0'))

    # 5. Aynı gerçekleşme olayı tekrar gelirse ikinci kez sayılmasın. 
    # İptal sonucunun toplam gerçekleşme bilgisi, geciken dolum olayının kaybolmasına veya çift sayılmasına yol açmasın.
    def test_duplicate_fills_and_cancel_sync(self):
        app = self.engine.request_approval(Decimal('10.0'))
        self.engine.consume_approval_and_submit(app, "order_1")
        
        self.engine.on_fill("order_1", "fill_1", Decimal('3.0'))
        self.engine.on_fill("order_1", "fill_1", Decimal('3.0')) # Duplicate
        
        self.assertEqual(self.engine.current_position, Decimal('3.0'))
        self.assertEqual(self.engine.reservations, Decimal('7.0'))
        
        # Absolute cancel confirmation says total filled was 5.0 (so a 2.0 fill was delayed/missed)
        self.engine.on_cancel_confirmed("order_1", final_filled_amount=Decimal('5.0'))
        
        self.assertEqual(self.engine.current_position, Decimal('5.0'))
        self.assertEqual(self.engine.reservations, Decimal('0.0'))
        
        # A delayed fill arrives for the canceled order. It should not be double counted.
        self.engine.on_fill("order_1", "fill_2", Decimal('2.0'))
        self.assertEqual(self.engine.current_position, Decimal('5.0'))
        self.assertEqual(self.engine.reservations, Decimal('0.0'))

    # 6. Hiç gönderilmemiş onayın süresi dolunca kapasite çözülsün.
    def test_approval_expiration_releases_capacity(self):
        app = self.engine.request_approval(Decimal('5.0'), ttl_seconds=5.0)
        self.assertEqual(self.engine.reservations, Decimal('5.0'))
        
        self.engine.advance_time(6.0)
        self.engine.cleanup_expired_approvals()
        
        self.assertEqual(self.engine.reservations, Decimal('0.0'))
        self.assertEqual(app.status, ApprovalStatus.EXPIRED)

    # 7. UNKNOWN emrin süresi dolunca kapasite korunsun ve kör tekrar gönderim engellensin.
    def test_unknown_order_keeps_capacity(self):
        app = self.engine.request_approval(Decimal('5.0'), ttl_seconds=5.0)
        self.engine.consume_approval_and_submit(app, "order_1")
        
        # order is in UNKNOWN state
        self.assertEqual(self.engine.orders["order_1"].state, OrderState.UNKNOWN)
        
        self.engine.advance_time(10.0)
        self.engine.cleanup_expired_approvals()
        
        # Reservation should STILL be 5.0 because it's an order reservation, not an approval reservation
        self.assertEqual(self.engine.reservations, Decimal('5.0'))
        
        # Trying to submit same approval fails (already consumed)
        self.assertFalse(self.engine.consume_approval_and_submit(app, "order_1_retry"))

    # 8. Bağlantı kopup geri geldiğinde mutabakat tamamlanana kadar yeni giriş engellensin.
    def test_connection_reconciliation_blocks_entry(self):
        self.engine.lose_connection()
        self.assertEqual(self.engine.system_state, SystemState.CONNECTION_LOST)
        self.assertIsNone(self.engine.request_approval(Decimal('5.0')))
        
        self.engine.restore_connection()
        self.assertEqual(self.engine.system_state, SystemState.RECONCILING)
        self.assertIsNone(self.engine.request_approval(Decimal('5.0')))
        
        self.engine.complete_reconciliation()
        self.assertEqual(self.engine.system_state, SystemState.OPERATIONAL)
        self.assertIsNotNone(self.engine.request_approval(Decimal('5.0')))

if __name__ == '__main__':
    unittest.main()
