1. **Fix Signed Position Accounting in `RiskEngine`**
   - Update `Order` class in `src/apge/simulator.py` to store `symbol` and `side`.
   - Update `consume_approval_and_submit` to pass `record.symbol` and `record.side` to the `Order` constructor.
   - Update `on_fill`, `on_cancel_confirmed`, `resolve_order`, and `resolve_conflict` in `RiskEngine` to use signed position accounting (`current_position += diff` for BUY, `current_position -= diff` for SELL).
   - If side is not BUY or SELL, fail closed (enter RECONCILING and ignore fill).
   - Add required deterministic tests for position accounting.
2. **Fix Cancel -> RiskEngine Lifecycle in `ExecutionEngine`**
   - In `ExecutionEngine.cancel_order`, call `self.risk_engine.request_cancel(client_order_id)` when cancellation is requested.
   - In `ExecutionEngine.cancel_order`, if `raw_status` or mapped state indicates cancellation, and Binance provides authoritative filled quantity (e.g. `executedQty`), call `self.risk_engine.on_cancel_confirmed`. If it doesn't provide authoritative filled quantity, or it's missing, fail closed (enter RECONCILING).
   - In `ExecutionEngine.handle_order_update`, if it's a cancellation, use `accumulated_filled_qty` as the authoritative fill total to call `self.risk_engine.on_cancel_confirmed(cid, final_filled_amount)`.
   - Add required deterministic tests for cancellation lifecycle.
3. **Grid Lifecycle Offline Validation**
   - Add deterministic offline tests for Grid generation -> Risk -> Execution -> Persistence -> TestnetRuntime reconciliation lifecycle.
4. **Full Offline Gate**
   - Run full pytest test suite and ensure 100% pass.
5. **Controlled Binance Testnet Grid**
   - Create a script that configures `TestnetRuntime` and creates a 1-level grid.
   - Ensure it validates all limits (0.001 BTC minimum size, step sizes, etc.).
6. **Testnet Proof**
   - Run the controlled script to produce the grid, cancel the grid, and verify exact states matching persistence and RiskEngine.
   - Generate report `docs/APGE_03_CONTROLLED_GRID_VALIDATION.md` with results.
