"""WO-041: standing orders are verified at load time; the sign CLI is idempotent.

Every case runs on a temp copy of ``config/standing_orders.yaml``. The signature is a
sha256 over the canonical YAML of ``orders``: tamper-evident, not authenticated.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from soundings.ladder import sign as sign_cli
from soundings.ladder.standing_orders import (
    SIGNATURE_LABEL,
    StandingScope,
    canonical_orders_yaml,
    effective_scope,
    load_verified,
    orders_digest,
    read_document,
    scenario_now,
)
from soundings.schemas import Rung, ToggleName, load_scenario
from soundings.schemas.config import STANDING_ORDERS_PATH

REPO = Path(__file__).resolve().parent.parent
PLACEHOLDER = "REPLACE_WITH_SHA256_AT_PHASE_4"
SIG_LINE = re.compile(r"^signature:.*$", re.MULTILINE)

ORDERS = yaml.safe_load(STANDING_ORDERS_PATH.read_text(encoding="utf-8"))["orders"]
SIGNED_AT = dt.datetime.fromisoformat(ORDERS["signed_at"])
VALID_UNTIL = dt.datetime.fromisoformat(ORDERS["valid_until"])
INSIDE = SIGNED_AT + (VALID_UNTIL - SIGNED_AT) / 2


@pytest.fixture
def orders_file(tmp_path: Path) -> Path:
    """A temp copy of the shipped orders with the placeholder signature restored."""
    path = tmp_path / "standing_orders.yaml"
    shutil.copyfile(STANDING_ORDERS_PATH, path)
    text = path.read_text(encoding="utf-8")
    path.write_text(SIG_LINE.sub(f'signature: "{PLACEHOLDER}"', text, count=1), encoding="utf-8")
    return path


def test_placeholder_is_unsigned(orders_file: Path) -> None:
    status = load_verified(orders_file, now=INSIDE)
    assert not status.signed
    assert status.reason == "unsigned"
    assert effective_scope(status) is None


def test_signing_gives_ok(orders_file: Path) -> None:
    digest = sign_cli.sign(orders_file)
    status = load_verified(orders_file, now=INSIDE)
    assert status.signed and status.reason == "ok"
    assert status.digest == digest == orders_digest(read_document(orders_file)["orders"])
    assert status.orders is not None
    assert status.orders.signed_by == ORDERS["signed_by"]
    scope = effective_scope(status)
    assert isinstance(scope, StandingScope)
    assert (
        scope.hold is not None and scope.hold.max_minutes == ORDERS["scope"]["hold"]["max_minutes"]
    )
    assert scope.slow is not None and list(scope.slow.basins) == ORDERS["scope"]["slow"]["basins"]


def test_one_character_edit_inside_orders_is_mismatch(orders_file: Path) -> None:
    sign_cli.sign(orders_file)
    text = orders_file.read_text(encoding="utf-8")
    edited = text.replace('"Watch Officer"', '"Watch Officar"', 1)
    assert edited != text
    orders_file.write_text(edited, encoding="utf-8")
    status = load_verified(orders_file, now=INSIDE)
    assert not status.signed and status.reason == "mismatch"
    assert effective_scope(status) is None


def test_number_edit_inside_orders_is_mismatch(orders_file: Path) -> None:
    sign_cli.sign(orders_file)
    text = orders_file.read_text(encoding="utf-8")
    edited = re.sub(r"max_minutes: 15\b", "max_minutes: 16", text, count=1)
    assert edited != text
    orders_file.write_text(edited, encoding="utf-8")
    assert load_verified(orders_file, now=INSIDE).reason == "mismatch"


def test_window_expired_and_not_yet_valid(orders_file: Path) -> None:
    sign_cli.sign(orders_file)
    second = dt.timedelta(seconds=1)
    after = load_verified(orders_file, now=VALID_UNTIL + second)
    assert not after.signed and after.reason == "expired"
    assert load_verified(orders_file, now=VALID_UNTIL).reason == "expired"
    assert load_verified(orders_file, now=VALID_UNTIL - second).reason == "ok"
    before = load_verified(orders_file, now=SIGNED_AT - second)
    assert not before.signed and before.reason == "not_yet_valid"
    assert load_verified(orders_file, now=SIGNED_AT).reason == "ok"
    assert effective_scope(after) is None and effective_scope(before) is None
    # Re-checking the window later keeps the verdict pure.
    ok = load_verified(orders_file, now=INSIDE)
    assert ok.at(VALID_UNTIL + second).reason == "expired"
    assert after.at(INSIDE).reason == "ok"


def test_missing_and_invalid(tmp_path: Path) -> None:
    assert load_verified(tmp_path / "absent.yaml", now=INSIDE).reason == "missing"
    bad = tmp_path / "bad.yaml"
    bad.write_text("orders: [1, 2]\nsignature: x\n", encoding="utf-8")
    assert load_verified(bad, now=INSIDE).reason == "invalid"
    # A matching signature over orders that fail the strict shape is still not signed.
    odd = tmp_path / "odd.yaml"
    odd.write_text("orders:\n  surprise: 1\nsignature: x\n", encoding="utf-8")
    sign_cli.sign(odd)
    status = load_verified(odd, now=INSIDE)
    assert not status.signed and status.reason == "invalid"


def test_sign_check_exit_codes(orders_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = orders_file.read_bytes()
    assert sign_cli.main(["--path", str(orders_file), "--check"]) == 1
    assert orders_file.read_bytes() == before  # --check writes nothing
    assert sign_cli.main(["--path", str(orders_file)]) == 0
    assert sign_cli.main(["--path", str(orders_file), "--check"]) == 0
    out = capsys.readouterr().out
    assert "not authenticated" in out and SIGNATURE_LABEL in out
    text = orders_file.read_text(encoding="utf-8")
    orders_file.write_text(text.replace("max_gates: 1", "max_gates: 2", 1), encoding="utf-8")
    assert sign_cli.main(["--path", str(orders_file), "--check"]) == 1


def test_sign_cli_module_runs(orders_file: Path) -> None:
    cmd = [sys.executable, "-m", "soundings.ladder.sign", "--path", str(orders_file)]
    assert subprocess.run([*cmd, "--check"], cwd=REPO, capture_output=True).returncode == 1
    assert subprocess.run(cmd, cwd=REPO, capture_output=True).returncode == 0
    assert subprocess.run([*cmd, "--check"], cwd=REPO, capture_output=True).returncode == 0


def test_signing_twice_is_byte_identical(orders_file: Path) -> None:
    sign_cli.sign(orders_file)
    once = orders_file.read_bytes()
    sign_cli.sign(orders_file)
    assert orders_file.read_bytes() == once


def test_comments_and_order_survive(orders_file: Path) -> None:
    before = orders_file.read_text(encoding="utf-8").splitlines()
    sign_cli.sign(orders_file)
    after = orders_file.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert len(changed) == 1
    assert changed[0][0].startswith("signature:") and changed[0][1].startswith("signature:")
    comments = [ln for ln in before if ln.lstrip().startswith("#") or "  # " in ln]
    assert comments and all(c in after for c in comments)


def test_digest_ignores_comments_and_key_order(orders_file: Path, tmp_path: Path) -> None:
    doc = read_document(orders_file)
    reordered = dict(reversed(list(doc["orders"].items())))
    assert orders_digest(reordered) == orders_digest(doc["orders"])
    assert canonical_orders_yaml(doc["orders"]).startswith("approvers:")
    noisy = tmp_path / "noisy.yaml"
    noisy.write_text(
        "# extra comment\n" + orders_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert orders_digest(read_document(noisy)["orders"]) == orders_digest(doc["orders"])


def test_degraded_mode_scope(orders_file: Path) -> None:
    sign_cli.sign(orders_file)
    status = load_verified(orders_file, now=INSIDE)
    full = effective_scope(status)
    assert full is not None and full.degraded == ()
    allowed = {Rung(r) for r in ORDERS["degraded_mode"]["allowed_rungs"]}
    for reason in ORDERS["degraded_mode"]["when"]:
        narrowed = effective_scope(status, [ToggleName(reason)])
        assert narrowed is not None
        assert narrowed.degraded == (ToggleName(reason),)
        assert (narrowed.slow is not None) is (Rung.SLOW in allowed)
        assert (narrowed.hold is not None) is (Rung.HOLD in allowed)
        assert narrowed.never == full.never == ("any_closure", "external_call_uscg_eod")
    # The never list maps onto boundary actions, and a degraded scope never widens.
    text = orders_file.read_text(encoding="utf-8")
    orders_file.write_text(
        text.replace("allowed_rungs: [watch, ask, slow, hold]", "allowed_rungs: [watch, ask]"),
        encoding="utf-8",
    )
    sign_cli.sign(orders_file)
    tight = load_verified(orders_file, now=INSIDE)
    assert tight.signed
    down = effective_scope(tight, [ToggleName.NETWORK_DOWN])
    assert down is not None and down.slow is None and down.hold is None
    up = effective_scope(tight, [])
    assert up is not None and up.slow is not None and up.hold is not None


def test_scenario_now_is_deterministic() -> None:
    s05 = load_scenario(REPO / "scenarios" / "S05-unfiled-closing-fuel-berth.yaml")
    t0 = scenario_now(s05, 0.0)
    assert t0.isoformat() == f"2026-10-17T{s05.hour_of_day:02d}:00:00-07:00"
    assert scenario_now(s05, 90.0) - t0 == dt.timedelta(seconds=90)
    assert scenario_now(s05.hour_of_day, 90.0) == scenario_now(s05, 90.0)
    assert SIGNED_AT <= t0 < VALID_UNTIL  # the catalog runs inside the signed window


def test_committed_orders_are_signed() -> None:
    """``uv run python -m soundings.ladder.sign --check`` exits 0 on the shipped file."""
    proc = subprocess.run(
        [sys.executable, "-m", "soundings.ladder.sign", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert load_verified(STANDING_ORDERS_PATH, now=INSIDE).signed
