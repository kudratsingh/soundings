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

Bounded live mode (WO-087, CLAUDE.md section 8 A4): ``live`` spends one unit of a
``LiveBudget`` (``LIVE_CALL_BUDGET``, persisted to ``artifacts/live_calls.json``) before
each transport call, runs the call under a per-call timeout (6 s), and turns a spent
budget, a timeout or **any** transport error into ``None`` so the caller uses its
deterministic fallback; nothing propagates. Configuration refusals (no transport,
``SOUNDINGS_CI``, no ``MODEL_NAME``) still raise before any transport call. Every call in
every mode leaves one ``LlmCallRecord`` (role, path in {live, replay, fallback}, reason,
fixture key, tokens in/out, latency) on ``ModelClient.call_log`` and in the module log.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from soundings.canonical import FLOAT_DECIMALS, _normalize, canonical_json, round6

# --- WO-087 begin: live budget import ---
from soundings.llm.live_budget import LiveBudget, budget_total_from_env

# --- WO-087 end ---

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "FLOAT_DECIMALS",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_ERROR",
    "REASON_REPLAY_MISS",
    "REASON_TIMEOUT",
    "CallPath",
    "FixtureStore",
    "LiveBudget",
    "LiveCallRefused",
    "LlmCallRecord",
    "TransportReply",
    "estimate_tokens",
    "last_path",
    "live_client_from_env",
    "reset_last_path",
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


# --- WO-087 begin: call records and the process-wide last path ---
CallPath = Literal["live", "replay", "fallback"]
DEFAULT_TIMEOUT_S: Final = 6.0
REASON_BUDGET_EXHAUSTED: Final = "budget_exhausted"
REASON_TIMEOUT: Final = "timeout"
REASON_ERROR: Final = "error"
REASON_REPLAY_MISS: Final = "replay_miss"


@dataclass(frozen=True)
class TransportReply:
    """A transport result with the provider's token usage (a bare dict is also accepted)."""

    output: ModelOutput
    tokens_in: int | None = None
    tokens_out: int | None = None


@dataclass(frozen=True)
class LlmCallRecord:
    """One model call as the audit log and ``/api/model_mode`` see it.

    ``path`` says what produced the caller's answer: ``live`` (a hosted reply), ``replay``
    (a recorded fixture) or ``fallback`` (``None`` to the caller, who uses its
    deterministic fallback; ``reason`` says why). ``latency_ms`` is measured only around a
    transport call; replayed and budget-refused calls record 0.0 so replay runs stay
    byte-identical. Tokens are the provider's usage when the transport reports it,
    otherwise ``estimate_tokens`` of the canonical text (``tokens_estimated``).
    """

    role: str
    path: CallPath
    reason: str | None
    fixture_key: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    tokens_estimated: bool = True


def estimate_tokens(text: str) -> int:
    """A coarse, deterministic token estimate: ``ceil(len(text) / 4)``."""
    return int(math.ceil(len(text) / 4))


_last_path_lock = threading.Lock()
_last_path: CallPath | None = None


def last_path() -> CallPath | None:
    """The path of the most recent model call in this process (``None`` before any)."""
    with _last_path_lock:
        return _last_path


def reset_last_path() -> None:
    """Forget the last path (tests, and a fresh demo run)."""
    global _last_path
    with _last_path_lock:
        _last_path = None


def _set_last_path(path: CallPath) -> None:
    global _last_path
    with _last_path_lock:
        _last_path = path


class _LiveTimeout(TimeoutError):
    """A transport call outlived ``timeout_s``."""


# --- WO-087 end ---


