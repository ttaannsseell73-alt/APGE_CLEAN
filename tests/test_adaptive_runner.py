"""Offline regression evidence; these tests do not pass the authenticated gate."""
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import apge.bot_runner as runner
from apge.binance_adapter import BinanceAdapter
from apge.binance_public_data import kline_close_time_ms, parse_adaptive_market_snapshot
from apge.config import RuntimeConfig
from apge.execution_engine import ExecutionEngine
from apge.grid_strategy import MarketRegime
from apge.market_regime import Candle
from apge.observability import AuditLogger, MetricsRegistry
from apge.persistence import Persistence
from apge.recovery import RecoveryManager
from apge.simulator import OrderState, RiskEngine, SystemState
from apge.testnet_runtime import TestnetRuntime, TestnetWebsocketTransport


D = Decimal
NOW = 6_100_000


def _rows(now=NOW, count=21):
    current_open = now // 300_000 * 300_000
    return [
        [opened, "100000", "100010", "99990", "100000", "1", opened + 299_999]
        for opened in range(current_open - (count - 1) * 300_000, current_open + 1, 300_000)
    ]


def _premium(now=NOW):
    return {"symbol": "BTCUSDT", "time": now, "markPrice": "100000", "indexPrice": "100000", "lastFundingRate": "-0.0001"}


def _snapshot(rows=None, premium=None, **kwargs):
    return parse_adaptive_market_snapshot(
        _rows() if rows is None else rows,
        _premium() if premium is None else premium,
        symbol="BTCUSDT", interval="5m", lookback=20, as_of_ms=NOW, **kwargs,
    )


def test_live_snapshot_uses_only_closed_bars_and_keeps_signed_funding():
    rows = _rows()
    rows[-1][2] = rows[-1][4] = "150000"  # forming shock must not influence the assessment
    snapshot = _snapshot(rows)
    assert len(snapshot.candles) == 20
    assert snapshot.candles[-1].close == D("100000")
    assert snapshot.last_close_time_ms == 5_999_999
    assert snapshot.funding_rate == D("-0.0001")


@pytest.mark.parametrize("case", ["short", "gap", "stale", "future", "fractional_time", "wrong_interval", "nan_price"])
def test_snapshot_rejects_incomplete_or_untrustworthy_history(case):
    rows = _rows()
    if case == "short":
        rows.pop(0)
    elif case == "gap":
        rows.pop(5)
    elif case == "stale":
        rows = _rows(now=NOW - 600_000)
    elif case == "future":
        rows.append([6_300_000, "100000", "100010", "99990", "100000", "1", 6_599_999])
    elif case == "fractional_time":
        rows[0][0] = 0.5
    elif case == "wrong_interval":
        rows[0][6] -= 1
    else:
        rows[0][1] = "NaN"
    with pytest.raises(ValueError):
        _snapshot(rows)


@pytest.mark.parametrize("field,value", [
    ("symbol", "ETHUSDT"), ("time", NOW - 5001), ("time", NOW + 1),
    ("time", True), ("time", "1.5"), ("lastFundingRate", "NaN"),
    ("lastFundingRate", "Infinity"), ("lastFundingRate", None),
    ("markPrice", "0"), ("indexPrice", "-1"),
])
def test_snapshot_rejects_invalid_or_stale_funding(field, value):
    premium = _premium()
    premium[field] = value
    with pytest.raises(ValueError):
        _snapshot(premium=premium)


def test_monthly_snapshot_uses_real_utc_calendar_in_leap_year():
    starts = [int(datetime(2024, month, 1, tzinfo=timezone.utc).timestamp() * 1000) for month in (1, 2, 3, 4)]
    rows = [[opened, "100", "101", "99", "100", "1", kline_close_time_ms(opened, "1M")] for opened in starts]
    now = starts[-1] + 100_000
    snapshot = parse_adaptive_market_snapshot(rows, _premium(now), symbol="BTCUSDT", interval="1M", lookback=3, as_of_ms=now)
    assert len(snapshot.candles) == 3
    assert rows[1][6] + 1 == starts[2]
    assert rows[1][6] - rows[1][0] + 1 == 29 * 86_400_000


