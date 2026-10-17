"""Standing orders: verified at load time, honoured in degraded mode (SPEC-LADDER section 5).

The ``signature`` in ``config/standing_orders.yaml`` is the sha256 hex digest of the
canonical YAML of the ``orders`` mapping (``canonical_orders_yaml``). The check is
tamper-evident, not authenticated: anyone can recompute a sha256, so it proves the orders
were not edited after signing, not who signed them. Keyed HMAC or PKI is a production step
(DECISIONS.md 23:05). ``python -m soundings.ladder.sign`` writes the line.

An unsigned, mismatched, expired or not-yet-valid file means nothing above ``ask``
executes: ``load_verified`` never raises on a bad signature, it returns ``signed=False``
with the reason. CLAUDE.md 2.4: no wall clock here; callers pass ``now``, and
``scenario_now`` anchors run-clock seconds on a fixed date.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import ValidationError

from soundings.schemas import Rung, ToggleName
from soundings.schemas._base import HarborId, StrictModel, from_file_data
from soundings.schemas.config import STANDING_ORDERS_PATH, HoldScope, Orders, SlowScope

from .boundary import NEVER_ACTIONS

SIGNATURE_LABEL = "tamper-evident (sha256 over the canonical orders), not authenticated"
"""The words the UI's About text uses for the standing-orders check (DECISIONS.md 23:05)."""

SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")

SCENARIO_DATE = (2026, 10, 17)
SCENARIO_TZ = dt.timezone(dt.timedelta(hours=-7))
"""Sim time is anchored on Saturday 2026-10-17, Pacific daylight time (-07:00)."""

Reason = Literal["ok", "missing", "unsigned", "mismatch", "invalid", "expired", "not_yet_valid"]

SLOW_RUNG = Rung.SLOW
HOLD_RUNG = Rung.HOLD


class StandingOrders(StrictModel):
    """The ``orders`` mapping, strict, with its two instants parsed (timezone-aware)."""

    orders: Orders
    signed_at: dt.datetime
    valid_until: dt.datetime

    @property
    def signed_by(self) -> str:
        return self.orders.signed_by

    @classmethod
    def from_mapping(cls, mapping: Any) -> StandingOrders:
        """Validate the raw ``orders`` mapping; raise ``ValueError`` on any shape error."""
        orders = Orders.model_validate(from_file_data(mapping))
        signed_at = _instant(orders.signed_at, "signed_at")
        valid_until = _instant(orders.valid_until, "valid_until")
        if valid_until <= signed_at:
            raise ValueError("valid_until must be after signed_at")
        for name in orders.degraded_mode.never:
            if name not in NEVER_ACTIONS:
                raise ValueError(f"degraded_mode.never names an unknown action: {name}")
        return cls(orders=orders, signed_at=signed_at, valid_until=valid_until)


