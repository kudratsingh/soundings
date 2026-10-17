"""WO-051: the 'Where the LLM layer added nothing' section from real LLM-planner runs.

No network and no recorded fixtures: the LLM arm replays from a ``tmp_path`` store that is
either empty or holds one hand-written reply installed by ``tests/helpers/static_client``
under the key S04's real ``probe_planner`` inputs compute.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from soundings.eval.harness import run_campaign
from soundings.eval.policies import PolicyName, outcome_for_gt
from soundings.eval.report import NO_LLM_EFFECT_SENTENCE, render_markdown
from soundings.eval.soundings_policy import SoundingsPolicy
from soundings.llm.client import FixtureStore
from soundings.schemas import LadderState
from soundings.sim.scenario import resolve_scenario
from tests.helpers.static_client import (
    VALID_CAMERA_FIRST,
    VIOLATING_RUNG,
    capture_planner_inputs,
    install_static_reply,
)

GT_CONTACT_S04 = "c-0013"
"""S04 seed 1: the first ground-truth observed id the planner is consulted for."""


def _campaign(store: FixtureStore, log: Path, suite: str = "tuesday"):  # type: ignore[no-untyped-def]
    return run_campaign(
        suite,
        (1,),
        policies=(PolicyName.SOUNDINGS,),
        perturb_magnitude=0.5,
        perturb_seed=17,
        include_misspecified=False,
        fixture_store=store,
        violations_log=log,
        workers=1,
    )


@pytest.fixture
def s04_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, object]]:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    captured = capture_planner_inputs(monkeypatch, resolve_scenario("S04"), 1, tmp_path)
    inputs = next(i for cid, i in captured if cid == GT_CONTACT_S04)
    return tmp_path, inputs


def test_no_fixtures_honest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUNDINGS_CI", raising=False)
    log = tmp_path / "violations.md"
    report = _campaign(FixtureStore(tmp_path / "empty"), log)
    md = render_markdown(report)
    diff = report.llm.diff
    assert diff is not None
    assert diff.stats.fixture_hits == 0
    assert diff.stats.calls > 0
    assert diff.stats.fallbacks == diff.stats.calls
    assert diff.differences == ()
    assert NO_LLM_EFFECT_SENTENCE in md
    assert "- differing contacts: 0" in md
    assert not log.exists() or "| " not in log.read_text(encoding="utf-8").split("|---|", 1)[-1]


def test_valid_fixture_diff(s04_store: tuple[Path, dict[str, object]]) -> None:
    tmp, inputs = s04_store
    store = FixtureStore(tmp / "valid")
    install_static_reply(store, inputs, VALID_CAMERA_FIRST)
    report = _campaign(store, tmp / "violations.md")
    diff = report.llm.diff
    assert diff is not None and diff.stats.fixture_hits == 1
    s04 = [d for d in diff.differences if d.scenario_id == "S04"]
    assert len(s04) == 1
    d = s04[0]
    assert "probe_sequence" in d.fields
    assert d.llm_probes[0].value == "camera_cue"
    assert d.fallback_probes != d.llm_probes
    assert d.verdict
    # the terminal state still comes from the stopping rule, not from the reply
    assert d.fallback_terminal is d.llm_terminal
    md = render_markdown(report)
    assert "| S04 | 1 | gt-1 |" in md
    assert NO_LLM_EFFECT_SENTENCE not in md


def test_violating_fixture_rejected(s04_store: tuple[Path, dict[str, object]]) -> None:
    """CLAUDE.md 2.2: a reply that names a rung is refused and logged; the contact's outcome
    equals the fallback run's."""
    tmp, inputs = s04_store
    store = FixtureStore(tmp / "violating")
    install_static_reply(store, inputs, VIOLATING_RUNG)
    log = tmp / "violations.md"
    sc = resolve_scenario("S04")
    llm = SoundingsPolicy(planner="llm", fixture_store=store, violations_log=log).run(sc, 1)
    fb = SoundingsPolicy().run(sc, 1)
    rows = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.startswith("| S04/")]
    assert len(rows) == 1
    assert "probe_planner" in rows[0] and "NAMES_RUNG" in rows[0]
    assert llm.planner_stats is not None and llm.planner_stats.guard_rejections == 1
    assert outcome_for_gt(llm, "gt-1").model_dump() == outcome_for_gt(fb, "gt-1").model_dump()
    assert llm.transition_log == fb.transition_log
    report = _campaign(store, tmp / "campaign-violations.md")
    assert report.llm.diff is not None
    assert report.llm.diff.stats.guard_rejections == 1
    assert report.llm.diff.differences == ()
    assert outcome_for_gt(fb, "gt-1").terminal_state is LadderState.HANDED_OFF
