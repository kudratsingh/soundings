"""Scenario files under ``scenarios/``.

The catalog is the glob ``scenarios/S[0-9][0-9]-*.yaml`` (sorted by path), so
``scenarios/smoke.yaml`` and ``scenarios/live/`` are never part of it. Resolving a
reference (``smoke``, a catalog id, or a path) into a validated ``Scenario`` is added
once ``soundings.schemas.scenario.load_scenario`` exists.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "scenarios"
CATALOG_GLOB = "S[0-9][0-9]-*.yaml"
SMOKE_PATH = SCENARIO_ROOT / "smoke.yaml"


def catalog_paths(root: Path | str = SCENARIO_ROOT) -> list[Path]:
    """Catalog scenario files, sorted by path."""
    return sorted(Path(root).glob(CATALOG_GLOB))
