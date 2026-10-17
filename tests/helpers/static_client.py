"""A static model client for the eval's LLM-planner tests (WO-051).

``capture_planner_inputs`` runs the soundings policy once with ``planner="llm"`` and an
empty ``FixtureStore``, spying on ``ModelClient._replay`` (as WO-033's adversarial test
does) to record the real ``probe_planner`` inputs per contact. ``install_static_reply``
then saves a hand-written output under the key those inputs compute, in a ``tmp_path``
store, so the next run reaches it through ``fixture_store=`` with no network and no
recorded fixture. The hand-written outputs live in ``tests/fixtures/eval_static/``,
outside ``model_calls/``, so WO-032's key-consistency test never reads them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from soundings.eval.soundings_policy import NO_CALL_REASONS, SoundingsPolicy
from soundings.llm.client import FixtureStore, ModelClient
from soundings.schemas import Scenario

STATIC_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "eval_static"
VALID_CAMERA_FIRST = STATIC_DIR / "eval_valid_camera_first.json"
VIOLATING_RUNG = STATIC_DIR / "eval_violating_rung.json"
PROBE_PLANNER = "probe_planner"


def load_static(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def capture_planner_inputs(
    monkeypatch: pytest.MonkeyPatch, scenario: Scenario, seed: int, tmp: Path
) -> list[tuple[str | None, dict[str, Any]]]:
    """Every ``probe_planner`` input of one LLM-arm run, with the contact it is about.

    The inputs carry no contact id, so the i-th model call is paired with the i-th planner
    choice that made a call (a spent budget makes none)."""
    captured: list[dict[str, Any]] = []
    original = ModelClient._replay

    def spy(self: ModelClient, role: str, inputs: Any) -> Any:
        if role == PROBE_PLANNER and isinstance(inputs, Mapping):
            captured.append(dict(inputs))
        return original(self, role, inputs)

    with monkeypatch.context() as m:
        m.setattr(ModelClient, "_replay", spy)
        result = SoundingsPolicy(
            planner="llm",
            fixture_store=FixtureStore(tmp / "capture-empty"),
            violations_log=tmp / "capture-violations.md",
        ).run_result(scenario, seed)
    called = [
        c.contact_id
        for c in result.planner_choices
        if c.choice.fallback_reason not in NO_CALL_REASONS
    ]
    assert len(called) == len(captured), (len(called), len(captured))
    return list(zip(called, captured, strict=True))


def install_static_reply(store: FixtureStore, inputs: Mapping[str, Any], reply: Path) -> None:
    """Save the hand-written ``reply`` under the key ``inputs`` compute."""
    store.save(PROBE_PLANNER, dict(inputs), load_static(reply))