class RecordingAdapter(runner.DryRunMockAdapter):
    parse_book_ticker = BinanceAdapter.parse_book_ticker
    parse_account_update = BinanceAdapter.parse_account_update

    def __init__(self):
        super().__init__()
        self.operations = []

    def submit_limit_order(self, **kwargs):
        self.operations.append(("submit", kwargs))
        return super().submit_limit_order(**kwargs)

    def cancel_order(self, symbol, orig_client_order_id):
        self.operations.append(("cancel", orig_client_order_id))
        return super().cancel_order(symbol, orig_client_order_id)


@pytest.fixture
def components():
    db = Persistence()
    adapter = RecordingAdapter()
    risk = RiskEngine(D("0.005"), require_explicit_side=True)
    execution = ExecutionEngine(db, adapter, risk)
    runtime = TestnetRuntime(adapter)
    runtime.symbol = "BTCUSDT"
    runtime.execution_engine = execution
    runtime.tick_size, runtime.step_size = D("0.1"), D("0.001")
    runtime.filters = {"minQty": D("0.001"), "maxQty": D("10"), "minNotional": D("5")}
    runtime.best_bid, runtime.best_ask = D("100000"), D("100001")
    runtime.bba_receive_timestamp = int(time.time() * 1000)
    runtime.is_connected = True
    runtime.stream_health = {"market": True, "user": True}
    yield db, adapter, risk, execution, runtime
    db.close()


def _cycle(components, config=RuntimeConfig(), candles=None):
    db, _, _, execution, runtime = components
    if candles is None:
        candles = [Candle(D("100000"), D("100010"), D("99990"), D("100000")) for _ in range(config.regime_lookback)]
    return runner._apply_adaptive_cycle(
        config, runtime, execution, AuditLogger(db), MetricsRegistry(),
        candles=candles, funding_rate=D("0"), source="SYNTHETIC_OFFLINE",
    )


def test_runner_config_controls_spacing_and_reprice_cancels_before_replacement(components):
    db, adapter, risk, _, _ = components
    first = _cycle(components)
    assert first.decision.grid_spacing == D("80.0004")
    first_cids = first.lifecycle.created_client_ids
    adapter.operations.clear()
    second = _cycle(components, replace(RuntimeConfig(), base_spacing_bps=D("16")))
    assert second.decision.grid_spacing == D("160.0008")
    assert set(second.lifecycle.canceled_client_ids) == set(first_cids)
    assert [op[0] for op in adapter.operations] == ["cancel", "cancel", "submit", "submit"]
    assert len(db.get_active_intents()) == 2
    assert risk.reservations == D("0.002")


@pytest.mark.parametrize("inventory", [D("0"), D("0.003"), D("-0.003")])
def test_runner_shock_gate_removes_normal_grid_and_only_reduces_inventory(components, inventory):
    db, adapter, risk, _, _ = components
    _cycle(components)
    adapter.operations.clear()
    risk.current_position = inventory
    candles = [Candle(D("100000"), D("100010"), D("99990"), D("100000")) for _ in range(19)]
    candles.append(Candle(D("100000"), D("105500"), D("99900"), D("105000")))
    result = _cycle(components, candles=candles)
    assert result.decision.regime == MarketRegime.SHOCK
    assert result.decision.risk_increasing_allowed is False
    submits = [op[1] for op in adapter.operations if op[0] == "submit"]
    if inventory == 0:
        assert submits == []
        assert db.get_active_intents() == []
    else:
        assert submits
        assert all(op["side"] == ("SELL" if inventory > 0 else "BUY") for op in submits)
        assert sum((op["quantity"] for op in submits), D("0")) <= abs(inventory)


@pytest.mark.parametrize("market_connected", [False, True])
def test_rest_bba_does_not_make_a_silent_or_disconnected_websocket_healthy(components, market_connected):
    _, adapter, risk, _, runtime = components
    runtime.ws_transports = [object()]
    runtime.stream_health["market"] = market_connected
    runtime.bba_stream_receive_timestamp = int(time.time() * 1000) - 10_000
    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "100000", "B": "1", "a": "100001", "A": "1", "u": 1})
    assert runtime.stream_health["market"] is market_connected
    assert runtime.is_stale_data()
    result = _cycle(components)
    assert result.lifecycle.blocked
    assert adapter.operations == []
    assert risk.system_state == SystemState.RECONCILING


