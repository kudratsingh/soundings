"""WO-092: the mentor brief is grounded in the repo and cites demo steps that exist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from soundings.schemas.denylist import find_denylisted

REPO = Path(__file__).resolve().parents[1]
BRIEF = REPO / "docs" / "MENTOR-BRIEF.md"
SCRIPT = REPO / "docs" / "demo-script.yaml"

STEP_RE = re.compile(r"\bstep (\d+) \((\d+:\d\d)\)")
PATH_RE = re.compile(r"`((?:config|scenarios|docs|twists|soundings|tests)/[^`\s]+)`")
VENDOR_RE = re.compile(
    r"\b(anthropic|claude|openai|gpt|gemini|llm|model|sonnet|opus|haiku|ibm|bob|retell)\b",
    re.IGNORECASE,
)
PAGE_HEADINGS = ("## Page 1 — William Doyle", "## Page 2 — Dr. Nick Wettels")
CHECKLIST_HEADING = "## What we will record from each session"


def _text() -> str:
    return BRIEF.read_text(encoding="utf-8")


def _section(text: str, start: str, end_prefix: str) -> str:
    i = text.index(start)
    j = text.find(end_prefix, i + len(start))
    return text[i : j if j != -1 else len(text)]


def _pages() -> dict[str, str]:
    text = _text()
    page1 = _section(text, PAGE_HEADINGS[0], "\n## ")
    page2 = _section(text, PAGE_HEADINGS[1], "\n## ")
    return {"doyle": page1, "wettels": page2}


def _steps() -> list[dict[str, object]]:
    data = yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))
    return list(data["steps"])


def test_brief_has_both_pages_and_checklist() -> None:
    text = _text()
    for heading in (*PAGE_HEADINGS, CHECKLIST_HEADING):
        assert heading in text, heading
    assert text.index(PAGE_HEADINGS[0]) < text.index(PAGE_HEADINGS[1])
    assert text.index(PAGE_HEADINGS[1]) < text.index(CHECKLIST_HEADING)
    checklist = text[text.index(CHECKLIST_HEADING) :]
    assert checklist.count("- [ ]") >= 10
    assert "shutdown_usd_per_min_default" in checklist


@pytest.mark.parametrize("who", ["doyle", "wettels"])
def test_page_order_decision_questions_show(who: str) -> None:
    page = _pages()[who]
    parts = ("### Decision we are stuck on", "### Three questions", "### What to show")
    idx = [page.index(p) for p in parts]
    assert idx == sorted(idx), f"{who}: sections out of order"
    decision = _section(page, parts[0], "\n### ")
    assert "current default" in decision and "What changes" in decision
    questions = _section(page, parts[1], "\n### ")
    assert len(re.findall(r"(?m)^\d\. ", questions)) == 3
    show = _section(page, parts[2], "\n## ")
    assert STEP_RE.search(show), f"{who}: no demo step cited"
    assert "Have open:" in show


def test_every_cited_step_exists_with_its_time_mark() -> None:
    steps = _steps()
    cited = STEP_RE.findall(_text())
    assert cited, "the brief cites no demo steps"
    for number, mark in cited:
        n = int(number)
        assert 1 <= n <= len(steps), f"step {n} is not in docs/demo-script.yaml"
        assert steps[n - 1]["t"] == mark, f"step {n} is at {steps[n - 1]['t']}, brief says {mark}"


@pytest.mark.parametrize("who", ["doyle", "wettels"])
def test_no_model_or_vendor_names_in_questions(who: str) -> None:
    page = _pages()[who]
    questions = _section(page, "### Three questions", "\n### ")
    assert not VENDOR_RE.search(questions), VENDOR_RE.findall(questions)


def test_cited_repo_paths_exist() -> None:
    for raw in PATH_RE.findall(_text()):
        path = raw.split(" ")[0].rstrip(".,;:")
        if "*" in path:
            assert list(REPO.glob(path)), f"no file matches {path}"
        else:
            assert (REPO / path).exists(), f"brief cites missing file {path}"


def test_honesty_labels_and_denylist() -> None:
    text = _text()
    lowered = text.lower()
    for phrase in (
        "detection is simulated",
        "stress-test estimate",
        "the work stops at the handoff",
    ):
        assert phrase in lowered, phrase
    assert find_denylisted(text) == []
