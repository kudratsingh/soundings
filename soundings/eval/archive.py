"""Per-invocation eval archives that a later run can never overwrite (WO-051 part B1).

Every CLI run writes into its own folder ``<out>/runs/<stamp>-<key>/``, where ``stamp``
is a fixed-width UTC time (``20261017T145501Z``) and ``key`` is the first 12 hex digits
of the report's ``inputs_sha256`` (suite, seeds, CLI arguments, config and scenario
files). The folder is created with ``mkdir(exist_ok=False)`` and every file in it with
``O_CREAT | O_EXCL``, so the same stamp and inputs twice is refused, not overwritten.

Write order: ``config.json`` (the resolved configuration), ``manifest.json`` (git sha,
mode, per-file input hashes, the hashes of both reports), ``report.md``, and
``report.json`` last. ``report.json`` is the completion marker: a folder without it is a
partial run whose other files are still evidence. Only after the archive is complete
are ``<out>/report.md`` and ``<out>/report.json`` refreshed as the "latest" copies that
``make eval`` and the deck read.

The report bytes never contain the stamp, so two runs on the same inputs leave
byte-identical ``report.json`` files in their two archives.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from soundings.canonical import canonical_json
from soundings.schemas._base import REPO_ROOT, StrictModel

RUNS_DIRNAME = "runs"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
STAMP_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")
KEY_LENGTH = 12
RUN_DIR_PATTERN = re.compile(r"^(\d{8}T\d{6}Z)-([0-9a-f]{12})$")
MANIFEST_NAME = "manifest.json"
CONFIG_NAME = "config.json"
REPORT_MD = "report.md"
REPORT_JSON = "report.json"
COMPLETION_MARKER = REPORT_JSON
MANIFEST_VERSION = 1
UNKNOWN_SHA = "unknown"


class ArchiveExistsError(FileExistsError):
    """The run folder (or a file in it) already exists; nothing was overwritten."""


class StampError(ValueError):
    """A ``--stamp`` that is not ``YYYYMMDDTHHMMSSZ``."""


class InputDigest(StrictModel):
    path: str
    sha256: str


class ArchiveManifest(StrictModel):
    manifest_version: int
    run_id: str
    stamp: str
    suite: str
    seeds: tuple[int, ...]
    mode: str
    git_sha: str
    inputs_sha256: str
    inputs: tuple[InputDigest, ...]
    outputs: dict[str, str]
    """sha256 of ``report.md`` and ``report.json`` as written into this folder."""
    completion_marker: str


@dataclass(frozen=True)
class ArchivedRun:
    folder: Path
    manifest: ArchiveManifest
    latest_md: Path
    latest_json: Path


def utc_stamp(now: datetime | None = None) -> str:
    """The folder stamp for ``now`` (default: the current UTC time)."""
    moment = now if now is not None else datetime.now(UTC)
    return moment.astimezone(UTC).strftime(STAMP_FORMAT)


def check_stamp(stamp: str) -> str:
    if not STAMP_PATTERN.fullmatch(stamp):
        raise StampError(f"bad stamp {stamp!r} (use YYYYMMDDTHHMMSSZ)")
    return stamp


def run_id(stamp: str, inputs_sha256: str) -> str:
    return f"{check_stamp(stamp)}-{inputs_sha256[:KEY_LENGTH]}"


def git_sha(repo: Path = REPO_ROOT) -> str:
    """``git rev-parse HEAD`` for ``repo``; ``unknown`` when git is unavailable."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_SHA
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else UNKNOWN_SHA


def _label(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def input_digests(paths: Sequence[Path]) -> tuple[InputDigest, ...]:
    """sha256 per input file, labelled repo-relative (never an absolute path)."""
    seen: dict[str, str] = {}
    for p in paths:
        seen[_label(p)] = hashlib.sha256(Path(p).read_bytes()).hexdigest()
    return tuple(InputDigest(path=k, sha256=seen[k]) for k in sorted(seen))


def write_exclusive(path: Path, data: bytes) -> None:
    """Create ``path`` and write ``data``; refuse if the file already exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise ArchiveExistsError(f"{path} already exists; refusing to overwrite") from exc
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_archive(
    out_dir: str | Path,
    *,
    stamp: str,
    suite: str,
    seeds: Sequence[int],
    mode: str,
    inputs_sha256: str,
    input_paths: Sequence[Path],
    config_snapshot: Mapping[str, Any],
    report_md: str,
    report_json: str,
    sha: str | None = None,
) -> ArchivedRun:
    """Write one immutable run folder, then refresh the latest copies in ``out_dir``."""
    out = Path(out_dir)
    rid = run_id(stamp, inputs_sha256)
    runs = out / RUNS_DIRNAME
    runs.mkdir(parents=True, exist_ok=True)
    folder = runs / rid
    try:
        folder.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ArchiveExistsError(f"run folder {rid} already exists; refusing to overwrite") from exc

    md_bytes = report_md.encode("utf-8")
    json_bytes = report_json.encode("utf-8")
    manifest = ArchiveManifest(
        manifest_version=MANIFEST_VERSION,
        run_id=rid,
        stamp=stamp,
        suite=suite,
        seeds=tuple(sorted(set(seeds))),
        mode=mode,
        git_sha=sha if sha is not None else git_sha(),
        inputs_sha256=inputs_sha256,
        inputs=input_digests(input_paths),
        outputs={REPORT_MD: _sha(md_bytes), REPORT_JSON: _sha(json_bytes)},
        completion_marker=COMPLETION_MARKER,
    )
    write_exclusive(folder / CONFIG_NAME, (canonical_json(dict(config_snapshot)) + "\n").encode())
    write_exclusive(folder / MANIFEST_NAME, (canonical_json(manifest) + "\n").encode())
    write_exclusive(folder / REPORT_MD, md_bytes)
    write_exclusive(folder / REPORT_JSON, json_bytes)

    latest_md, latest_json = out / REPORT_MD, out / REPORT_JSON
    latest_md.write_bytes(md_bytes)
    latest_json.write_bytes(json_bytes)
    return ArchivedRun(
        folder=folder, manifest=manifest, latest_md=latest_md, latest_json=latest_json
    )


def is_complete(folder: Path) -> bool:
    """A run folder is complete once its ``report.json`` exists."""
    return (folder / COMPLETION_MARKER).is_file()


def list_archives(out_dir: str | Path) -> list[Path]:
    """Run folders under ``<out>/runs``, oldest first by the stamp in the name."""
    runs = Path(out_dir) / RUNS_DIRNAME
    if not runs.is_dir():
        return []
    found = [p for p in runs.iterdir() if p.is_dir() and RUN_DIR_PATTERN.fullmatch(p.name)]
    return sorted(found, key=lambda p: p.name)


def newest_archive(out_dir: str | Path, *, complete_only: bool = True) -> Path | None:
    """The newest run folder by the stamp in its name (never by file mtime)."""
    for folder in reversed(list_archives(out_dir)):
        if not complete_only or is_complete(folder):
            return folder
    return None


def load_manifest(folder: Path) -> ArchiveManifest:
    return ArchiveManifest.model_validate_json((folder / MANIFEST_NAME).read_text("utf-8"))
