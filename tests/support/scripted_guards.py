"""Test-only guard port: permit or block per ``GuardName`` (WO-040).

Slow and hold permits carry a bounded ``granted_scope`` shaped like the standing orders
(one basin for 30 min; one gate or berth for 15 min), so the machine can arm its timers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from soundings.ladder.guard_port import context_hash
from soundings.ladder.types import (
    ActionScope,
    GuardContext,
    GuardDecision,
    GuardName,
    GuardVerdict,
)

SLOW_SCOPE = ActionScope(action="advisory_one_basin", basin="inner_harbor_west", max_minutes=30)
HOLD_SCOPE = ActionScope(
    action="hold_one_gate_or_berth", asset_id="fuel_berth_1", gates=1, max_minutes=15
)


class ScriptedGuards:
    """Every guard permits unless the script says ``block``. ``calls`` logs each ask."""

    def __init__(
        self,
        script: Mapping[GuardName, GuardVerdict] | None = None,
        *,
        blocked: Iterable[GuardName] = (),
    ) -> None:
        self.script: dict[GuardName, GuardVerdict] = {g: GuardVerdict.PERMIT for g in GuardName}
        self.script.update(script or {})
        for g in blocked:
            self.script[g] = GuardVerdict.BLOCK
        self.calls: list[GuardName] = []

    def set(self, script: Mapping[GuardName, GuardVerdict]) -> None:
        self.script = {g: GuardVerdict.PERMIT for g in GuardName}
        self.script.update(script)

    def decide(self, name: GuardName, ctx: GuardContext) -> GuardDecision:
        self.calls.append(name)
        verdict = self.script[name]
        scope = None
        if verdict is GuardVerdict.PERMIT and name is GuardName.SLOW_PERMITTED:
            scope = SLOW_SCOPE
        if verdict is GuardVerdict.PERMIT and name is GuardName.HOLD_PERMITTED:
            scope = HOLD_SCOPE
        return GuardDecision(
            guard=name,
            verdict=verdict,
            reason=f"scripted_{verdict.value}",
            inputs_hash=context_hash(ctx),
            granted_scope=scope,
        )