class Transport(Protocol):
    """One hosted-model request. Tests inject a stub; nothing here defaults to the network.

    It returns the parsed JSON object, or a ``TransportReply`` carrying token usage too.
    """

    def __call__(
        self, *, model: str, role: str, system: str | None, inputs: Any
    ) -> ModelOutput | TransportReply: ...


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def parse_model_json(text: str, role: str) -> dict[str, Any]:
    """Parse a model reply as one JSON object, tolerating a Markdown code fence or prose.

    Order: the raw text; the text inside a single ``\`\`\`json`` fence; the span from the
    first ``{`` to the last ``}``. Anything else raises ``ValueError``. This is parse
    robustness only: the object still goes through the role guard like any other reply,
    and nothing here changes what the reply says.
    """
    candidates = [text]
    fenced = _FENCE_RE.match(text)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"role={role}: model reply is not a JSON object")
    raise ValueError(f"role={role}: model reply is not JSON")


def anthropic_transport(
    *, model: str, role: str, system: str | None, inputs: Any
) -> TransportReply:
    """The vendor transport: one request, no SDK retries, a JSON object back.

    The SDK is imported here, inside the live path only. Credentials come from the SDK's
    own environment resolution; this module never reads a ``.env`` file. The caller has
    already checked ``in_ci()`` and the model name; this function is never unit-tested
    against the network.
    """
    import anthropic  # lazy: never imported at module import time

    client = anthropic.Anthropic(max_retries=0, timeout=DEFAULT_TIMEOUT_S + 1.0)
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
    parsed = parse_model_json("".join(texts), role)
    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "input_tokens", None)
    tokens_out = getattr(usage, "output_tokens", None)
    return TransportReply(
        parsed,
        tokens_in if isinstance(tokens_in, int) else None,
        tokens_out if isinstance(tokens_out, int) else None,
    )


# --- WO-093 begin: the recording transport (owner-approved fixture run only) ---
RECORD_TIMEOUT_S: Final = 60.0
"""SDK timeout for one fixture-run request: a SITREP reply can outlast the 7 s stage cap,
and in record mode a timeout stops the whole run (never retried)."""


def anthropic_record_transport(
    *, model: str, role: str, system: str | None, inputs: Any
) -> TransportReply:
    """``anthropic_transport`` with ``RECORD_TIMEOUT_S`` instead of the stage timeout.

    Used only by ``python -m soundings.llm.fixtures record`` after the owner's approval;
    one request, no SDK retries, the SDK imported here (lazily). Never unit-tested
    against the network.
    """
    import anthropic  # lazy: never imported at module import time

    client = anthropic.Anthropic(max_retries=0, timeout=RECORD_TIMEOUT_S)
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
    parsed = parse_model_json("".join(texts), role)
    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "input_tokens", None)
    tokens_out = getattr(usage, "output_tokens", None)
    return TransportReply(
        parsed,
        tokens_in if isinstance(tokens_in, int) else None,
        tokens_out if isinstance(tokens_out, int) else None,
    )


