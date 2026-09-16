import json
import os
import sys
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from apge.binance_adapter import BinanceAdapter
from apge.bot_runner import RequestsTransport
from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import MarketRegime
from apge.persistence import Persistence
from apge.reconciliation import Reconciler
from apge.simulator import RiskEngine, SystemState
from apge.testnet_runtime import TestnetRuntime

HTTP_URL = "https://testnet.binancefuture.com"
WS_URL = "wss://fstream.binancefuture.com"
SYMBOL = "BTCUSDT"
HARD_MAX_BASE_SIZE = Decimal("0.001")


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step


def minimum_valid_base_size(price: Decimal, min_qty: Decimal, step_size: Decimal,
                            min_notional: Decimal) -> Decimal:
    if price <= 0:
        raise ValueError("price must be positive")
    by_notional = min_notional / price
    return max(ceil_to_step(min_qty, step_size), ceil_to_step(by_notional, step_size))


def _btc_position(positions):
    for row in positions:
        if row.get("symbol") == SYMBOL:
            return Decimal(str(row.get("positionAmt", "0")))
    return Decimal("0")


def _book_ticker(adapter):
    return adapter.transport.get(
        f"{adapter.http_url}/fapi/v1/ticker/bookTicker",
        params={"symbol": SYMBOL},
    )


