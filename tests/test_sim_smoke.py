from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from soundings.sim import COLUMNS, format_rows, run_smoke
from soundings.sim.__main__ import main

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_sixty_rows_one_per_tick() -> None:
    rows = run_smoke(seed=7, ticks=60)
    assert len(rows) == 60
    assert [r.tick for r in rows] == list(range(60))
    assert all(r.contact_id == rows[0].contact_id for r in rows)


def test_same_seed_identical() -> None:
    assert run_smoke(7, 60) == run_smoke(7, 60)
    assert format_rows(run_smoke(7, 60)) == format_rows(run_smoke(7, 60))


def test_different_seed_differs() -> None:
    assert run_smoke(7, 60) != run_smoke(8, 60)


def test_floats_rounded_to_six_dp() -> None:
    for r in run_smoke(7, 60):
        for v in (r.lat, r.lon, r.depth_m, r.speed_mps):
            assert v == round(v, 6)


def test_walk_is_slow_and_bounded() -> None:
    rows = run_smoke(3, 60)
    for r in rows:
        assert 0.2 <= r.speed_mps <= 2.5
        assert 2.0 <= r.depth_m <= 30.0
    # 60 ticks at <= 2.5 m/s cannot move more than ~0.0015 deg of latitude.
    assert abs(rows[-1].lat - rows[0].lat) < 0.002


def test_format_has_header_and_rows() -> None:
    lines = format_rows(run_smoke(7, 5)).splitlines()
    assert lines[0].split("\t") == list(COLUMNS)
    assert len(lines) == 6


def test_unknown_scenario_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seed", "1", "--scenario", "nope"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_seed_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


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
    assert len(lines) == 61  # header + 60 data rows
    assert a.stdout.decode() == format_rows(run_smoke(7, 60))


def test_cli_unknown_scenario_exit_code() -> None:
    res = _cli("--seed", "7", "--scenario", "nope")
    assert res.returncode == 2
    assert b"unknown scenario" in res.stderr
