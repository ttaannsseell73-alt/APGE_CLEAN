import argparse
import logging
import os
import sys
import time
from dataclasses import replace
from decimal import Decimal

from apge.binance_adapter import BinanceAdapter, Transport
from apge.config import RuntimeConfig
from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import MarketRegime
from apge.observability import AuditLogger, MetricsRegistry
from apge.persistence import Persistence
from apge.reconciliation import Reconciler
from apge.recovery import RecoveryManager
from apge.simulator import OrderState, RiskEngine, SystemState
from apge.testnet_runtime import TestnetRuntime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("apge.bot_runner")


class RequestsTransport(Transport):
    def __init__(self):
        import requests
        self.session = requests.Session()

    def get(self, url, params=None, headers=None):
        response = self.session.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    def post(self, url, data=None, headers=None):
        response = self.session.post(url, data=data, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    def put(self, url, data=None, headers=None):
        response = self.session.put(url, data=data, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json() if response.content else {}

    def delete(self, url, params=None, headers=None):
        response = self.session.delete(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json() if response.content else {}


class DryRunMockAdapter:
    """Order-only no-network adapter used by the default runner mode."""

    def __init__(self):
        self._next_order_id = 1

    def submit_limit_order(self, **kwargs):
        order_id = self._next_order_id
        self._next_order_id += 1
        return {
            "status": "NEW",
            "orderId": str(order_id),
            "executedQty": "0",
            "avgPrice": "0",
        }

    def cancel_order(self, symbol, orig_client_order_id):
        return {
            "status": "CANCELED",
            "orderId": f"dry-{orig_client_order_id}",
            "executedQty": "0",
            "avgPrice": "0",
        }

    @staticmethod
    def _map_order_state(status):
        mapping = {
            "NEW": OrderState.OPEN,
            "CANCELED": OrderState.CANCELED,
            "FILLED": OrderState.FILLED,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
        }
        return mapping.get(status, OrderState.UNKNOWN)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="APGE deterministic V1 runner (DRY-RUN by default)")
    parser.add_argument("--dry-run", action="store_true", help="Compatibility flag; DRY-RUN is already the default.")
    parser.add_argument("--testnet", action="store_true", help="Compatibility flag; authenticated mutation still requires --allow-testnet-orders.")
    parser.add_argument(
        "--allow-testnet-orders",
        action="store_true",
        help="Explicitly permit LIMIT GTC order mutation on Binance Futures TESTNET only.",
    )
    parser.add_argument("--symbol", default=None, help="Override APGE_SYMBOL for this run.")
    parser.add_argument("--cycles", type=int, default=None, help="Override APGE_MAX_CYCLES for this run.")
    return parser.parse_args(argv)


def _build_config(args) -> RuntimeConfig:
    config = RuntimeConfig.from_env()
    if args.symbol is not None:
        config = replace(config, symbol=args.symbol)
    if args.cycles is not None:
        config = replace(config, max_cycles=args.cycles)
    return config.validate()


def _cancel_local_apge_orders(config, execution_engine) -> bool:
    for intent in list(execution_engine.persistence.get_active_intents()):
        cid = intent["client_order_id"]
        if intent.get("symbol") != config.symbol or not cid.startswith("APGE_"):
            continue
        if execution_engine.risk_engine.system_state != SystemState.OPERATIONAL:
            return False
        if not execution_engine.cancel_order(config.symbol, cid):
            return False
    return not execution_engine.persistence.get_active_intents()


def _run_dry(config, db, risk, execution, audit, metrics, recovery) -> int:
    runtime = TestnetRuntime(execution.adapter)  # only uses the order mock in this path after setup below
    # The dry runner does not perform network I/O. Provide validated deterministic
    # exchange-like constraints and a high enough synthetic BBA for BTC minNotional.
    runtime.symbol = config.symbol
    runtime.execution_engine = execution
    runtime.tick_size = Decimal("0.1")
    runtime.step_size = Decimal("0.001")
    runtime.filters = {
        "minQty": Decimal("0.001"),
        "maxQty": Decimal("100"),
        "minNotional": Decimal("5"),
    }
    runtime.stream_health = {"market": True, "user": True}
    runtime.is_connected = True
    runtime.best_bid = Decimal("100000")
    runtime.best_ask = Decimal("100001")
    runtime.bba_receive_timestamp = int(time.time() * 1000)

    risk.system_state = SystemState.OPERATIONAL
    recovery.save(system_state=risk.system_state, inventory=runtime.current_inventory, clean_shutdown=False)
    audit.event("runner_started", mode="dry-run", symbol=config.symbol)

    for cycle in range(config.max_cycles):
        runtime.bba_receive_timestamp = int(time.time() * 1000)
        runtime.run_grid_cycle(
            grid_spacing=config.grid_spacing,
            base_size=config.base_size,
            level_count=config.level_count,
            max_inventory=config.max_inventory,
            system_state=risk.system_state,
            market_regime=MarketRegime.NEUTRAL,
            execution_engine=execution,
        )
        metrics.increment("cycles")
        metrics.gauge("inventory", risk.current_position)
        recovery.save(
            system_state=risk.system_state,
            inventory=risk.current_position,
            last_event_seq=cycle + 1,
            clean_shutdown=False,
        )
        if risk.system_state != SystemState.OPERATIONAL:
            audit.event("runner_fail_closed", mode="dry-run", state=risk.system_state.name)
            return 2

    if not _cancel_local_apge_orders(config, execution):
        audit.event("cleanup_failed", mode="dry-run", state=risk.system_state.name)
        return 2

    recovery.save(
        system_state=risk.system_state,
        inventory=risk.current_position,
        last_event_seq=config.max_cycles,
        clean_shutdown=True,
    )
    audit.event("runner_stopped", mode="dry-run", metrics=metrics.snapshot())
    return 0


def _run_authenticated_testnet(config, db, adapter, risk, execution, audit, metrics, recovery) -> int:
    runtime = TestnetRuntime(adapter)
    reconciler = Reconciler(db, adapter, risk)
    runtime.attach_components(execution, reconciler, config.symbol)
    risk.system_state = SystemState.RECONCILING
    recovery.save(system_state=risk.system_state, inventory=Decimal("0"), clean_shutdown=False)
    audit.event("runner_started", mode="authenticated-testnet", symbol=config.symbol)

    if not runtime.sync_server_time() or not runtime.load_exchange_info(config.symbol):
        risk.system_state = SystemState.HALTED
        audit.event("preflight_failed", stage="time_or_exchange_info")
        return 2
    if not reconciler.resolve_state(config.symbol):
        risk.system_state = SystemState.HALTED
        audit.event("preflight_failed", stage="reconciliation")
        return 2

    runtime.current_inventory = reconciler.last_position_amount
    risk.complete_reconciliation()
    if risk.system_state != SystemState.OPERATIONAL:
        risk.system_state = SystemState.HALTED
        audit.event("preflight_failed", stage="risk_reconciliation")
        return 2

    listen_key = None
    try:
        listen_key = adapter.start_user_data_stream()
        runtime.start_event_loops(listen_key)
        last_keepalive = time.monotonic()

        for cycle in range(config.max_cycles):
            if time.monotonic() - last_keepalive >= 25 * 60:
                adapter.keepalive_user_data_stream(listen_key)
                last_keepalive = time.monotonic()

            # A REST BBA is only a bootstrap/fallback. Risk-increasing activity
            # remains blocked until both websocket streams are healthy and fresh.
            try:
                book = adapter.transport.get(
                    f"{adapter.http_url}/fapi/v1/ticker/bookTicker",
                    params={"symbol": config.symbol},
                )
                runtime.handle_book_ticker({
                    "s": config.symbol,
                    "b": book["bidPrice"],
                    "B": book["bidQty"],
                    "a": book["askPrice"],
                    "A": book["askQty"],
                    "u": 1,
                })
            except Exception:
                runtime.handle_data_uncertainty()

            runtime.run_grid_cycle(
                grid_spacing=config.grid_spacing,
                base_size=config.base_size,
                level_count=config.level_count,
                max_inventory=config.max_inventory,
                system_state=risk.system_state,
                market_regime=MarketRegime.NEUTRAL,
                execution_engine=execution,
            )
            metrics.increment("cycles")
            metrics.gauge("inventory", risk.current_position)
            recovery.save(
                system_state=risk.system_state,
                inventory=risk.current_position,
                last_event_seq=cycle + 1,
                clean_shutdown=False,
            )
            if risk.system_state == SystemState.HALTED:
                audit.event("runner_fail_closed", mode="authenticated-testnet", state="HALTED")
                return 2
            time.sleep(float(config.cycle_interval_seconds))

        # Controlled finite runner always exits flat in order-intent space. It
        # cancels APGE-owned intents only; unrelated exchange orders are untouched.
        if risk.system_state != SystemState.OPERATIONAL or not _cancel_local_apge_orders(config, execution):
            audit.event("cleanup_failed", mode="authenticated-testnet", state=risk.system_state.name)
            return 2
        if not reconciler.resolve_state(config.symbol):
            risk.system_state = SystemState.HALTED
            audit.event("cleanup_failed", mode="authenticated-testnet", stage="final_reconciliation")
            return 2
        runtime.current_inventory = reconciler.last_position_amount
        risk.complete_reconciliation()
        if risk.system_state != SystemState.OPERATIONAL:
            return 2

        recovery.save(
            system_state=risk.system_state,
            inventory=risk.current_position,
            last_event_seq=config.max_cycles,
            clean_shutdown=True,
        )
        audit.event("runner_stopped", mode="authenticated-testnet", metrics=metrics.snapshot())
        return 0
    finally:
        runtime.stop_event_loops()
        if listen_key:
            try:
                adapter.close_user_data_stream(listen_key)
            except Exception:
                logger.warning("Failed to close TESTNET listenKey during shutdown")


def main(argv=None):
    args = _parse_args(argv)
    try:
        config = _build_config(args)
    except ValueError as exc:
        logger.error("Invalid APGE configuration: %s", exc)
        return 2

    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    http_url = os.environ.get("BINANCE_TESTNET_HTTP_URL", "https://testnet.binancefuture.com")
    ws_url = os.environ.get("BINANCE_TESTNET_WS_URL", "wss://fstream.binancefuture.com")

    if args.allow_testnet_orders and (not api_key or not api_secret):
        logger.error("Authenticated TESTNET mutation requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET")
        return 2

    db = Persistence(config.db_path)
    audit = AuditLogger(db)
    metrics = MetricsRegistry()
    recovery = RecoveryManager(db)
    risk = RiskEngine(position_limit=config.position_limit, require_explicit_side=True)

    try:
        if args.allow_testnet_orders:
            adapter = BinanceAdapter(
                transport=RequestsTransport(),
                http_url=http_url,
                ws_url=ws_url,
                api_key=api_key,
                api_secret=api_secret,
            )
            execution = ExecutionEngine(db, adapter, risk)
            return _run_authenticated_testnet(
                config, db, adapter, risk, execution, audit, metrics, recovery
            )

        execution = ExecutionEngine(db, DryRunMockAdapter(), risk)
        return _run_dry(config, db, risk, execution, audit, metrics, recovery)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
