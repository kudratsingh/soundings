"""The one probe idempotency key (docs/SPEC-INVESTIGATION.md section 5).

``idempotency_key = hash(contact_id, probe_id, args, tick_bucket)``. Every probe uses this
module; WO-031 re-exports it and no other module defines a second formula. A tick bucket
is ``TICK_BUCKET_S`` seconds of sim time, so repeating a probe within the same 30 s window
returns the cached result instead of running again (CLAUDE.md 2.4).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any

from soundings.canonical import canonical_json

TICK_BUCKET_S = 30
"""Width of one idempotency bucket in sim seconds."""


def tick_bucket(tick: int, tick_s: float) -> int:
    """``(tick * tick_s) // TICK_BUCKET_S`` as an int.

    ``tick_bucket(14, 2) == 0`` and ``tick_bucket(15, 2) == 1``.
    """
    if tick < 0:
        raise ValueError("tick must be >= 0")
    if tick_s <= 0:
        raise ValueError("tick_s must be > 0")
    return int(math.floor(round(tick * tick_s, 9) / TICK_BUCKET_S))


def idempotency_key(
    contact_id: str, probe_id: str, args: Mapping[str, Any] | None, bucket: int
) -> str:
    """sha256 hex of ``canonical_json([contact_id, probe_id, args or {}, bucket])``."""
    payload = [str(contact_id), str(probe_id), dict(args or {}), int(bucket)]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