def main() -> int:
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    if not api_key or not api_secret:
        print("BLOCKED: BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET are not available.")
        return 2

    adapter = BinanceAdapter(
        transport=RequestsTransport(),
        http_url=HTTP_URL,
        ws_url=WS_URL,
        api_key=api_key,
        api_secret=api_secret,
    )
    db = Persistence()
    risk = RiskEngine(position_limit=Decimal("0.002"), require_explicit_side=True)
    risk.system_state = SystemState.RECONCILING
    execution = ExecutionEngine(db, adapter, risk)
    reconciler = Reconciler(db, adapter, risk)
    runtime = TestnetRuntime(adapter)
    runtime.attach_components(execution, reconciler, SYMBOL)

    result = {
        "symbol": SYMBOL,
        "base_sha": "0c94e8a75e09a8cec0a57179c154f057f83cce04",
        "testnet_only": True,
        "market_order_used": False,
        "leverage_changed": False,
        "margin_mode_changed": False,
    }

    try:
        if not runtime.sync_server_time():
            raise RuntimeError("server time sync failed")
        if not runtime.load_exchange_info(SYMBOL):
            raise RuntimeError("exchange-info load failed")

        positions = adapter.get_positions()
        open_before = adapter.get_open_orders(symbol=SYMBOL)
        initial_position = _btc_position(positions)
        result["initial_exchange_inventory"] = str(initial_position)
        result["initial_exchange_open_orders"] = len(open_before)

        # Fail closed. This validation never takes over or cancels unrelated state.
        if open_before:
            raise RuntimeError("preflight found existing exchange open orders; refusing to touch them")
        if initial_position != 0:
            raise RuntimeError("preflight found non-zero BTCUSDT position; refusing controlled-grid validation")

        if not reconciler.resolve_state(SYMBOL):
            raise RuntimeError("startup reconciliation failed")
        runtime.current_inventory = reconciler.last_position_amount
        risk.complete_reconciliation()
        if risk.system_state != SystemState.OPERATIONAL:
            raise RuntimeError("RiskEngine did not become OPERATIONAL after reconciliation")

        book = _book_ticker(adapter)
        bid = Decimal(str(book["bidPrice"]))
        ask = Decimal(str(book["askPrice"]))
        runtime.handle_book_ticker({
            "s": SYMBOL,
            "b": str(bid),
            "B": str(book.get("bidQty", "0")),
            "a": str(ask),
            "A": str(book.get("askQty", "0")),
            "u": 1,
        })

        min_qty = Decimal(str(runtime.filters["minQty"]))
        min_notional = Decimal(str(runtime.filters["minNotional"]))
        step = Decimal(str(runtime.step_size))
        reference_price = min(bid, ask)
        base_size = minimum_valid_base_size(reference_price, min_qty, step, min_notional)
        result["calculated_base_size"] = str(base_size)
        if base_size > HARD_MAX_BASE_SIZE:
            raise RuntimeError(
                f"exchange filters require base size {base_size} > hard max {HARD_MAX_BASE_SIZE}")

        # Keep both orders passive and materially away from the spread.
        spacing = max(Decimal(str(runtime.tick_size)) * Decimal("10"), reference_price * Decimal("0.001"))
        spacing = ceil_to_step(spacing, Decimal(str(runtime.tick_size)))
        result["grid_spacing"] = str(spacing)
        result["best_bid"] = str(bid)
        result["best_ask"] = str(ask)

        runtime.run_grid_cycle(
            grid_spacing=spacing,
            base_size=base_size,
            level_count=1,
            max_inventory=base_size,
            system_state=risk.system_state,
            market_regime=MarketRegime.NEUTRAL,
            execution_engine=execution,
        )

        active1 = db.get_active_intents()
        if len(active1) != 2:
            raise RuntimeError(f"expected exactly 2 active APGE intents, got {len(active1)}")
        if {row["side"] for row in active1} != {"BUY", "SELL"}:
            raise RuntimeError("controlled grid did not create exactly one BUY and one SELL")
        cids1 = {row["client_order_id"] for row in active1}
        if any(not cid.startswith("APGE_") for cid in cids1):
            raise RuntimeError("non-APGE CID created")

        exchange1 = adapter.get_open_orders(symbol=SYMBOL)
        exchange_cids1 = {str(row.get("clientOrderId", "")) for row in exchange1}
        if not cids1.issubset(exchange_cids1):
            raise RuntimeError("exchange open orders do not contain both local APGE intents")

        # Repeat the unchanged snapshot. No duplicate order may be created.
        runtime.run_grid_cycle(
            grid_spacing=spacing,
            base_size=base_size,
            level_count=1,
            max_inventory=base_size,
            system_state=risk.system_state,
            market_regime=MarketRegime.NEUTRAL,
            execution_engine=execution,
        )
        active2 = db.get_active_intents()
        cids2 = {row["client_order_id"] for row in active2}
        if cids2 != cids1 or len(active2) != 2:
            raise RuntimeError("duplicate-cycle invariant failed")
        result["duplicate_cycle_new_submissions"] = 0

        # Cleanup only the two APGE orders created by this validation.
        for cid in sorted(cids1):
            try:
                execution.cancel_order(SYMBOL, cid)
            except Exception:
                # A fill/cancel race is never guessed. Reconcile instead.
                risk.restore_connection()

        if risk.system_state != SystemState.OPERATIONAL:
            if not reconciler.resolve_state(SYMBOL):
                raise RuntimeError("post-cancel reconciliation failed")
            runtime.current_inventory = reconciler.last_position_amount
            risk.complete_reconciliation()

        exchange_final = adapter.get_open_orders(symbol=SYMBOL)
        final_apge = [row for row in exchange_final if str(row.get("clientOrderId", "")).startswith("APGE_")]
        local_final = db.get_active_intents()
        positions_final = adapter.get_positions()
        final_position = _btc_position(positions_final)

        result.update({
            "grid_buy_cid": next(row["client_order_id"] for row in active1 if row["side"] == "BUY"),
            "grid_sell_cid": next(row["client_order_id"] for row in active1 if row["side"] == "SELL"),
            "final_exchange_open_apge_orders": len(final_apge),
            "final_local_active_intents": len(local_final),
            "final_reservations": str(risk.reservations),
            "final_exchange_inventory": str(final_position),
            "final_local_inventory": str(runtime.current_inventory),
            "system_state": risk.system_state.name,
        })

        if final_apge:
            raise RuntimeError("APGE exchange orders remain after cleanup")
        if local_final:
            raise RuntimeError("local active intents remain after cleanup")
        if risk.reservations != 0:
            raise RuntimeError("risk reservations remain after cleanup")
        if risk.system_state != SystemState.OPERATIONAL:
            raise RuntimeError("system did not finish OPERATIONAL")
        if final_position != runtime.current_inventory:
            raise RuntimeError("exchange/local inventory mismatch")

        result["reconciliation"] = "PASS"
        result["status"] = "APGE-03 CONTROLLED GRID VALIDATION PASS"
        Path("apge03_testnet_result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        result["status"] = "FAIL_CLOSED"
        result["error"] = str(exc)
        Path("apge03_testnet_result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
