"""WO-082 hand-built slot cases, shared by the unit, API and subprocess determinism tests.

The six-plan table (zone ``inner_basin_research``, 2026-10-17 at -07:00, ``now`` 08:00,
capacity 2, 15-minute step) has a unique optimum of 210 minutes: P-B +120 and P-C +90.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from soundings.registry.models import PlanStatus, PlanView

PDT = timezone(timedelta(hours=-7))
ZONE = "inner_basin_research"
NOW = datetime(2026, 10, 17, 8, 0, tzinfo=PDT)

# plan id -> (start, end, earliest, latest, conflicted); times are HH:MM on 2026-10-17 PDT
SIX: dict[str, tuple[str, str, str | None, str | None, bool]] = {
    "P-F": ("07:30", "09:30", None, None, True),
    "P-A": ("09:00", "11:00", "09:00", "09:00", True),
    "P-B": ("09:00", "11:00", "09:00", "11:00", True),
    "P-C": ("09:30", "11:30", "09:30", "11:30", True),
    "P-D": ("10:00", "11:00", "10:00", "10:00", True),
    "P-E": ("13:00", "14:00", "13:00", "15:00", False),
}
SIX_EXPECTED_SHIFT: dict[str, int] = {
    "P-F": 0,
    "P-A": 0,
    "P-B": 120,
    "P-C": 90,
    "P-D": 0,
    "P-E": 0,
}
SIX_OPTIMUM_MIN = 210


def at(hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, 17, hour, minute, tzinfo=PDT)


def view(
    plan_id: str,
    start: str,
    end: str,
    *,
    earliest: str | None = None,
    latest: str | None = None,
    conflicted: bool = True,
    area: str = ZONE,
    modem: int = 0,
) -> PlanView:
    return PlanView(
        plan_id=plan_id,
        operator="Harbor Survey Co (demo)",
        vehicle=f"ROV-{plan_id}",
        modem_id=f"MOD-{4100 + modem}",
        area=area,
        start_tick=None,
        end_tick=None,
        start_at=at(start),
        end_at=at(end),
        contact_channel="radio ch 16",
        status=PlanStatus.CONFLICTED if conflicted else PlanStatus.ACTIVE,
        earliest_start_at=None if earliest is None else at(earliest),
        latest_start_at=None if latest is None else at(latest),
    )


def six_plans(**latest_overrides: str) -> list[PlanView]:
    """The six-plan table as views; ``P_C="10:30"`` style overrides set a latest start."""
    out = []
    for i, (pid, (start, end, earliest, latest, conflicted)) in enumerate(SIX.items()):
        latest = latest_overrides.get(pid.replace("-", "_"), latest)
        out.append(
            view(pid, start, end, earliest=earliest, latest=latest, conflicted=conflicted, modem=i)
        )
    return out


def six_plan_bodies() -> list[dict[str, Any]]:
    """The six-plan table as ``POST /api/plans`` bodies, in table order."""
    bodies = []
    for i, (pid, (start, end, earliest, latest, _)) in enumerate(SIX.items()):
        body: dict[str, Any] = {
            "plan_id": pid,
            "operator": "Harbor Survey Co (demo)",
            "vehicle": f"ROV-{pid}",
            "modem_id": f"MOD-{4100 + i}",
            "area": ZONE,
            "start_at": at(start).isoformat(),
            "end_at": at(end).isoformat(),
            "contact_channel": "radio ch 16",
            "start_tick": 0 if pid == "P-B" else None,
            "end_tick": 1440 if pid == "P-B" else None,
            "tick_s": 5.0 if pid == "P-B" else None,
        }
        if earliest is not None:
            body["earliest_start_at"] = at(earliest).isoformat()
        if latest is not None:
            body["latest_start_at"] = at(latest).isoformat()
        bodies.append(body)
    return bodies
