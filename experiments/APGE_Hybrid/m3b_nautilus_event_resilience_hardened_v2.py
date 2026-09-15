"""
M3B - Nautilus Event Resilience Tests
=====================================
Uses real Nautilus LimitOrder, Cache, and TestEventStubs.
sqlite3 for persistent reservation state, proposal history, and fill tracking.
Decimal-only quantities.

Scenario mapping to original M3A (m3_event_resilience.py) 10 scenarios:
  M3A-1  Submit-Accepted gap reservation       -> M3B S1 (IDEMPOTENCY & ACCEPTED GAP)
  M3A-2  Delayed Accepted / Idempotency        -> M3B S1 (IDEMPOTENCY & ACCEPTED GAP)
  M3A-3  Duplicate Partial Fill                -> M3B S5 (DUPLICATE VS SEPARATE PARTIAL FILLS)
  M3A-4  Cancel / Fill Race                    -> M3B S4 (CANCEL REQUEST -> FILL -> CANCEL)
  M3A-5  Duplicate Cancel Confirmation         -> M3B S4 (CANCEL REQUEST -> FILL -> CANCEL)
  M3A-6  Rejected Order                        -> M3B S6 (NAUTILUS OrderRejected EVENT)
  M3A-7  Unknown Order State                   -> M3B S2 (TIMEOUT & FAIL-CLOSED)
  M3A-8  Out of Order (Filled < Accepted)      -> M3B S7 (FILLED BEFORE ACCEPTED)
  M3A-9  Restart Reconciliation                -> M3B S3 (RESTART RECONCILIATION WITH REAL CACHE)
  M3A-10 Stress Test (1000 events)             -> M3B S8 (PROPERTY / STRESS TEST)
"""

import sqlite3
import uuid
import random
import logging
import os
import sys
import traceback
from decimal import Decimal

from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.objects import Quantity, Price
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import (
    TraderId, StrategyId, ClientOrderId, VenueOrderId,
    PositionId, TradeId,
)
from nautilus_trader.model.orders import LimitOrder
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.providers import TestInstrumentProvider

logging.basicConfig(level=logging.INFO, format="%(message)s")