# --- WO-093 end ---


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
    """Mode-aware model gateway. Callers supply a deterministic fallback for ``None``.

    ``live_budget`` caps live calls (WO-087). In ``live`` mode with no budget passed, the
    client reads ``LIVE_CALL_BUDGET``; when that is unset too the injected transport is
    uncapped, which only a test can reach: the one constructor that wires the vendor
    transport, ``live_client_from_env``, refuses without ``LIVE_CALL_BUDGET``.
    """

    def __init__(
        self,
        mode: ModelMode | None = None,
        store: FixtureStore | None = None,
        model_name: str | None = None,
        *,
        live_budget: LiveBudget | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Transport | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        self.mode = mode if mode is not None else ModelMode.from_env()
        self.store = store if store is not None else FixtureStore()
        self.model_name = model_name if model_name is not None else model_name_from_env()
        self.transport = transport
        self.timeout_s = float(timeout_s)
        if live_budget is None and self.mode is ModelMode.LIVE:
            live_budget = LiveBudget.from_env()
        self.live_budget = live_budget
        self.call_log: list[LlmCallRecord] = []

    @property
    def last_call(self) -> LlmCallRecord | None:
        """The record of this client's most recent call."""
        return self.call_log[-1] if self.call_log else None

    @property
    def last_path(self) -> CallPath | None:
        last = self.last_call
        return None if last is None else last.path

    def call(self, role: str, inputs: Any, *, system: str | None = None) -> ModelOutput | None:
        """Return the model output for (role, inputs), or ``None`` for the caller's fallback.

        Replay: the recorded fixture; on a miss, raise ``ReplayMiss`` in CI, otherwise
        log a warning and return ``None`` so the caller uses its fallback.

        Record: a recorded fixture is returned without a call; on a miss, one transport
        call, the fixture is written, and the written output is returned. Live: one
        bounded transport call, nothing written; a spent budget, a timeout or any error
        returns ``None``. Both raise ``NotImplementedError`` with no injected transport
        and ``LiveCallRefused`` in CI or without ``MODEL_NAME``. Neither retries.
        """
        _check_role(role)
        key = fixture_key(role, inputs)
        if self.mode is ModelMode.REPLAY:
            output = self._replay(role, inputs)
            if output is None:
                self._record(role, "fallback", REASON_REPLAY_MISS, key, inputs, system, None)
            else:
                self._record(role, "replay", None, key, inputs, system, output)
            return output
        if self.mode is ModelMode.RECORD:
            hit = self.store.load(role, inputs)
            if hit is not None:
                self._record(role, "replay", None, key, inputs, system, hit)
                return hit
            self._refuse_unless_ready()
            started = time.perf_counter()
            reply = self._invoke(role, inputs, system)
            output = _reply_output(role, reply)
            self.store.save(role, inputs, output)
            saved = self.store.load(role, inputs)
            self._record(
                role, "live", None, key, inputs, system, saved, reply=reply, started=started
            )
            return saved
        return self._live(role, inputs, system, key)

    # --- WO-087 begin: the bounded live path ---
    def _live(self, role: str, inputs: Any, system: str | None, key: str) -> ModelOutput | None:
        self._refuse_unless_ready()
        budget = self.live_budget
        if budget is not None and not budget.spend():
            self._record(role, "fallback", REASON_BUDGET_EXHAUSTED, key, inputs, system, None)
            return None
        started = time.perf_counter()
        try:
            reply = self._with_timeout(role, lambda: self._invoke(role, inputs, system))
            output = _reply_output(role, reply)
        except _LiveTimeout:
            self._record(
                role, "fallback", REASON_TIMEOUT, key, inputs, system, None, started=started
            )
            return None
        except Exception as exc:  # any transport failure falls back; nothing propagates
            reason = f"{REASON_ERROR}:{type(exc).__name__}"
            log.warning("live call role=%s failed (%s): %s; caller falls back", role, reason, exc)
            self._record(role, "fallback", reason, key, inputs, system, None, started=started)
            return None
        self._record(role, "live", None, key, inputs, system, output, reply=reply, started=started)
        return output

    def _with_timeout(
        self, role: str, fn: Callable[[], ModelOutput | TransportReply]
    ) -> ModelOutput | TransportReply:
        """Run ``fn`` on a daemon thread and give up after ``timeout_s`` seconds.

        A call that outlives the timeout keeps running on its thread (a blocking SDK call
        cannot be interrupted) but its result is discarded; the thread is a daemon, so it
        never holds the process open.
        """
        box: dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=target, name=f"live-call-{role}", daemon=True).start()
        if not done.wait(self.timeout_s):
            raise _LiveTimeout(f"role={role}: no reply within {self.timeout_s} s")
        if "error" in box:
            raise box["error"]
        value: ModelOutput | TransportReply = box["value"]
        return value

    def _record(
        self,
        role: str,
        path: CallPath,
        reason: str | None,
        key: str,
        inputs: Any,
        system: str | None,
        output: ModelOutput | None,
        *,
        reply: ModelOutput | TransportReply | None = None,
        started: float | None = None,
    ) -> LlmCallRecord:
        tokens_in: int | None = None
        tokens_out: int | None = None
        if isinstance(reply, TransportReply):
            tokens_in, tokens_out = reply.tokens_in, reply.tokens_out
        estimated = tokens_in is None or tokens_out is None
        if tokens_in is None:
            tokens_in = estimate_tokens((system or "") + canonical_json(inputs))
        if tokens_out is None:
            tokens_out = 0 if output is None else estimate_tokens(canonical_json(output))
        latency_ms = 0.0 if started is None else round6((time.perf_counter() - started) * 1000.0)
        record = LlmCallRecord(
            role=role,
            path=path,
            reason=reason,
            fixture_key=key,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            tokens_estimated=estimated,
        )
        self.call_log.append(record)
        _set_last_path(path)
        log.info(
            "llm_call role=%s path=%s reason=%s tokens_in=%d tokens_out=%d latency_ms=%.3f key=%s",
            role,
            path,
            reason or "-",
            tokens_in,
            tokens_out,
            latency_ms,
            key,
        )
        return record

    # --- WO-087 end ---

    def _refuse_unless_ready(self) -> None:
        if self.transport is None:
            raise NotImplementedError(
                "no live calls in Phase 0 defaults: record/live need an explicitly "
                "injected transport (owner-approved paid run only, WORKFLOW section 7)"
            )
        if in_ci():
            raise LiveCallRefused(f"MODEL_MODE={self.mode.value} refused: SOUNDINGS_CI is set")
        if self.model_name is None:
            raise LiveCallRefused(f"MODEL_MODE={self.mode.value} refused: MODEL_NAME is not set")

    def _invoke(self, role: str, inputs: Any, system: str | None) -> ModelOutput | TransportReply:
        transport = self.transport
        model = self.model_name
        if transport is None or model is None:  # _refuse_unless_ready ran first
            raise LiveCallRefused(f"role={role}: no transport or model name")
        return transport(model=model, role=role, system=system, inputs=inputs)

    def _replay(self, role: str, inputs: Any) -> ModelOutput | None:
        output = self.store.load(role, inputs)
        if output is not None:
            return output
        key = fixture_key(role, inputs)
        if in_ci():
            raise ReplayMiss(role, key)
        log.warning("replay miss role=%s key=%s; caller falls back", role, key)
        return None


