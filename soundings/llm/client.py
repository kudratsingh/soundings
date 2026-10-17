"""The single gate for model calls: mode switch plus a content-addressed fixture store.

Binding rules (CLAUDE.md):

* Section 2.3: no paid runs without the owner's explicit approval. The default mode is
  ``replay``; CI pins ``MODEL_MODE=replay`` and sets ``SOUNDINGS_CI``, under which a
  replay miss raises instead of silently falling back. ``record`` and ``live`` (WO-032)
  refuse under ``SOUNDINGS_CI`` and need an explicitly injected ``transport``; with no
  transport they raise ``NotImplementedError``, so no default code path can reach a
  hosted model. The later owner-approved fixture run passes ``anthropic_transport``.
* Section 2.4: determinism. Every call is keyed by
  ``sha256(role + canonical_json(inputs))`` and replayed from
  ``tests/fixtures/model_calls/<role>/<key>.json``. Floats are rounded to 6 dp before
  hashing so float noise cannot change a key.

The model name comes only from the ``MODEL_NAME`` environment variable. The vendor SDK
is imported lazily, inside the live path, never at module import time.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Protocol

from soundings.canonical import FLOAT_DECIMALS, _normalize, canonical_json

log = logging.getLogger(__name__)

__all__ = [
    "FLOAT_DECIMALS",
    "FixtureStore",
    "LiveCallRefused",
    "ModelClient",
    "ModelMode",
    "ModelOutput",
    "ReplayMiss",
    "Transport",
    "anthropic_transport",
    "canonical_json",
    "fixture_key",
    "default_fixture_root",
    "in_ci",
    "model_name_from_env",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "model_calls"
FIXTURE_ROOT_ENV = "SOUNDINGS_FIXTURE_ROOT"
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_LIVE_MAX_TOKENS = 2048

ModelOutput = dict[str, Any]


class ModelMode(enum.StrEnum):
    REPLAY = "replay"
    RECORD = "record"
    LIVE = "live"

    @classmethod
    def from_env(cls) -> ModelMode:
        """Read ``MODEL_MODE`` (default ``replay``). An unknown value raises ``ValueError``."""
        raw = os.environ.get("MODEL_MODE", cls.REPLAY.value).strip().lower()
        try:
            return cls(raw)
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"MODEL_MODE={raw!r} is not one of: {allowed}") from None


def model_name_from_env() -> str | None:
    """The model name, from ``MODEL_NAME`` only. ``None`` when unset."""
    name = os.environ.get("MODEL_NAME", "").strip()
    return name or None


def in_ci() -> bool:
    """True when ``SOUNDINGS_CI`` is set to anything other than empty or ``0``."""
    return os.environ.get("SOUNDINGS_CI", "").strip() not in ("", "0")


def default_fixture_root() -> Path:
    """``SOUNDINGS_FIXTURE_ROOT`` when set (non-empty), else ``DEFAULT_FIXTURE_ROOT``."""
    override = os.environ.get(FIXTURE_ROOT_ENV, "").strip()
    return Path(override) if override else DEFAULT_FIXTURE_ROOT


class LiveCallRefused(RuntimeError):
    """A ``record`` or ``live`` call was refused (CI, or no model name configured)."""


class Transport(Protocol):
    """One hosted-model request. Tests inject a stub; nothing here defaults to the network."""

    def __call__(
        self, *, model: str, role: str, system: str | None, inputs: Any
    ) -> ModelOutput: ...


def anthropic_transport(*, model: str, role: str, system: str | None, inputs: Any) -> ModelOutput:
    """The vendor transport: one request, no SDK retries, a JSON object back.

    The SDK is imported here, inside the live path only. Credentials come from the SDK's
    own environment resolution; this module never reads a ``.env`` file. The caller has
    already checked ``in_ci()`` and the model name; this function is never unit-tested
    against the network.
    """
    import anthropic  # lazy: never imported at module import time

    client = anthropic.Anthropic(max_retries=0)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": _LIVE_MAX_TOKENS,
        "messages": [{"role": "user", "content": canonical_json(inputs)}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    if getattr(response, "stop_reason", None) == "refusal":
        raise ValueError(f"role={role}: the model declined the request")
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    parsed = json.loads("".join(texts))
    if not isinstance(parsed, dict):
        raise ValueError(f"role={role}: model reply is not a JSON object")
    return parsed


class ReplayMiss(LookupError):
    """No recorded fixture for this (role, inputs) pair while in replay mode."""

    def __init__(self, role: str, key: str) -> None:
        super().__init__(f"replay miss: role={role} key={key}")
        self.role = role
        self.key = key


def fixture_key(role: str, inputs: Any) -> str:
    """``sha256(role + canonical_json(inputs))`` as lowercase hex."""
    return hashlib.sha256((role + canonical_json(inputs)).encode("utf-8")).hexdigest()


def _check_role(role: str) -> None:
    if not _ROLE_RE.match(role):
        raise ValueError(f"invalid role name {role!r}")


class FixtureStore:
    """Recorded model calls at ``<root>/<role>/<key>.json``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else default_fixture_root()

    def path(self, role: str, key: str) -> Path:
        _check_role(role)
        return self.root / role / f"{key}.json"

    def load(self, role: str, inputs: Any) -> ModelOutput | None:
        """Return the recorded output for (role, inputs), or ``None`` if absent."""
        p = self.path(role, fixture_key(role, inputs))
        if not p.is_file():
            return None
        record = json.loads(p.read_text(encoding="utf-8"))
        output = record["output"]
        if not isinstance(output, dict):
            raise ValueError(f"fixture {p} has a non-object output")
        return output

    def save(self, role: str, inputs: Any, output: ModelOutput) -> Path:
        """Write a fixture atomically (via a ``.tmp`` sibling) and return its path."""
        key = fixture_key(role, inputs)
        p = self.path(role, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        record = {"role": role, "key": key, "inputs": _normalize(inputs), "output": output}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(p)
        return p


class ModelClient:
    """Mode-aware model gateway. Callers supply a deterministic fallback for ``None``."""

    def __init__(
        self,
        mode: ModelMode | None = None,
        store: FixtureStore | None = None,
        model_name: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.mode = mode if mode is not None else ModelMode.from_env()
        self.store = store if store is not None else FixtureStore()
        self.model_name = model_name if model_name is not None else model_name_from_env()
        self.transport = transport

    def call(self, role: str, inputs: Any, *, system: str | None = None) -> ModelOutput | None:
        """Return the model output for (role, inputs).

        Replay: the recorded fixture; on a miss, raise ``ReplayMiss`` in CI, otherwise
        log a warning and return ``None`` so the caller uses its fallback.

        Record: a recorded fixture is returned without a call; on a miss, one transport
        call, the fixture is written, and the written output is returned. Live: one
        transport call, nothing written. Both raise ``NotImplementedError`` with no
        injected transport and ``LiveCallRefused`` in CI or without ``MODEL_NAME``.
        Neither retries.
        """
        _check_role(role)
        if self.mode is ModelMode.REPLAY:
            return self._replay(role, inputs)
        if self.mode is ModelMode.RECORD:
            hit = self.store.load(role, inputs)
            if hit is not None:
                return hit
            output = self._transport_call(role, inputs, system)
            self.store.save(role, inputs, output)
            return self.store.load(role, inputs)
        return self._transport_call(role, inputs, system)

    def _transport_call(self, role: str, inputs: Any, system: str | None) -> ModelOutput:
        if self.transport is None:
            raise NotImplementedError(
                "no live calls in Phase 0 defaults: record/live need an explicitly "
                "injected transport (owner-approved paid run only, WORKFLOW section 7)"
            )
        if in_ci():
            raise LiveCallRefused(f"MODEL_MODE={self.mode.value} refused: SOUNDINGS_CI is set")
        if self.model_name is None:
            raise LiveCallRefused(f"MODEL_MODE={self.mode.value} refused: MODEL_NAME is not set")
        output = self.transport(model=self.model_name, role=role, system=system, inputs=inputs)
        if not isinstance(output, dict):
            raise ValueError(f"role={role}: transport returned a non-object output")
        return output

    def _replay(self, role: str, inputs: Any) -> ModelOutput | None:
        output = self.store.load(role, inputs)
        if output is not None:
            return output
        key = fixture_key(role, inputs)
        if in_ci():
            raise ReplayMiss(role, key)
        log.warning("replay miss role=%s key=%s; caller falls back", role, key)
        return None
