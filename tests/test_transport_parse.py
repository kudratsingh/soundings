"""The Anthropic transports parse one JSON object from a reply, fence or prose included.

A reply wrapped in a Markdown code fence, or prefixed with a sentence, must not abort the
owner's recorded fixture run after one call. The parser is robustness only: the object it
returns still goes through the role guard like any other reply.
"""

from __future__ import annotations

import pytest

from soundings.llm.client import parse_model_json

OBJ = '{"probe": "hail", "reason": "closest cheap probe"}'


def test_raw_json_object() -> None:
    assert parse_model_json(OBJ, "probe_planner") == {
        "probe": "hail",
        "reason": "closest cheap probe",
    }


@pytest.mark.parametrize(
    "text",
    [
        f"```json\n{OBJ}\n```",
        f"```\n{OBJ}\n```",
        f"  ```json\n{OBJ}\n```  \n",
        f"Here is the plan:\n{OBJ}",
        f"{OBJ}\n\nLet me know if you want changes.",
        f"Sure.\n```json\n{OBJ}\n```\nDone.",
    ],
)
def test_fenced_or_prose_wrapped_object(text: str) -> None:
    assert parse_model_json(text, "probe_planner")["probe"] == "hail"


def test_nested_braces_inside_strings_and_objects() -> None:
    text = '```json\n{"a": {"b": "}"}, "c": [1, {"d": 2}]}\n```'
    assert parse_model_json(text, "sitrep_writer") == {"a": {"b": "}"}, "c": [1, {"d": 2}]}


def test_json_array_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_model_json("[1, 2, 3]", "probe_planner")


@pytest.mark.parametrize("text", ["", "no json here", "```json\nnot json\n```", "{broken"])
def test_non_json_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="probe_planner"):
        parse_model_json(text, "probe_planner")
