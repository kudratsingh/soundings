"""WO-051 part B1: immutable per-invocation eval archives.

Every CLI run lands in its own ``<out>/runs/<stamp>-<key>/`` folder created with
exclusive creation; the same stamp and inputs twice is refused (exit 4), a different
stamp coexists, report.json is written last, the manifest records mode=replay, and two
runs on the same inputs leave byte-identical report.json files in their archives.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import soundings.eval.__main__ as cli
import soundings.eval.archive as archive
from soundings.eval.archive import (
    COMPLETION_MARKER,
    ArchiveExistsError,
    StampError,
    is_complete,
    list_archives,
    load_manifest,
    newest_archive,
    run_id,
    utc_stamp,
    write_archive,
    write_exclusive,
)

runner = CliRunner()
HASH_A = "a" * 64
HASH_B = "b" * 64
STAMP_1 = "20261017T150000Z"
STAMP_2 = "20261017T150001Z"


def _write(out: Path, stamp: str, inputs_hash: str = HASH_A, md: str = "# r\n") -> Path:
    src = out.parent / "input.yaml"
    if not src.exists():
        src.write_text("k: 1\n", encoding="utf-8")
    return write_archive(
        out,
        stamp=stamp,
        suite="catalog",
        seeds=(2, 1),
        mode="replay",
        inputs_sha256=inputs_hash,
        input_paths=[src],
        config_snapshot={"args": {"suite": "catalog"}},
        report_md=md,
        report_json='{"x":1}\n',
        sha="0" * 40,
    ).folder


def test_stamp_format_and_run_id() -> None:
    assert utc_stamp(datetime(2026, 10, 17, 14, 55, 1, tzinfo=UTC)) == "20261017T145501Z"
    assert run_id(STAMP_1, HASH_A) == f"{STAMP_1}-{'a' * 12}"
    for bad in ("2026-10-17", "20261017T150000", "x"):
        with pytest.raises(StampError):
            run_id(bad, HASH_A)


def test_same_stamp_refuses_and_different_stamp_succeeds(tmp_path: Path) -> None:
    out = tmp_path / "eval"
    first = _write(out, STAMP_1, md="# first\n")
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    with pytest.raises(ArchiveExistsError):
        _write(out, STAMP_1, md="# second\n")
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before  # untouched
    assert (out / "report.md").read_text(encoding="utf-8") == "# first\n"

    second = _write(out, STAMP_2, md="# second\n")
    assert second != first and is_complete(first) and is_complete(second)
    assert list_archives(out) == [first, second]
    assert (out / "report.md").read_text(encoding="utf-8") == "# second\n"


def test_same_stamp_different_inputs_coexist(tmp_path: Path) -> None:
    out = tmp_path / "eval"
    a = _write(out, STAMP_1, HASH_A)
    b = _write(out, STAMP_1, HASH_B)
    assert a.name != b.name and len(list_archives(out)) == 2


def test_write_exclusive_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    write_exclusive(target, b"one")
    with pytest.raises(ArchiveExistsError):
        write_exclusive(target, b"two")
    assert target.read_bytes() == b"one"


def test_report_json_is_written_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    real = archive.write_exclusive

    def spy(path: Path, data: bytes) -> None:
        order.append(path.name)
        real(path, data)

    monkeypatch.setattr(archive, "write_exclusive", spy)
    _write(tmp_path / "eval", STAMP_1)
    assert order == ["config.json", "manifest.json", "report.md", COMPLETION_MARKER]


def test_partial_run_is_flagged_and_latest_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "eval"
    done = _write(out, STAMP_1, md="# done\n")
    real = archive.write_exclusive

    def fail_marker(path: Path, data: bytes) -> None:
        if path.name == COMPLETION_MARKER:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(archive, "write_exclusive", fail_marker)
    with pytest.raises(OSError):
        _write(out, STAMP_2, md="# partial\n")
    partial = list_archives(out)[-1]
    assert not is_complete(partial) and (partial / "report.md").is_file()
    assert newest_archive(out) == done
    assert newest_archive(out, complete_only=False) == partial
    assert (out / "report.md").read_text(encoding="utf-8") == "# done\n"


def test_newest_is_by_name_not_mtime(tmp_path: Path) -> None:
    out = tmp_path / "eval"
    later = _write(out, "20261018T000000Z")
    earlier = _write(out, "20261017T000000Z")  # written second, older stamp
    assert newest_archive(out) == later and list_archives(out) == [earlier, later]
    (out / "runs" / "not-a-run").mkdir()
    assert newest_archive(out) == later


def test_manifest_contents(tmp_path: Path) -> None:
    folder = _write(tmp_path / "eval", STAMP_1)
    m = load_manifest(folder)
    assert m.mode == "replay" and m.seeds == (1, 2) and m.stamp == STAMP_1
    assert m.git_sha == "0" * 40 and m.inputs_sha256 == HASH_A
    assert set(m.outputs) == {"report.md", "report.json"}
    assert [i.path for i in m.inputs] == ["input.yaml"]  # never an absolute path
    assert json.loads((folder / "config.json").read_text("utf-8")) == {"args": {"suite": "catalog"}}


def _cli(out: Path, stamp: str, suite: str = "lost_vehicle", seeds: str = "1,2") -> int:
    result = runner.invoke(
        cli.app,
        [
            "--suite",
            suite,
            "--seeds",
            seeds,
            "--mode",
            "replay",
            "--out",
            str(out),
            "--stamp",
            stamp,
            "--no-misspecified",
        ],
    )
    return result.exit_code


def test_cli_archives_and_refuses_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_MODE", "replay")
    out = tmp_path / "out"
    assert _cli(out, STAMP_1) == 0
    assert _cli(out, STAMP_1) == cli.EXIT_ARCHIVE_EXISTS
    assert _cli(out, STAMP_2) == 0
    first, second = list_archives(out)
    for folder in (first, second):
        assert sorted(p.name for p in folder.iterdir()) == [
            "config.json",
            "manifest.json",
            "report.json",
            "report.md",
        ]
        m = load_manifest(folder)
        assert m.mode == "replay" and m.suite == "lost_vehicle" and m.seeds == (1, 2)
        assert any(i.path == "config/ladder.yaml" for i in m.inputs)
        assert any(i.path.startswith("scenarios/S02-") for i in m.inputs)
        snap = json.loads((folder / "config.json").read_text("utf-8"))
        assert snap["args"]["mode"] == "replay" and "stopping" in snap["ladder"]
    # Determinism: same inputs, two archives, byte-identical reports (and latest copies).
    for name in ("report.json", "report.md"):
        assert (
            (first / name).read_bytes() == (second / name).read_bytes() == (out / name).read_bytes()
        )


def test_cli_bad_stamp_exits_2(tmp_path: Path) -> None:
    assert _cli(tmp_path / "out", "yesterday") == 2
    assert not (tmp_path / "out").exists()


def test_cli_paid_mode_still_refused_and_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        cli.app, ["--suite", "harbor_night", "--seeds", "1", "--mode", "live", "--out", str(out)]
    )
    assert result.exit_code == cli.EXIT_PAID_REFUSED
    assert "estimated model calls:" in result.output
    assert not out.exists()