def test_expired_user_listen_key_blocks_risk_increasing_activity(components):
    _, adapter, risk, _, runtime = components
    transport = TestnetWebsocketTransport("wss://fstream.binancefuture.com/ws/test-key", runtime, "test-key")
    transport._running = True
    transport._on_message(None, json.dumps({"e": "listenKeyExpired"}))
    assert runtime.stream_health["user"] is False
    assert risk.system_state == SystemState.CONNECTION_LOST
    assert _cycle(components).lifecycle.blocked
    assert adapter.operations == []


def test_disagreeing_account_position_blocks_decisions_until_reconciliation(components):
    _, adapter, risk, execution, runtime = components
    runtime.handle_user_data_event({"e": "ACCOUNT_UPDATE", "a": {"P": [
        {"s": "BTCUSDT", "pa": "0.003", "ep": "100000", "up": "0", "mt": "cross"},
    ]}}, execution)
    assert runtime.current_inventory == D("0.003")
    assert risk.system_state == SystemState.RECONCILING
    assert _cycle(components).lifecycle.blocked
    assert adapter.operations == []


def test_funding_that_expires_while_waiting_for_event_lock_cannot_submit(components):
    db, adapter, risk, execution, runtime = components
    runner._apply_adaptive_cycle(
        RuntimeConfig(), runtime, execution, AuditLogger(db), MetricsRegistry(),
        candles=[Candle(D("100000"), D("100010"), D("99990"), D("100000")) for _ in range(20)],
        funding_rate=D("0"), source="SYNTHETIC_OFFLINE",
        premium_time_ms=int(time.time() * 1000) - 10_000,
    )
    assert adapter.operations == []
    assert risk.system_state == SystemState.RECONCILING
    assert _events(db, "adaptive_inputs_expired")


def test_conflicting_dry_and_mutation_flags_are_rejected_before_execution():
    with pytest.raises(SystemExit) as exc:
        runner._parse_args(["--dry-run", "--allow-testnet-orders"])
    assert exc.value.code == 2


def test_cancel_fill_race_cannot_replace_using_the_previous_inventory(components, monkeypatch):
    _, adapter, risk, _, _ = components
    _cycle(components)
    original_cancel = adapter.cancel_order
    observed_fill = []

    def filled_cancel(symbol, cid):
        response = original_cancel(symbol, cid)
        if not observed_fill:
            response["executedQty"] = "0.001"
            response["avgPrice"] = "100000"
            observed_fill.append(cid)
        return response

    monkeypatch.setattr(adapter, "cancel_order", filled_cancel)
    adapter.operations.clear()
    result = _cycle(components, replace(RuntimeConfig(), base_spacing_bps=D("16")))
    assert result.lifecycle.blocked
    assert "inventory changed" in result.lifecycle.blocked_reason
    assert all(op[0] == "cancel" for op in adapter.operations)
    assert risk.system_state == SystemState.RECONCILING


def _events(db, kind):
    return [json.loads(event["payload_json"]) for event in db.get_audit_events() if event["event_type"] == kind]


def test_installed_runner_dry_path_is_offline_adaptive_and_clean_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "dry.sqlite3"
    monkeypatch.setenv("APGE_DB_PATH", str(path))
    monkeypatch.setenv("APGE_BASE_SPACING_BPS", "16")
    monkeypatch.setenv("APGE_REGIME_LOOKBACK", "3")
    monkeypatch.setattr(runner, "RequestsTransport", lambda: pytest.fail("dry runner attempted a network transport"))
    assert runner.main(["--cycles", "2"]) == 0
    assert runner.main(["--cycles", "1"]) == 0
    db = Persistence(str(path))
    try:
        decisions = _events(db, "adaptive_decision")
        assert len(decisions) == 3
        assert all(item["source"] == "SYNTHETIC_OFFLINE" for item in decisions)
        assert all(D(item["grid_spacing"]) == D("160.0008") for item in decisions)
        assert len(db.get_all_intents()) == 4  # 2 unchanged cycles then a restart
        assert db.get_active_intents() == []
        checkpoint = RecoveryManager(db).load()
        assert checkpoint.clean_shutdown is True
        assert checkpoint.inventory == "0"
    finally:
        db.close()


