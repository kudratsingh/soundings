"""WO-088 part C: the presenter beats for the responder (a probe dispatched to look)."""

from __future__ import annotations

from pathlib import Path

from soundings.demo.presenter import script_payload
from soundings.demo.script import DemoScript
from soundings.schemas.denylist import find_denylisted

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "demo-script.yaml"
LOOK_CAPTION = (
    "The platform goes to look. It never goes closer than 25 m. The call is still a person's."
)
VO_NOTE = REPO / "docs" / "work-orders" / "WO-062-ADDENDUM-responder-vo.md"


def test_harbor_night_gains_the_look_caption() -> None:
    script = DemoScript.load(SCRIPT)
    night = script.steps[3]
    assert night.scenario == "S05"
    assert night.caption == "Our work stops here. The call is a person's."
    assert night.subcaption == LOOK_CAPTION
    assert "look" in night.say and "25 m standoff" in night.say
    assert sum(1 for s in script.steps if s.subcaption) == 1


def test_tuesday_beat_finds_the_glider_in_the_drift_box() -> None:
    tuesday = DemoScript.load(SCRIPT).steps[2]
    assert "found in the drift box" in tuesday.say


def test_subcaption_reaches_the_presenter_payload() -> None:
    payload = script_payload(DemoScript.load(SCRIPT))
    steps = payload["steps"]
    assert steps[3]["subcaption"] == LOOK_CAPTION
    assert "subcaption" not in steps[0]


def test_subcaption_fits_and_is_clean() -> None:
    assert len(LOOK_CAPTION) <= 160
    assert not find_denylisted(LOOK_CAPTION)
    # The WO-088 vocabulary: look, station, standoff; never chase or follow.
    for word in ("chase", "follow", "close with"):
        assert word not in LOOK_CAPTION.lower()


def test_vo_note_carries_the_line() -> None:
    text = VO_NOTE.read_text(encoding="utf-8")
    assert LOOK_CAPTION in text
    assert "found in the drift box" in text
    assert not find_denylisted(text)
