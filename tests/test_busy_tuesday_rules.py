"""WO-080 rule proofs (CLAUDE.md section 2) for the generator, AIS feed and grading.

Part A: section 2.2 import scans, 2.4 (no clock, no module-level RNG, PYTHONHASHSEED
cmp of the generated YAML) and 2.5 (denylist).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "soundings" / "sim" / "generate.py"
AIS = ROOT / "soundings" / "sim" / "ais.py"
GRADING = ROOT / "soundings" / "sim" / "grading.py"
RUNNER = ROOT / "soundings" / "runner.py"
DENY_FILES = [
    GEN,
    AIS,
    GRADING,
    ROOT / "scenarios" / "BT-01.yaml",
    *sorted((ROOT / "tests").glob("test_busy_tuesday*.py")),
    ROOT / "tests" / "test_vessel_transit.py",
    ROOT / "tests" / "test_generate.py",
    *sorted((ROOT / "tests").glob("test_tick_skipping.py")),
]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


@pytest.mark.parametrize("path", [GEN, AIS, GRADING], ids=lambda p: p.name)
def test_rule_2_2_no_llm_imports(path: Path) -> None:
    assert not any(m.startswith("soundings.llm") for m in _imports(path))


def test_rule_2_2_runner_never_imports_grading() -> None:
    assert "soundings.sim.grading" not in _imports(RUNNER)
    assert not re.search(r"\bsim\.grading\b|\bgrading\.", RUNNER.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [GEN, AIS], ids=lambda p: p.name)
def test_rule_2_4_no_clock_no_global_rng(path: Path) -> None:
    mods = _imports(path)
    assert not {"time", "datetime", "random"} & mods
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"now", "utcnow", "today", "perf_counter", "time_ns"}
            # numpy's legacy global RNG: np.random.<draw>(...) other than the seeded ctors.
            if (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "random"
                and node.attr not in {"default_rng", "Generator", "SeedSequence"}
            ):
                pytest.fail(f"{path.name}: module-level RNG np.random.{node.attr}")


def test_rule_2_4_generator_hashseed_cmp(tmp_path: Path) -> None:
    outs = []
    out = tmp_path / "BT-01.yaml"
    for hs in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": hs}
        subprocess.run(
            [sys.executable, "-m", "soundings.sim.generate", "--seed", "7", "--out", str(out)],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]


def test_rule_2_5_denylist_clean() -> None:
    patterns = [
        re.compile(line.strip(), re.IGNORECASE)
        for line in (ROOT / "config" / "denylist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    hits = []
    for f in DENY_FILES:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), start=1):
            for p in patterns:
                if p.search(line):
                    hits.append(f"{f.name}:{n}")
    assert hits == []


def test_rule_2_1_generator_outputs_only_scenarios() -> None:
    """generate.py and ais.py define no function that sends to a vessel or contact."""
    banned = re.compile(r"\b(send|command|steer|route|instruct|notify|dispatch)\w*", re.I)
    for path in (GEN, AIS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                assert not banned.match(node.name), (path.name, node.name)


# --- part B: the tick-skipping block in runner.py ----------------------------------------


def _skip_block() -> str:
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index("# --- tick skipping (WO-080) ---")
    end = text.index("# --- end tick skipping (WO-080) ---")
    return text[start:end]


def test_rule_2_1_skip_block_only_schedules() -> None:
    """The block defines no sender and assigns no ladder state; it returns ticks only."""
    block = _skip_block()
    tree = ast.parse(block)
    banned = re.compile(r"\b(send|command|steer|route|instruct|notify|dispatch)\w*", re.I)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            assert not banned.match(node.name), node.name
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (isinstance(target, ast.Attribute) and target.attr == "state")
    assert ".advance(" not in block and "check_transition" not in block


def test_rule_2_1_nothing_past_handed_off_reachable() -> None:
    from soundings.runner import ASK_TRANSITIONS
    from soundings.schemas.enums import LadderState

    beyond = {LadderState.WATCH_AFTER_HANDOFF, LadderState.RESOLVED_DEPARTED}
    assert not any(to in beyond for _, _, to in ASK_TRANSITIONS)


def test_rule_2_2_skip_block_reads_no_truth() -> None:
    block = _skip_block()
    tree = ast.parse(block)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            pytest.fail("the skip block imports nothing")
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"truth_paths", "ais_tracks", "contact_truth", "gt_id"}
    for name in ("truth_paths", "ais_tracks", "contact_truth", "grading", "gt_id"):
        assert name not in block, name
    assert "import soundings.llm" not in block and "from soundings.llm" not in block
    runner = RUNNER.read_text(encoding="utf-8")
    assert "ais_tracks" not in runner and "truth_paths" not in runner.split('"""', 2)[2]
