"""Canonical JSON and content hashing, shared by every module that hashes (CLAUDE.md 2.4).

``canonical_json`` is the single serializer used for fixture keys, track hashes, evidence
row ids and table hashes: sorted keys, compact separators, ASCII only, floats rounded to
``FLOAT_DECIMALS`` (6) with ``-0.0`` collapsed to ``0.0``, and NaN or infinity rejected.
Pydantic models are dumped with ``model_dump(mode="json")`` and enums by value, so a model
and its JSON round trip hash identically.
"""

from __future__ import annotations

import enum
import hashlib
import json
from typing import Any

from pydantic import BaseModel

FLOAT_DECIMALS = 6


def round6(x: float) -> float:
    """Round to ``FLOAT_DECIMALS`` places; ``-0.0`` becomes ``0.0``. NaN or infinity raise."""
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("round6 does not accept NaN or infinity")
    rounded = round(float(x), FLOAT_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded


def _normalize(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _normalize(obj.model_dump(mode="json"))
    if isinstance(obj, enum.Enum):
        return _normalize(obj.value)
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError("canonical_json does not accept NaN or infinity")
        rounded = round(obj, FLOAT_DECIMALS)
        return 0.0 if rounded == 0.0 else rounded  # collapse -0.0
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, enum.Enum):
                k = k.value
            if not isinstance(k, str):
                raise TypeError(f"canonical_json requires str keys, got {type(k).__name__}")
            out[k] = _normalize(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    raise TypeError(f"canonical_json cannot encode {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, compact separators, floats rounded to 6 dp, ASCII only."""
    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def stable_hash(obj: Any) -> str:
    """``sha256(canonical_json(obj))`` as lowercase hex."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
