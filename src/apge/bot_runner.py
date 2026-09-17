import argparse
import json
import logging
import os
import sys
import time
from dataclasses import fields, replace
from decimal import Decimal

from apge.adaptive_controller import run_adaptive_grid_cycle
from apge.adaptive_policy import AdaptivePolicyConfig
from apge.binance_adapter import BinanceAdapter, Transport
from apge.binance_public_data import parse_adaptive_market_snapshot
from apge.config import RuntimeConfig
from apge.execution_engine import ExecutionEngine
from apge.market_regime import Candle, RegimeConfig
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="DRY-RUN is already the default; cannot be combined with order mutation.")
    parser.add_argument("--testnet", action="store_true", help="Compatibility flag; authenticated mutation still requires --allow-testnet-orders.")
    mode.add_argument(
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


def _bind_runner_identity(db, mode, symbol):
    """A synthetic dry database must never become TESTNET exchange state."""
    identity = {"mode": mode, "symbol": symbol}
    raw = db.get_runtime_state("runner_identity_v1")
    if raw is not None:
        try:
            if json.loads(raw) != identity:
                raise ValueError("use a separate database for each runner mode and symbol")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid persisted runner identity") from exc
    if mode == "dry-run" and db.get_active_intents():
        raise ValueError("dry-run cannot recover active persisted orders through its synthetic adapter")
    db.update_runtime_state("runner_identity_v1", json.dumps(identity, sort_keys=True))


def _policy_config(config):
    return AdaptivePolicyConfig(**{field.name: getattr(config, field.name) for field in fields(AdaptivePolicyConfig)})


def _apply_adaptive_cycle(config, runtime, execution, audit, metrics, *, candles, funding_rate, source, **timestamps):
    # Account and trade events may arrive separately. The RiskEngine's reconciled
    # position plus validated trade fills is the position used for reservations.
    # Hold the same runtime lock used by user-data events across diff application.
    with runtime.event_lock:
        if runtime.ws_transports and (not all(runtime.stream_health.values()) or runtime.is_stale_data()):
            runtime.handle_data_uncertainty()
        if "premium_time_ms" in timestamps:
            now_ms = int(time.time() * 1000) + runtime.server_time_offset
            if not 0 <= now_ms - timestamps["premium_time_ms"] <= 5000:
                runtime.handle_data_uncertainty()
                audit.event("adaptive_inputs_expired", source=source)
        runtime.current_inventory = execution.risk_engine.current_position
        result = run_adaptive_grid_cycle(
            runtime=runtime,
            execution_engine=execution,
            candles=candles,
            funding_rate=funding_rate,
            nominal_base_size=config.base_size,
            level_count=config.level_count,
            max_inventory=config.max_inventory,
            regime_config=RegimeConfig(lookback=config.regime_lookback),
            policy_config=_policy_config(config),
        )
        if result.decision is not None:
            decision = result.decision
            metrics.gauge("grid_spacing", decision.grid_spacing)
            metrics.gauge("target_inventory", decision.target_inventory)
            audit.event(
                "adaptive_decision",
                source=source,
                regime=decision.regime.name,
                grid_spacing=decision.grid_spacing,
                base_size=decision.base_size,
                target_inventory=decision.target_inventory,
                risk_increasing_allowed=decision.risk_increasing_allowed,
                funding_rate=funding_rate,
                proposal_count=result.proposal_count,
                created_client_ids=result.lifecycle.created_client_ids,
                canceled_client_ids=result.lifecycle.canceled_client_ids,
                blocked_reason=result.lifecycle.blocked_reason,
                **timestamps,
            )
        metrics.increment("orders_created", Decimal(len(result.lifecycle.created_client_ids)))
        metrics.increment("orders_canceled", Decimal(len(result.lifecycle.canceled_client_ids)))
        if result.lifecycle.blocked:
            metrics.increment("blocked_cycles")
            audit.event("adaptive_cycle_blocked", source=source, reason=result.lifecycle.blocked_reason)
        return result


def _fetch_adaptive_snapshot(config, adapter):
    rows = adapter.get_klines(config.symbol, config.adaptive_interval, config.regime_lookback + 1)
    premium = adapter.get_premium_index(config.symbol)
    return parse_adaptive_market_snapshot(
        rows,
        premium,
        symbol=config.symbol,
        interval=config.adaptive_interval,
        lookback=config.regime_lookback,
        as_of_ms=int(adapter.clock() * 1000) + int(adapter.server_time_offset),
    )


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
    candles = tuple(Candle(Decimal("100000"), Decimal("100010"), Decimal("99990"), Decimal("100000")) for _ in range(config.regime_lookback))

    risk.system_state = SystemState.OPERATIONAL
    recovery.save(system_state=risk.system_state, inventory=runtime.current_inventory, clean_shutdown=False)
    audit.event("runner_started", mode="dry-run", symbol=config.symbol, source="SYNTHETIC_OFFLINE")

    for cycle in range(config.max_cycles):
        runtime.bba_receive_timestamp = int(time.time() * 1000)
        result = _apply_adaptive_cycle(
            config, runtime, execution, audit, metrics,
            candles=candles, funding_rate=Decimal("0"), source="SYNTHETIC_OFFLINE",
        )
        metrics.increment("cycles")
        metrics.gauge("inventory", risk.current_position)
        recovery.save(
            system_state=risk.system_state,
            inventory=risk.current_position,
            last_event_seq=cycle + 1,
            clean_shutdown=False,
        )
        if result.lifecycle.blocked or risk.system_state != SystemState.OPERATIONAL:
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
        recovery.save(system_state=risk.system_state, inventory=risk.current_position, clean_shutdown=False)
        audit.event("preflight_failed", stage="time_or_exchange_info")
        return 2
    if not reconciler.resolve_state(config.symbol):
        risk.system_state = SystemState.HALTED
        recovery.save(system_state=risk.system_state, inventory=risk.current_position, clean_shutdown=False)
        audit.event("preflight_failed", stage="reconciliation")
        return 2

    runtime.current_inventory = reconciler.last_position_amount
    risk.complete_reconciliation()
    if risk.system_state != SystemState.OPERATIONAL:
        risk.system_state = SystemState.HALTED
        recovery.save(system_state=risk.system_state, inventory=risk.current_position, clean_shutdown=False)
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

            if risk.system_state == SystemState.RECONCILING and all(runtime.stream_health.values()) and not runtime.is_stale_data():
                runtime.handle_reconnect()

            if risk.system_state == SystemState.OPERATIONAL and all(runtime.stream_health.values()) and not runtime.is_stale_data():
                try:
                    snapshot = _fetch_adaptive_snapshot(config, adapter)
                except Exception as exc:
                    runtime.handle_data_uncertainty()
                    metrics.increment("blocked_cycles")
                    audit.event("adaptive_inputs_invalid", reason=type(exc).__name__)
                    return 2
                _apply_adaptive_cycle(
                    config, runtime, execution, audit, metrics,
                    candles=snapshot.candles,
                    funding_rate=snapshot.funding_rate,
                    source="BINANCE_FUTURES_TESTNET",
                    last_close_time_ms=snapshot.last_close_time_ms,
                    premium_time_ms=snapshot.premium_time_ms,
                )
            else:
                metrics.increment("blocked_cycles")
                audit.event("adaptive_cycle_waiting", state=risk.system_state.name)
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
        with runtime.event_lock:
            if risk.system_state != SystemState.OPERATIONAL or not _cancel_local_apge_orders(config, execution):
                audit.event("cleanup_failed", mode="authenticated-testnet", state=risk.system_state.name)
                return 2
        # Stop event ingestion before the final authoritative snapshot so a late
        # asynchronous event cannot invalidate a just-written clean checkpoint.
        if not runtime.stop_event_loops():
            risk.system_state = SystemState.HALTED
            audit.event("cleanup_failed", mode="authenticated-testnet", stage="stream_shutdown")
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
        checkpoint = recovery.load()
        if checkpoint is not None and not checkpoint.clean_shutdown:
            recovery.save(
                system_state=risk.system_state,
                inventory=risk.current_position,
                last_event_seq=checkpoint.last_event_seq,
                clean_shutdown=False,
            )


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
    try:
        _bind_runner_identity(db, "authenticated-testnet" if args.allow_testnet_orders else "dry-run", config.symbol)
    except ValueError as exc:
        logger.error("APGE database isolation rejected startup: %s", exc)
        db.close()
        return 2
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
    except Exception as exc:
        risk.restore_connection()
        audit.event("runner_failed", reason=type(exc).__name__, state=risk.system_state.name)
        recovery.save(system_state=risk.system_state, inventory=risk.current_position, clean_shutdown=False)
        logger.error("APGE runner stopped safely: %s", type(exc).__name__)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
