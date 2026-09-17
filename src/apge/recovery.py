import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from apge.simulator import SystemState


@dataclass(frozen=True)
class RecoveryCheckpoint:
    version: int
    system_state: str
    inventory: str
    last_event_seq: int
    clean_shutdown: bool


class RecoveryManager:
    """Persist restart metadata without treating local state as exchange authority.

    Recovery checkpoints are diagnostic and orchestration hints only. Exchange
    position/open-order reconciliation remains mandatory before OPERATIONAL state.
    """

    KEY = "recovery_checkpoint_v1"

    def __init__(self, persistence):
        self.persistence = persistence

    @staticmethod
    def _validate_inventory(value: Decimal) -> Decimal:
        if not isinstance(value, Decimal) or value.is_nan() or value.is_infinite():
            raise ValueError("inventory must be a finite Decimal")
        return value

    def save(
        self,
        *,
        system_state: SystemState,
        inventory: Decimal,
        last_event_seq: int = 0,
        clean_shutdown: bool = False,
    ) -> RecoveryCheckpoint:
        self._validate_inventory(inventory)
        if not isinstance(system_state, SystemState):
            raise ValueError("system_state must be a SystemState")
        if not isinstance(last_event_seq, int) or last_event_seq < 0:
            raise ValueError("last_event_seq must be a non-negative integer")
        checkpoint = RecoveryCheckpoint(
            version=1,
            system_state=system_state.name,
            inventory=str(inventory),
            last_event_seq=last_event_seq,
            clean_shutdown=bool(clean_shutdown),
        )
        self.persistence.update_runtime_state(
            self.KEY, json.dumps(asdict(checkpoint), sort_keys=True, separators=(",", ":"))
        )
        return checkpoint

    def load(self) -> RecoveryCheckpoint | None:
        raw = self.persistence.get_runtime_state(self.KEY)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            checkpoint = RecoveryCheckpoint(
                version=int(data["version"]),
                system_state=str(data["system_state"]),
                inventory=str(data["inventory"]),
                last_event_seq=int(data["last_event_seq"]),
                clean_shutdown=bool(data["clean_shutdown"]),
            )
            if checkpoint.version != 1:
                raise ValueError("unsupported recovery checkpoint version")
            SystemState[checkpoint.system_state]
            self._validate_inventory(Decimal(checkpoint.inventory))
            if checkpoint.last_event_seq < 0:
                raise ValueError("negative last_event_seq")
            return checkpoint
        except Exception as exc:
            raise ValueError("invalid recovery checkpoint") from exc

    def requires_reconciliation(self) -> bool:
        checkpoint = self.load()
        if checkpoint is None:
            return True
        return (not checkpoint.clean_shutdown) or checkpoint.system_state != SystemState.OPERATIONAL.name
