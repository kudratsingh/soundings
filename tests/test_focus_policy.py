"""WO-060D: the focus policy (web/src/focus.ts) over a synthetic stream, run through node.

The pure functions are exercised by ``tests/js/focus_check.mjs`` (Node >= 22.6 strips the
TypeScript types; no build, no browser, no model call). The test skips when node is missing
or too old; the web CI job runs the same script on Node 22.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from soundings.demo.script import DemoStep

REPO = Path(__file__).resolve().parents[1]
CHECK = REPO / "tests" / "js" / "focus_check.mjs"


@pytest.fixture(scope="module")
def obs() -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", str(CHECK)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 and "strip-types" in proc.stderr:
        pytest.skip(f"node cannot strip TypeScript types: {proc.stderr.strip()[:200]}")
    assert proc.returncode == 0, proc.stderr
    return dict(json.loads(proc.stdout))


def test_sticky_starts_on_the_first_detected_case(obs: dict[str, Any]) -> None:
    assert obs["before_escalation"] == "c-0001"


def test_first_escalation_picks_the_first_case_past_ask(obs: dict[str, Any]) -> None:
    assert obs["first_escalation"] == "c-0002"
    assert obs["after_escalation"] == "c-0002"


def test_sticky_holds_through_a_more_severe_later_case(obs: dict[str, Any]) -> None:
    assert obs["more_severe_sticky"] == "c-0002"
    assert obs["hero_holding"] == "c-0002"
    assert obs["hero_handed_off"] == "c-0002"
    assert obs["reacquired_focus"] == "c-0002"  # a re-acquired id stays in the hero's case


def test_a_benign_side_story_does_not_hold_the_focus(obs: dict[str, Any]) -> None:
    # S05's shape: an investigation that opens and clears benign before the hero is asked.
    assert obs["side_story_open"] == "c-0101"
    assert obs["side_story_cleared"] == "c-0101"  # first detected until someone else escalates
    assert obs["hero_after_side_story"] == "c-0102"
    # A re-acquired id of the side story that escalates later never steals the focus.
    assert obs["side_story_reacquired"] == "c-0102"


def test_auto_behaves_as_before(obs: dict[str, Any]) -> None:
    assert obs["more_severe_auto"] == "c-0003"


def test_pinned_holds_and_sticky_returns(obs: dict[str, Any]) -> None:
    assert obs["pinned_mode"] == "pinned"
    assert obs["pinned_holds"] == "c-0001"
    assert obs["back_to_sticky"] == "c-0002"
    assert obs["mode_cycle"] == ["sticky", "auto", "sticky"]
    assert obs["reset"] == {"mode": "sticky", "pinned": None, "policy": "first_escalation"}


def test_presenter_policies(obs: dict[str, Any]) -> None:
    assert obs["policy_first_detected"] == "c-0001"
    assert obs["policy_contact"] == "c-0003"
    assert obs["policy_unknown_contact"] == "c-0002"  # falls back to first_escalation


def test_key_moments_are_the_focused_case_in_order(obs: dict[str, Any]) -> None:
    assert obs["moments"] == [
        "c-0002:detected",
        "c-0002:hail sent",
        "c-0002:escalated to investigation",
        "c-0002:hold placed",
        "c-0002:handed off",
    ]
    assert obs["others"].startswith("others · ")
    assert "1 cleared at ask" in obs["others"] and "1 holding" in obs["others"]


def test_trace_line_from_start_to_finish(obs: dict[str, Any]) -> None:
    assert obs["trace"] == (
        "DETECTED t 2 s → WATCHING → ASKING t 10 s → INVESTIGATING t 16 s"
        " → HOLDING t 40 s → HANDED_OFF t 60 s"
    )
    assert "⟲ c-0005 DETECTED t 100 s" in obs["reacquired_trace"]
    assert obs["entered_ask"].startswith("WATCHING → ASKING")
    assert obs["entered_handoff"].startswith("HOLDING → HANDED_OFF")
    assert obs["entered_detected"] == "first fix"


@pytest.mark.parametrize("value", ["first_escalation", "first_detected", "c-0001"])
def test_demo_step_accepts_a_focus(value: str) -> None:
    step = DemoStep.model_validate({"t": "0:00", "scenario": "S05", "say": "x", "focus": value})
    assert step.focus == value


def test_demo_step_rejects_an_unknown_focus() -> None:
    with pytest.raises(ValueError, match="focus"):
        DemoStep.model_validate({"t": "0:00", "scenario": "S05", "say": "x", "focus": "everyone"})
