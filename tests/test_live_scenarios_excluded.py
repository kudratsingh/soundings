"""A judge's live scenario file never joins the catalog listing (rehearsal audit 2026-10-17).

``scenarios/live/L-*.yaml`` is written by POST /api/inject with ``expected: {}``; before this
fix it made three catalog-parsing tests fail after any judge Go.
"""

from __future__ import annotations

from pathlib import Path

from soundings.schemas.scenario import scenario_paths


def test_live_files_are_excluded_by_default(tmp_path: Path) -> None:
    (tmp_path / "S01-a.yaml").write_text("id: S01\n", encoding="utf-8")
    (tmp_path / "live").mkdir()
    (tmp_path / "live" / "L-20261017T175153.yaml").write_text("id: L-1\n", encoding="utf-8")
    assert [p.name for p in scenario_paths(tmp_path)] == ["S01-a.yaml"]
    # sorted by full path: root files first, then root/live
    assert [p.name for p in scenario_paths(tmp_path, include_live=True)] == [
        "S01-a.yaml",
        "L-20261017T175153.yaml",
    ]


def test_repo_catalog_listing_has_no_live_files() -> None:
    root = Path(__file__).resolve().parents[1] / "scenarios"
    assert all(p.parent == root for p in scenario_paths(root))
