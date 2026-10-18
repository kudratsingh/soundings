"""WO-062C: GET /api/eval/summary serves the three-policy outcome rows of the eval report
(read-only), labelled stress-test estimate; a missing report is a clear 404."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from soundings.api import create_app
from soundings.api_routes.eval_summary import METRICS, MISSING_DETAIL, get_report_path


def _client(path: Path) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_report_path] = lambda: path
    return TestClient(app)


def _report() -> dict[str, Any]:
    def row(policy: str, **kw: float) -> dict[str, Any]:
        base = {m: 0 for m in METRICS}
        base.update(policy=policy, gt_contacts=45, runs=40, **kw)
        return base

    return {
        "label": "stress-test estimate",
        "mode": "replay",
        "suite": "catalog",
        "seeds": [1, 2, 3, 4, 5],
        "scenarios": ["S01", "S05"],
        "inputs_sha256": "ab" * 32,
        "outcomes": {
            "bang": row("bang", false_shutdowns=433, shutdown_minutes=1292.966668),
            "human_queue": row("human_queue", interruptions_per_hour=30.705908),
            "soundings": row(
                "soundings", at_or_below_ask_pct=68.888889, median_s_seen_to_handled=58.0
            ),
        },
    }


def test_present_report(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report()), encoding="utf-8")
    r = _client(path).get("/api/eval/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "stress-test estimate"
    assert body["metrics"] == list(METRICS)
    assert [row["policy"] for row in body["rows"]] == ["soundings", "bang", "human_queue"]
    ours, bang, queue = body["rows"]
    assert ours["at_or_below_ask_pct"] == pytest.approx(68.888889)
    assert ours["median_s_seen_to_handled"] == 58.0
    assert bang["false_shutdowns"] == 433 and bang["shutdown_minutes"] == pytest.approx(
        1292.97, abs=0.01
    )
    assert queue["interruptions_per_hour"] == pytest.approx(30.705908)
    for row in body["rows"]:
        assert set(METRICS) <= set(row)
        assert "gt_contacts" not in row  # only the six card numbers


def test_absent_report_is_a_clear_404(tmp_path: Path) -> None:
    r = _client(tmp_path / "nope.json").get("/api/eval/summary")
    assert r.status_code == 404
    assert r.json()["detail"] == MISSING_DETAIL
    assert "make eval" in r.json()["detail"]


@pytest.mark.parametrize("text", ["{not json", "[]", json.dumps({"outcomes": {}})])
def test_unreadable_report_is_a_500_with_the_reason(tmp_path: Path, text: str) -> None:
    path = tmp_path / "report.json"
    path.write_text(text, encoding="utf-8")
    r = _client(path).get("/api/eval/summary")
    assert r.status_code == 500
    assert r.json()["detail"].startswith("eval report unreadable")


def test_route_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report()), encoding="utf-8")
    client = _client(path)
    before = path.read_bytes()
    for method in ("post", "put", "delete"):
        assert getattr(client, method)("/api/eval/summary").status_code == 405
    assert path.read_bytes() == before


def test_missing_policy_row_is_skipped(tmp_path: Path) -> None:
    report = _report()
    del report["outcomes"]["human_queue"]
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    body = _client(path).get("/api/eval/summary").json()
    assert [row["policy"] for row in body["rows"]] == ["soundings", "bang"]
