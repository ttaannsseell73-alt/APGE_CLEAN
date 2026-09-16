from pathlib import Path


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"{label}: expected 1 match, got {n}")
    return text.replace(old, new, 1)


# Persistence needs fill-id lookup so duplicated websocket events can be
# distinguished from malformed new fills before cumulative validation.
p = Path('src/apge/persistence.py')
s = p.read_text()
marker = '''    def get_recorded_fill_total(self, client_order_id: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT quantity FROM fills WHERE client_order_id = ?", (client_order_id,)
        ).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))

'''
addition = marker + '''    def has_fill(self, fill_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)
        ).fetchone() is not None

'''
s = once(s, marker, addition, 'persistence has_fill')
p.write_text(s)


# Fix websocket duplicate semantics: real ORDER_TRADE_UPDATE always carries
# cumulative z; a duplicate trade id must compare z with already-persisted
# cumulative quantity, not add lastQty a second time.
p = Path('src/apge/execution_engine.py')
s = p.read_text()
old = '''                if filled_qty > Decimal("0"):
                    try:
                        total_qty = Decimal(str(intent["quantity"]))
                        persisted_filled = Decimal(str(intent["filled_quantity"]))
                        cumulative = self._authoritative_filled_qty(update.get("accumulated_filled_qty"))
                    except Exception:
                        cumulative = None
                        total_qty = Decimal("-1")
                        persisted_filled = Decimal("-1")

                    expected_cumulative = persisted_filled + filled_qty
                    if (filled_qty.is_nan() or filled_qty.is_infinite() or
                            total_qty <= 0 or persisted_filled < 0 or
                            expected_cumulative > total_qty or cumulative is None or
                            cumulative != expected_cumulative):
                        self.persistence.update_intent_status(
                            cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                        # Let RiskEngine record an overfill conflict when applicable,
                        # then force reconciliation for all other inconsistencies.
                        self.risk_engine.on_fill(
                            order_id=cid, fill_id=str(trade_id), amount=filled_qty)
                        self.risk_engine.restore_connection()
                        return

                    # This handles duplicates internally by checking fill_id
                    fill_applied = self.persistence.add_fill(
                        fill_id=f"ws_fill_{trade_id}",
                        client_order_id=cid,
                        quantity=filled_qty,
                        price=filled_price
                    )
                    if fill_applied:
                        logger.info(f"Applied fill {filled_qty} @ {filled_price} for {cid}")
                        # Tell risk engine to release risk for this fill amount
                        self.risk_engine.on_fill(order_id=cid, fill_id=str(trade_id), amount=filled_qty)
'''
new = '''                if filled_qty > Decimal("0"):
                    persistence_fill_id = f"ws_fill_{trade_id}"
                    duplicate = self.persistence.has_fill(persistence_fill_id)
                    try:
                        total_qty = Decimal(str(intent["quantity"]))
                        persisted_filled = Decimal(str(intent["filled_quantity"]))
                        cumulative = self._authoritative_filled_qty(update.get("accumulated_filled_qty"))
                    except Exception:
                        cumulative = None
                        total_qty = Decimal("-1")
                        persisted_filled = Decimal("-1")

                    expected_cumulative = (
                        persisted_filled if duplicate else persisted_filled + filled_qty)
                    if (filled_qty.is_nan() or filled_qty.is_infinite() or
                            total_qty <= 0 or persisted_filled < 0 or
                            expected_cumulative > total_qty or cumulative is None or
                            cumulative != expected_cumulative):
                        self.persistence.update_intent_status(
                            cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                        if not duplicate:
                            # Let RiskEngine record an overfill conflict when applicable.
                            self.risk_engine.on_fill(
                                order_id=cid, fill_id=str(trade_id), amount=filled_qty)
                        self.risk_engine.restore_connection()
                        return

                    if not duplicate:
                        fill_applied = self.persistence.add_fill(
                            fill_id=persistence_fill_id,
                            client_order_id=cid,
                            quantity=filled_qty,
                            price=filled_price
                        )
                        if not fill_applied:
                            self.persistence.update_intent_status(
                                cid, OrderState.UNKNOWN, intent.get("exchange_order_id"), raw_status=raw_status)
                            self.risk_engine.restore_connection()
                            return
                        logger.info(f"Applied fill {filled_qty} @ {filled_price} for {cid}")
                        self.risk_engine.on_fill(
                            order_id=cid, fill_id=str(trade_id), amount=filled_qty)
'''
s = once(s, old, new, 'duplicate websocket fill validation')
p.write_text(s)