@pytest.mark.parametrize("identity", [
    {"mode": "authenticated-testnet", "symbol": "BTCUSDT"},
    {"mode": "dry-run", "symbol": "ETHUSDT"},
])
def test_dry_startup_rejects_foreign_database_without_mutating_checkpoint(tmp_path, monkeypatch, identity):
    path = tmp_path / "foreign.sqlite3"
    db = Persistence(str(path))
    db.update_runtime_state("runner_identity_v1", json.dumps(identity))
    RecoveryManager(db).save(system_state=SystemState.HALTED, inventory=D("0.003"), last_event_seq=99)
    original = db.get_runtime_state(RecoveryManager.KEY)
    db.close()
    monkeypatch.setenv("APGE_DB_PATH", str(path))
    assert runner.main(["--cycles", "1"]) == 2
    reopened = Persistence(str(path))
    try:
        assert reopened.get_runtime_state(RecoveryManager.KEY) == original
        assert reopened.get_audit_events() == []
    finally:
        reopened.close()


def test_dry_startup_cannot_cancel_legacy_persisted_orders_through_a_fake_adapter(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    db = Persistence(str(path))
    db.save_intent("APGE_PREEXISTING", "BTCUSDT", "BUY", D("0.001"), D("100000"), OrderState.OPEN)
    db.close()
    monkeypatch.setenv("APGE_DB_PATH", str(path))
    assert runner.main(["--cycles", "1"]) == 2
    reopened = Persistence(str(path))
    try:
        assert reopened.get_intent("APGE_PREEXISTING")["status"] == "OPEN"
        assert reopened.get_runtime_state("runner_identity_v1") is None
    finally:
        reopened.close()


class TestnetTransport:
    """A stateful offline HTTP fixture, never a real TESTNET connection."""
    __test__ = False

    def __init__(self, invalid_funding_on=None):
        self.orders = {}
        self.requests = []
        self.premium_calls = 0
        self.invalid_funding_on = invalid_funding_on

    def get(self, url, params=None, headers=None):
        self.requests.append(("GET", url, params))
        now = int(time.time() * 1000)
        if url.endswith("/time"):
            return {"serverTime": now}
        if url.endswith("/exchangeInfo"):
            return {"symbols": [{"symbol": "BTCUSDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]}]}
        if url.endswith("/positionRisk"):
            return [{"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"}]
        if url.endswith("/openOrders"):
            return [dict(order) for order in self.orders.values() if order["status"] == "NEW"]
        if url.endswith("/bookTicker"):
            return {"bidPrice": "100000", "bidQty": "1", "askPrice": "100001", "askQty": "1"}
        if url.endswith("/klines"):
            return _rows(now, params["limit"])
        if url.endswith("/premiumIndex"):
            self.premium_calls += 1
            premium = _premium(now)
            premium["lastFundingRate"] = "NaN" if self.premium_calls == self.invalid_funding_on else "0"
            return premium
        if url.endswith("/order"):
            return dict(self.orders[params["origClientOrderId"]])
        raise AssertionError(url)

    def post(self, url, data=None, headers=None):
        self.requests.append(("POST", url, data))
        if url.endswith("/listenKey"):
            return {"listenKey": "test-listen-key"}
        cid = data["newClientOrderId"]
        order = {"symbol": data["symbol"], "side": data["side"], "clientOrderId": cid, "orderId": len(self.orders) + 1,
                 "status": "NEW", "origQty": data["quantity"], "price": data["price"], "executedQty": "0", "avgPrice": "0"}
        self.orders[cid] = order
        return dict(order)

    def delete(self, url, params=None, headers=None):
        self.requests.append(("DELETE", url, params))
        if url.endswith("/listenKey"):
            return {}
        order = self.orders[params["origClientOrderId"]]
        order["status"] = "CANCELED"
        return dict(order)


def _start_offline_streams(runtime, key):
    class Stream:
        def stop(self):
            pass
    runtime.ws_transports = [Stream(), Stream()]
    runtime.execution_engine.risk_engine.restore_connection()
    runtime.handle_stream_open("market")
    runtime.handle_stream_open("user")
    runtime.handle_book_ticker({"s": "BTCUSDT", "b": "100000", "B": "1", "a": "100001", "A": "1", "u": 1}, from_stream=True)


@pytest.mark.parametrize("invalid_funding_on", [None, 2])
def test_authenticated_runner_wiring_uses_real_core_and_fails_closed_on_bad_data(tmp_path, monkeypatch, invalid_funding_on):
    transport = TestnetTransport(invalid_funding_on)
    adapter = BinanceAdapter(transport, "https://testnet.binancefuture.com", "wss://fstream.binancefuture.com", "test-key", "test-secret")
    db = Persistence(str(tmp_path / "testnet-fixture.sqlite3"))
    risk = RiskEngine(D("0.005"), require_explicit_side=True)
    execution = ExecutionEngine(db, adapter, risk)
    monkeypatch.setattr(TestnetRuntime, "start_event_loops", _start_offline_streams)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    try:
        status = runner._run_authenticated_testnet(
            replace(RuntimeConfig(), max_cycles=2), db, adapter, risk, execution,
            AuditLogger(db), MetricsRegistry(), RecoveryManager(db),
        )
        decisions = _events(db, "adaptive_decision")
        assert decisions
        assert decisions[0]["regime"] == "NEUTRAL"
        assert decisions[0]["last_close_time_ms"] < int(time.time() * 1000)
        kline_calls = [request for request in transport.requests if request[1].endswith("/klines")]
        assert all(request[2] == {"symbol": "BTCUSDT", "interval": "5m", "limit": 21} for request in kline_calls)
        checkpoint = RecoveryManager(db).load()
        assert transport.requests[-1][0:2] == ("DELETE", "https://testnet.binancefuture.com/fapi/v1/listenKey")
        if invalid_funding_on:
            assert status == 2
            assert len(transport.orders) == 2
            assert len(db.get_active_intents()) == 2
            assert risk.system_state == SystemState.RECONCILING
            assert checkpoint.clean_shutdown is False
            assert checkpoint.system_state == "RECONCILING"
            assert _events(db, "adaptive_inputs_invalid")
        else:
            assert status == 0
            assert len(decisions) == 2
            assert db.get_active_intents() == []
            assert risk.reservations == 0
            assert checkpoint.clean_shutdown is True
    finally:
        db.close()


def test_testnet_preflight_failure_persists_halted_unclean_state(tmp_path, monkeypatch):
    transport = TestnetTransport()
    adapter = BinanceAdapter(transport, "https://testnet.binancefuture.com", "wss://fstream.binancefuture.com", "test-key", "test-secret")
    monkeypatch.setattr(adapter, "get_server_time", lambda: {})
    db = Persistence(str(tmp_path / "preflight.sqlite3"))
    risk = RiskEngine(D("0.005"), require_explicit_side=True)
    try:
        assert runner._run_authenticated_testnet(RuntimeConfig(), db, adapter, risk, ExecutionEngine(db, adapter, risk),
                                               AuditLogger(db), MetricsRegistry(), RecoveryManager(db)) == 2
        checkpoint = RecoveryManager(db).load()
        assert checkpoint.system_state == "HALTED"
        assert checkpoint.clean_shutdown is False
        assert transport.orders == {}
    finally:
        db.close()


def test_stream_that_cannot_stop_prevents_a_clean_success_checkpoint(tmp_path, monkeypatch):
    transport = TestnetTransport()
    adapter = BinanceAdapter(transport, "https://testnet.binancefuture.com", "wss://fstream.binancefuture.com", "test-key", "test-secret")
    db = Persistence(str(tmp_path / "shutdown.sqlite3"))
    risk = RiskEngine(D("0.005"), require_explicit_side=True)

    def start(runtime, key):
        _start_offline_streams(runtime, key)
        runtime.ws_transports[0].stop = lambda: False

    monkeypatch.setattr(TestnetRuntime, "start_event_loops", start)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    try:
        status = runner._run_authenticated_testnet(
            replace(RuntimeConfig(), max_cycles=1), db, adapter, risk, ExecutionEngine(db, adapter, risk),
            AuditLogger(db), MetricsRegistry(), RecoveryManager(db),
        )
        assert status == 2
        assert db.get_active_intents() == []
        assert risk.system_state == SystemState.HALTED
        checkpoint = RecoveryManager(db).load()
        assert checkpoint.clean_shutdown is False
        assert checkpoint.system_state == "HALTED"
        assert _events(db, "runner_stopped") == []
    finally:
        db.close()


def test_stopped_transport_ignores_late_messages(components):
    _, _, risk, _, runtime = components
    transport = TestnetWebsocketTransport("wss://fstream.binancefuture.com/ws/test-key", runtime, "test-key")
    transport._running = True
    assert transport.stop() is True
    transport._on_message(None, json.dumps({"e": "listenKeyExpired"}))
    transport._on_error(None, "closed")
    assert risk.system_state == SystemState.OPERATIONAL
