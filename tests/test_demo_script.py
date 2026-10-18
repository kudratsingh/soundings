"""WO-062 Part A: the demo script is a validated input, not free text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from soundings.demo.schedule import Pace, build_schedule
from soundings.demo.script import (
    ACTIONS,
    ActionName,
    DemoScript,
    DemoStep,
    format_mark,
    parse_mark,
    pending_note,
)
from soundings.schemas.denylist import DENYLIST, find_denylisted
from soundings.schemas.enums import LadderState, ToggleName

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "demo-script.yaml"
# The Part A pitch sequence, frozen when WO-062P replaced docs/demo-script.yaml.
PART_A = REPO / "tests" / "fixtures" / "demo" / "part-a-script.yaml"


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


def test_part_a_script_loads() -> None:
    script = DemoScript.load(PART_A)
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


@pytest.mark.parametrize("path", [SCRIPT, PART_A], ids=["shot-list", "part-a"])
def test_say_lines_verbatim(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    loaded = DemoScript.load(path)
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
    paths = [SCRIPT, PART_A, *sorted((REPO / "soundings" / "demo").glob("*.py"))]
    paths += sorted((REPO / "tests").glob("test_demo_*.py"))
    for path in paths:
        assert not find_denylisted(path.read_text(encoding="utf-8")), path.name
        assert not find_denylisted(path.name), path.name


# --- WO-062P: the 3:00 shot list ---------------------------------------------------------

ADDENDUM = REPO / "docs" / "work-orders" / "WO-062-ADDENDUM-video-sound.md"

# The owner's quoted captions (WO-062 addenda, Directive 1 section 2), shot by shot. Shot 3
# quotes no caption; its caption is the directive's own beat list (checked word for word
# against the directive sentence below).
DIRECTIVE_CAPTIONS: dict[int, str] = {
    0: "A working harbor sees ~40 things underwater on a Tuesday. One of them isn't filed.",
    1: "Today the safe answer is close the berth. Soundings climbs the gentlest rung that "
    "works: watch → ask → slow → hold → hand to a person.",
    3: "Our work stops here. The call is a person's.",
    4: "3 AM, nobody answers: it does only what the watch commander pre-signed.",
    5: "Detection is simulated. The decisions aren't.",
    6: "VTS for the harbor floor.",
}
SHOT3_DIRECTIVE = (
    "Busy Tuesday at 20x. Filed jobs clear at ask; rung ledger counts; a plan is filed from "
    "a phone (QR), conflict → proposed slot → accepted; a vessel transit triggers a slow "
    "advisory near divers. Caption on each beat."
)
DIRECTIVE_STARTS = ("0:00", "0:12", "0:30", "1:00", "1:50", "2:15", "2:40")


def _words(text: str) -> list[str]:
    return re.findall(r"[\w→']+", text.lower())


def test_shot_list_loads_with_presenter_fields() -> None:
    script = DemoScript.load(SCRIPT)
    assert len(script.steps) == 7
    assert tuple(s.t for s in script.steps) == DIRECTIVE_STARTS
    assert [s.kind for s in script.steps] == [
        "catalog",
        "action",
        "catalog",
        "catalog",
        "action",
        "action",
        "action",
    ]
    for step in script.steps:
        assert step.t and step.caption and step.speed in ("1x", "20x"), step.t
        assert step.say
    # WO-062B: Harbor Night (S05) runs at 20x so the hold and the handoff land in the shot.
    assert [s.speed for s in script.steps] == ["1x", "1x", "20x", "20x", "20x", "1x", "1x"]
    assert script.steps[3].scenario == "S05" and script.steps[3].pause_for_click
    assert script.steps[2].beats == (
        ActionName.FILE_PLAN_QR,
        ActionName.PROPOSE_SLOT,
        ActionName.VESSEL_ADVISORY,
    )
    twist = script.steps[4]
    assert twist.action is ActionName.TWIST_TOGGLES and twist.scenario_ref == "S05"
    assert twist.toggles == (ToggleName.NETWORK_DOWN, ToggleName.APPROVER_UNREACHABLE)
    assert script.steps[0].spotlight == "harbor-map"
    assert script.steps[0].expect_state is LadderState.CLEARED_AT_ASK


def test_captions_verbatim_from_the_directive() -> None:
    script = DemoScript.load(SCRIPT)
    for i, caption in DIRECTIVE_CAPTIONS.items():
        assert script.steps[i].caption == caption, i
    shot3 = script.steps[2].caption
    assert shot3 is not None
    # Every word of the shot-3 caption is from the directive's beat sentence, in order.
    source = _words(SHOT3_DIRECTIVE)
    pos = 0
    for word in _words(shot3):
        pos = source.index(word, pos) + 1
    if ADDENDUM.is_file():  # the owner's file (PR #25) once it is on main
        text = ADDENDUM.read_text(encoding="utf-8")
        for caption in DIRECTIVE_CAPTIONS.values():
            assert caption in text, caption
        assert SHOT3_DIRECTIVE in text


def test_captions_fit_two_lines() -> None:
    # 44 px text on a 1920 px bar holds about 80 characters a line.
    for step in DemoScript.load(SCRIPT).steps:
        assert step.caption is not None and len(step.caption) <= 160, step.t


def test_total_duration_170_to_180() -> None:
    script = DemoScript.load(SCRIPT)
    assert script.end == "3:00"
    assert script.end_s is not None and 170 <= script.end_s <= 180
    assert script.marks_s[-1] < script.end_s


def test_part_a_scheduler_on_the_shot_list() -> None:
    script = DemoScript.load(SCRIPT)
    video = build_schedule(script.marks_s, pace=Pace.VIDEO)
    assert 170.0 <= video.total_s <= 180.0
    assert [st.mark_s for st in video.steps] == list(script.marks_s)
    assert [st.at_s for st in video.steps] == [float(m) for m in script.marks_s]
    realtime = build_schedule(script.marks_s, pace=Pace.REALTIME, tail_s=20.0)
    assert realtime.total_s == float(script.end_s or 0)


def test_actions_and_pending_notes() -> None:
    assert set(ACTIONS) == set(ActionName)
    assert pending_note(ActionName.END_CARD) is None
    assert pending_note(ActionName.LADDER_CHIPS) is None
    assert pending_note(ActionName.PROPOSE_SLOT) is None  # WO-082 merged
    script = DemoScript.load(SCRIPT)
    assert script.steps[0].pending == ()
    assert script.steps[5].pending == ()  # WO-086 merged
    assert script.steps[2].pending == ()  # WO-061, WO-082 and WO-083 all merged
    assert pending_note(ActionName.FILE_PLAN_QR) is None
    assert pending_note(ActionName.JUDGE_DROPS_ROGUE) is None


def test_action_step_shapes() -> None:
    ok = DemoStep.model_validate({"t": "0:00", "action": "end_card", "say": "x", "speed": "1x"})
    assert ok.kind == "action" and ok.label == "action end_card" and ok.speed_factor == 1.0
    with pytest.raises(ValidationError, match="exactly one"):
        DemoStep.model_validate(_step(action="end_card"))
    with pytest.raises(ValidationError):
        DemoStep.model_validate({"t": "0:00", "action": "close_the_berth", "say": "x"})
    with pytest.raises(ValidationError, match="twist_toggles"):
        DemoStep.model_validate({"t": "0:00", "action": "twist_toggles", "say": "x"})
    with pytest.raises(ValidationError, match="toggles"):
        DemoStep.model_validate(
            {"t": "0:00", "action": "end_card", "toggles": ("network_down",), "say": "x"}
        )
    with pytest.raises(ValidationError, match="beats"):
        DemoStep.model_validate(
            {"t": "0:00", "action": "end_card", "beats": ("propose_slot",), "say": "x"}
        )
    with pytest.raises(ValidationError):
        DemoStep.model_validate(_step(speed="5x"))
    with pytest.raises(ValidationError, match="spotlight"):
        DemoStep.model_validate(_step(spotlight="#map div"))
    with pytest.raises(ValidationError):
        DemoStep.model_validate(_step(caption=f"We {_denylisted_word()} it."))
    with pytest.raises(ValidationError, match="end"):
        DemoScript.model_validate({"steps": (_step(t="0:10"),), "end": "0:10"})
