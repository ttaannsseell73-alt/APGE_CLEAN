import sqlite3
import threading
from typing import Dict, Any, List, Optional
from decimal import Decimal

from apge.simulator import OrderState


class Persistence:
    """Deterministic SQLite-based persistence and audit layer."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def close(self):
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            delattr(self._local, "conn")

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS intents (
                    client_order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    status TEXT NOT NULL,
                    raw_exchange_status TEXT,
                    exchange_order_id TEXT,
                    filled_quantity TEXT DEFAULT '0',
                    average_price TEXT DEFAULT '0',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(client_order_id) REFERENCES intents(client_order_id)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def save_intent(self, client_order_id: str, symbol: str, side: str, quantity: Decimal, price: Decimal, status: OrderState):
        with self.conn:
            self.conn.execute("""
                INSERT INTO intents (client_order_id, symbol, side, quantity, price, status, raw_exchange_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (client_order_id, symbol, side, str(quantity), str(price), status.name, "NEW"))

    def update_intent_status(self, client_order_id: str, status: OrderState, exchange_order_id: Optional[str] = None, raw_status: Optional[str] = None):
        with self.conn:
            if exchange_order_id and raw_status:
                self.conn.execute("""
                    UPDATE intents SET status = ?, exchange_order_id = ?, raw_exchange_status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE client_order_id = ?
                """, (status.name, exchange_order_id, raw_status, client_order_id))
            elif exchange_order_id:
                self.conn.execute("""
                    UPDATE intents SET status = ?, exchange_order_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE client_order_id = ?
                """, (status.name, exchange_order_id, client_order_id))
            elif raw_status:
                self.conn.execute("""
                    UPDATE intents SET status = ?, raw_exchange_status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE client_order_id = ?
                """, (status.name, raw_status, client_order_id))
            else:
                self.conn.execute("""
                    UPDATE intents SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE client_order_id = ?
                """, (status.name, client_order_id))

    def get_intent(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM intents WHERE client_order_id = ?", (client_order_id,)).fetchone()
        if row:
            return dict(row)
        return None

    def get_active_intents(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT * FROM intents WHERE status NOT IN ('FILLED', 'CANCELED', 'REJECTED')
        """).fetchall()
        return [dict(row) for row in rows]

    def get_all_intents(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM intents").fetchall()
        return [dict(row) for row in rows]

    def add_fill(self, fill_id: str, client_order_id: str, quantity: Decimal, price: Decimal):
        with self.conn:
            if (not isinstance(quantity, Decimal) or quantity.is_nan() or
                    quantity.is_infinite() or quantity <= 0):
                return False
            if (not isinstance(price, Decimal) or price.is_nan() or
                    price.is_infinite() or price < 0):
                return False

            if self.conn.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone():
                return False

            intent_row = self.conn.execute(
                "SELECT quantity, filled_quantity, average_price FROM intents WHERE client_order_id = ?",
                (client_order_id,)).fetchone()
            if not intent_row:
                return False

            order_qty = Decimal(intent_row["quantity"])
            old_filled = Decimal(intent_row["filled_quantity"])
            old_avg_price = Decimal(intent_row["average_price"])
            new_filled = old_filled + quantity
            if new_filled > order_qty:
                return False

            self.conn.execute("""
                INSERT INTO fills (fill_id, client_order_id, quantity, price)
                VALUES (?, ?, ?, ?)
            """, (fill_id, client_order_id, str(quantity), str(price)))

            total_value = (old_avg_price * old_filled) + (price * quantity)
            new_avg_price = total_value / new_filled if new_filled > 0 else Decimal("0")
            self.conn.execute("""
                UPDATE intents SET filled_quantity = ?, average_price = ?, updated_at = CURRENT_TIMESTAMP
                WHERE client_order_id = ?
            """, (str(new_filled), str(new_avg_price), client_order_id))
            return True

    def sync_filled_quantity(self, client_order_id: str, cumulative_quantity: Decimal, average_price: Optional[Decimal] = None):
        """Persist an authoritative cumulative fill quantity without inventing a trade id."""
        with self.conn:
            if average_price is None:
                self.conn.execute("""
                    UPDATE intents SET filled_quantity = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE client_order_id = ?
                """, (str(cumulative_quantity), client_order_id))
            else:
                self.conn.execute("""
                    UPDATE intents SET filled_quantity = ?, average_price = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE client_order_id = ?
                """, (str(cumulative_quantity), str(average_price), client_order_id))

    def get_recorded_fill_total(self, client_order_id: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT quantity FROM fills WHERE client_order_id = ?", (client_order_id,)
        ).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows), Decimal("0"))

    def has_fill(self, fill_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)
        ).fetchone() is not None

    def update_runtime_state(self, key: str, value: str):
        with self.conn:
            self.conn.execute("""
                INSERT INTO runtime_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))

    def get_runtime_state(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
        if row:
            return row["value"]
        return None

    def append_audit_event(self, event_type: str, payload_json: str) -> int:
        if not event_type or not isinstance(payload_json, str):
            raise ValueError("audit event requires event_type and JSON payload text")
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO audit_events (event_type, payload_json) VALUES (?, ?)",
                (event_type, payload_json),
            )
            return int(cursor.lastrowid)

    def get_audit_events(self, limit: int = 1000) -> List[Dict[str, Any]]:
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("audit limit must be a positive integer")
        rows = self.conn.execute(
            "SELECT event_id, event_type, payload_json, created_at "
            "FROM audit_events ORDER BY event_id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
