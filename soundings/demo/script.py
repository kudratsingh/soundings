"""The demo script: ``docs/demo-script.yaml`` as a validated input (WO-062).

Each step has a time mark ``t`` (``m:ss``), either a ``scenario`` (a catalog id such as
``S05``, ``live`` or ``twist``) or ``eval: true``, and a ``say`` line that must pass the
denylist. Unknown keys are rejected. ``DemoScript.load`` checks that the marks are strictly
increasing, that a ``twist`` step names ``scenario_ref`` and ``toggles``, and that a
``continue_run`` step follows a step on the same scenario.

``inject`` is kept as a plain mapping in Part A; Part B types it as WO-061's
``InjectRequest`` and makes it required on ``live``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from soundings.schemas._base import StrictModel, from_file_data
from soundings.schemas.denylist import FreeText
from soundings.schemas.enums import LadderState, ToggleName
from soundings.sim.scenario import UnknownScenarioError, scenario_path

_MARK = re.compile(r"^(\d{1,2}):([0-5]\d)$")
_CATALOG_ID = re.compile(r"^S\d{2}$")
LIVE = "live"
TWIST = "twist"
EVAL = "eval"

NonNegInt = Annotated[int, Field(ge=0)]


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

    @field_validator("t")
    @classmethod
    def _mark(cls, value: str) -> str:
        parse_mark(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> DemoStep:
        if self.eval == (self.scenario is not None):
            raise ValueError("a step has exactly one of `scenario` or `eval: true`")
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
        """``eval``, ``live``, ``twist`` or ``catalog``."""
        if self.eval:
            return EVAL
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
    def label(self) -> str:
        """Transcript label: ``S05``, ``twist S05+network_down``, ``live`` or ``eval``."""
        if self.kind == TWIST:
            return f"twist {self.scenario_ref}+" + "+".join(t.value for t in self.toggles)
        if self.kind == EVAL:
            return EVAL
        return str(self.scenario)


class DemoScript(StrictModel):
    """The whole pitch sequence, steps in time-mark order."""

    steps: Annotated[tuple[DemoStep, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _order(self) -> DemoScript:
        marks = [s.mark_s for s in self.steps]
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


__all__ = ["DemoScript", "DemoStep", "format_mark", "parse_mark"]
