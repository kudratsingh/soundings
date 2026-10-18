"""The demo script: ``docs/demo-script.yaml`` as a validated input (WO-062).

Each step has a time mark ``t`` (``m:ss``), either a ``scenario`` (a catalog id such as
``S05``, ``live`` or ``twist``) or ``eval: true``, and a ``say`` line that must pass the
denylist. Unknown keys are rejected. ``DemoScript.load`` checks that the marks are strictly
increasing, that a ``twist`` step names ``scenario_ref`` and ``toggles``, and that a
``continue_run`` step follows a step on the same scenario.

``inject`` is kept as a plain mapping in Part A; Part B types it as WO-061's
``InjectRequest`` and makes it required on ``live``.

WO-062P (presenter mode) adds the shot-list fields: ``caption`` (on screen), ``speed``
(``1x`` | ``20x``), ``action`` (a named step that is not a catalog run, see ``ACTIONS``),
``beats`` (pending actions inside a scenario step), ``spotlight`` (an element name or id)
and ``pause_for_click``; and an optional script-level ``end`` mark (when the last shot
ends). A step has exactly one of ``scenario``, ``eval: true`` or ``action``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from soundings.schemas._base import StrictModel, from_file_data
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import LadderState, ToggleName, _VocabEnum
from soundings.sim.scenario import UnknownScenarioError, scenario_path

_MARK = re.compile(r"^(\d{1,2}):([0-5]\d)$")
_CATALOG_ID = re.compile(r"^S\d{2}$")
LIVE = "live"
TWIST = "twist"
EVAL = "eval"

ACTION = "action"
NonNegInt = Annotated[int, Field(ge=0)]
Speed = Literal["1x", "20x"]
SPEED_FACTOR: dict[str, float] = {"1x": 1.0, "20x": 20.0}
_SPOTLIGHT = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class ActionName(_VocabEnum):
    """Named presenter actions (WO-062P). Each is shipped by the WO in ``ACTIONS``."""

    LADDER_CHIPS = "ladder_chips"
    FILE_PLAN_QR = "file_plan_qr"
    PROPOSE_SLOT = "propose_slot"
    VESSEL_ADVISORY = "vessel_advisory"
    JUDGE_DROPS_ROGUE = "judge_drops_rogue"
    TWIST_TOGGLES = "twist_toggles"
    RUNG_LEDGER_EVAL = "rung_ledger_eval"
    END_CARD = "end_card"


ACTIONS: dict[ActionName, str | None] = {
    # None: the presenter overlay renders it today. A WO id: pending until that WO lands.
    ActionName.LADDER_CHIPS: None,
    ActionName.FILE_PLAN_QR: None,  # WO-061: opens the judge panel on its QR code
    ActionName.PROPOSE_SLOT: None,  # merged 2026-10-17; rendered live
    ActionName.VESSEL_ADVISORY: None,  # merged 2026-10-17; rendered live
    ActionName.JUDGE_DROPS_ROGUE: None,  # WO-061: POST /api/inject (closing on fuel_berth_2)
    ActionName.TWIST_TOGGLES: None,  # WO-060B merged 2026-10-17; rendered live
    ActionName.RUNG_LEDGER_EVAL: None,  # merged 2026-10-17; rendered live
    ActionName.END_CARD: None,
}


def pending_note(action: ActionName) -> str | None:
    """``"action propose_slot pending WO-082"``, or None when the action is live."""
    wo = ACTIONS[action]
    return None if wo is None else f"action {action.value} pending {wo}"


def parse_mark(text: str) -> int:
    """``"m:ss"`` to whole seconds (``"2:45"`` -> 165). Raises ``ValueError`` otherwise."""
    m = _MARK.match(text)
    if m is None:
        raise ValueError(f"time mark {text!r} is not m:ss")
    return int(m.group(1)) * 60 + int(m.group(2))


def format_mark(seconds: int) -> str:
    """Whole seconds to ``"m:ss"`` (165 -> ``"2:45"``)."""
    if seconds < 0:
        raise ValueError("a time mark is never negative")
    return f"{seconds // 60}:{seconds % 60:02d}"


def _check_catalog_id(ref: str) -> str:
    if not _CATALOG_ID.match(ref):
        raise ValueError(f"scenario {ref!r} is not a catalog id (S01..S99)")
    try:
        scenario_path(ref)
    except UnknownScenarioError as exc:
        raise ValueError(f"unknown scenario id {ref!r}") from exc
    return ref


# --- WO-060D begin: the presenter's focus per step ---
FOCUS_POLICIES = frozenset({"first_escalation", "first_detected"})
_CONTACT_ID = re.compile(r"^c-\d{4,}$")
# --- WO-060D end ---


class DemoStep(StrictModel):
    """One step of the pitch sequence."""

    t: StrictStr
    scenario: StrictStr | None = None
    eval: StrictBool = False
    say: FreeText
    seed: NonNegInt | None = None
    scenario_ref: StrictStr | None = None
    toggles: tuple[ToggleName, ...] = ()
    continue_run: StrictBool = False
    approve: StrictBool = False
    inject: dict[str, Any] | None = None
    expect_state: LadderState | None = None
    # WO-062P presenter fields.
    caption: FreeText | None = None
    speed: Speed | None = None
    action: ActionName | None = None
    beats: tuple[ActionName, ...] = ()
    spotlight: StrictStr | None = None
    pause_for_click: StrictBool = False
    # --- WO-088 begin ---
    subcaption: FreeText | None = None
    """A second, smaller caption line under ``caption`` (presenter mode only)."""
    # --- WO-088 end ---
    # --- WO-060D begin ---
    focus: StrictStr | None = None
    """Which case the screen follows when this step's run starts (presenter mode only):
    ``first_escalation`` (the default), ``first_detected`` or a contact id (``c-0001``)."""
    # --- WO-060D end ---

    @field_validator("focus")
    @classmethod
    def _focus(cls, value: str | None) -> str | None:
        if value is not None and value not in FOCUS_POLICIES and not _CONTACT_ID.match(value):
            raise ValueError(
                f"focus {value!r} is not first_escalation, first_detected or a contact id"
            )
        return value

    @field_validator("t")
    @classmethod
    def _mark(cls, value: str) -> str:
        parse_mark(value)
        return value

    @field_validator("spotlight")
    @classmethod
    def _spotlight(cls, value: str | None) -> str | None:
        if value is not None and not _SPOTLIGHT.match(value):
            raise ValueError(f"spotlight {value!r} is not an element name (a-z, 0-9, -)")
        return value

    @model_validator(mode="after")
    def _shape(self) -> DemoStep:
        kinds = (self.scenario is not None) + self.eval + (self.action is not None)
        if kinds != 1:
            raise ValueError("a step has exactly one of `scenario`, `eval: true` or `action`")
        if self.action is ActionName.TWIST_TOGGLES:
            if self.scenario_ref is None or not self.toggles:
                raise ValueError("a twist_toggles action needs `scenario_ref` and `toggles`")
            _check_catalog_id(self.scenario_ref)
            return self
        if self.action is not None and (self.toggles or self.scenario_ref is not None):
            raise ValueError("only a twist step or a twist_toggles action takes toggles")
        if self.beats and self.kind != "catalog":
            raise ValueError("`beats` belong to a catalog scenario step")
        if self.scenario is not None and self.scenario not in (LIVE, TWIST):
            _check_catalog_id(self.scenario)
        if self.scenario == TWIST:
            if self.scenario_ref is None:
                raise ValueError("a twist step needs `scenario_ref` (the catalog id it runs)")
            if not self.toggles:
                raise ValueError("a twist step needs `toggles`")
        if self.scenario_ref is not None:
            if self.scenario != TWIST:
                raise ValueError("`scenario_ref` is only for a twist step")
            _check_catalog_id(self.scenario_ref)
        if self.inject is not None and self.scenario != LIVE:
            raise ValueError("`inject` is only for a live step")
        return self

    @property
    def mark_s(self) -> int:
        return parse_mark(self.t)

    @property
    def kind(self) -> str:
        """``eval``, ``action``, ``live``, ``twist`` or ``catalog``."""
        if self.eval:
            return EVAL
        if self.action is not None:
            return ACTION
        if self.scenario in (LIVE, TWIST):
            return str(self.scenario)
        return "catalog"

    @property
    def run_ref(self) -> str | None:
        """The catalog id this step runs (``scenario_ref`` for a twist), else None."""
        if self.kind == "catalog":
            return self.scenario
        if self.kind == TWIST:
            return self.scenario_ref
        return None

    @property
    def speed_factor(self) -> float | None:
        """``20x`` -> 20.0; None when the step names no speed."""
        return None if self.speed is None else SPEED_FACTOR[self.speed]

    @property
    def pending(self) -> tuple[str, ...]:
        """Notes for every action or beat of this step that no merged WO ships yet."""
        names = ((self.action,) if self.action is not None else ()) + self.beats
        return tuple(n for n in (pending_note(a) for a in names) if n is not None)

    @property
    def label(self) -> str:
        """Transcript label: ``S05``, ``twist S05+network_down``, ``live``, ``eval``,
        ``action end_card``."""
        if self.action is not None:
            return f"action {self.action.value}"
        if self.kind == TWIST:
            return f"twist {self.scenario_ref}+" + "+".join(t.value for t in self.toggles)
        if self.kind == EVAL:
            return EVAL
        return str(self.scenario)


class DemoScript(StrictModel):
    """The whole pitch sequence, steps in time-mark order."""

    steps: Annotated[tuple[DemoStep, ...], Field(min_length=1)]
    end: StrictStr | None = None

    @field_validator("end")
    @classmethod
    def _end_mark(cls, value: str | None) -> str | None:
        if value is not None:
            parse_mark(value)
        return value

    @model_validator(mode="after")
    def _order(self) -> DemoScript:
        marks = [s.mark_s for s in self.steps]
        if self.end is not None and parse_mark(self.end) <= marks[-1]:
            raise ValueError("`end` must come after the last step's mark")
        for prev, cur in zip(marks, marks[1:], strict=False):
            if cur <= prev:
                raise ValueError("time marks must be strictly increasing")
        for i, step in enumerate(self.steps):
            if step.continue_run:
                if i == 0 or self.steps[i - 1].run_ref != step.run_ref or step.run_ref is None:
                    raise ValueError(
                        f"step {step.t}: `continue_run` needs a previous step on the same scenario"
                    )
        return self

    @classmethod
    def load(cls, path: str | Path) -> DemoScript:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(from_file_data(data))

    @property
    def marks_s(self) -> tuple[int, ...]:
        return tuple(s.mark_s for s in self.steps)

    @property
    def end_s(self) -> int | None:
        """When the last shot ends (the video length), or None when ``end`` is unset."""
        return None if self.end is None else parse_mark(self.end)


__all__ = [
    "ACTIONS",
    "SPEED_FACTOR",
    "ActionName",
    "DemoScript",
    "DemoStep",
    "format_mark",
    "parse_mark",
    "pending_note",
]