# --- WO-087 begin: reply unwrap and the stage constructor ---
def _reply_output(role: str, reply: ModelOutput | TransportReply) -> ModelOutput:
    output = reply.output if isinstance(reply, TransportReply) else reply
    if not isinstance(output, dict):
        raise ValueError(f"role={role}: transport returned a non-object output")
    return output


def live_client_from_env(store: FixtureStore | None = None) -> ModelClient:
    """The client a stage or demo process uses: ``MODEL_MODE`` from the environment.

    ``replay`` (the default) gets no transport. ``live`` gets the vendor transport only
    with ``LIVE_CALL_BUDGET`` set (the owner's ``approve paid run stage live budget N``
    sets it); without it, or under ``SOUNDINGS_CI``, this raises ``LiveCallRefused``
    before any client exists. ``record`` stays a recording-script concern and is refused.
    """
    mode = ModelMode.from_env()
    if mode is ModelMode.REPLAY:
        return ModelClient(mode=mode, store=store)
    if mode is ModelMode.RECORD:
        raise LiveCallRefused("MODEL_MODE=record is for the fixture recording script only")
    if in_ci():
        raise LiveCallRefused("MODEL_MODE=live refused: SOUNDINGS_CI is set")
    total = budget_total_from_env()
    if total is None:
        raise LiveCallRefused("MODEL_MODE=live refused: LIVE_CALL_BUDGET is not set")
    return ModelClient(
        mode=mode,
        store=store,
        live_budget=LiveBudget(total=total),
        transport=anthropic_transport,
    )


# --- WO-087 end ---
