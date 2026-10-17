"""``track_hash``: one content hash per run, over what the sim reports (CLAUDE.md 2.4).

The hash covers the observed tracks, the detections, and the observed-to-truth map,
each sorted by observed id (detections by tick, then observed id), serialized with
``soundings.canonical.canonical_json`` (sorted keys, floats at 6 dp). Two runs with the
same scenario and seed give the same hex digest, byte for byte, on any machine.
"""

from __future__ import annotations

from typing import Any

from soundings.canonical import stable_hash
from soundings.sim.types import SimRun


def track_hash_payload(run: SimRun) -> dict[str, Any]:
    """The exact structure ``track_hash`` hashes."""
    return {
        "scenario_id": run.scenario_id,
        "seed": run.seed,
        "tick_s": run.tick_s,
        "n_ticks": run.n_ticks,
        "tracks": [run.tracks[cid] for cid in sorted(run.tracks)],
        "detections": sorted(run.detections, key=lambda d: (d.tick, d.observed_contact_id)),
        "contact_truth": {cid: run.contact_truth[cid] for cid in sorted(run.contact_truth)},
    }


def track_hash(run: SimRun) -> str:
    """``stable_hash`` (sha256 hex) of ``track_hash_payload(run)``."""
    return stable_hash(track_hash_payload(run))
