from pathlib import Path


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"{label}: expected 1 match, got {n}")
    return text.replace(old, new, 1)


# A websocket trade must never mutate persistence if the in-memory RiskEngine
# has no matching order. That indicates startup/reconnect state was not rebuilt
# coherently, so fail closed before applying the event.
p = Path('src/apge/execution_engine.py')
s = p.read_text()
old = '''        # If it's a fill, apply it safely
        if update["execution_type"] == "TRADE":
            trade_id = update.get("trade_id")
'''
new = '''        # If it's a fill, apply it safely. Persistence and RiskEngine must
        # describe the same tracked order before any mutation is allowed.
        if update["execution_type"] == "TRADE":
            if cid not in self.risk_engine.orders:
                self.persistence.update_intent_status(
                    cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                self.risk_engine.restore_connection()
                return
            trade_id = update.get("trade_id")
'''
s = once(s, old, new, 'risk/persistence trade coherence gate')
p.write_text(s)


# Upgrade the focused execution test mock to the same persistence contract and
# real Binance ORDER_TRADE_UPDATE cumulative-fill semantics used by production.
p = Path('tests/test_execution_engine_risk_release.py')
s = p.read_text()
old = '''class MockPersistence:
    def get_intent(self, cid):
        return {"status": "OPEN", "exchange_order_id": None}
    def add_fill(self, fill_id, client_order_id, quantity, price):
        return True # Mock successful fill application
    def update_intent_status(self, *args, **kwargs):
        pass
'''
new = '''class MockPersistence:
    def __init__(self):
        self._fills = set()
        self._filled = Decimal("0")

    def get_intent(self, cid):
        return {
            "status": "OPEN",
            "exchange_order_id": None,
            "quantity": "1.0",
            "filled_quantity": str(self._filled),
        }

    def has_fill(self, fill_id):
        return fill_id in self._fills

    def add_fill(self, fill_id, client_order_id, quantity, price):
        if fill_id in self._fills:
            return False
        self._fills.add(fill_id)
        self._filled += quantity
        return True

    def update_intent_status(self, *args, **kwargs):
        pass
'''
s = once(s, old, new, 'focused persistence mock contract')
s = once(
    s,
    '        "last_filled_price": Decimal("70000"),\n        "trade_id": "999"\n',
    '        "last_filled_price": Decimal("70000"),\n        "accumulated_filled_qty": Decimal("1.0"),\n        "trade_id": "999"\n',
    'focused cumulative fill event')
p.write_text(s)
