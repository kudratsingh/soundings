"""Rule proofs for soundings/sim (CLAUDE.md sections 2.4 and 2.5).

(a) S05 runs to completion with ``time.time``, ``time.perf_counter`` and
``datetime.datetime.now`` patched to raise. (b) An AST scan finds no global-RNG use and
no module-level generator in the sim, and no wall-clock call. (c) No behavior name,
toggle name, or string in the sim's YAML files matches config/denylist.txt.
"""

from __future__ import annotations

import ast
import datetime
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from soundings.schemas.enums import BehaviorType, ToggleName
from soundings.sim.behaviors import BEHAVIOR_TYPES
from soundings.sim.hashing import track_hash
from soundings.sim.run import run_scenario
from soundings.sim.scenario import resolve_scenario

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_DIR = REPO_ROOT / "soundings" / "sim"
DENYLIST = [
    re.compile(line, re.IGNORECASE)
    for line in (REPO_ROOT / "config" / "denylist.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
SIM_YAML = [
    REPO_ROOT / "config" / "sim.yaml",
    REPO_ROOT / "config" / "current_field.yaml",
    *sorted((REPO_ROOT / "scenarios").glob("*.yaml")),
]

RANDOM_FUNCS = {
    "random",
    "randint",
    "randrange",
    "choice",
    "choices",
    "shuffle",
    "sample",
    "uniform",
    "gauss",
    "normalvariate",
    "seed",
}
WALL_CLOCK = {
    ("time", "time"),
    ("time", "perf_counter"),
    ("time", "monotonic"),
    ("datetime", "now"),
}


class _NoWallClock(datetime.datetime):
    @classmethod
    def now(cls, tz: datetime.tzinfo | None = None) -> _NoWallClock:
        raise AssertionError("datetime.now called in the sim")


def _raise(*_: object) -> float:
    raise AssertionError("wall clock read in the sim")


def test_a_s05_runs_with_wall_clock_patched_to_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = resolve_scenario("S05")
    expected = track_hash(run_scenario(scenario))
    with monkeypatch.context() as m:
        m.setattr(time, "time", _raise)
        m.setattr(time, "perf_counter", _raise)
        m.setattr(datetime, "datetime", _NoWallClock)
        with pytest.raises(AssertionError):
            time.time()
        with pytest.raises(AssertionError):
            datetime.datetime.now()
        run = run_scenario(scenario)
        got = track_hash(run)
    assert run.n_ticks == scenario.duration_ticks
    assert got == expected


def _sim_modules() -> list[Path]:
    return sorted(SIM_DIR.glob("*.py"))


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _calls(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield _dotted(node.func)


@pytest.mark.parametrize("path", _sim_modules(), ids=lambda p: p.name)
def test_no_global_rng_or_wall_clock(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _calls(tree):
        parts = name.split(".")
        # np.random.<dist>(...) / numpy.random.<dist>(...) except the generator factories.
        if len(parts) == 3 and parts[0] in {"np", "numpy"} and parts[1] == "random":
            assert parts[2] in {"default_rng", "SeedSequence", "Generator", "PCG64"}, name
        # random.<fn>(...) from the stdlib module.
        if len(parts) == 2 and parts[0] == "random":
            assert parts[1] not in RANDOM_FUNCS, name
        if len(parts) >= 2:
            assert (parts[-2], parts[-1]) not in WALL_CLOCK, name


@pytest.mark.parametrize("path", _sim_modules(), ids=lambda p: p.name)
def test_no_module_level_rng(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign | ast.AnnAssign) and stmt.value is not None:
            for name in _calls(stmt.value):
                assert not name.endswith(("default_rng", "Random", "SeedSequence")), name


def _yaml_strings(obj: object) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _yaml_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _yaml_strings(item)


def _clean(text: str) -> bool:
    return not any(p.search(text) for p in DENYLIST)


def test_denylist_compiled() -> None:
    assert len(DENYLIST) >= 10


def test_behavior_names_clean() -> None:
    assert all(_clean(name) for name in BEHAVIOR_TYPES)
    assert all(_clean(b.value) for b in BehaviorType)


def test_toggle_names_clean() -> None:
    assert len(ToggleName) == 9
    assert all(_clean(t.value) and _clean(t.name) for t in ToggleName)


@pytest.mark.parametrize("path", SIM_YAML, ids=lambda p: p.name)
def test_yaml_strings_and_comments_clean(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert _clean(text), path.name  # covers comments too
    for s in _yaml_strings(yaml.safe_load(text)):
        assert _clean(s), (path.name, s)


@pytest.mark.parametrize("path", _sim_modules(), ids=lambda p: p.name)
def test_sim_source_clean(path: Path) -> None:
    assert _clean(path.read_text(encoding="utf-8")), path.name
