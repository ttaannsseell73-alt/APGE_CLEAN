# FINAL REPORT

- **Final HEAD SHA**: 4f70f7b5a43b4cb5b1f233cc078bfd268164801f
- **Exact APGE classes/files used**:
    - `src/apge/binance_adapter.py` (BinanceAdapter)
    - `src/apge/bot_runner.py` (RequestsTransport)
    - `src/apge/persistence.py` (Persistence)
    - `src/apge/execution_engine.py` (ExecutionEngine)
    - `src/apge/simulator.py` (RiskEngine, OrderState, SystemState)
    - `src/apge/grid_strategy.py` (OrderProposal)
    - `src/apge/testnet_runtime.py` (TestnetRuntime)
    - `src/apge/reconciliation.py` (Reconciler)
- **Symbol**: BTCUSDT
- **Side**: BUY
- **Quantity**: 0.001
- **Fill price**: 75780.20
- **Deterministic clientOrderId**: APGE_00d06ecec49121d8b922
- **Intent persistence evidence**: `Saved Intent: {'client_order_id': 'APGE_00d06ecec49121d8b922', 'symbol': 'BTCUSDT', 'side': 'BUY', 'quantity': '0.001', 'price': '75780.20', 'status': 'OPEN', 'raw_exchange_status': 'NEW', 'exchange_order_id': '28587849391', 'filled_quantity': '0', 'average_price': '0', 'created_at': '2026-09-16 15:49:09', 'updated_at': '2026-09-16 15:49:10'}`
- **RiskEngine approval evidence**: The order was successfully submitted, which requires RiskEngine approval in ExecutionEngine (`self.risk_engine.request_approval(...)`). We saw `System State before submission: SystemState.OPERATIONAL`.
- **Exchange ACK evidence**: `exchange_order_id`: '28587849391'
- **WebSocket event evidence**: `Order filled: {'client_order_id': 'APGE_00d06ecec49121d8b922', 'symbol': 'BTCUSDT', 'side': 'BUY', 'quantity': '0.001', 'price': '75780.20', 'status': 'FILLED', 'raw_exchange_status': 'FILLED', 'exchange_order_id': '28587849391', 'filled_quantity': '0.001', 'average_price': '75779.6', 'created_at': '2026-09-16 15:49:09', 'updated_at': '2026-09-16 15:49:10'}`
- **Fill persistence evidence**: `Filled quantity = 0.001`, average_price = 75779.6
- **Duplicate replay/idempotency evidence**: `Intent after replay: 0.001`, `IDEMPOTENCY PASSED`
- **Exchange final position**: 0.0037
- **APGE local inventory**: 0.0037 (derived from initial inventory 0.0027 + filled quantity 0.001)
- **Final reserved risk**: 0.000
- **Final open orders**: 0
- **Reconciliation result**: `Reconciliation complete: success=True`
- **pytest PASS / FAIL**: `92 passed, 2 warnings in 0.17s`
- **Warnings/blockers**: None

CONTROLLED FILL VALIDATION PASS
READY FOR CONTROLLED GRID VALIDATION
