"""The Phase 0 smoke exit test after the rewire onto ``scenarios/smoke.yaml``.

``python -m soundings.sim --seed 7 --ticks 60 --scenario smoke`` prints 60 tick lines
plus ``track_hash=<hex>``, exits 0, and is byte-identical across runs; an unknown
scenario exits 2.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from soundings.sim.__main__ import main
from soundings.sim.hashing import track_hash
from soundings.sim.run import format_run
from soundings.sim.smoke import SMOKE_NODE_ID, run_smoke, smoke_harbor

REPO_ROOT = Path(__file__).resolve().parent.parent
HASH_LINE = re.compile(r"^track_hash=[0-9a-f]{64}$")


def _cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "soundings.sim", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )


def test_cli_byte_identical_across_runs() -> None:
    args = ("--seed", "7", "--ticks", "60", "--scenario", "smoke")
    a, b = _cli(*args), _cli(*args)
    assert a.returncode == 0 and b.returncode == 0, a.stderr
    assert a.stdout == b.stdout
    lines = a.stdout.decode().splitlines()
    assert len(lines) == 61  # 60 tick lines + the hash line
    assert [int(line.split("\t")[0]) for line in lines[:60]] == list(range(60))
    assert HASH_LINE.match(lines[-1])
    run = run_smoke(7, 60)
    assert a.stdout.decode() == format_run(run, track_hash(run))


def test_cli_unknown_scenario_exit_code() -> None:
    res = _cli("--seed", "7", "--scenario", "nope")
    assert res.returncode == 2
    assert b"unknown scenario" in res.stderr


def test_unknown_scenario_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seed", "1", "--scenario", "nope"]) == 2
    assert "unknown scenario" in capsys.readouterr().err
    assert main(["--seed", "1", "--scenario", "S99"]) == 2


def test_seed_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_negative_ticks_exit_2() -> None:
    assert main(["--seed", "1", "--ticks", "-1"]) == 2


def test_smoke_run_shape() -> None:
    run = run_smoke(7, 60)
    assert run.scenario_id == "smoke" and run.n_ticks == 60 and run.seed == 7
    assert {d.node_id for d in run.detections} == {SMOKE_NODE_ID}
    assert not any(d.is_false_alarm for d in run.detections)
    assert {t.gt_id for t in run.contact_truth.values()} == {"gt-1", "gt-2"}
    assert [n.node_id for n in smoke_harbor().sensor_nodes] == [SMOKE_NODE_ID]


def test_same_seed_identical_different_seed_differs() -> None:
    assert track_hash(run_smoke(7, 60)) == track_hash(run_smoke(7, 60))
    assert track_hash(run_smoke(7, 60)) != track_hash(run_smoke(8, 60))


def test_cli_accepts_catalog_id_and_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seed", "3", "--ticks", "5", "--scenario", "S04"]) == 0
    by_id = capsys.readouterr().out
    path = REPO_ROOT / "scenarios" / "S04-unfiled-slow-contact-benign.yaml"
    assert main(["--seed", "3", "--ticks", "5", "--scenario", str(path)]) == 0
    assert capsys.readouterr().out == by_id
    assert len(by_id.splitlines()) == 6
