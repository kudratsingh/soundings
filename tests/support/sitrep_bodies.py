"""Hand-written ``sitrep_writer`` reply bodies for the S05-shaped final SITREP.

The prose is written by hand; only the evidence row ids are looked up from the inputs
(row ids are content hashes, so a body cannot be written without them). ``good_body``
is a valid reply; ``rung_change_body`` is the same reply claiming ``hold`` while the
machine computed ``hand_to_person``. The other bodies break one guard rule each. The
denylist body builds its word from the pattern at test time, so no denylisted literal is
stored in the repo.
"""

from __future__ import annotations

import re
from typing import Any

from soundings.ladder.sitrep import SitrepInputs
from soundings.schemas.denylist import DENYLIST, find_denylisted
from soundings.schemas.enums import FeatureName, Rung


def _latest_id(inputs: SitrepInputs, feature: FeatureName) -> str:
    rows = [r for r in inputs.contact_rows if r.feature is feature]
    return max(enumerate(rows), key=lambda ir: (ir[1].t, ir[0]))[1].row_id


def good_body(inputs: SitrepInputs) -> dict[str, Any]:
    f = FeatureName
    return {
        "contact_summary": (
            f"Contact {inputs.contact_id} is inside the fuel berths zone near fuel_berth_1, "
            "with no registry match and no answer to the hail"
        ),
        "facts": [
            {
                "text": "No filed plan matches the contact (registry lookup at tick 12)",
                "evidence_ids": [_latest_id(inputs, f.REGISTRY_MATCH)],
            },
            {
                "text": "The acoustic hail at tick 12 got no reply",
                "evidence_ids": [_latest_id(inputs, f.HAIL_REPLY)],
            },
            {
                "text": "First detected inside the fuel berths zone",
                "evidence_ids": [_latest_id(inputs, f.ZONE_ID)],
            },
            {
                "text": "The nearest critical asset is fuel_berth_1",
                "evidence_ids": [_latest_id(inputs, f.NEAREST_ASSET_ID)],
            },
        ],
        "assumptions": [],
        "unknowns": [
            {"text": "missing: propeller_tonal_present", "evidence_ids": []},
            {"text": "missing: depth", "evidence_ids": []},
        ],
        "person_must_decide": [
            "Whether the contact at fuel_berth_1 needs people on scene, and who is told"
        ],
        "recommended_rung": inputs.recommended_rung.value,
    }


def rung_change_body(inputs: SitrepInputs) -> dict[str, Any]:
    body = good_body(inputs)
    body["recommended_rung"] = Rung.HOLD.value
    return body


def bad_citation_body(inputs: SitrepInputs) -> dict[str, Any]:
    body = good_body(inputs)
    body["facts"][0]["evidence_ids"] = ["0123456789abcdef"]
    return body


def uncited_fact_body(inputs: SitrepInputs) -> dict[str, Any]:
    body = good_body(inputs)
    body["facts"][1]["evidence_ids"] = []
    return body


def extra_control_key_body(inputs: SitrepInputs) -> dict[str, Any]:
    body = good_body(inputs)
    body["next_state"] = "HANDED_OFF"
    return body


def probability_in_prose_body(inputs: SitrepInputs) -> dict[str, Any]:
    body = good_body(inputs)
    body["contact_summary"] = "Contact is 70% likely an unauthorized vehicle"
    return body


def denylisted_word() -> str:
    """A denylisted word built from the first pattern (never stored as a literal)."""
    word = re.sub(r"\(.*?\)\?|\\b", "", DENYLIST[0].pattern)
    assert find_denylisted(word) == [word]
    return word


def denylist_body(inputs: SitrepInputs) -> dict[str, Any]:
    body = good_body(inputs)
    body["person_must_decide"] = [f"Whether to {denylisted_word()} the contact"]
    return body
