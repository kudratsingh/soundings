"""WO-060E: quiet the other cases. One focused case on screen; `others · n` opens the rest;
`show all` reveals every event (debugging, hidden in presenter mode).

The pure functions in ``web/src/quiet.ts`` (the cue gate ``web/src/sfx.ts`` applies) run
through ``tests/js/quiet_check.mjs`` on Node >= 22.6 (type stripping; no build, no browser,
no model call); the test skips when node is missing or too old. The static checks pin the
defaults the screen starts with: the others row folded on every run start, the cue gate in
the sound hook, the every-case drawer behind `show all`, and opacity-only motion.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "web" / "src"
CHECK = REPO / "tests" / "js" / "quiet_check.mjs"


def _src(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


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


# ---------------------------------------------------------------- the cases list


def test_default_view_shows_only_the_focused_case(obs: dict[str, Any]) -> None:
    assert obs["focus"] == "c-0002"  # the first case past ask holds the focus
    assert obs["default_shown"] == ["c-0002"]
    assert obs["default_folded_n"] == obs["total"] - 1 == 5
    assert obs["default_others_row"] is True


def test_others_row_counts_and_summarises_by_severity(obs: dict[str, Any]) -> None:
    assert obs["others_label"] == "others · 5"
    assert obs["others_summary"] == "1 handed to a person · 4 cleared at ask"


def test_opening_others_keeps_the_focus_on_top(obs: dict[str, Any]) -> None:
    assert obs["open_shown"][0] == "c-0002"
    assert sorted(obs["open_shown"]) == sorted(obs["show_all_shown"])
    assert len(obs["open_shown"]) == obs["total"]
    assert obs["open_others_row"] is True


def test_show_all_is_the_old_list(obs: dict[str, Any]) -> None:
    assert len(obs["show_all_shown"]) == obs["total"]
    assert obs["show_all_others_row"] is False
    assert obs["no_focus_shown_n"] == obs["total"]  # nothing folds before a focus exists


def test_minimal_focus_sticky_and_pinned(obs: dict[str, Any]) -> None:
    assert obs["focus_before_ask"] == "c-0001"  # nobody past ask yet: the first detected
    assert obs["focus_pinned"] == "c-0005"
    assert obs["focus_pinned_unknown"] == "c-0002"


def test_others_row_starts_folded_on_every_run() -> None:
    app = _src("App.tsx")
    assert "const [othersOpen, setOthersOpen] = useState(false)" in app
    assert "useEffect(() => setOthersOpen(false), [runId])" in app
    assert "const [showAll, setShowAll] = useState(false)" in app


# ---------------------------------------------------------------- sounds


def test_only_the_focused_case_sounds(obs: dict[str, Any]) -> None:
    assert obs["cues_focused_contacts"] == ["c-0002", "c-0002"]
    assert obs["cues_focused_states"] == ["ASKING", "HANDED_OFF"]  # hail ping, handoff chime
    assert set(obs["cues_show_all_contacts"]) == {"c-0001", "c-0002", "c-0003", "c-0006"}
    assert obs["cues_no_focus_n"] == 5
    assert obs["responder_other"] is False
    assert obs["responder_mine"] is True
    assert obs["responder_none"] is False


def test_sound_hook_applies_the_gate_and_keeps_the_twist_run_wide() -> None:
    sfx = _src("sfx.ts")
    assert "export function useSfx(run: RunState, muted: boolean, gate?: CueGate)" in sfx
    assert "soundsForTransitions(cueEntries(fresh, g.focusIds, g.showAll))" in sfx
    assert "cueAllowed(run.responder?.target_contact_id, g.focusIds, g.showAll)" in sfx
    # the twist cue is not gated: it belongs to the run, not to a case
    assert re.search(r"cur\.twist = true\n\s+playSound\(TWIST_SOUND, mutedRef\.current\)", sfx)
    presenter = (WEB / "components" / "PresenterMode.tsx").read_text(encoding="utf-8")
    assert "useSfx(run, muted, cueGate)" in presenter
    gate = "cueGate={{ focusIds: summary ? selectedIds : null, showAll: quietAll }}"
    assert gate in _src("App.tsx")


# ---------------------------------------------------------------- moments, drawers, map


def test_key_moments_are_the_focused_cases(obs: dict[str, Any]) -> None:
    assert obs["moments_cases"] == ["c-0002"]
    assert len(obs["moments_show_all_cases"]) == 6


def test_every_case_drawer_only_with_show_all() -> None:
    view = (WEB / "components" / "CaseView.tsx").read_text(encoding="utf-8")
    assert "{showAll && (" in view
    block = view.split("{showAll && (", 1)[1].split("{/* --- WO-060E end --- */}", 1)[0]
    assert 'data-testid="transitions-drawer"' in block
    assert "useState(false)" in view  # both drawers start collapsed
    assert "const [evOpen, setEvOpen] = useState(false)" in view
    assert "const [trOpen, setTrOpen] = useState(false)" in view


def test_map_quiets_the_other_contacts(obs: dict[str, Any]) -> None:
    assert obs["map_quiet"] == [True, False]
    assert obs["map_quiet_show_all"] is False
    assert obs["map_quiet_no_focus"] is False
    assert obs["dim"] == pytest.approx(0.35)
    hmap = (WEB / "components" / "HarborMap.tsx").read_text(encoding="utf-8")
    assert "{!quiet /* WO-060E: no label outside the focused case */ && (" in hmap
    assert "{lead && !quiet /* WO-060E */ && <Polyline" in hmap


# ---------------------------------------------------------------- presenter, motion


def test_show_all_hidden_in_presenter_mode() -> None:
    app = _src("App.tsx")
    assert "showAll={presenting ? null : showAll}" in app
    assert "const quietAll = showAll && !presenting" in app
    lst = (WEB / "components" / "CaseList.tsx").read_text(encoding="utf-8")
    assert "{showAll !== null && (" in lst


def test_unfold_animates_opacity_only() -> None:
    css = _src("index.css")
    block = css.split("/* --- WO-060E begin", 1)[1].split("/* --- WO-060E end --- */", 1)[0]
    frames = block.split("@keyframes quiet-unfold", 1)[1].split("@media", 1)[0]
    props = set(re.findall(r"([a-z-]+)\s*:", frames))
    assert props == {"opacity"}, props
    assert "height: 32px" in block  # the others row is the case row's height
    assert "transition: height" not in block and "max-height" not in block
