"""WO-062 Part A: the demo script is a validated input, not free text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from soundings.demo.script import DemoScript, DemoStep, format_mark, parse_mark
from soundings.schemas.denylist import DENYLIST, find_denylisted
from soundings.schemas.enums import LadderState, ToggleName

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "demo-script.yaml"


def _denylisted_word() -> str:
    """A word the denylist rejects, derived from config/denylist.txt at run time."""
    for pattern in DENYLIST:
        text = re.sub(r"\\b", "", pattern.pattern)
        text = re.sub(r"\([^)]*\)\?", "", text)
        text = re.sub(r"\[(\w)\w*\]", r"\1", text)
        if re.fullmatch(r"[a-z ]+", text) and find_denylisted(text):
            return text
    raise AssertionError("no plain-word pattern in the denylist")


def _step(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"t": "0:00", "scenario": "S01", "say": "Filed survey."}
    base.update(kw)
    return base


def test_real_script_loads() -> None:
    script = DemoScript.load(SCRIPT)
    assert len(script.steps) == 7
    assert [s.kind for s in script.steps] == [
        "catalog",
        "catalog",
        "catalog",
        "catalog",
        "live",
        "twist",
        "eval",
    ]
    assert script.marks_s == (0, 30, 60, 120, 165, 210, 240)
    twist = script.steps[5]
    assert twist.scenario_ref == "S05" and twist.toggles == (ToggleName.NETWORK_DOWN,)
    assert script.steps[3].continue_run and script.steps[3].approve
    assert script.steps[0].expect_state is LadderState.CLEARED_AT_ASK


def test_say_lines_verbatim() -> None:
    raw = yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))
    loaded = DemoScript.load(SCRIPT)
    assert [s["say"] for s in raw["steps"]] == [s.say for s in loaded.steps]


@pytest.mark.parametrize(
    ("text", "secs"), [("0:00", 0), ("0:30", 30), ("2:45", 165), ("10:05", 605)]
)
def test_parse_mark(text: str, secs: int) -> None:
    assert parse_mark(text) == secs
    assert format_mark(secs) == text


@pytest.mark.parametrize("text", ["", "1:5", "1:60", "-0:10", "1:00:00", "a:bc", "90"])
def test_parse_mark_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_mark(text)


def test_marks_strictly_increasing() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        DemoScript.model_validate({"steps": (_step(t="0:30"), _step(t="0:30"))})
    with pytest.raises(ValidationError, match="strictly increasing"):
        DemoScript.model_validate({"steps": (_step(t="1:00"), _step(t="0:30"))})


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        DemoStep.model_validate(_step(colour="red"))


def test_unknown_scenario_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown scenario"):
        DemoStep.model_validate(_step(scenario="S99"))
    with pytest.raises(ValidationError, match="catalog id"):
        DemoStep.model_validate(_step(scenario="smoke"))


def test_twist_needs_toggles_and_ref() -> None:
    with pytest.raises(ValidationError, match="toggles"):
        DemoStep.model_validate(_step(scenario="twist", scenario_ref="S05"))
    with pytest.raises(ValidationError, match="scenario_ref"):
        DemoStep.model_validate(_step(scenario="twist", toggles=("network_down",)))
    with pytest.raises(ValidationError):
        DemoStep.model_validate(_step(scenario="twist", scenario_ref="S05", toggles=("nope",)))


def test_scenario_xor_eval() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        DemoStep.model_validate(_step(eval=True))
    with pytest.raises(ValidationError, match="exactly one"):
        DemoStep.model_validate({"t": "0:00", "say": "x"})
    assert DemoStep.model_validate({"t": "0:00", "eval": True, "say": "x"}).kind == "eval"


def test_continue_run_needs_same_scenario() -> None:
    with pytest.raises(ValidationError, match="continue_run"):
        DemoScript.model_validate({"steps": (_step(continue_run=True),)})
    with pytest.raises(ValidationError, match="continue_run"):
        DemoScript.model_validate(
            {"steps": (_step(), _step(t="0:10", scenario="S02", continue_run=True))}
        )
    ok = DemoScript.model_validate({"steps": (_step(), _step(t="0:10", continue_run=True))})
    assert ok.steps[1].continue_run


def test_denylisted_say_rejected() -> None:
    word = _denylisted_word()
    with pytest.raises(ValidationError):
        DemoStep.model_validate(_step(say=f"We {word} it."))


def test_denylist_grep_clean() -> None:
    paths = [SCRIPT, *sorted((REPO / "soundings" / "demo").glob("*.py"))]
    paths += sorted((REPO / "tests").glob("test_demo_*.py"))
    for path in paths:
        assert not find_denylisted(path.read_text(encoding="utf-8")), path.name
        assert not find_denylisted(path.name), path.name
