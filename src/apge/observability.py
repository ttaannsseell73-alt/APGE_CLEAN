import json
import threading
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict


_SENSITIVE_KEYS = {
    "api_key",
    "api_secret",
    "secret",
    "signature",
    "authorization",
    "x-mbx-apikey",
    "x_mbx_apikey",
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_KEYS:
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _json_safe(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class MetricsRegistry:
    """Small deterministic in-process metrics registry for V1.

    This intentionally avoids coupling the core to Prometheus/OpenTelemetry. A
    future exporter can consume snapshot() without changing risk/execution logic.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._counters: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        self._gauges: Dict[str, Decimal] = {}

    def increment(self, name: str, amount: Decimal = Decimal("1")) -> None:
        if not name:
            raise ValueError("metric name must be non-empty")
        if not isinstance(amount, Decimal) or amount.is_nan() or amount.is_infinite():
            raise ValueError("counter amount must be a finite Decimal")
        with self._lock:
            self._counters[name] += amount

    def gauge(self, name: str, value: Decimal) -> None:
        if not name:
            raise ValueError("metric name must be non-empty")
        if not isinstance(value, Decimal) or value.is_nan() or value.is_infinite():
            raise ValueError("gauge value must be a finite Decimal")
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            return {
                "counters": {k: str(self._counters[k]) for k in sorted(self._counters)},
                "gauges": {k: str(self._gauges[k]) for k in sorted(self._gauges)},
            }


class AuditLogger:
    """Durable structured audit writer backed by Persistence."""

    def __init__(self, persistence):
        self.persistence = persistence

    def event(self, event_type: str, **payload: Any) -> int:
        if not event_type:
            raise ValueError("event_type must be non-empty")
        clean = _json_safe(payload)
        payload_json = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        return self.persistence.append_audit_event(event_type, payload_json)


__all__ = ["AuditLogger", "MetricsRegistry"]
