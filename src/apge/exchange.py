from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol

from apge.simulator import OrderState


class ExchangeAdapter(Protocol):
    """Venue-neutral contract consumed by APGE core services.

    Venue-specific adapters may expose additional methods, but core execution and
    reconciliation code must depend only on this interface. The current V1 order
    model is LIMIT-only and intentionally excludes leverage/margin mutations.
    """

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
        time_in_force: str = "GTC",
    ) -> Dict[str, Any]:
        ...

    def cancel_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        ...

    def get_positions(self) -> List[Dict[str, Any]]:
        ...

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        ...

    def query_order(self, symbol: str, orig_client_order_id: str) -> Dict[str, Any]:
        ...

    def get_server_time(self) -> Dict[str, Any]:
        ...

    def get_exchange_info(self) -> Dict[str, Any]:
        ...

    def _map_order_state(self, status: str) -> OrderState:
        ...
