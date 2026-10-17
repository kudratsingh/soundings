"""WO-043 part B: ``check_expected`` (units) and the catalog verifier over runner v3."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from soundings.eval.catalog_verify import Graded, check_expected, suite_paths, verify_scenario
from soundings.schemas import Hypothesis, LadderState
from soundings.schemas.scenario import Expected

REPO = Path(__file__).resolve().parents[1]
S = LadderState

S05_EXPECTED = Expected(
    terminal_state=S.HANDED_OFF,
    passes_through=(S.HOLDING,),
    min_threat_mass=0.6,
    max_human_interruptions=1,
    max_probes=4,
)
S02_EXPECTED = Expected(
    terminal_state=S.LOST_VEHICLE,
    sitrep_has_search_box=True,
    search_box_contains_truth=True,
    max_human_interruptions=0,
)
CLEAN_S05 = Graded(
    terminal_state=S.HANDED_OFF,
    visited=(S.WATCHING, S.ASKING, S.INVESTIGATING, S.ADVISING_SLOW, S.HOLDING, S.HANDED_OFF),
    threat_mass=0.79,
    top_hypothesis=Hypothesis.UNAUTHORIZED_UUV.value,
    interruptions=(1, 1),
    probes=(2, 3),
)
CLEAN_S02 = Graded(
    terminal_state=S.LOST_VEHICLE, has_search_box=True, box_contains_truth=True, interruptions=(0,)
)


def _keys(g: Graded, exp: Expected) -> set[str]:
    return {f.key for f in check_expected(g, exp)}


def test_check_expected_units() -> None:
    from dataclasses import replace

    assert check_expected(CLEAN_S05, S05_EXPECTED) == []
    assert check_expected(CLEAN_S02, S02_EXPECTED) == []
    cases = [
        (replace(CLEAN_S05, terminal_state=S.RESOLVED_BENIGN), "terminal_state"),
        (replace(CLEAN_S05, visited=(S.WATCHING, S.HANDED_OFF)), "passes_through"),
        (replace(CLEAN_S05, threat_mass=0.5), "min_threat_mass"),
        (replace(CLEAN_S05, interruptions=(2,)), "max_human_interruptions"),
        (replace(CLEAN_S05, probes=(5,)), "max_probes"),
    ]
    for g, key in cases:
        assert _keys(g, S05_EXPECTED) == {key}, key
    out_of_order = Expected(
        terminal_state=S.HANDED_OFF, passes_through=(S.HOLDING, S.ADVISING_SLOW)
    )
    assert _keys(CLEAN_S05, out_of_order) == {"passes_through"}
    assert _keys(
        replace(CLEAN_S02, has_search_box=False, box_contains_truth=None), S02_EXPECTED
    ) == {
        "sitrep_has_search_box",
        "search_box_contains_truth",
    }
    assert _keys(replace(CLEAN_S02, box_contains_truth=False), S02_EXPECTED) == {
        "search_box_contains_truth"
    }
    top = Expected(terminal_state=S.HANDED_OFF, top_hypothesis=Hypothesis.BIOLOGIC)
    assert _keys(CLEAN_S05, top) == {"top_hypothesis"}


S04_REASON = (
    "S04's re-acquired track (c-0009 from tick 366) ends in WO-033's uncertain C_BUDGET "
    "handoff (P(UNFILED_KNOWN_OPERATOR) 0.605, registry confidence 0.57), so the lineage end is "
    "HANDED_OFF with 1 interruption; the primary track resolves benign with 0 "
    "(test_runner_v3::test_s04_*). Same open question as WO-033's strict xfail; the expected "
    "block is not changed."
)
PATHS = [
    pytest.param(p, marks=pytest.mark.xfail(strict=True, reason=S04_REASON), id=p.stem[:3])
    if p.name.startswith("S04")
    else pytest.param(p, id=p.stem[:5] if p.stem.startswith("BT") else p.stem[:3])
    for p in suite_paths()
]


@pytest.mark.parametrize("path", PATHS)
def test_catalog_expected(path: Path, tmp_path: Path) -> None:
    row = verify_scenario(path, db_dir=tmp_path)
    assert row["audit_ok"]
    assert row["failures"] == [], row
    assert row["ok"]


def test_verify_cli_rows() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "soundings.runner", "verify", "--scenario", "S01"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("ok   S01 seed=1 terminal=CLEARED_AT_ASK")


def test_no_model_imports() -> None:
    import ast

    tree = ast.parse(
        (REPO / "soundings" / "eval" / "catalog_verify.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for name in names:
            assert not name.startswith(("soundings.llm", "anthropic", "time")), name