def _instant(text: str, what: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError(f"{what} must carry a UTC offset")
    return value


class StandingScope(StrictModel):
    """What the signed orders pre-authorize right now.

    ``slow`` / ``hold`` are ``None`` when that rung is not pre-authorized (for example
    while a degraded reason is active and the rung is not in ``allowed_rungs``).
    ``never`` holds boundary action names and is absolute.
    """

    slow: SlowScope | None
    hold: HoldScope | None
    primary_channel: HarborId | None = None
    backup_channel: HarborId | None = None
    degraded_when: tuple[ToggleName, ...] = ()
    allowed_rungs: tuple[Rung, ...] = ()
    never: tuple[HarborId, ...] = ()
    degraded: tuple[ToggleName, ...] = ()
    """The degraded reasons this scope was narrowed for (sorted)."""

    @classmethod
    def from_orders(cls, so: StandingOrders) -> StandingScope:
        o = so.orders
        primary = o.approvers.get("primary")
        backup = o.approvers.get("backup")
        return cls(
            slow=o.scope.slow,
            hold=o.scope.hold if o.scope.hold.max_gates > 0 else None,
            primary_channel=None if primary is None else primary.channel,
            backup_channel=None if backup is None else backup.channel,
            degraded_when=tuple(sorted(set(o.degraded_mode.when))),
            allowed_rungs=o.degraded_mode.allowed_rungs,
            never=tuple(sorted({NEVER_ACTIONS[n] for n in o.degraded_mode.never})),
        )

    def under(self, reasons: tuple[ToggleName, ...] | list[ToggleName]) -> StandingScope:
        """This scope while ``reasons`` hold: only ``allowed_rungs`` survive. Never widens."""
        active = tuple(sorted(set(reasons) & set(self.degraded_when)))
        if not active:
            return self
        allowed = set(self.allowed_rungs)
        return self.model_copy(
            update={
                "slow": self.slow if SLOW_RUNG in allowed else None,
                "hold": self.hold if HOLD_RUNG in allowed else None,
                "degraded": tuple(sorted(set(self.degraded) | set(active))),
            }
        )


class StandingOrdersStatus(StrictModel):
    """The verdict of ``load_verified``. ``orders`` is set whenever the file parsed."""

    signed: bool
    reason: Reason
    orders: StandingOrders | None = None
    digest: str | None = None

    def at(self, now: dt.datetime) -> StandingOrdersStatus:
        """Re-check the validity window at ``now``; a broken signature stays broken."""
        if self.orders is None or self.reason not in ("ok", "expired", "not_yet_valid"):
            return self
        reason = _window(self.orders, now)
        return self.model_copy(update={"signed": reason == "ok", "reason": reason})


def _window(so: StandingOrders, now: dt.datetime) -> Reason:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if now < so.signed_at:
        return "not_yet_valid"
    if now >= so.valid_until:
        return "expired"
    return "ok"


def canonical_orders_yaml(mapping: Any) -> str:
    """``yaml.safe_dump`` of the loaded ``orders`` mapping, keys sorted, block style."""
    return yaml.safe_dump(mapping, sort_keys=True, default_flow_style=False, allow_unicode=True)


def orders_digest(mapping: Any) -> str:
    """The sha256 hex digest of ``canonical_orders_yaml(mapping)``."""
    return hashlib.sha256(canonical_orders_yaml(mapping).encode("utf-8")).hexdigest()


def read_document(path: str | Path) -> dict[str, Any]:
    """The whole YAML document as a mapping (``ValueError`` if it is not one)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("orders"), dict):
        raise ValueError("standing orders must be a mapping with an `orders` mapping")
    return doc


def load_verified(
    path: str | Path = STANDING_ORDERS_PATH, *, now: dt.datetime
) -> StandingOrdersStatus:
    """Verify the file at ``path`` at instant ``now``. Never raises on a bad signature.

    Order of checks: missing file, unparseable document (``invalid``), placeholder or
    malformed signature (``unsigned``), digest mismatch (``mismatch``), strict shape
    (``invalid``), then the validity window (``not_yet_valid`` / ``expired``).
    """
    p = Path(path)
    if not p.is_file():
        return StandingOrdersStatus(signed=False, reason="missing")
    try:
        doc = read_document(p)
    except (ValueError, yaml.YAMLError):
        return StandingOrdersStatus(signed=False, reason="invalid")
    digest = orders_digest(doc["orders"])
    signature = doc.get("signature")
    if not isinstance(signature, str) or not SIGNATURE_RE.match(signature):
        return StandingOrdersStatus(signed=False, reason="unsigned", digest=digest)
    if signature != digest:
        return StandingOrdersStatus(signed=False, reason="mismatch", digest=digest)
    try:
        so = StandingOrders.from_mapping(doc["orders"])
    except (ValidationError, ValueError):
        return StandingOrdersStatus(signed=False, reason="invalid", digest=digest)
    reason = _window(so, now)
    return StandingOrdersStatus(signed=reason == "ok", reason=reason, orders=so, digest=digest)


def effective_scope(
    status: StandingOrdersStatus,
    degraded_reasons: tuple[ToggleName, ...] | list[ToggleName] = (),
) -> StandingScope | None:
    """``None`` unless signed; otherwise the orders' scope narrowed for ``degraded_reasons``."""
    if not status.signed or status.orders is None:
        return None
    return StandingScope.from_orders(status.orders).under(tuple(degraded_reasons))


class _HasHour(Protocol):
    @property
    def hour_of_day(self) -> int: ...


def scenario_now(scenario: _HasHour | int, now_s: float) -> dt.datetime:
    """2026-10-17 ``hour_of_day``:00 -07:00 plus ``now_s`` run-clock seconds (no wall clock)."""
    hour = scenario if isinstance(scenario, int) else scenario.hour_of_day
    y, m, d = SCENARIO_DATE
    return dt.datetime(y, m, d, hour, 0, tzinfo=SCENARIO_TZ) + dt.timedelta(seconds=now_s)


__all__ = [
    "SIGNATURE_LABEL",
    "StandingOrders",
    "StandingOrdersStatus",
    "StandingScope",
    "canonical_orders_yaml",
    "effective_scope",
    "load_verified",
    "orders_digest",
    "read_document",
    "scenario_now",
]
