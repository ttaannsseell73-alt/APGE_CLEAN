from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Tuple

from apge.grid_strategy import OrderProposal
from apge.simulator import SystemState


@dataclass(frozen=True)
class GridLifecycleResult:
    created_client_ids: Tuple[str, ...]
    canceled_client_ids: Tuple[str, ...]
    blocked_reason: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)


def apply_grid_diff(symbol: str, proposals: Iterable[OrderProposal], execution_engine) -> GridLifecycleResult:
    """Diff desired proposals against persistent active intents and apply safely.

    Cancellation is always processed before creation. Any uncertainty or duplicate
    active intent moves the RiskEngine into RECONCILING and stops new exposure.
    """
    risk = execution_engine.risk_engine
    if risk.system_state != SystemState.OPERATIONAL:
        return GridLifecycleResult((), (), f"risk state is {risk.system_state.name}")

    desired = list(proposals)
    desired_keys = [(p.side, p.price, p.quantity) for p in desired]
    if len(set(desired_keys)) != len(desired_keys):
        risk.restore_connection()
        return GridLifecycleResult((), (), "duplicate desired grid key")

    active = [
        intent for intent in execution_engine.persistence.get_active_intents()
        if intent["symbol"] == symbol
    ]
    order_map = {}
    for intent in active:
        try:
            key = (
                str(intent["side"]),
                Decimal(str(intent["price"])),
                Decimal(str(intent["quantity"])),
            )
        except Exception:
            risk.restore_connection()
            return GridLifecycleResult((), (), "invalid persistent active intent")
        order_map.setdefault(key, []).append(str(intent["client_order_id"]))

    duplicate_live = [key for key, cids in order_map.items() if len(cids) > 1]
    if duplicate_live:
        risk.restore_connection()
        return GridLifecycleResult((), (), "duplicate active local grid intent")

    desired_set = set(desired_keys)
    current_set = set(order_map)
    to_cancel = sorted(current_set - desired_set, key=lambda item: (item[0], item[1], item[2]))
    to_create = sorted(desired_set - current_set, key=lambda item: (item[0], item[1], item[2]))

    canceled = []
    decision_position = risk.current_position
    for key in to_cancel:
        cid = order_map[key][0]
        try:
            ok = execution_engine.cancel_order(symbol, cid)
        except Exception:
            risk.restore_connection()
            return GridLifecycleResult((), tuple(canceled), f"cancel raised for {cid}")
        if not ok:
            risk.restore_connection()
            return GridLifecycleResult((), tuple(canceled), f"cancel was not authoritative for {cid}")
        canceled.append(cid)
        if risk.system_state != SystemState.OPERATIONAL:
            return GridLifecycleResult((), tuple(canceled), "cancel outcome requires reconciliation")

    if risk.current_position != decision_position:
        # An authoritative cancel can reveal a fill. The original inventory
        # target and risk-reducing quantities were computed before that fill.
        risk.restore_connection()
        return GridLifecycleResult((), tuple(canceled), "inventory changed during cancellation; recompute after reconciliation")

    proposal_by_key = {(p.side, p.price, p.quantity): p for p in desired}
    created = []
    for key in to_create:
        if risk.system_state != SystemState.OPERATIONAL:
            return GridLifecycleResult(tuple(created), tuple(canceled), "risk state changed before creation")
        cid = execution_engine.execute_proposal(symbol, proposal_by_key[key])
        if cid is not None:
            created.append(cid)
        if risk.system_state != SystemState.OPERATIONAL:
            return GridLifecycleResult(tuple(created), tuple(canceled), "submission requires reconciliation")

    return GridLifecycleResult(tuple(created), tuple(canceled), "")
