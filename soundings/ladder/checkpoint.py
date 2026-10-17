"""One checkpoint per ladder transition, hash-chained per contact (CLAUDE.md section 4).

``hash`` is ``stable_hash`` of every other field, and ``prev_hash`` is the previous
checkpoint's hash (``GENESIS_HASH`` for the first), so a machine can be rebuilt from its
checkpoints and any edit to one breaks the chain. Same pattern as WO-033's investigation
checkpoints.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from soundings.canonical import canonical_json, stable_hash
from soundings.schemas import LadderState, Rung
from soundings.schemas._base import NonNegInt, StrictModel

from .types import GuardDecision, LadderEvent

GENESIS_HASH = "0" * 64


class CheckpointError(ValueError):
    """A checkpoint sequence that does not link, hash, or replay."""


class LadderCheckpoint(StrictModel):
    seq: NonNegInt
    contact_id: str
    tick: NonNegInt
    now_s: float
    from_state: LadderState
    event: LadderEvent
    guard_decisions: tuple[GuardDecision, ...]
    to_state: LadderState
    rung: Rung
    action_keys: tuple[str, ...]
    prev_hash: str
    hash: str

    @staticmethod
    def compute_hash(fields: dict[str, Any]) -> str:
        return stable_hash({k: v for k, v in fields.items() if k != "hash"})

    @classmethod
    def build(cls, **fields: Any) -> LadderCheckpoint:
        """Build a checkpoint and fill in ``hash`` from the other fields."""
        probe = cls.model_validate({**fields, "hash": GENESIS_HASH})
        return probe.model_copy(update={"hash": cls.compute_hash(probe.model_dump(mode="json"))})

    def verify_hash(self) -> bool:
        return self.hash == self.compute_hash(self.model_dump(mode="json"))


def verify_chain(checkpoints: Sequence[LadderCheckpoint]) -> None:
    """Raise ``CheckpointError`` unless seq runs 0..n-1, hashes recompute and links hold."""
    prev = GENESIS_HASH
    for i, cp in enumerate(checkpoints):
        if cp.seq != i:
            raise CheckpointError(f"checkpoint seq {cp.seq} at position {i}")
        if cp.prev_hash != prev:
            raise CheckpointError(f"checkpoint {i}: prev_hash does not link")
        if not cp.verify_hash():
            raise CheckpointError(f"checkpoint {i}: hash does not match its content")
        if i and cp.contact_id != checkpoints[0].contact_id:
            raise CheckpointError(f"checkpoint {i}: another contact")
        if i and cp.from_state is not checkpoints[i - 1].to_state:
            raise CheckpointError(f"checkpoint {i}: from_state does not follow")
        prev = cp.hash


class CheckpointStore(Protocol):
    def append(self, checkpoint: LadderCheckpoint) -> None: ...

    def load(self, contact_id: str | None = None) -> tuple[LadderCheckpoint, ...]: ...


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self._items: list[LadderCheckpoint] = []

    def append(self, checkpoint: LadderCheckpoint) -> None:
        self._items.append(checkpoint)

    def load(self, contact_id: str | None = None) -> tuple[LadderCheckpoint, ...]:
        return tuple(c for c in self._items if contact_id is None or c.contact_id == contact_id)


class JsonlCheckpointStore:
    """Append-only JSONL, one canonical-JSON checkpoint per line (6-dp floats)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, checkpoint: LadderCheckpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(canonical_json(checkpoint) + "\n")

    def load(self, contact_id: str | None = None) -> tuple[LadderCheckpoint, ...]:
        if not self.path.exists():
            return ()
        out: list[LadderCheckpoint] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            cp = LadderCheckpoint.model_validate_json(line)
            if contact_id is None or cp.contact_id == contact_id:
                out.append(cp)
        return tuple(out)
