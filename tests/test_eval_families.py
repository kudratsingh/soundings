"""WO-051 part B1: scenario families and ``--suite <family>``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import soundings.eval.__main__ as cli
from soundings.eval.families import (
    CATALOG_FAMILIES,
    FAMILIES,
    OPTIONAL_IDS,
    FamilyError,
    family_of,
    family_paths,
)
from soundings.eval.harness import SuiteError, default_out, resolve_suite
from soundings.sim.scenario import SCENARIO_ROOT, catalog_paths

runner = CliRunner()


def _ids(paths: list[Path]) -> list[str]:
    return [p.name[:5] if p.name.startswith("TR-") else p.name[:3] for p in paths]


def test_registry_is_the_agreed_one() -> None:
    assert FAMILIES["tuesday"] == ("S01", "S03", "S04", "S07", "S08")
    assert FAMILIES["harbor_night"] == ("S05", "S06")
    assert FAMILIES["lost_vehicle"] == ("S02",)
    assert FAMILIES["busy"] == ("BT-01",)
    assert FAMILIES["traps"] == ("TR-01", "TR-02", "TR-03", "TR-04")
    assert OPTIONAL_IDS == {"BT-01"}


def test_catalog_families_cover_the_catalog_exactly_once() -> None:
    listed = [sid for fam in CATALOG_FAMILIES for sid in FAMILIES[fam]]
    assert len(listed) == len(set(listed))
    assert sorted(listed) == sorted(p.name[:3] for p in catalog_paths())
    assert family_of("S05") == "harbor_night" and family_of("S99") is None


@pytest.mark.parametrize("family", ["tuesday", "harbor_night", "lost_vehicle", "traps"])
def test_family_resolves_sorted_files(family: str) -> None:
    paths = resolve_suite(family)
    assert paths == sorted(paths)
    assert sorted(_ids(paths)) == sorted(FAMILIES[family])
    assert default_out(family) == Path("artifacts/eval") / family


def test_busy_is_skipped_until_bt01_exists(tmp_path: Path) -> None:
    root = tmp_path / "scenarios"
    root.mkdir()
    assert family_paths("busy", scenario_root=root) == []
    (root / "BT-01.yaml").write_text("id: BT-01\n", encoding="utf-8")
    assert family_paths("busy", scenario_root=root) == [root / "BT-01.yaml"]
    if not (SCENARIO_ROOT / "BT-01.yaml").is_file():
        with pytest.raises(SuiteError, match="no scenarios"):
            resolve_suite("busy")


def test_missing_required_id_and_unknown_family_raise(tmp_path: Path) -> None:
    with pytest.raises(FamilyError, match="S05"):
        family_paths("harbor_night", scenario_root=tmp_path)
    with pytest.raises(FamilyError):
        family_paths("weekend")
    with pytest.raises(SuiteError):
        resolve_suite("weekend")


def test_cli_family_suite_over_baselines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_MODE", "replay")
    out = tmp_path / "out"
    result = runner.invoke(
        cli.app,
        [
            "--suite",
            "tuesday",
            "--seeds",
            "1",
            "--mode",
            "replay",
            "--out",
            str(out),
            "--no-misspecified",
            "--stamp",
            "20261017T150000Z",
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "- suite: tuesday" in md
    assert "- scenarios: S01, S03, S04, S07, S08" in md
