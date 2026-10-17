"""WO-051 CLI tests: seed forms, suite resolution, paid-mode refusal, no network."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import soundings.eval.__main__ as cli
from soundings.eval import harness
from soundings.eval.harness import (
    SeedsError,
    SuiteError,
    default_out,
    parse_seeds,
    resolve_suite,
)
from soundings.sim.scenario import catalog_paths

REPO = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_seed_forms() -> None:
    for text in ("1,2,3,4,5", "1..5", "5,1..4"):
        assert parse_seeds(text) == (1, 2, 3, 4, 5), text
    for bad in ("1..", "a", "0..-1"):
        with pytest.raises(SeedsError):
            parse_seeds(bad)
        result = runner.invoke(cli.app, ["--suite", "catalog", "--seeds", bad, "--mode", "replay"])
        assert result.exit_code == 2, (bad, result.output)


def _write_twist_scenarios(folder: Path, names: list[str]) -> list[Path]:
    folder.mkdir(parents=True)
    out = []
    for src in sorted(catalog_paths()):
        if src.name.split("-")[0] in names:
            dst = folder / src.name
            dst.write_bytes(src.read_bytes())
            out.append(dst)
    return sorted(out)


def test_suite_resolution(tmp_path: Path) -> None:
    cat = resolve_suite("catalog")
    assert len(cat) == 8
    assert cat == sorted(cat)
    assert [p.name.split("-")[0] for p in cat] == [f"S0{i}" for i in range(1, 9)]
    assert default_out("catalog") == Path("artifacts/eval")

    # twist01 from a suite.yaml (paths relative to the twist folder or the repo)
    a = tmp_path / "a"
    local = _write_twist_scenarios(a / "01" / "local", ["S04"])
    (a / "01" / "suite.yaml").write_text(
        yaml.safe_dump(["local/" + local[0].name, "scenarios/S01-filed-survey-on-plan.yaml"]),
        encoding="utf-8",
    )
    got = resolve_suite("twist01", twists_root=a)
    assert got == sorted(got)
    assert {p.name for p in got} == {local[0].name, "S01-filed-survey-on-plan.yaml"}

    # twist01 from a scenarios/ folder
    b = tmp_path / "b"
    made = _write_twist_scenarios(b / "01" / "scenarios", ["S02", "S05"])
    assert resolve_suite("twist01", twists_root=b) == made
    assert default_out("twist01") == Path("artifacts/eval/twist01")

    with pytest.raises(SuiteError):
        resolve_suite("twist02", twists_root=b)
    with pytest.raises(SuiteError):
        resolve_suite("nope")
    result = runner.invoke(cli.app, ["--suite", "nope", "--seeds", "1", "--mode", "replay"])
    assert result.exit_code == 2


def test_twist_cli_default_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_twist_scenarios(tmp_path / "twists" / "01" / "scenarios", ["S01"])
    monkeypatch.setattr(harness, "TWISTS_ROOT", tmp_path / "twists")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_MODE", "replay")
    result = runner.invoke(
        cli.app, ["--suite", "twist01", "--seeds", "1", "--mode", "replay", "--no-misspecified"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "artifacts" / "eval" / "twist01" / "report.md").is_file()
    assert (tmp_path / "artifacts" / "eval" / "twist01" / "report.json").is_file()


@pytest.mark.parametrize("mode", ["live", "record"])
def test_live_mode_refused(mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import soundings.llm.client as client

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a model client must never be constructed")

    monkeypatch.setattr(client.ModelClient, "__init__", boom)
    monkeypatch.setattr(cli, "run_campaign", boom)
    monkeypatch.setenv("MODEL_MODE", "replay")
    out = tmp_path / "out"
    result = runner.invoke(
        cli.app,
        ["--suite", "catalog", "--seeds", "1,2,3,4,5", "--mode", mode, "--out", str(out)],
    )
    assert result.exit_code == 3, result.output
    # 9 catalog contacts (S03 has two) x 5 seeds x llm_calls_per_contact 4
    assert "estimated model calls: 180" in result.output
    assert "eval campaigns are paid runs; needs `approve paid run`" in result.output
    assert not out.exists()


def test_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    twists = tmp_path / "twists"
    (twists / "01").mkdir(parents=True)
    (twists / "01" / "suite.yaml").write_text(
        yaml.safe_dump(
            [
                "scenarios/S01-filed-survey-on-plan.yaml",
                "scenarios/S05-unfiled-closing-fuel-berth.yaml",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(harness, "TWISTS_ROOT", twists)
    monkeypatch.setenv("MODEL_MODE", "replay")

    def no_socket(*_a: object, **_k: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_socket)
    out = tmp_path / "out"
    result = runner.invoke(
        cli.app, ["--suite", "twist01", "--seeds", "1", "--mode", "replay", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "- scenarios: S01, S05" in md


def test_unknown_mode_exits_2() -> None:
    result = runner.invoke(cli.app, ["--suite", "catalog", "--seeds", "1", "--mode", "turbo"])
    assert result.exit_code == 2


def test_makefile_invocation_is_the_documented_one() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert (
        "python -m soundings.eval --suite catalog --seeds 1,2,3,4,5 --mode replay "
        "--out artifacts/eval"
    ) in makefile
