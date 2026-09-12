"""End-to-end tests for evaluation_id idempotency, over real HTTP.

Run with the API reachable at API_BASE_URL (default http://localhost:8000),
same as test_api.py. Every test mints fresh evaluation_id values (uuid4),
so the suite is isolated from whatever ledger the server already holds and
can run repeatedly against the same database file.
"""

import json
import os
import time
import uuid
from decimal import Decimal

import httpx
import pytest

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
AREA_MM2 = 22500  # 150 mm cube face


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


def make_payload(design_strength, loads, area=AREA_MM2):
    return {
        "design_strength_mpa": design_strength,
        "specimens": [{"area_mm2": area, "load_kn": load} for load in loads],
    }


def make_dimensions_payload(design_strength, loads, width=150.0, depth=150.0):
    return {
        "design_strength_mpa": design_strength,
        "specimens": [
            {"width_mm": width, "depth_mm": depth, "load_kn": load} for load in loads
        ],
    }


def evaluate(payload):
    return httpx.post(f"{BASE_URL}/evaluate", json=payload, timeout=10)


def without_replayed(body):
    return {key: value for key, value in body.items() if key != "replayed"}


def test_first_submission_is_saved_then_replayed_identically():
    payload = make_payload(30.0, [700, 720, 710])
    payload["evaluation_id"] = fresh_id()

    first = evaluate(payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["replayed"] is False
    assert first_body["strengths_mpa"] == [31.1, 32.0, 31.6]
    assert first_body["mean_strength_mpa"] == 31.6
    assert first_body["passed"] is True

    replay = evaluate(payload)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    # Apart from the replayed flag the replayed body is the first result.
    assert without_replayed(replay_body) == without_replayed(first_body)


def test_numerically_equal_representations_are_the_same_request():
    evaluation_id = fresh_id()
    first_payload = make_payload(30.0, [700, 720, 710])
    first_payload["evaluation_id"] = evaluation_id
    first = evaluate(first_payload)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    # Same Decimal values, different JSON representations: "30.00" string
    # for the design strength (batch-level numerics stay
    # representation-flexible), 22500.0 for the area, exponent notation.
    # Specimen numerics themselves must be JSON numbers — string forms
    # like "22500" are rejected with 422.
    equivalent_payload = {
        "design_strength_mpa": "30.00",
        "evaluation_id": evaluation_id,
        "specimens": [
            {"area_mm2": 22500.0, "load_kn": 7.0E2},
            {"area_mm2": 22500.00, "load_kn": 720.0},
            {"area_mm2": 2.25e4, "load_kn": 710.000},
        ],
    }
    replay = evaluate(equivalent_payload)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    assert without_replayed(replay_body) == without_replayed(first.json())


def test_equivalent_area_and_dimensions_replay_first_result():
    # First submission enters the loaded face as an area; the retry enters
    # the same face as width/depth. Normalization to effective area makes
    # them the same business input, so the retry replays instead of raising
    # a false 409 conflict.
    evaluation_id = fresh_id()
    first_payload = make_payload(30.0, [800, 800, 500])  # failing batch
    first_payload["evaluation_id"] = evaluation_id
    first = evaluate(first_payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["replayed"] is False
    assert first_body["passed"] is False
    assert first_body["reasons"] == ["MIN_BELOW_85_PERCENT"]

    dimensions_payload = make_dimensions_payload(30.0, [800, 800, 500])
    dimensions_payload["evaluation_id"] = evaluation_id
    replay = evaluate(dimensions_payload)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    assert without_replayed(replay_body) == without_replayed(first_body)


def test_equivalent_area_and_dimensions_replay_in_either_order():
    # Same contract in reverse: dimensions first, equivalent area retry.
    evaluation_id = fresh_id()
    first_payload = make_dimensions_payload(30.0, [700, 720, 710])
    first_payload["evaluation_id"] = evaluation_id
    first = evaluate(first_payload)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    area_payload = make_payload(30.0, [700, 720, 710])
    area_payload["evaluation_id"] = evaluation_id
    replay = evaluate(area_payload)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    assert without_replayed(replay_body) == without_replayed(first.json())


def test_non_equivalent_dimensions_under_same_id_still_conflict():
    # Conflict protection survives normalization: a different face size is
    # a different business input even in dimensions form.
    evaluation_id = fresh_id()
    first_payload = make_dimensions_payload(30.0, [700, 720, 710], width=150.0, depth=150.0)
    first_payload["evaluation_id"] = evaluation_id
    first = evaluate(first_payload)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    conflicting_payload = make_dimensions_payload(
        30.0, [700, 720, 710], width=100.0, depth=200.0
    )
    conflicting_payload["evaluation_id"] = evaluation_id
    conflict = evaluate(conflicting_payload)
    assert conflict.status_code == 409
    assert conflict.json()["evaluation_id"] == evaluation_id

    # The first result still replays under its original (equivalent) input.
    replay = evaluate(first_payload)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_invalid_dimensions_with_id_do_not_occupy_the_identifier():
    # A half-entered face (width without depth), a mixed expression and a
    # non-positive dimension all fail with 422 *before* the ledger is
    # written; the same id then works as a genuine first submission.
    evaluation_id = fresh_id()

    missing_depth = make_dimensions_payload(30.0, [700, 720, 710])
    del missing_depth["specimens"][0]["depth_mm"]
    missing_depth["evaluation_id"] = evaluation_id
    rejected = evaluate(missing_depth)
    assert rejected.status_code == 422

    mixed = make_dimensions_payload(30.0, [700, 720, 710])
    mixed["specimens"][1]["area_mm2"] = 22500
    mixed["evaluation_id"] = evaluation_id
    also_rejected = evaluate(mixed)
    assert also_rejected.status_code == 422

    zero_width = make_dimensions_payload(30.0, [700, 720, 710])
    zero_width["specimens"][2]["width_mm"] = 0
    zero_width["evaluation_id"] = evaluation_id
    still_rejected = evaluate(zero_width)
    assert still_rejected.status_code == 422

    valid_payload = make_dimensions_payload(30.0, [700, 720, 710])
    valid_payload["evaluation_id"] = evaluation_id
    first = evaluate(valid_payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["replayed"] is False
    assert first_body["strengths_mpa"] == [31.1, 32.0, 31.6]

    # Replay through the numerically equivalent area expression.
    equivalent_area_payload = make_payload(30.0, [700, 720, 710])
    equivalent_area_payload["evaluation_id"] = evaluation_id
    replay = evaluate(equivalent_area_payload)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_conflicting_input_returns_409_and_keeps_original_record():
    evaluation_id = fresh_id()
    original_payload = make_payload(30.0, [700, 720, 710])
    original_payload["evaluation_id"] = evaluation_id
    first = evaluate(original_payload)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    conflicting_payload = make_payload(30.0, [800, 800, 500])
    conflicting_payload["evaluation_id"] = evaluation_id
    conflict = evaluate(conflicting_payload)
    assert conflict.status_code == 409
    conflict_body = conflict.json()
    assert "evaluation_id" in conflict_body["detail"]
    assert conflict_body["evaluation_id"] == evaluation_id
    # A conflict carries no verdict fields.
    assert "strengths_mpa" not in conflict_body
    assert "passed" not in conflict_body

    # The original record survived the conflict: the first input still
    # replays the first result.
    replay = evaluate(original_payload)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    assert without_replayed(replay_body) == without_replayed(first.json())


def test_invalid_request_with_id_leaves_no_placeholder_record():
    evaluation_id = fresh_id()
    invalid_payload = make_payload(30.0, [700, 720, 710])
    invalid_payload["calibration_factor"] = 1.2  # outside 0.9500-1.0500
    invalid_payload["evaluation_id"] = evaluation_id
    rejected = evaluate(invalid_payload)
    assert rejected.status_code == 422

    invalid_specimen_payload = make_payload(30.0, [0, 720, 710])
    invalid_specimen_payload["evaluation_id"] = evaluation_id
    also_rejected = evaluate(invalid_specimen_payload)
    assert also_rejected.status_code == 422

    # The id was never recorded: a later valid request is a first
    # submission, not a replay and not a conflict.
    valid_payload = make_payload(30.0, [700, 720, 710])
    valid_payload["evaluation_id"] = evaluation_id
    first = evaluate(valid_payload)
    assert first.status_code == 200
    assert first.json()["replayed"] is False


def test_same_input_under_different_ids_is_stored_independently():
    loads = [700, 720, 710]
    first_payload = make_payload(30.0, loads)
    first_payload["evaluation_id"] = fresh_id()
    second_payload = make_payload(30.0, loads)
    second_payload["evaluation_id"] = fresh_id()

    first = evaluate(first_payload)
    second = evaluate(second_payload)
    assert first.status_code == 200
    assert second.status_code == 200
    # Each id sees its own first submission; neither is a replay.
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is False
    assert without_replayed(first.json()) == without_replayed(second.json())


def test_explicit_neutral_factor_differs_from_omitted_factor():
    # An explicit calibration_factor echoes applied_calibration_factor in
    # the response, so it is a different business input than omitting it,
    # even though both compute with factor 1.
    evaluation_id = fresh_id()
    plain_payload = make_payload(30.0, [700, 720, 710])
    plain_payload["evaluation_id"] = evaluation_id
    first = evaluate(plain_payload)
    assert first.status_code == 200
    assert "applied_calibration_factor" not in first.json()

    explicit_payload = make_payload(30.0, [700, 720, 710])
    explicit_payload["calibration_factor"] = 1.0
    explicit_payload["evaluation_id"] = evaluation_id
    conflict = evaluate(explicit_payload)
    assert conflict.status_code == 409


def test_calibrated_request_replays_with_calibration_trace():
    evaluation_id = fresh_id()
    payload = make_payload(30.0, [660, 670, 680])
    payload["calibration_factor"] = 1.01
    payload["evaluation_id"] = evaluation_id

    first = evaluate(payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["replayed"] is False
    assert first_body["applied_calibration_factor"] == 1.01
    assert first_body["passed"] is True

    replay = evaluate(payload)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    assert replay_body["applied_calibration_factor"] == 1.01
    assert without_replayed(replay_body) == without_replayed(first_body)


def test_replay_preserves_extreme_values_exactly():
    # Strengths beyond float64 precision must survive the SQLite round trip
    # digit-for-digit; a float parse of the stored body would truncate them.
    evaluation_id = fresh_id()
    payload = make_payload(30.0, [1e30, 1e30, 1e30])
    payload["evaluation_id"] = evaluation_id

    first = evaluate(payload)
    assert first.status_code == 200
    replay = evaluate(payload)
    assert replay.status_code == 200

    replay_body = json.loads(replay.text, parse_float=Decimal)
    expected = Decimal("44444444444444444444444444444.4")
    assert replay_body["replayed"] is True
    assert replay_body["strengths_mpa"] == [expected, expected, expected]
    assert replay_body["mean_strength_mpa"] == expected
    # Byte-level: only the replayed flag may differ from the first response.
    first_body = json.loads(first.text, parse_float=Decimal)
    assert without_replayed(replay_body) == without_replayed(first_body)


def test_anonymous_requests_stay_computation_only_and_untracked():
    # No evaluation_id: no replayed field, and nothing is recorded — the
    # same business input can still be registered under a fresh id.
    anonymous_payload = make_payload(30.0, [800, 800, 500])
    anonymous = evaluate(anonymous_payload)
    assert anonymous.status_code == 200
    anonymous_body = anonymous.json()
    assert "replayed" not in anonymous_body
    assert anonymous_body["passed"] is False
    assert anonymous_body["reasons"] == ["MIN_BELOW_85_PERCENT"]

    identified_payload = make_payload(30.0, [800, 800, 500])
    identified_payload["evaluation_id"] = fresh_id()
    identified = evaluate(identified_payload)
    assert identified.status_code == 200
    identified_body = identified.json()
    # First submission for this id, unaffected by the anonymous call.
    assert identified_body["replayed"] is False
    assert without_replayed(identified_body) == anonymous_body


def test_invalid_evaluation_id_returns_422():
    for bad_id in ("", "   ", 123, {"nested": True}):
        payload = make_payload(30.0, [700, 720, 710])
        payload["evaluation_id"] = bad_id
        response = evaluate(payload)
        assert response.status_code == 422, f"evaluation_id={bad_id!r}"
        locations = [error["loc"] for error in response.json()["detail"]]
        assert any(loc[-1] == "evaluation_id" for loc in locations)
