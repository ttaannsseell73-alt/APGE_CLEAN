import json
from decimal import Decimal

import pytest

from apge.config import RuntimeConfig
from apge.observability import AuditLogger, MetricsRegistry
from apge.persistence import Persistence
from apge.recovery import RecoveryManager
from apge.simulator import SystemState


D = Decimal


def test_runtime_config_is_env_driven_and_fail_closed(monkeypatch):
    monkeypatch.setenv("APGE_SYMBOL", "ETHUSDT")
    monkeypatch.setenv("APGE_POSITION_LIMIT", "0.010")
    monkeypatch.setenv("APGE_MAX_INVENTORY", "0.008")
    monkeypatch.setenv("APGE_BASE_SIZE", "0.001")
    monkeypatch.setenv("APGE_LEVEL_COUNT", "2")
    monkeypatch.setenv("APGE_GRID_SPACING", "25")
    monkeypatch.setenv("APGE_MAX_CYCLES", "7")

    config = RuntimeConfig.from_env()
    assert config.symbol == "ETHUSDT"
    assert config.position_limit == D("0.010")
    assert config.max_inventory == D("0.008")
    assert config.level_count == 2
    assert config.grid_spacing == D("25")
    assert config.max_cycles == 7

    monkeypatch.setenv("APGE_POSITION_LIMIT", "NaN")
    with pytest.raises(ValueError):
        RuntimeConfig.from_env()


def test_runtime_config_rejects_incoherent_risk_limits():
    with pytest.raises(ValueError):
        RuntimeConfig(position_limit=D("0.005"), max_inventory=D("0.006")).validate()
    with pytest.raises(ValueError):
        RuntimeConfig(base_size=D("0.006")).validate()


def test_audit_trail_redacts_secrets_and_survives_restart(tmp_path):
    path = tmp_path / "audit.sqlite3"
    db = Persistence(str(path))
    audit = AuditLogger(db)
    event_id = audit.event(
        "startup",
        api_key="do-not-store",
        nested={"api_secret": "also-secret", "inventory": D("0.001")},
    )
    assert event_id == 1
    db.close()

    reopened = Persistence(str(path))
    events = reopened.get_audit_events()
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["api_key"] == "[REDACTED]"
    assert payload["nested"]["api_secret"] == "[REDACTED]"
    assert payload["nested"]["inventory"] == "0.001"
    reopened.close()


def test_metrics_registry_is_deterministic_and_validates_numbers():
    metrics = MetricsRegistry()
    metrics.increment("orders_submitted")
    metrics.increment("orders_submitted", D("2"))
    metrics.gauge("inventory", D("-0.001"))
    assert metrics.snapshot() == {
        "counters": {"orders_submitted": "3"},
        "gauges": {"inventory": "-0.001"},
    }
    with pytest.raises(ValueError):
        metrics.gauge("bad", D("NaN"))


def test_recovery_checkpoint_roundtrip_requires_exchange_reconciliation_after_unclean_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    db = Persistence(str(path))
    recovery = RecoveryManager(db)
    saved = recovery.save(
        system_state=SystemState.OPERATIONAL,
        inventory=D("0.002"),
        last_event_seq=12,
        clean_shutdown=False,
    )
    assert saved.last_event_seq == 12
    db.close()

    reopened = Persistence(str(path))
    recovery2 = RecoveryManager(reopened)
    loaded = recovery2.load()
    assert loaded is not None
    assert loaded.system_state == "OPERATIONAL"
    assert loaded.inventory == "0.002"
    assert recovery2.requires_reconciliation() is True

    recovery2.save(
        system_state=SystemState.OPERATIONAL,
        inventory=D("0.002"),
        last_event_seq=13,
        clean_shutdown=True,
    )
    assert recovery2.requires_reconciliation() is False

    reopened.update_runtime_state(RecoveryManager.KEY, "not-json")
    with pytest.raises(ValueError):
        recovery2.load()
    reopened.close()