# Bring mocks up to the authoritative exchange contract used by production.
p = Path('tests/test_apge03_hardening.py')
s = p.read_text()
s = s.replace(
    'return {"status": "NEW", "orderId": "ext-new", "clientOrderId": kwargs["client_order_id"]}',
    'return {"status": "NEW", "orderId": "ext-new", "clientOrderId": kwargs["client_order_id"], "executedQty": "0"}')
s = s.replace(
    'return {"status": "NEW", "orderId": f"ext-{len(self.submissions)}", "clientOrderId": kwargs["client_order_id"]}',
    'return {"status": "NEW", "orderId": f"ext-{len(self.submissions)}", "clientOrderId": kwargs["client_order_id"], "executedQty": "0"}')
p.write_text(s)

p = Path('tests/test_offline_e2e_grid.py')
s = p.read_text()
s = s.replace(
    'return {"status": "NEW", "orderId": "ext_new", "clientOrderId": kwargs["client_order_id"]}',
    'return {"status": "NEW", "orderId": "ext_new", "clientOrderId": kwargs["client_order_id"], "executedQty": "0"}')
s = s.replace(
    '"last_filled_price": Decimal("99.0"),\n        "mapped_state": OrderState.FILLED,',
    '"last_filled_price": Decimal("99.0"),\n        "accumulated_filled_qty": Decimal("5.0"),\n        "mapped_state": OrderState.FILLED,')
p.write_text(s)

p = Path('tests/test_offline_e2e_lifecycle1.py')
s = p.read_text()
s = s.replace(
    'return {"status": "NEW", "orderId": "ext1", "clientOrderId": kwargs["client_order_id"]}',
    'return {"status": "NEW", "orderId": "ext1", "clientOrderId": kwargs["client_order_id"], "executedQty": "0"}')
# First partial fill cumulative is 1, duplicate remains 1, final fill cumulative is 2.
needle = '"last_filled_price": Decimal("100.0"),\n        "mapped_state": OrderState.PARTIALLY_FILLED, "order_status": "PARTIALLY_FILLED"'
pos = s.find(needle)
if pos < 0:
    raise SystemExit('lifecycle first partial event not found')
s = s[:pos] + s[pos:].replace(needle,
    '"last_filled_price": Decimal("100.0"),\n        "accumulated_filled_qty": Decimal("1.0"),\n        "mapped_state": OrderState.PARTIALLY_FILLED, "order_status": "PARTIALLY_FILLED"', 1)
pos = s.find(needle)
if pos < 0:
    raise SystemExit('lifecycle duplicate partial event not found')
s = s[:pos] + s[pos:].replace(needle,
    '"last_filled_price": Decimal("100.0"),\n        "accumulated_filled_qty": Decimal("1.0"),\n        "mapped_state": OrderState.PARTIALLY_FILLED, "order_status": "PARTIALLY_FILLED"', 1)
s = s.replace(
    '"last_filled_price": Decimal("100.0"),\n        "mapped_state": OrderState.FILLED, "order_status": "FILLED"',
    '"last_filled_price": Decimal("100.0"),\n        "accumulated_filled_qty": Decimal("2.0"),\n        "mapped_state": OrderState.FILLED, "order_status": "FILLED"')
p.write_text(s)

# The old persistence-only cancel/fill race test bypassed RiskEngine entirely.
# Harden it to assert fail-closed behavior instead of allowing persistence to
# advance behind RiskEngine's back.
p = Path('tests/test_offline_e2e_lifecycle2.py')
s = p.read_text()
old = '''    # WS update arrives saying it's filled
    engine.handle_order_update({
        "client_order_id": cid,
        "execution_type": "TRADE",
        "trade_id": "trade1",
        "last_filled_qty": Decimal("1.0"),
        "last_filled_price": Decimal("100.0"),
        "mapped_state": OrderState.FILLED, "order_status": "FILLED"
    })

    # It must be updated to FILLED
    intent = db.get_intent(cid)
    assert intent["status"] == "FILLED"
    assert Decimal(intent["filled_quantity"]) == Decimal("1.0")
'''
new = '''    # A websocket fill for persistence state that has no matching RiskEngine
    # order is an inconsistent restart/race snapshot and must fail closed.
    engine.handle_order_update({
        "client_order_id": cid,
        "execution_type": "TRADE",
        "trade_id": "trade1",
        "last_filled_qty": Decimal("1.0"),
        "last_filled_price": Decimal("100.0"),
        "accumulated_filled_qty": Decimal("1.0"),
        "mapped_state": OrderState.FILLED, "order_status": "FILLED"
    })

    intent = db.get_intent(cid)
    assert intent["status"] == "UNKNOWN"
    assert Decimal(intent["filled_quantity"]) == Decimal("0")
    assert engine.risk_engine.system_state == SystemState.RECONCILING
'''
s = once(s, old, new, 'legacy cancel/fill race hardening')
p.write_text(s)