# ─────────────────────────────────────────────────────────────────
#  Risk Engine
# ─────────────────────────────────────────────────────────────────
class M3BRiskEngine:
    def __init__(
        self,
        cache: Cache,
        max_exposure: Decimal,
        db_path: str = ":memory:",
        clock_time: int = 1000,
    ):
        self.cache = cache
        self.MAX_TOTAL_EXPOSURE = max_exposure

        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self._init_db()
        self._instruments = {}

        self.halted = True  # Starts halted until reconcile()

        self.clock_time = clock_time
        self.SUBMIT_TIMEOUT = 5000  # clock units

        self.trader_id = TraderId("TEST-TRADER")
        self.strategy_id = StrategyId("TEST-STRATEGY")

        self.realized_exposure = Decimal("0")

    # -- persistence -------------------------------------------------------

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    client_order_id TEXT PRIMARY KEY,
                    proposal_id     TEXT UNIQUE,
                    instrument_id   TEXT,
                    side            TEXT,
                    qty             TEXT,
                    ts_submit       INTEGER
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    client_order_id TEXT,
                    ts_submit INTEGER
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS fills (
                    trade_id TEXT PRIMARY KEY,
                    client_order_id TEXT,
                    instrument_id TEXT,
                    side TEXT,
                    qty TEXT,
                    price TEXT,
                    position_id TEXT,
                    applied INTEGER DEFAULT 0
                )
            """)
            
            # Migration: Ensure new columns exist without losing data
            cur = self.conn.cursor()
            cur.execute("PRAGMA table_info(fills)")
            columns = [info[1] for info in cur.fetchall()]
            if "instrument_id" not in columns:
                self.conn.execute("ALTER TABLE fills ADD COLUMN instrument_id TEXT")
            if "side" not in columns:
                self.conn.execute("ALTER TABLE fills ADD COLUMN side TEXT")
            if "price" not in columns:
                self.conn.execute("ALTER TABLE fills ADD COLUMN price TEXT")
            if "position_id" not in columns:
                self.conn.execute("ALTER TABLE fills ADD COLUMN position_id TEXT")
            if "applied" not in columns:
                self.conn.execute("ALTER TABLE fills ADD COLUMN applied INTEGER DEFAULT 0")

    def close(self):
        self.conn.close()

    def record_reservation(
        self, client_order_id: str, proposal_id: str,
        instrument_id: str, side: str, qty: Decimal,
    ):
        with self.conn:
            self.conn.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?)",
                (client_order_id, proposal_id, instrument_id,
                 side, str(qty), self.clock_time),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO proposals VALUES (?, ?, ?)",
                (proposal_id, client_order_id, self.clock_time),
            )

    def delete_reservation(self, client_order_id: str):
        with self.conn:
            self.conn.execute(
                "DELETE FROM reservations WHERE client_order_id = ?",
                (client_order_id,),
            )

    def get_reservation_by_proposal(self, proposal_id: str):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT client_order_id, qty FROM reservations WHERE proposal_id = ?",
            (proposal_id,),
        )
        return cur.fetchone()

    def has_proposal(self, proposal_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT client_order_id FROM proposals WHERE proposal_id = ?", (proposal_id,))
        return cur.fetchone() is not None

    def get_all_reservations(self):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT client_order_id, proposal_id, qty, ts_submit FROM reservations"
        )
        return cur.fetchall()

    def is_reservation(self, client_order_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT client_order_id FROM reservations WHERE client_order_id = ?",
            (client_order_id,),
        )
        return cur.fetchone() is not None

    # -- reconciliation ----------------------------------------------------

    def reconcile(self):
        self.halted = True
        unresolved = False
        try:
            reservations = self.get_all_reservations()
            for cid, pid, qty_str, ts_submit in reservations:
                order = self.cache.order(ClientOrderId(cid))
                if order is not None:
                    if order.is_closed or order.is_open:
                        self.delete_reservation(cid)
                    else:
                        unresolved = True
                        logging.warning(
                            "Reconciliation: Order %s stuck in SUBMITTED. Engine HALTED.", cid)
                else:
                    unresolved = True
                    logging.warning(
                        "Reconciliation: Reservation %s not in cache, state unknown. Engine HALTED.", cid)

            # Check native position objects vs persistent fills
            cache_realized = Decimal("0")
            for pos in self.cache.positions():
                cache_realized += pos.quantity.as_decimal()
                
            cur = self.conn.cursor()
            cur.execute("SELECT qty, applied, trade_id, client_order_id, position_id, price, instrument_id, side FROM fills")
            rows = cur.fetchall()
            
            # First try to recover pending fills
            persistent_fills_by_cid = {}
            for r in rows:
                qty_str, applied, trade_id, cid_str, pos_id_str, px_str, instrument_id_str, side_str = r
                persistent_fills_by_cid.setdefault(cid_str, []).append(r)

                missing_metadata = []
                if px_str is None or px_str == "None":
                    missing_metadata.append("price")
                if instrument_id_str is None or instrument_id_str == "":
                    missing_metadata.append("instrument_id")
                if side_str is None or side_str == "":
                    missing_metadata.append("side")
                if missing_metadata:
                    unresolved = True
                    logging.error(
                        "Reconciliation: Fill %s has incomplete persisted metadata (%s). HALTING.",
                        trade_id, ", ".join(missing_metadata),
                    )
                    continue

                metadata_order = self.cache.order(ClientOrderId(cid_str))
                if metadata_order is not None:
                    order_instrument_id = str(getattr(metadata_order.instrument_id, "value", metadata_order.instrument_id))
                    order_side = str(getattr(metadata_order.side, "value", metadata_order.side))
                    if instrument_id_str != order_instrument_id or side_str != order_side:
                        unresolved = True
                        logging.error(
                            "Reconciliation: Fill %s metadata mismatches cached order (instrument/side). HALTING.",
                            trade_id,
                        )
                        continue

                if not applied:
                    logging.info("Reconciliation: Attempting recovery of partial fill %s", trade_id)
                    order = metadata_order
                    if not order:
                        unresolved = True
                        logging.error("Reconciliation: Cannot recover fill %s, order %s missing.", trade_id, cid_str)
                        continue
                    instrument = self._instruments.get(order.instrument_id) or self.cache.instrument(order.instrument_id)
                    if not instrument:
                        unresolved = True
                        logging.error("Reconciliation: Cannot recover fill %s, instrument missing.", trade_id)
                        continue
                    
                    pos_id = PositionId(pos_id_str) if pos_id_str else None
                    fill_ev = TestEventStubs.order_filled(
                        order=order, instrument=instrument,
                        trade_id=TradeId(trade_id), last_qty=Quantity.from_str(qty_str), 
                        position_id=pos_id, last_px=Price.from_str(px_str)
                    )
                    
                    try:
                        self.process_venue_event(order, fill_ev, update_cache=True)
                        logging.info("Reconciliation: Successfully recovered partial fill %s", trade_id)
                    except Exception as e:
                        unresolved = True
                        logging.error("Reconciliation: Recovery failed for fill %s: %s", trade_id, e)

            # Re-fetch persistent fills after potential recovery
            cur.execute("SELECT qty FROM fills WHERE applied = 1")
            applied_rows = cur.fetchall()
            persistent_realized = sum((Decimal(r[0]) for r in applied_rows), Decimal("0"))
            
            # Re-fetch cache realized in case of recovery
            cache_realized = sum((pos.quantity.as_decimal() for pos in self.cache.positions()), Decimal("0"))
            
            # Check that cache filled orders have persistent records
            for order in self.cache.orders():
                if order.filled_qty.as_decimal() > 0:
                    fills = persistent_fills_by_cid.get(order.client_order_id.value, [])
                    if not fills:
                        unresolved = True
                        logging.error("Reconciliation: Order %s is filled in cache but missing persistent fills. HALTING.", order.client_order_id.value)
                    else:
                        persisted_qty = sum(Decimal(r[0]) for r in fills)
                        if persisted_qty != order.filled_qty.as_decimal():
                            unresolved = True
                            logging.error("Reconciliation: Order %s cache qty (%s) mismatches persistent qty (%s). HALTING.", order.client_order_id.value, order.filled_qty.as_decimal(), persisted_qty)
            
            if cache_realized != persistent_realized:
                unresolved = True
                logging.error("Reconciliation: Cache position (%s) mismatches persistent fills (%s). Engine HALTED.", cache_realized, persistent_realized)

            if not unresolved:
                self.realized_exposure = persistent_realized
                self.halted = False
                logging.info("Reconciliation successful. Engine is active. Realized exposure: %s", self.realized_exposure)
            else:
                self.halted = True
                logging.error("Reconciliation failed due to unresolved orders or position mismatch. Engine HALTED.")
        except Exception as e:
            self.halted = True
            logging.error("Reconciliation: Fatal error during reconciliation: %s. HALTING.", e)

    # -- exposure helpers --------------------------------------------------

    def _get_independent_exposure(self) -> Decimal:
        open_exp = Decimal("0")
        for order in self.cache.orders():
            if order.is_open:
                open_exp += order.leaves_qty.as_decimal()
        return open_exp

    # -- core evaluation ---------------------------------------------------

    def evaluate_and_submit(
        self, proposal_id: str, instrument, side: OrderSide,
        qty: Decimal, price: Decimal,
    ):
        """Returns a LimitOrder on success, None on rejection."""
        self._instruments[instrument.id] = instrument
        if self.halted:
            logging.info("  -> BLOCKED: Risk Engine is HALTED.")
            return None

        if self.has_proposal(proposal_id):
            logging.info(
                "  -> IDEMPOTENCY: Proposal %s already processed in history.",
                proposal_id)
            return None

        realized = self.realized_exposure
        open_exp = self._get_independent_exposure()
        reservations = self.get_all_reservations()
        reserved_exp = sum(Decimal(r[2]) for r in reservations)

        total_exp = realized + open_exp + reserved_exp

        if total_exp + qty > self.MAX_TOTAL_EXPOSURE:
            logging.info("  -> RISK_REJECTED: %s > %s",
                         total_exp + qty, self.MAX_TOTAL_EXPOSURE)
            return None

        client_order_id = ClientOrderId(str(uuid.uuid4()))
        self.record_reservation(
            client_order_id.value, proposal_id,
            instrument.id.value, side.value, qty,
        )

        order = LimitOrder(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            instrument_id=instrument.id,
            client_order_id=client_order_id,
            order_side=side,
            quantity=Quantity.from_str(str(qty)),
            price=Price.from_str(str(price)),
            init_id=UUID4(),
            ts_init=self.clock_time,
        )
        return order

    # -- timeout -----------------------------------------------------------

    def check_timeouts(self):
        reservations = self.get_all_reservations()
        for cid, pid, qty_str, ts_submit in reservations:
            if self.clock_time - ts_submit > self.SUBMIT_TIMEOUT:
                order = self.cache.order(ClientOrderId(cid))
                if order and (order.is_open or order.is_closed):
                    self.delete_reservation(cid)
                else:
                    self.halted = True
                    logging.info(
                        "  -> TIMEOUT: Reservation %s timed out. Engine HALTED.", cid)

    # -- APGE entry layer --------------------------------------------------

    def process_venue_event(self, order, event, update_cache=True):
        """APGE entry layer for venue events. Handles expected errors gracefully."""
        is_recovery = False
        event_type = type(event).__name__
        cid = event.client_order_id.value
        
        if event_type == "OrderFilled":
            trade_id = event.trade_id.value
            try:
                event_qty = event.last_qty.as_decimal()
                event_px = event.last_px.as_decimal() if getattr(event, "last_px", None) is not None else None
                event_pos_id = event.position_id.value if getattr(event, "position_id", None) else ""

                event_instrument_obj = getattr(event, "instrument_id", None) or getattr(order, "instrument_id", None)
                event_instrument_id = (
                    str(getattr(event_instrument_obj, "value", event_instrument_obj))
                    if event_instrument_obj is not None else None
                )
                event_side_obj = getattr(event, "order_side", None) or getattr(event, "side", None) or getattr(order, "side", None)
                event_side = (
                    str(getattr(event_side_obj, "value", event_side_obj))
                    if event_side_obj is not None else None
                )

                # A fill without immutable duplicate-verification metadata is not safe to apply.
                if event_px is None or not event_instrument_id or not event_side:
                    self.halted = True
                    logging.error(
                        "  -> CRITICAL: Fill %s is missing price/instrument_id/side metadata. HALTED.",
                        trade_id,
                    )
                    raise Exception("Incomplete fill metadata")

                cur = self.conn.cursor()
                cur.execute(
                    "SELECT qty, client_order_id, applied, price, position_id, instrument_id, side "
                    "FROM fills WHERE trade_id = ?",
                    (trade_id,),
                )
                row = cur.fetchone()
                
                if row:
                    saved_qty = Decimal(row[0])
                    saved_cid = row[1]
                    saved_applied = row[2]
                    saved_px = Decimal(row[3]) if row[3] not in (None, "None") else None
                    saved_pos_id = row[4]
                    saved_instrument_id = row[5]
                    saved_side = row[6]

                    # Legacy/corrupt rows cannot be treated as safe duplicates. In particular,
                    # applied=1 with price=NULL must HALT here instead of being silently ignored.
                    if saved_px is None or not saved_instrument_id or not saved_side:
                        self.halted = True
                        logging.error(
                            "  -> CRITICAL: Persisted fill %s has incomplete duplicate metadata. HALTED.",
                            trade_id,
                        )
                        raise Exception("Unverifiable duplicate fill metadata")
                    
                    inconsistent = (
                        (saved_qty != event_qty)
                        or (saved_cid != order.client_order_id.value)
                        or (saved_px != event_px)
                        or (saved_pos_id != event_pos_id)
                        or (saved_instrument_id != event_instrument_id)
                        or (saved_side != event_side)
                    )

                    if inconsistent:
                        self.halted = True
                        logging.error("  -> CRITICAL INCONSISTENCY: Duplicate fill %s arrived with different content", trade_id)
                        raise Exception("Duplicate fill inconsistency")
                    
                    if saved_applied == 1:
                        logging.info("  -> DUPLICATE_FILL: Ignored already processed trade %s", trade_id)
                        return
                    else:
                        is_recovery = True
                        logging.info("  -> RECOVERY_IN_PROGRESS: Fill %s exists but applied=0, continuing...", trade_id)
                else:
                    # WRITE-AHEAD LOGGING BEFORE ANY NATIVE MUTATION
                    with self.conn:
                        self.conn.execute(
                            "INSERT INTO fills "
                            "(trade_id, client_order_id, instrument_id, side, qty, price, position_id, applied) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)", 
                            (
                                trade_id, cid, event_instrument_id, event_side, str(event_qty),
                                str(event_px), event_pos_id,
                            )
                        )
            except Exception as e:
                self.halted = True
                logging.error("  -> DATABASE_ERROR: SQLite failure processing fill %s: %s. HALTED.", trade_id, e)
                raise

        try:
            order.apply(event)
        except Exception as e:
            err_msg = str(e)
            err_type = type(e).__name__
            if is_recovery:
                trade_id_val = getattr(event, "trade_id", None)
                native_verified = False
                if trade_id_val:
                    for ev in getattr(order, "events", []):
                        if type(ev).__name__ == "OrderFilled" and getattr(ev, "trade_id", None) == trade_id_val:
                            ev_qty = ev.last_qty.as_decimal() if hasattr(ev, "last_qty") else None
                            ev_px = ev.last_px.as_decimal() if hasattr(ev, "last_px") else None
                            ev_pos = ev.position_id.value if getattr(ev, "position_id", None) else ""
                            
                            evt_qty = event.last_qty.as_decimal()
                            evt_px = event.last_px.as_decimal() if hasattr(event, "last_px") else None
                            evt_pos = event.position_id.value if getattr(event, "position_id", None) else ""
                            
                            if ev_qty == evt_qty and ev_px == evt_px and ev_pos == evt_pos:
                                native_verified = True
                            else:
                                logging.error("  -> CRITICAL: Native duplicate fill %s on Order has different content!", trade_id_val.value)
                                self.halted = True
                                raise Exception("Native order duplicate content mismatch")
                if native_verified:
                    logging.info("  -> RECOVERY: order.apply raised %s, verified native presence. Proceeding.", err_type)
                else:
                    logging.error("  -> RECOVERY_FAILED: order.apply raised %s without native verification: %s", err_type, err_msg)
                    self.halted = True
                    raise
            elif "InvalidStateTrigger" in err_type:
                acceptable = [
                    "FILLED -> ACCEPTED",
                    "CANCELED -> CANCELED",
                    "FILLED -> CANCELED",
                    "REJECTED -> REJECTED",
                    "REJECTED -> CANCELED",
                ]
                if any(acc in err_msg for acc in acceptable):
                    logging.info("  -> LATE_EVENT: Ignored late/duplicate event on order %s: %s", order.client_order_id.value, err_msg)
                    return
                else:
                    logging.error("  -> UNEXPECTED_ERROR applying event: %s: %s", err_type, e)
                    self.halted = True
                    raise
            elif "KeyError" in err_type and "already contained in '_trade_ids'" in err_msg:
                logging.error("  -> CRITICAL: Native duplicate fill %s is NOT in DB! HALT.", getattr(event, "trade_id", "unknown"))
                self.halted = True
                raise
            else:
                logging.error("  -> UNEXPECTED_ERROR applying event: %s: %s", err_type, e)
                self.halted = True
                raise

        try:
            if update_cache:
                if not self.cache.order(order.client_order_id):
                    self.cache.add_order(order)
                else:
                    self.cache.update_order(order)
            
            self.on_event(event, order, is_recovery)
        except Exception as e:
            self.halted = True
            logging.error("  -> APGE_ENTRY_ERROR: Exception during native caching or on_event: %s. HALTED.", e)
            raise

    def on_event(self, event, order=None, is_recovery=False):
        event_type = type(event).__name__
        cid = event.client_order_id.value

        if event_type == "OrderAccepted":
            # Do not release reservation unless it is actually in the cache as open
            cached_order = self.cache.order(ClientOrderId(cid))
            if cached_order and cached_order.is_open:
                self.delete_reservation(cid)
            else:
                logging.info("  -> OrderAccepted but not open in Cache for %s. Reservation kept.", cid)

        elif event_type == "OrderFilled":
            trade_id = event.trade_id.value
            fill_qty = event.last_qty.as_decimal()
            try:
                # 2. Normal fill akışında gerçek Nautilus Position oluştur/güncelle
                pos_id = getattr(event, "position_id", None)
                if pos_id:
                    pos = self.cache.position(pos_id)
                    if pos:
                        try:
                            pos.apply(event)
                        except Exception as pe:
                            err_pe = str(pe)
                            verified = False
                            pos_fills = getattr(pos, "fills", getattr(pos, "_fills", []))
                            for f in pos_fills:
                                if getattr(f, "trade_id", None) == getattr(event, "trade_id", None):
                                    f_qty = f.qty.as_decimal() if hasattr(f, "qty") else (f.last_qty.as_decimal() if hasattr(f, "last_qty") else None)
                                    f_px = f.price.as_decimal() if hasattr(f, "price") else (f.last_px.as_decimal() if hasattr(f, "last_px") else None)
                                    f_pos = f.position_id.value if getattr(f, "position_id", None) else ""
                                    
                                    ev_qty = event.last_qty.as_decimal()
                                    ev_px = event.last_px.as_decimal() if hasattr(event, "last_px") else None
                                    ev_pos = event.position_id.value if getattr(event, "position_id", None) else ""
                                    
                                    if f_qty == ev_qty and f_px == ev_px and f_pos == ev_pos:
                                        verified = True
                                    else:
                                        self.halted = True
                                        raise Exception("Native position has trade_id but different qty/price/pos_id")
                                    break
                            
                            if verified:
                                logging.info("  -> RECOVERY: pos.apply raised %s, verified native presence. Proceeding.", type(pe).__name__)
                            else:
                                logging.error("  -> RECOVERY_FAILED: pos.apply raised %s without native verification: %s", type(pe).__name__, err_pe)
                                self.halted = True
                                raise
                    elif order:
                        instrument = self._instruments.get(order.instrument_id) or self.cache.instrument(order.instrument_id)
                        if not instrument:
                            raise ValueError(f"Instrument not found for {order.instrument_id}")
                        from nautilus_trader.model.position import Position
                        pos = Position(instrument, event)
                        self.cache.add_position(pos, True)
                    else:
                        raise ValueError(f"No order/position context for fill {trade_id}")
                
                # 3. Update persistent state to applied=1
                with self.conn:
                    self.conn.execute("UPDATE fills SET applied = 1 WHERE trade_id = ?", (trade_id,))
                
                if not is_recovery:
                    self.realized_exposure += fill_qty
                    
                self.delete_reservation(cid)
                
            except Exception as e:
                logging.error("  -> PARTIAL_FILL_UPDATE_FAILED: %s. HALTED.", e)
                self.halted = True
                raise

        elif event_type in ("OrderCanceled", "OrderRejected", "OrderExpired"):
            self.delete_reservation(cid)

    # -- invariant check ---------------------------------------------------

    def assert_invariants(
        self, actual_position: Decimal, actual_open: Decimal, context: str = "",
    ) -> Decimal:
        realized = self.realized_exposure
        open_exp = self._get_independent_exposure()
        reserved = sum(Decimal(r[2]) for r in self.get_all_reservations())

        total = realized + open_exp + reserved

        assert realized >= Decimal("0"), \
            f"Realized negative: {realized} ({context})"
        assert open_exp >= Decimal("0"), \
            f"Open negative: {open_exp} ({context})"
        assert reserved >= Decimal("0"), \
            f"Reserved negative: {reserved} ({context})"
        assert total <= self.MAX_TOTAL_EXPOSURE, \
            f"Exposure {total} exceeded {self.MAX_TOTAL_EXPOSURE} ({context})"

        assert realized == actual_position, \
            f"Realized mismatch! Engine: {realized}, Actual: {actual_position} ({context})"
        assert open_exp == actual_open, \
            f"Open mismatch! Engine: {open_exp}, Actual: {actual_open} ({context})"

        return total


# ─────────────────────────────────────────────────────────────────
#  Test Harness
# ─────────────────────────────────────────────────────────────────
instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")
assertions_count = 0


def inc(n: int = 1):
    global assertions_count
    assertions_count += n


DB_FILE = "test_risk.db"


def _fresh_db():
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)


def run_tests():
    global assertions_count
    assertions_count = 0

    # ── SCENARIO 1: IDEMPOTENCY & ACCEPTED GAP ──────────────
    print("\n=== SCENARIO 1: IDEMPOTENCY & ACCEPTED GAP ===")
    _fresh_db()
    cache1 = Cache()
    engine1 = M3BRiskEngine(cache1, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine1.reconcile()

    # 1) Submit first order
    o1 = engine1.evaluate_and_submit(
        proposal_id="PROP-1",
        instrument=instrument,
        side=OrderSide.BUY,
        qty=Decimal("4.0"),
        price=Decimal("100"),
    )
    inc()
    assert o1 is not None

    sub1 = TestEventStubs.order_submitted(o1)
    engine1.process_venue_event(o1, sub1)

    # 2) Accepted event arrives BEFORE cache update
    acc1 = TestEventStubs.order_accepted(o1)
    # Call on_event directly to simulate the race condition where the event is processed
    # but the cache hasn't been updated (order not mutated yet).
    engine1.on_event(acc1)
    
    # Check that reservation is still held, total exposure is 4.0
    res = engine1.get_reservation_by_proposal("PROP-1")
    inc(2)
    assert res is not None, "Reservation dropped prematurely"
    assert engine1.assert_invariants(Decimal("0"), Decimal("0"), "After early ACCEPTED") == Decimal("4.0")

    # Second proposal should not exploit any gap
    o2 = engine1.evaluate_and_submit(
        proposal_id="PROP-2",
        instrument=instrument,
        side=OrderSide.BUY,
        qty=Decimal("7.0"), # 4.0 (res) + 7.0 > 10.0 -> Risk Reject
        price=Decimal("100"),
    )
    inc()
    assert o2 is None, "Exposure gap exploited!"

    # Now cache actually updates (order mutates to open)
    engine1.process_venue_event(o1, acc1, update_cache=True) 
    
    res_after = engine1.get_reservation_by_proposal("PROP-1")
    inc(2)
    assert res_after is None, "Reservation not dropped after cache sync"
    assert engine1.assert_invariants(Decimal("0"), Decimal("4.0"), "After late cache sync") == Decimal("4.0")

    # 3) Submit same proposal again
    o1_dup = engine1.evaluate_and_submit("PROP-1", instrument, OrderSide.BUY, Decimal("4.0"), Decimal("100"))
    inc()
    assert o1_dup is None, "Idempotency failed in normal operation"

    # Restart
    cache1_copy = Cache()
    cache1_copy.add_order(o1)
    engine1_restart = M3BRiskEngine(cache1_copy, Decimal("10.0"), db_path=engine1.db_path)
    engine1_restart.reconcile()
    inc()
    assert engine1_restart.halted is False

    # Proposal again after restart
    o1_dup2 = engine1_restart.evaluate_and_submit("PROP-1", instrument, OrderSide.BUY, Decimal("4.0"), Decimal("100"))
    inc()
    assert o1_dup2 is None, "Idempotency failed after restart"

    engine1.close()
    engine1_restart.close()
    print("PASS Scenario 1")


    # ── SCENARIO 2: TIMEOUT & FAIL-CLOSED ──────────────
    print("\n=== SCENARIO 2: TIMEOUT & FAIL-CLOSED ===")
    _fresh_db()
    cache2 = Cache()
    engine2 = M3BRiskEngine(cache2, Decimal("10.0"), db_path=DB_FILE)
    engine2.reconcile()

    o2 = engine2.evaluate_and_submit("PROP-TO", instrument, OrderSide.BUY, Decimal("5.0"), Decimal("100"))
    # event is lost
    engine2.clock_time += 6000
    engine2.check_timeouts()

    inc()
    assert engine2.halted, "Engine should halt on unknown order state timeout"
    assert engine2.assert_invariants(Decimal("0"), Decimal("0"), "After Timeout Halt") == Decimal("5.0")

    o2_fail = engine2.evaluate_and_submit("PROP-TO-2", instrument, OrderSide.BUY, Decimal("2.0"), Decimal("100"))
    inc()
    assert o2_fail is None, "Should block new orders while halted"

    engine2.close()
    print("PASS Scenario 2")


    # ── SCENARIO 3: RESTART RECONCILIATION WITH REAL CACHE ──────────────
    print("\n=== SCENARIO 3: RESTART RECONCILIATION WITH REAL CACHE ===")
    _fresh_db()
    cache3 = Cache()
    engine3 = M3BRiskEngine(cache3, Decimal("10.0"), db_path=DB_FILE)
    engine3.reconcile()

    o3 = engine3.evaluate_and_submit("PROP-3", instrument, OrderSide.BUY, Decimal("3.0"), Decimal("100"))
    sub3 = TestEventStubs.order_submitted(o3)
    engine3.process_venue_event(o3, sub3)
    acc3 = TestEventStubs.order_accepted(o3)
    engine3.process_venue_event(o3, acc3)

    fill3_pos = TestEventStubs.order_filled(o3, instrument, position_id=PositionId("POS-3"), last_qty=Quantity.from_str("3.0"))
    engine3.process_venue_event(o3, fill3_pos)

    inc()
    assert engine3.assert_invariants(Decimal("3.0"), Decimal("0"), "After Fill") == Decimal("3.0")
    engine3.close()

    # RESTART with position
    cache3_restart = Cache()
    
    # We must construct a native Position object from fill3 and add it to cache to simulate true Nautilus recovery
    from nautilus_trader.model.position import Position

    pos3 = Position(instrument, fill3_pos)
    
    engine3_restart = M3BRiskEngine(cache3_restart, Decimal("10.0"), db_path=DB_FILE)
    
    # Recreate the order in FILLED state (leaves_qty=0)
    o3_new = LimitOrder(
        trader_id=engine3_restart.trader_id,
        strategy_id=engine3_restart.strategy_id,
        instrument_id=instrument.id,
        client_order_id=o3.client_order_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str("3.0"),
        price=Price.from_str("100"),
        init_id=UUID4(),
        ts_init=1000,
    )
    o3_new.apply(TestEventStubs.order_submitted(o3_new))
    o3_new.apply(TestEventStubs.order_accepted(o3_new))
    o3_new.apply(fill3_pos) # FILLED, leaves_qty = 0
    cache3_restart.add_order(o3_new)
    cache3_restart.add_position(pos3, True)
    
    engine3_restart.reconcile()
    inc()
    assert not engine3_restart.halted
    
    # Process duplicate fill BEFORE native mutation knows about it
    engine3_restart.process_venue_event(o3_new, fill3_pos)
    
    inc()
    assert not engine3_restart.halted
    assert engine3_restart.assert_invariants(Decimal("3.0"), Decimal("0"), "After Duplicate Fill on Restart") == Decimal("3.0")

    # Now verify unresolvable cache -> HALT
    o3a = engine3_restart.evaluate_and_submit("PROP-3a", instrument, OrderSide.BUY, Decimal("2.0"), Decimal("100"))
    # Not added to cache, restart immediately
    engine3_restart.close()
    
    engine3_halt = M3BRiskEngine(cache3_restart, Decimal("10.0"), db_path=DB_FILE)
    engine3_halt.reconcile() # Should halt immediately because PROP-3a is not in cache and state unknown
    inc()
    assert engine3_halt.halted, "Engine must halt immediately on unresolved cache during reconciliation"
    
    # Inconsistent duplicate test (trigger HALT)
    # We can test this on engine3_halt since it's already halted, wait no, let's test it on a fresh engine.
    engine3_halt.close()

    # Create fresh engine for inconsistent duplicate fill
    cache3_dup = Cache()
    pos3_dup = Position(instrument, fill3_pos)
    cache3_dup.add_order(o3_new)
    cache3_dup.add_position(pos3_dup, True)
    
    # Remove the unresolved reservation from DB so we don't halt on reconcile
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM reservations WHERE proposal_id = 'PROP-3a'")
    conn.commit()
    conn.close()
        
    engine3_dup = M3BRiskEngine(cache3_dup, Decimal("10.0"), db_path=DB_FILE)
    engine3_dup.reconcile()
    assert not engine3_dup.halted
    
    fill3_inconsistent = TestEventStubs.order_filled(o3, instrument, position_id=PositionId("POS-3"), trade_id=fill3_pos.trade_id, last_qty=Quantity.from_str("1.0"))
    try:
        engine3_dup.process_venue_event(o3_new, fill3_inconsistent)
    except Exception:
        pass
    inc()
    assert engine3_dup.halted, "Engine must halt on inconsistent duplicate"
    engine3_dup.close()

    print("PASS Scenario 3")


    # ── SCENARIO 4: CANCEL REQUEST -> FILL -> CANCEL ──────────────
    print("\n=== SCENARIO 4: CANCEL REQUEST -> FILL -> CANCEL ===")
    _fresh_db()
    cache4 = Cache()
    engine4 = M3BRiskEngine(cache4, Decimal("10.0"), db_path=DB_FILE)
    engine4.reconcile()

    o4 = engine4.evaluate_and_submit("PROP-4", instrument, OrderSide.BUY, Decimal("5.0"), Decimal("100"))
    engine4.process_venue_event(o4, TestEventStubs.order_submitted(o4))
    engine4.process_venue_event(o4, TestEventStubs.order_accepted(o4))
    
    # Real Cancel Request (PENDING_CANCEL)
    pend_cancel = TestEventStubs.order_pending_cancel(o4)
    engine4.process_venue_event(o4, pend_cancel)
    
    inc()
    assert engine4.assert_invariants(Decimal("0"), Decimal("5.0"), "After PENDING_CANCEL") == Decimal("5.0")

    # Race: Fill arrives
    fill4 = TestEventStubs.order_filled(o4, instrument)
    engine4.process_venue_event(o4, fill4)
    
    inc()
    assert engine4.assert_invariants(Decimal("5.0"), Decimal("0"), "After Race Fill") == Decimal("5.0")

    # Late Cancel Confirmation
    canc4 = TestEventStubs.order_canceled(o4)
    engine4.process_venue_event(o4, canc4)
    
    # Should not change account, should gracefully ignore FILLED -> CANCELED
    inc()
    assert engine4.assert_invariants(Decimal("5.0"), Decimal("0"), "After Late Cancel Conf") == Decimal("5.0")
    
    # Second duplicate Cancel Conf
    engine4.process_venue_event(o4, canc4)
    inc()
    assert engine4.assert_invariants(Decimal("5.0"), Decimal("0"), "After Duplicate Cancel Conf") == Decimal("5.0")
    engine4.close()
    print("PASS Scenario 4")


    # ── SCENARIO 5: DUPLICATE VS SEPARATE PARTIAL FILLS ──────────────
    print("\n=== SCENARIO 5: DUPLICATE VS SEPARATE PARTIAL FILLS ===")
    _fresh_db()
    cache5 = Cache()
    engine5 = M3BRiskEngine(cache5, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine5.reconcile()

    o5 = engine5.evaluate_and_submit(
        proposal_id="PROP-5", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("10.0"), price=Decimal("100")
    )
    engine5.process_venue_event(o5, TestEventStubs.order_submitted(o5))
    engine5.process_venue_event(o5, TestEventStubs.order_accepted(o5))

    fill_qty = Quantity.from_str("3.0")
    # First partial fill
    f5_1 = TestEventStubs.order_filled(o5, instrument, last_qty=fill_qty)
    engine5.process_venue_event(o5, f5_1)

    inc()
    assert engine5.assert_invariants(Decimal("3.0"), Decimal("7.0"), "After 1st Partial") == Decimal("10.0")

    # Duplicate first partial
    engine5.process_venue_event(o5, f5_1)
    inc()
    assert engine5.assert_invariants(Decimal("3.0"), Decimal("7.0"), "After Dup Partial") == Decimal("10.0")

    # Second distinct partial fill
    f5_2 = TestEventStubs.order_filled(o5, instrument, last_qty=fill_qty, trade_id=TradeId("distinct-trade-id-2"))
    engine5.process_venue_event(o5, f5_2)
    inc()
    assert engine5.assert_invariants(Decimal("6.0"), Decimal("4.0"), "After 2nd Partial") == Decimal("10.0")

    engine5.close()
    
    # Regresyon Testi: 0.1 + 0.2 = 0.3 (Pure SQLite)
    _fresh_db()
    conn_test = sqlite3.connect(DB_FILE)
    conn_test.execute("CREATE TABLE fills (trade_id TEXT PRIMARY KEY, client_order_id TEXT, position_id TEXT, qty TEXT, price TEXT)")
    conn_test.execute("INSERT INTO fills (trade_id, client_order_id, position_id, qty, price) VALUES (?, ?, ?, ?, ?)", ("t-float-1", "test", "pos", "0.1", "100"))
    conn_test.execute("INSERT INTO fills (trade_id, client_order_id, position_id, qty, price) VALUES (?, ?, ?, ?, ?)", ("t-float-2", "test", "pos", "0.2", "100"))
    conn_test.commit()
    cur = conn_test.cursor()
    cur.execute("SELECT qty FROM fills")
    rows = cur.fetchall()
    assert sum((Decimal(r[0]) for r in rows), Decimal("0")) == Decimal("0.3")
    conn_test.close()
    
    _fresh_db()
    cache5b = Cache()
    engine5b = M3BRiskEngine(cache5b, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine5b.reconcile()
    
    o5b = engine5b.evaluate_and_submit(
        proposal_id="PROP-5B", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("3.0"), price=Decimal("100")
    )
    sub5b = TestEventStubs.order_submitted(o5b)
    acc5b = TestEventStubs.order_accepted(o5b)
    engine5b.process_venue_event(o5b, sub5b)
    engine5b.process_venue_event(o5b, acc5b)
    
    fill_5b_1 = TestEventStubs.order_filled(o5b, instrument, position_id=PositionId("POS-5B"), trade_id=TradeId("t-5b-1"), last_qty=Quantity.from_str("1.0"))
    fill_5b_2 = TestEventStubs.order_filled(o5b, instrument, position_id=PositionId("POS-5B"), trade_id=TradeId("t-5b-2"), last_qty=Quantity.from_str("2.0"))
    engine5b.process_venue_event(o5b, fill_5b_1)
    engine5b.process_venue_event(o5b, fill_5b_2)
    
    # Trigger reconcile to test sum
    cache5b_restart = Cache()
    cache5b_restart.add_instrument(instrument)
    cache5b_restart.add_order(o5b)
    
    from nautilus_trader.model.position import Position
    pos5b = Position(instrument, fill_5b_1)
    pos5b.apply(fill_5b_2)
    cache5b_restart.add_position(pos5b, True)
    
    engine5b_test = M3BRiskEngine(cache5b_restart, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine5b_test.reconcile()
    inc()
    assert not engine5b_test.halted
    assert engine5b_test.realized_exposure == Decimal("3.0")
    
    engine5b.close()
    engine5b_test.close()

    print("PASS Scenario 5")


    # ── SCENARIO 6: NAUTILUS OrderRejected EVENT ──────────────
    print("\n=== SCENARIO 6: NAUTILUS OrderRejected EVENT ===")
    _fresh_db()
    cache6 = Cache()
    engine6 = M3BRiskEngine(cache6, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine6.reconcile()

    o6 = engine6.evaluate_and_submit(
        proposal_id="PROP-6", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("1.0"), price=Decimal("100")
    )
    inc()
    assert engine6.assert_invariants(Decimal("0"), Decimal("0"), "After Submitting O6") == Decimal("1.0")

    # The exchange immediately rejects it
    rej6 = TestEventStubs.order_rejected(o6)
    engine6.process_venue_event(o6, rej6)

    inc()
    # Reservation dropped, realized exposure 0, total 0
    assert engine6.assert_invariants(Decimal("0"), Decimal("0"), "After Reject O6") == Decimal("0")

    # Duplicate reject
    engine6.process_venue_event(o6, rej6)
    inc()
    assert engine6.assert_invariants(Decimal("0"), Decimal("0"), "After Duplicate Reject O6") == Decimal("0")
    
    # Engine still running, new prop should pass
    o6b = engine6.evaluate_and_submit(
        proposal_id="PROP-6B", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("10.0"), price=Decimal("100")
    )
    inc()
    assert o6b is not None
    assert engine6.assert_invariants(Decimal("0"), Decimal("0"), "After Submitting O6B") == Decimal("10.0")

    engine6.close()
    print("PASS Scenario 6")


    # ── SCENARIO 7: FILLED BEFORE ACCEPTED (OUT-OF-ORDER) ──────────────
    print("\n=== SCENARIO 7: FILLED BEFORE ACCEPTED (OUT-OF-ORDER) ===")
    _fresh_db()
    cache7 = Cache()
    engine7 = M3BRiskEngine(cache7, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine7.reconcile()

    o7 = engine7.evaluate_and_submit(
        proposal_id="PROP-7", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("1.0"), price=Decimal("100")
    )
    
    sub7 = TestEventStubs.order_submitted(o7)
    engine7.process_venue_event(o7, sub7)

    # Filled arrives before accepted
    fill7 = TestEventStubs.order_filled(o7, instrument, last_qty=Quantity.from_str("1.0"))
    engine7.process_venue_event(o7, fill7)

    inc()
    assert o7.is_closed
    assert engine7.assert_invariants(Decimal("1.0"), Decimal("0"), "After Out-of-order Fill") == Decimal("1.0")

    # Late accepted arrives, engine handles naturally expected Exception
    acc7_late = TestEventStubs.order_accepted(o7)
    engine7.process_venue_event(o7, acc7_late)

    inc()
    assert engine7.assert_invariants(Decimal("1.0"), Decimal("0"), "After Late Accept") == Decimal("1.0")
    assert not engine7.halted

    engine7.close()
    print("PASS Scenario 7")


    # ── SCENARIO 8: PROPERTY / STRESS TEST ──────────────
    print("\n=== SCENARIO 8: PROPERTY / STRESS TEST (1000 EVENTS) ===")
    random.seed(42)
    _fresh_db()
    cache8 = Cache()
    engine8 = M3BRiskEngine(cache8, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine8.reconcile()

    active_orders = []
    
    actions_processed = 0
    submits_ok = 0
    risk_rejects = 0
    nautilus_rejects = 0
    accepts = 0
    fills = 0
    cancels = 0
    duplicates = 0
    idle = 0
    
    expected_realized = Decimal("0")
    expected_open = Decimal("0")
    
    for i in range(1000):
        engine8.clock_time += 1
        action = random.choice(["SUBMIT", "ACCEPT", "FILL", "CANCEL", "REJECT", "DUPLICATE"])

        if action == "SUBMIT":
            qty = Decimal(str(random.randint(1, 5)))
            o = engine8.evaluate_and_submit(
                proposal_id=f"STRESS-{i}", instrument=instrument,
                side=OrderSide.BUY, qty=qty, price=Decimal("100")
            )
            actions_processed += 1
            if o:
                submits_ok += 1
                engine8.process_venue_event(o, TestEventStubs.order_submitted(o))
                active_orders.append(o)
            else:
                risk_rejects += 1

        elif action == "ACCEPT" and active_orders:
            o = random.choice(active_orders)
            if not o.is_closed and not o.is_open:
                actions_processed += 1
                accepts += 1
                expected_open += o.quantity.as_decimal()
                engine8.process_venue_event(o, TestEventStubs.order_accepted(o))
            else:
                idle += 1
        
        elif action == "FILL" and active_orders:
            o = random.choice(active_orders)
            if not o.is_closed:
                actions_processed += 1
                fills += 1
                fill_qty = o.leaves_qty.as_decimal()
                was_open = o.is_open
                engine8.process_venue_event(o, TestEventStubs.order_filled(o, instrument))
                if was_open:
                    expected_open -= fill_qty
                expected_realized += fill_qty
            else:
                idle += 1

        elif action == "CANCEL" and active_orders:
            o = random.choice(active_orders)
            if not o.is_closed:
                actions_processed += 1
                cancels += 1
                was_open = o.is_open
                qty = o.leaves_qty.as_decimal()
                engine8.process_venue_event(o, TestEventStubs.order_canceled(o))
                if was_open:
                    expected_open -= qty
            else:
                idle += 1
                
        elif action == "REJECT" and active_orders:
            o = random.choice(active_orders)
            if not o.is_closed and not o.is_open:
                actions_processed += 1
                nautilus_rejects += 1
                engine8.process_venue_event(o, TestEventStubs.order_rejected(o))
            else:
                idle += 1

        elif action == "DUPLICATE" and active_orders:
            o = random.choice(active_orders)
            if o.is_closed:
                actions_processed += 1
                duplicates += 1
                engine8.process_venue_event(o, TestEventStubs.order_canceled(o))
            else:
                idle += 1
        else:
            idle += 1

        engine8.check_timeouts()
        inc()
        assert not engine8.halted, "Stress test unexpectedly halted"

        # Check invariants constantly using independent test oracle variables
        engine8.assert_invariants(expected_realized, expected_open, "Stress Test")
        inc()

    inc(6)
    assert submits_ok > 0
    assert accepts > 0
    assert fills > 0
    assert cancels > 0
    assert nautilus_rejects > 0
    assert duplicates > 0

    print("PASS Scenario 8")
    
    print("\n=== STRESS REPORT ===")
    print(f"  Total iterations:       1000")
    print(f"  Actions processed:      {actions_processed}")
    print(f"  Idle iterations:        {idle}")
    print(f"  ---")
    print(f"  SUBMIT_OK:              {submits_ok}")
    print(f"  RISK_REJECTED:          {risk_rejects}")
    print(f"  NAUTILUS_REJECTED:      {nautilus_rejects}")
    print(f"  ACCEPT:                 {accepts}")
    print(f"  FILL:                   {fills}")
    print(f"  CANCEL:                 {cancels}")
    print(f"  DUPLICATE_EVENT:        {duplicates}")
    
    engine8.close()


    # ── SCENARIO 9: RESTART WITH OPEN ORDER AND NEW FILL ──────────────
    print("\n=== SCENARIO 9: RESTART WITH OPEN ORDER AND NEW FILL ===")
    _fresh_db()
    cache9 = Cache()
    engine9 = M3BRiskEngine(cache9, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine9.reconcile()
    
    o9 = engine9.evaluate_and_submit(
        proposal_id="PROP-9", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("5.0"), price=Decimal("100")
    )
    sub9 = TestEventStubs.order_submitted(o9)
    acc9 = TestEventStubs.order_accepted(o9)
    engine9.process_venue_event(o9, sub9)
    engine9.process_venue_event(o9, acc9)
    engine9.close()
    
    cache9_restart = Cache()
    cache9_restart.add_instrument(instrument)
    # Restore order in OPEN state
    o9_new = LimitOrder(
        trader_id=o9.trader_id, strategy_id=o9.strategy_id, instrument_id=o9.instrument_id,
        client_order_id=o9.client_order_id, order_side=o9.side,
        quantity=o9.quantity, price=o9.price, init_id=o9.init_id, ts_init=o9.ts_init
    )
    o9_new.apply(TestEventStubs.order_submitted(o9_new))
    o9_new.apply(TestEventStubs.order_accepted(o9_new))
    cache9_restart.add_order(o9_new)
    
    engine9_test = M3BRiskEngine(cache9_restart, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine9_test.reconcile()
    inc()
    assert not engine9_test.halted
    assert engine9_test.realized_exposure == Decimal("0")
    
    # Fill arrives AFTER restart
    fill9 = TestEventStubs.order_filled(o9_new, instrument, position_id=PositionId("POS-9"), trade_id=TradeId("t-9"), last_qty=Quantity.from_str("5.0"))
    engine9_test.process_venue_event(o9_new, fill9)
    
    inc()
    assert not engine9_test.halted
    assert engine9_test.realized_exposure == Decimal("5.0")
    
    # Assert native cache position
    cache_pos9 = cache9_restart.position(PositionId("POS-9"))
    inc()
    assert cache_pos9 is not None
    assert cache_pos9.quantity.as_decimal() == Decimal("5.0")
    
    engine9_test.close()
    print("PASS Scenario 9")


    # ── SCENARIO 10: PARTIAL UPDATE HALT & RECOVERY ──────────────
    print("\n=== SCENARIO 10: PARTIAL UPDATE HALT & RECOVERY ===")
    _fresh_db()
    cache10 = Cache()
    engine10 = M3BRiskEngine(cache10, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine10.reconcile()
    
    o10 = engine10.evaluate_and_submit(
        proposal_id="PROP-10", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("4.0"), price=Decimal("100")
    )
    engine10.process_venue_event(o10, TestEventStubs.order_submitted(o10))
    engine10.process_venue_event(o10, TestEventStubs.order_accepted(o10))
    
    fill10 = TestEventStubs.order_filled(o10, instrument, position_id=PositionId("POS-10"), trade_id=TradeId("t-10"), last_qty=Quantity.from_str("4.0"))
    
    # Induce controlled failure during Position update by clearing instruments
    # Since cache10 doesn't have it natively, this will trigger the 'Instrument not found' ValueError
    engine10._instruments.clear()
    
    try:
        engine10.process_venue_event(o10, fill10)
    except Exception:
        pass
        
    inc()
    assert engine10.halted
    
    cur_chk = engine10.conn.cursor()
    cur_chk.execute("SELECT applied FROM fills WHERE trade_id = ?", ("t-10",))
    res_chk = cur_chk.fetchone()
    inc(2)
    assert res_chk is not None
    assert res_chk[0] == 0, "applied flag must remain 0 after position error"
    
    o_rej_early = engine10.evaluate_and_submit(
        proposal_id="PROP-REJ-EARLY", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("1.0"), price=Decimal("100")
    )
    inc()
    assert o_rej_early is None, "New entry should be rejected while engine is halted"
    
    # Restore instrument for normal operation
    engine10._instruments[instrument.id] = instrument
    engine10.close()
    
    # RECOVER: Reconcile should see the fill in SQLite with applied=0.
    # It should reconstruct the fill and successfully complete it.
    cache10_restart = Cache()
    cache10_restart.add_instrument(instrument)
    # The order was only OPEN when it crashed (or FILLED depending on how fast it saved, but we simulate OPEN)
    o10_new = LimitOrder(
        trader_id=o10.trader_id, strategy_id=o10.strategy_id, instrument_id=o10.instrument_id,
        client_order_id=o10.client_order_id, order_side=o10.side,
        quantity=o10.quantity, price=o10.price, init_id=o10.init_id, ts_init=o10.ts_init
    )
    o10_new.apply(TestEventStubs.order_submitted(o10_new))
    o10_new.apply(TestEventStubs.order_accepted(o10_new))
    cache10_restart.add_order(o10_new)
    
    engine10_test = M3BRiskEngine(cache10_restart, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    
    # Verify new entry is rejected BEFORE recovery
    o_rej = engine10_test.evaluate_and_submit(
        proposal_id="PROP-REJ", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("1.0"), price=Decimal("100")
    )
    inc()
    assert o_rej is None, "New entry should be rejected before recovery"
    
    # Run reconciliation to automatically recover
    engine10_test.reconcile()
    inc()
    
    # Verify native Position, open amount, and persistent record are correct
    assert not engine10_test.halted, "Engine should un-halt after recovery"
    assert engine10_test.realized_exposure == Decimal("4.0")
    assert engine10_test.assert_invariants(Decimal("4.0"), Decimal("0"), "After Recovery") == Decimal("4.0")
    
    cur = engine10_test.conn.cursor()
    cur.execute("SELECT applied FROM fills WHERE trade_id = ?", ("t-10",))
    res = cur.fetchone()
    inc(2)
    assert res is not None
    assert res[0] == 1, "Fill should be marked as fully applied"
    
    # Send same fill again, it should be ignored completely
    engine10_test.process_venue_event(o10_new, fill10)
    inc()
    assert not engine10_test.halted
    assert engine10_test.realized_exposure == Decimal("4.0")
    
    engine10_test.close()
    print("PASS Scenario 10")


    # ── SCENARIO 11: WRITE-AHEAD VERIFICATION ──────────────
    print("\n=== SCENARIO 11: WRITE-AHEAD VERIFICATION ===")
    _fresh_db()
    cache11 = Cache()
    engine11 = M3BRiskEngine(cache11, max_exposure=Decimal("10.0"), db_path=DB_FILE)
    engine11.reconcile()
    
    o11 = engine11.evaluate_and_submit(
        proposal_id="PROP-11", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("4.0"), price=Decimal("100")
    )
    engine11.process_venue_event(o11, TestEventStubs.order_submitted(o11))
    engine11.process_venue_event(o11, TestEventStubs.order_accepted(o11))
    
    original_on_event = engine11.on_event
    def fake_on_event(event, order=None, is_recovery=False):
        conn_check = sqlite3.connect(DB_FILE)
        cur = conn_check.cursor()
        cur.execute("SELECT applied FROM fills WHERE trade_id = ?", ("t-11",))
        res = cur.fetchone()
        conn_check.close()
        
        assert res is not None, "Fill is not in DB yet!"
        assert res[0] == 0, "Fill is already applied=1 before native changes finish!"
        
        raise ValueError("Simulated Cache Error")
        
    engine11.on_event = fake_on_event
    
    fill11 = TestEventStubs.order_filled(o11, instrument, position_id=PositionId("POS-11"), trade_id=TradeId("t-11"), last_qty=Quantity.from_str("4.0"))
    
    try:
        engine11.process_venue_event(o11, fill11)
    except Exception:
        pass
        
    inc(2) # assertions inside mock
    assert engine11.halted, "Engine must halt on cache error"
    
    cur_chk = engine11.conn.cursor()
    cur_chk.execute("SELECT applied FROM fills WHERE trade_id = ?", ("t-11",))
    res_chk = cur_chk.fetchone()
    inc(2)
    assert res_chk is not None
    assert res_chk[0] == 0, "Record must remain applied=0 after cache error"
    
    engine11.close()
    
    # RECOVERY SHOULD FAIL IF PRICE IS NULL
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO fills (trade_id, client_order_id, instrument_id, side, qty, price, position_id, applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 ("t-null", o11.client_order_id.value, instrument.id.value, str(OrderSide.BUY.value), "1.0", None, "POS-11", 0))
    conn.commit()
    conn.close()
    
    cache11_restart = Cache()
    cache11_restart.add_instrument(instrument)
    o11_new = LimitOrder(
        trader_id=o11.trader_id, strategy_id=o11.strategy_id, instrument_id=o11.instrument_id,
        client_order_id=o11.client_order_id, order_side=o11.side,
        quantity=o11.quantity, price=o11.price, init_id=o11.init_id, ts_init=o11.ts_init
    )
    o11_new.apply(TestEventStubs.order_submitted(o11_new))
    o11_new.apply(TestEventStubs.order_accepted(o11_new))
    cache11_restart.add_order(o11_new)
    
    engine11_test = M3BRiskEngine(cache11_restart, Decimal("10.0"), db_path=DB_FILE)
    engine11_test.reconcile()
    inc()
    assert engine11_test.halted, "Engine must halt if price is NULL during recovery"
    engine11_test.close()
    print("PASS Scenario 11")

    # ── SCENARIO 12: UNBOUND LOCAL, RECON PARTIAL QTY, RECON DB ERROR, MISSING EVENTS ──────────────
    print("\n=== SCENARIO 12: EDGE CASES ===")
    
    # 1. Pos apply UnboundLocalError path
    _fresh_db()
    cache12 = Cache()
    cache12.add_instrument(instrument)
    engine12 = M3BRiskEngine(cache12, Decimal("10.0"), db_path=DB_FILE)
    engine12.reconcile()
    
    o12 = engine12.evaluate_and_submit(
        proposal_id="PROP-12", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("5.0"), price=Decimal("100")
    )
    engine12.process_venue_event(o12, TestEventStubs.order_submitted(o12))
    engine12.process_venue_event(o12, TestEventStubs.order_accepted(o12))
    
    # First fill partial, leaving 3.0 remaining
    fill12_1 = TestEventStubs.order_filled(o12, instrument, position_id=PositionId("POS-12"), trade_id=TradeId("t-12-1"), last_qty=Quantity.from_str("2.0"), last_px=Price.from_str("100"))
    engine12.process_venue_event(o12, fill12_1)
    
    # Simulate a pos.apply error where fill is NOT found in pos.fills
    class CacheProxy:
        def __init__(self, real_cache):
            self.real_cache = real_cache
            self.called = False
        def __getattr__(self, item):
            return getattr(self.real_cache, item)
        def position(self, pos_id):
            proxy_self = self
            class DummyPos:
                def __init__(self):
                    self.fills = []
                def apply(self, event):
                    proxy_self.called = True
                    raise ValueError("Simulated pos.apply error")
            return DummyPos()
            
    engine12.cache = CacheProxy(cache12)
    
    # Send another partial fill which will be accepted by order.apply, but raise error in pos.apply
    fill12_2 = TestEventStubs.order_filled(o12, instrument, position_id=PositionId("POS-12"), trade_id=TradeId("t-12-2"), last_qty=Quantity.from_str("2.0"), last_px=Price.from_str("100"))
    try:
        engine12.process_venue_event(o12, fill12_2)
    except ValueError as e:
        if "Simulated pos.apply error" not in str(e):
            raise
    
    assert engine12.cache.called, "Position error path was not reached"
    assert engine12.halted, "Engine must halt on pos.apply error if fill not verified"
    
    # Check that applied=0 for the failed fill
    conn12 = sqlite3.connect(DB_FILE)
    row = conn12.execute("SELECT applied FROM fills WHERE trade_id = 't-12-2'").fetchone()
    assert row is not None and row[0] == 0, "Failed pos.apply should leave applied=0"
    conn12.close()
    
    inc()
    engine12.close()
    
    # 2. Missing events test (fallback removed)
    _fresh_db()
    cache12_2 = Cache()
    cache12_2.add_instrument(instrument)
    engine12_2 = M3BRiskEngine(cache12_2, Decimal("10.0"), db_path=DB_FILE)
    engine12_2.reconcile()
    
    o12_2 = engine12_2.evaluate_and_submit(
        proposal_id="PROP-12-2", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("4.0"), price=Decimal("100")
    )
    engine12_2.process_venue_event(o12_2, TestEventStubs.order_submitted(o12_2))
    engine12_2.process_venue_event(o12_2, TestEventStubs.order_accepted(o12_2))
    fill12_3 = TestEventStubs.order_filled(o12_2, instrument, position_id=PositionId("POS-12-2"), trade_id=TradeId("t-12-3"), last_qty=Quantity.from_str("4.0"), last_px=Price.from_str("100"))
    
    # Insert applied=0 to trigger is_recovery = True
    conn12_2 = sqlite3.connect(DB_FILE)
    conn12_2.execute("INSERT INTO fills (trade_id, client_order_id, instrument_id, side, qty, price, position_id, applied) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                     ("t-12-3", o12_2.client_order_id.value, instrument.id.value, str(OrderSide.BUY.value), "4.0", "100", "POS-12-2"))
    conn12_2.commit()
    conn12_2.close()
                     
    # Mock order to raise error without events
    class OrderProxy:
        def __init__(self, real_order):
            self.real_order = real_order
            self.events = []
        def apply(self, event):
            raise ValueError("Simulated order error")
        def __getattr__(self, item):
            return getattr(self.real_order, item)
            
    mock_order = OrderProxy(o12_2)

    try:
        engine12_2.process_venue_event(mock_order, fill12_3)
    except ValueError as e:
        if "Simulated order error" not in str(e):
            raise
    
    assert engine12_2.halted, "Engine must halt when order.apply fails and events are missing"
    inc()
    engine12_2.close()
    
    # 3. Recon DB Read Error
    _fresh_db()
    cache12_3 = Cache()
    engine12_3 = M3BRiskEngine(cache12_3, Decimal("10.0"), db_path=DB_FILE)
    
    engine12_3.reconcile()
    assert engine12_3.halted is False, "Initial reconciliation should succeed"
    
    class BadConn:
        def __init__(self, real_conn):
            self.real_conn = real_conn
        def cursor(self):
            raise sqlite3.OperationalError("Simulated DB Read Error")
        def close(self):
            self.real_conn.close()
    engine12_3.conn = BadConn(engine12_3.conn)
    
    engine12_3.reconcile()
    assert engine12_3.halted, "Engine must halt on DB read error in reconcile"
    engine12_3.close()
    inc()
    
    # 4. Recon missing partial qty
    _fresh_db()
    cache12_4 = Cache()
    cache12_4.add_instrument(instrument)
    
    engine12_4 = M3BRiskEngine(cache12_4, Decimal("10.0"), db_path=DB_FILE)
    engine12_4.reconcile()
    
    o12_4 = engine12_4.evaluate_and_submit(
        proposal_id="PROP-12-4", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("5.0"), price=Decimal("100")
    )
    o12_4.apply(TestEventStubs.order_submitted(o12_4))
    o12_4.apply(TestEventStubs.order_accepted(o12_4))
    
    # Apply a fill to cache so order.filled_qty = 5
    fill12_4_ev = TestEventStubs.order_filled(o12_4, instrument, position_id=PositionId("POS-12-4"), trade_id=TradeId("t-12-4"), last_qty=Quantity.from_str("5.0"), last_px=Price.from_str("100"))
    o12_4.apply(fill12_4_ev)
    cache12_4.add_order(o12_4)
    
    # Create a native position for qty=3
    fill12_4_ev_3 = TestEventStubs.order_filled(o12_4, instrument, position_id=PositionId("POS-12-4"), trade_id=TradeId("t-12-4-partial"), last_qty=Quantity.from_str("3.0"), last_px=Price.from_str("100"))
    pos12_4 = Position(instrument, fill12_4_ev_3)
    cache12_4.add_position(pos12_4, True)
    
    # Close the setup engine before reopening the same SQLite DB on Windows.
    # The original Scenario 12 ended immediately after this block, so this latent
    # handle leak was previously masked by process exit.
    engine12_4.close()

    # Add only qty=3 to DB
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO fills (trade_id, client_order_id, instrument_id, side, qty, price, position_id, applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 ("t-12-4-partial", o12_4.client_order_id.value, instrument.id.value, str(OrderSide.BUY.value), "3.0", "100", "POS-12-4", 1))
    conn.commit()
    conn.close()
    
    engine12_4 = M3BRiskEngine(cache12_4, Decimal("10.0"), db_path=DB_FILE)
    engine12_4.reconcile()
    assert engine12_4.halted, "Engine must halt when cache qty mismatches DB qty"
    inc()
    engine12_4.close()

    # 5. Hardened duplicate metadata: persisted instrument_id + side must match.
    for field_name, bad_value in (("instrument_id", "BROKEN.INSTRUMENT"), ("side", "SELL")):
        _fresh_db()
        cache12_meta = Cache()
        cache12_meta.add_instrument(instrument)
        engine12_meta = M3BRiskEngine(cache12_meta, Decimal("10.0"), db_path=DB_FILE)
        engine12_meta.reconcile()

        o12_meta = engine12_meta.evaluate_and_submit(
            proposal_id=f"PROP-12-META-{field_name}", instrument=instrument,
            side=OrderSide.BUY, qty=Decimal("1.0"), price=Decimal("100")
        )
        engine12_meta.process_venue_event(o12_meta, TestEventStubs.order_submitted(o12_meta))
        engine12_meta.process_venue_event(o12_meta, TestEventStubs.order_accepted(o12_meta))
        trade_id_meta = f"t-12-meta-{field_name}"
        fill12_meta = TestEventStubs.order_filled(
            o12_meta, instrument, position_id=PositionId(f"POS-12-META-{field_name}"),
            trade_id=TradeId(trade_id_meta), last_qty=Quantity.from_str("1.0"),
            last_px=Price.from_str("100"),
        )
        engine12_meta.process_venue_event(o12_meta, fill12_meta)

        stored_meta = engine12_meta.conn.execute(
            "SELECT instrument_id, side FROM fills WHERE trade_id = ?", (trade_id_meta,)
        ).fetchone()
        assert stored_meta == (instrument.id.value, str(OrderSide.BUY.value)), "Fill metadata was not persisted canonically"

        engine12_meta.conn.execute(
            f"UPDATE fills SET {field_name} = ? WHERE trade_id = ?", (bad_value, trade_id_meta)
        )
        engine12_meta.conn.commit()
        try:
            engine12_meta.process_venue_event(o12_meta, fill12_meta)
            raise AssertionError(f"Duplicate with mismatched {field_name} must not be ignored")
        except Exception as e:
            assert "Duplicate fill inconsistency" in str(e), f"Unexpected mismatch error: {e}"
        assert engine12_meta.halted, f"Engine must HALT on duplicate {field_name} mismatch"
        inc(3)
        engine12_meta.close()

    # 6. Hardened live duplicate path: applied=1 + price=NULL must HALT immediately.
    _fresh_db()
    cache12_null = Cache()
    cache12_null.add_instrument(instrument)
    engine12_null = M3BRiskEngine(cache12_null, Decimal("10.0"), db_path=DB_FILE)
    engine12_null.reconcile()

    o12_null = engine12_null.evaluate_and_submit(
        proposal_id="PROP-12-NULL", instrument=instrument,
        side=OrderSide.BUY, qty=Decimal("1.0"), price=Decimal("100")
    )
    engine12_null.process_venue_event(o12_null, TestEventStubs.order_submitted(o12_null))
    engine12_null.process_venue_event(o12_null, TestEventStubs.order_accepted(o12_null))
    fill12_null = TestEventStubs.order_filled(
        o12_null, instrument, position_id=PositionId("POS-12-NULL"),
        trade_id=TradeId("t-12-null-live"), last_qty=Quantity.from_str("1.0"),
        last_px=Price.from_str("100"),
    )
    engine12_null.process_venue_event(o12_null, fill12_null)
    engine12_null.conn.execute(
        "UPDATE fills SET price = NULL WHERE trade_id = ?", ("t-12-null-live",)
    )
    engine12_null.conn.commit()

    try:
        engine12_null.process_venue_event(o12_null, fill12_null)
        raise AssertionError("applied=1 duplicate with price=NULL must not be ignored")
    except Exception as e:
        assert "Unverifiable duplicate fill metadata" in str(e), f"Unexpected NULL-price error: {e}"
    assert engine12_null.halted, "Engine must HALT on applied=1 duplicate with price=NULL"
    inc(2)
    engine12_null.close()
    
    print("PASS Scenario 12")

if __name__ == "__main__":
    try:
        run_tests()
        print(f"\n[SUCCESS] M3B Validation Passed! Total Assertions: {assertions_count}")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n[ERROR] Assertion Failed: {e}")
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[FATAL] Unexpected Error: {e}")
        traceback.print_exc()
        sys.exit(2)
