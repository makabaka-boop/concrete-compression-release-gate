"""End-to-end tests for GET /evaluations/{evaluation_id}, over real HTTP.

The write/replay/query flow runs against the API at API_BASE_URL (default
http://localhost:8000), same as test_api.py. The restart-persistence and
legacy-migration cases each spawn a throwaway uvicorn process bound to a
temporary ledger file, so they exercise the real HTTP stack and the real
SQLite file without touching the shared ledger.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app import store

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def wait_for_api() -> None:
    deadline = time.time() + 60
    while True:
        try:
            response = httpx.get(f"{BASE_URL}/health", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            pytest.fail(f"API at {BASE_URL} did not become healthy within 60s")
        time.sleep(1)


def fresh_id() -> str:
    return f"test-{uuid.uuid4().hex}"


def evaluate(payload, base_url=BASE_URL):
    return httpx.post(f"{base_url}/evaluate", json=payload, timeout=10)


def get_record(evaluation_id, base_url=BASE_URL):
    return httpx.get(f"{base_url}/evaluations/{evaluation_id}", timeout=10)


def without_replayed(body):
    return {key: value for key, value in body.items() if key != "replayed"}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def run_server(db_path):
    """Serve the app over real HTTP against ``db_path`` until the block exits."""
    port = _free_port()
    env = dict(os.environ, EVALUATION_DB_PATH=str(db_path))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError("spawned API process exited before becoming healthy")
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise TimeoutError("spawned API did not become healthy within 30s")
            time.sleep(0.2)
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)


def make_flow_payload(evaluation_id):
    """One batch entered as face dimensions with an explicit calibration factor."""
    return {
        "design_strength_mpa": 30.0,
        "calibration_factor": 1.01,
        "evaluation_id": evaluation_id,
        "specimens": [
            {"width_mm": 100, "depth_mm": 200, "load_kn": 660},
            {"width_mm": 100, "depth_mm": 200, "load_kn": 670},
            {"width_mm": 100, "depth_mm": 200, "load_kn": 680},
        ],
    }


def test_dimensions_and_calibration_write_replay_then_query():
    # Acceptance flow: one submission with dimensions and a calibration
    # factor ties together the first write, the replay and the retrieval.
    evaluation_id = fresh_id()
    payload = make_flow_payload(evaluation_id)

    first = evaluate(payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["replayed"] is False
    assert first_body["strengths_mpa"] == [33.3, 33.8, 34.3]
    assert first_body["mean_strength_mpa"] == 33.8
    assert first_body["passed"] is True
    assert first_body["applied_calibration_factor"] == 1.01

    # The record is queryable right after the first write.
    record = get_record(evaluation_id)
    assert record.status_code == 200
    snapshot_before_replay = record.json()

    # The retry replays the first result and must not rewrite the record.
    replay = evaluate(payload)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    assert without_replayed(replay_body) == without_replayed(first_body)

    record_after_replay = get_record(evaluation_id)
    assert record_after_replay.status_code == 200
    assert record_after_replay.json() == snapshot_before_replay

    body = snapshot_before_replay
    assert body["evaluation_id"] == evaluation_id
    assert body["created_at"]
    assert body["snapshot_available"] is True
    # Normalized input: design strength as evaluated, and each specimen
    # reduced to its effective area (100 mm x 200 mm = 20000 mm²) + load.
    assert body["design_strength_mpa"] == 30.0
    assert body["specimens"] == [
        {"area_mm2": 20000, "load_kn": 660},
        {"area_mm2": 20000, "load_kn": 670},
        {"area_mm2": 20000, "load_kn": 680},
    ]
    # Calibration submission mode: explicitly sent, with the exact factor.
    assert body["calibration_explicit"] is True
    assert body["calibration_factor"] == 1.01
    # The stored first verdict matches both adjudicated responses.
    assert body["result"] == without_replayed(first_body)
    assert body["result"] == without_replayed(replay_body)


def test_query_without_calibration_records_submission_mode():
    evaluation_id = fresh_id()
    payload = {
        "design_strength_mpa": 30.0,
        "evaluation_id": evaluation_id,
        "specimens": [{"area_mm2": 22500, "load_kn": load} for load in [700, 720, 710]],
    }
    first = evaluate(payload)
    assert first.status_code == 200
    assert "applied_calibration_factor" not in first.json()

    body = get_record(evaluation_id).json()
    assert body["snapshot_available"] is True
    # Factor omitted: the record shows the neutral factor actually used and
    # marks it as not explicitly submitted.
    assert body["calibration_explicit"] is False
    assert body["calibration_factor"] == 1
    assert "applied_calibration_factor" not in body["result"]


def test_query_unknown_id_returns_404_without_fabricated_record():
    response = get_record(fresh_id())
    assert response.status_code == 404
    body = response.json()
    assert "detail" in body
    # A 404 carries no verdict-shaped placeholder.
    assert "result" not in body
    assert "snapshot_available" not in body
    assert "created_at" not in body


def test_anonymous_request_is_never_persisted_and_not_queryable():
    # A request without evaluation_id computes immediately and writes
    # nothing, so there is no record to query afterwards.
    payload = {
        "design_strength_mpa": 30.0,
        "specimens": [{"area_mm2": 22500, "load_kn": load} for load in [700, 720, 710]],
    }
    response = evaluate(payload)
    assert response.status_code == 200
    assert "replayed" not in response.json()
    assert get_record(fresh_id()).status_code == 404


def test_conflict_does_not_rewrite_record_time_or_snapshot():
    evaluation_id = fresh_id()
    original = make_flow_payload(evaluation_id)
    first = evaluate(original)
    assert first.status_code == 200
    before = get_record(evaluation_id).json()

    conflicting = make_flow_payload(evaluation_id)
    conflicting["specimens"] = [
        {"width_mm": 150, "depth_mm": 150, "load_kn": 700},
        {"width_mm": 150, "depth_mm": 150, "load_kn": 720},
        {"width_mm": 150, "depth_mm": 150, "load_kn": 710},
    ]
    conflict = evaluate(conflicting)
    assert conflict.status_code == 409

    after = get_record(evaluation_id).json()
    assert after == before
    assert after["specimens"] == before["specimens"]

    # The original input still replays the first result.
    replay = evaluate(original)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_query_content_is_unchanged_across_service_restart(tmp_path):
    # Real HTTP against two consecutive server processes sharing one ledger
    # file: the queried record must be identical after the restart.
    db_path = tmp_path / "evaluations.db"
    evaluation_id = fresh_id()
    payload = make_flow_payload(evaluation_id)

    with run_server(db_path) as base_url:
        first = evaluate(payload, base_url)
        assert first.status_code == 200
        assert first.json()["replayed"] is False
        record_before = get_record(evaluation_id, base_url)
        assert record_before.status_code == 200

    with run_server(db_path) as base_url:
        record_after = get_record(evaluation_id, base_url)
        assert record_after.status_code == 200
        assert record_after.json() == record_before.json()
        # The ledger itself survived too: the same submission replays.
        replay = evaluate(payload, base_url)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True


def test_legacy_table_migrates_and_stays_queryable(tmp_path):
    # Pre-seed a ledger in the pre-snapshot schema (no request_snapshot
    # column), then boot the service on it: the compatible migration must
    # keep the old record queryable by id, time and verdict.
    db_path = tmp_path / "legacy.db"
    evaluation_id = "legacy-batch-0001"
    fingerprint = store.build_fingerprint(
        Decimal("30.0"),
        [(Decimal(22500), Decimal(load)) for load in (700, 720, 710)],
        Decimal("1"),
        False,
    )
    response_json = (
        '{"strengths_mpa":[31.1,32.0,31.6],"mean_strength_mpa":31.6,'
        '"passed":true,"reasons":[]}'
    )
    created_at = "2026-01-01T00:00:00.000Z"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE evaluations ("
        " evaluation_id TEXT PRIMARY KEY,"
        " fingerprint TEXT NOT NULL,"
        " response_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO evaluations (evaluation_id, fingerprint, response_json, created_at)"
        " VALUES (?, ?, ?, ?)",
        (evaluation_id, fingerprint, response_json, created_at),
    )
    connection.commit()
    connection.close()

    with run_server(db_path) as base_url:
        record = get_record(evaluation_id, base_url)
        assert record.status_code == 200
        body = record.json()
        assert body["evaluation_id"] == evaluation_id
        assert body["created_at"] == created_at
        # The old row has no snapshot: id, time and verdict are queryable,
        # but the original input is reported as not reconstructable.
        assert body["snapshot_available"] is False
        assert "design_strength_mpa" not in body
        assert "specimens" not in body
        assert "calibration_factor" not in body
        assert "calibration_explicit" not in body
        assert body["result"] == {
            "strengths_mpa": [31.1, 32.0, 31.6],
            "mean_strength_mpa": 31.6,
            "passed": True,
            "reasons": [],
        }

        # The migrated ledger still protects the old record: the matching
        # input replays it, and the record is not rewritten.
        payload = {
            "design_strength_mpa": 30.0,
            "evaluation_id": evaluation_id,
            "specimens": [
                {"area_mm2": 22500, "load_kn": load} for load in [700, 720, 710]
            ],
        }
        replay = evaluate(payload, base_url)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert get_record(evaluation_id, base_url).json() == body

        # New writes on the migrated database store snapshots as usual.
        new_id = fresh_id()
        new_payload = make_flow_payload(new_id)
        assert evaluate(new_payload, base_url).status_code == 200
        new_record = get_record(new_id, base_url).json()
        assert new_record["snapshot_available"] is True
        assert new_record["specimens"][0] == {"area_mm2": 20000, "load_kn": 660}


def test_queried_verdict_preserves_extreme_values_exactly():
    # Strengths beyond float64 precision must survive snapshot + retrieval
    # digit-for-digit, like they do on the replay path.
    evaluation_id = fresh_id()
    payload = {
        "design_strength_mpa": 30.0,
        "evaluation_id": evaluation_id,
        "specimens": [{"area_mm2": 22500, "load_kn": 1e30}] * 3,
    }
    first = evaluate(payload)
    assert first.status_code == 200

    record = get_record(evaluation_id)
    assert record.status_code == 200
    body = json.loads(record.text, parse_float=Decimal)
    expected = Decimal("44444444444444444444444444444.4")
    assert body["result"]["strengths_mpa"] == [expected, expected, expected]
    assert body["result"]["mean_strength_mpa"] == expected
