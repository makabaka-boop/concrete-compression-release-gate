"""End-to-end tests against the live HTTP API.

Run with the API reachable at API_BASE_URL (default http://localhost:8000).
All calculations are verified through the real HTTP chain, never by
stubbing the application.
"""

import json
import os
import time
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


def make_payload(design_strength, loads, area=AREA_MM2):
    return {
        "design_strength_mpa": design_strength,
        "specimens": [{"area_mm2": area, "load_kn": load} for load in loads],
    }


def evaluate(payload):
    return httpx.post(f"{BASE_URL}/evaluate", json=payload, timeout=10)


def test_health_endpoint():
    response = httpx.get(f"{BASE_URL}/health", timeout=5)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_pass_when_mean_and_minimum_meet_design():
    # 700/720/710 kN on 22500 mm² -> 31.1 / 32.0 / 31.6 MPa, mean 31.6 MPa
    response = evaluate(make_payload(30.0, [700, 720, 710]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [31.1, 32.0, 31.6]
    assert body["mean_strength_mpa"] == 31.6
    assert body["passed"] is True
    assert body["reasons"] == []


def test_average_passes_but_low_outlier_fails_batch():
    # Headline scenario: mean 31.1 MPa >= 30.0 hides a 22.2 MPa outlier
    # below 85.0% of design (25.5 MPa).
    response = evaluate(make_payload(30.0, [800, 800, 500]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [35.6, 35.6, 22.2]
    assert body["mean_strength_mpa"] == 31.1
    assert body["passed"] is False
    assert body["reasons"] == ["MIN_BELOW_85_PERCENT"]


def test_mean_below_design_only():
    # 29.3 / 29.8 / 30.2 MPa, mean 29.8 < 30.0; minimum 29.3 >= 25.5
    response = evaluate(make_payload(30.0, [660, 670, 680]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [29.3, 29.8, 30.2]
    assert body["mean_strength_mpa"] == 29.8
    assert body["passed"] is False
    assert body["reasons"] == ["MEAN_BELOW_DESIGN"]


def test_both_conditions_fail_in_fixed_order():
    # mean 23.7 < 30.0 and minimum 17.8 < 25.5
    response = evaluate(make_payload(30.0, [600, 600, 400]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [26.7, 26.7, 17.8]
    assert body["mean_strength_mpa"] == 23.7
    assert body["passed"] is False
    assert body["reasons"] == ["MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT"]


def test_threshold_equality_counts_as_pass():
    # Strengths exactly 25.5 / 32.0 / 32.5 MPa: mean exactly 30.0 MPa and
    # minimum exactly 85.0% of design (25.5 MPa) must both pass.
    response = evaluate(make_payload(30.0, [573.75, 720, 731.25]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [25.5, 32.0, 32.5]
    assert body["mean_strength_mpa"] == 30.0
    assert body["passed"] is True
    assert body["reasons"] == []


def test_strength_rounding_is_half_up_not_bankers():
    # 707.625 kN / 22500 mm² = 31.45 MPa exactly; ROUND_HALF_UP -> 31.5
    # (ROUND_HALF_EVEN would give 31.4).
    response = evaluate(make_payload(30.0, [707.625, 720, 720]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [31.5, 32.0, 32.0]
    assert body["mean_strength_mpa"] == 31.8
    assert body["passed"] is True


def test_mean_is_computed_from_rounded_strengths_then_rounded():
    # 31.1 / 31.2 / 31.2 MPa -> sum 93.5 -> mean 31.1666... -> 31.2 MPa
    response = evaluate(make_payload(30.0, [699.75, 702, 702]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [31.1, 31.2, 31.2]
    assert body["mean_strength_mpa"] == 31.2
    assert body["passed"] is True


def test_design_strength_scales_the_85_percent_threshold():
    # Design 40.0 MPa -> 85% threshold 34.0 MPa; minimum 33.8 fails it
    # while mean 34.1 >= 40.0 is false too, so both reasons appear.
    response = evaluate(make_payload(40.0, [900, 900, 760]))
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [40.0, 40.0, 33.8]
    assert body["mean_strength_mpa"] == 37.9
    assert body["passed"] is False
    assert body["reasons"] == ["MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT"]


def test_calibration_factor_turns_borderline_batch_into_pass():
    # Uncalibrated: 29.3 / 29.8 / 30.2 MPa, mean 29.8 < 30.0 -> fail.
    # Factor 1.01 scales every load first: 29.6 / 30.1 / 30.5, mean 30.1.
    payload = make_payload(30.0, [660, 670, 680])
    payload["calibration_factor"] = 1.01
    response = evaluate(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [29.6, 30.1, 30.5]
    assert body["mean_strength_mpa"] == 30.1
    assert body["passed"] is True
    assert body["reasons"] == []
    assert body["applied_calibration_factor"] == 1.01


def test_calibration_lower_boundary_factor_is_accepted():
    # Factor 0.9500 (lower bound): 29.6 / 30.4 / 30.0 MPa, mean exactly
    # 30.0 -> threshold equality still passes.
    payload = make_payload(30.0, [700, 720, 710])
    payload["calibration_factor"] = 0.95
    response = evaluate(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [29.6, 30.4, 30.0]
    assert body["mean_strength_mpa"] == 30.0
    assert body["passed"] is True
    assert body["reasons"] == []
    assert body["applied_calibration_factor"] == 0.95


def test_calibration_upper_boundary_factor_is_accepted():
    # Factor 1.0500 (upper bound): 30.8 / 31.3 / 31.7 MPa, mean 31.3.
    payload = make_payload(30.0, [660, 670, 680])
    payload["calibration_factor"] = 1.05
    response = evaluate(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [30.8, 31.3, 31.7]
    assert body["mean_strength_mpa"] == 31.3
    assert body["passed"] is True
    assert body["reasons"] == []
    assert body["applied_calibration_factor"] == 1.05


def test_calibrated_load_is_not_rounded_before_strength():
    # 700.62 kN * 1.01 = 707.6262 kN -> 31.45005... MPa -> 31.5 MPa.
    # Rounding the calibrated load to 0.1 kN (707.6) would give 31.4 MPa.
    payload = make_payload(30.0, [700.62, 720, 720])
    payload["calibration_factor"] = 1.01
    response = evaluate(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [31.5, 32.3, 32.3]
    assert body["mean_strength_mpa"] == 32.0
    assert body["passed"] is True
    assert body["applied_calibration_factor"] == 1.01


def test_explicit_neutral_factor_still_echoes_applied_factor():
    # Factor 1.0 changes nothing numerically but was explicitly provided,
    # so the response must carry applied_calibration_factor.
    payload = make_payload(30.0, [700, 720, 710])
    payload["calibration_factor"] = 1.0
    response = evaluate(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [31.1, 32.0, 31.6]
    assert body["mean_strength_mpa"] == 31.6
    assert body["passed"] is True
    assert body["applied_calibration_factor"] == 1.0


def test_legacy_request_without_calibration_is_fully_compatible():
    # Same payload as before the contract extension: identical values and
    # exactly the original four response fields, no calibration trace.
    response = evaluate(make_payload(30.0, [700, 720, 710]))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"strengths_mpa", "mean_strength_mpa", "passed", "reasons"}
    assert body["strengths_mpa"] == [31.1, 32.0, 31.6]
    assert body["mean_strength_mpa"] == 31.6
    assert body["passed"] is True
    assert body["reasons"] == []


def test_extremely_large_loads_return_complete_verdict():
    # 1e30 kN is legal (positive) but each strength 1e30*1000/22500 MPa has
    # 30 significant digits — beyond the default 28-digit decimal context;
    # the verdict must still be computed exactly instead of failing with an
    # internal error.
    response = evaluate(make_payload(30.0, [1e30, 1e30, 1e30]))
    assert response.status_code == 200
    body = json.loads(response.text, parse_float=Decimal)
    expected = Decimal("44444444444444444444444444444.4")
    assert body["strengths_mpa"] == [expected, expected, expected]
    assert body["mean_strength_mpa"] == expected
    assert body["passed"] is True
    assert body["reasons"] == []


def test_extremely_small_areas_return_complete_verdict():
    # 1e-25 mm² is legal (positive) but each strength tops 1e30 MPa — beyond
    # the default 28-digit decimal context; the verdict must still be
    # computed exactly instead of failing with an internal error.
    response = evaluate(make_payload(30.0, [700, 720, 710], area=1e-25))
    assert response.status_code == 200
    body = json.loads(response.text, parse_float=Decimal)
    assert body["strengths_mpa"] == [
        Decimal("7000000000000000000000000000000.0"),
        Decimal("7200000000000000000000000000000.0"),
        Decimal("7100000000000000000000000000000.0"),
    ]
    assert body["mean_strength_mpa"] == Decimal("7100000000000000000000000000000.0")
    assert body["passed"] is True
    assert body["reasons"] == []


def test_extreme_values_still_apply_release_criteria():
    # Design 1e29 MPa: mean 3.1e28 < design and minimum 4.4e27 < 85.0% of
    # design, so both reasons appear in the fixed order.
    response = evaluate(make_payload(1e29, [1e30, 1e30, 1e29]))
    assert response.status_code == 200
    body = json.loads(response.text, parse_float=Decimal)
    assert body["strengths_mpa"] == [
        Decimal("44444444444444444444444444444.4"),
        Decimal("44444444444444444444444444444.4"),
        Decimal("4444444444444444444444444444.4"),
    ]
    assert body["mean_strength_mpa"] == Decimal("31111111111111111111111111111.1")
    assert body["passed"] is False
    assert body["reasons"] == ["MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT"]


def test_high_precision_calibration_factor_is_echoed_exactly():
    # 17 significant digits fit the 0.9500-1.0500 range but not a float64;
    # the applied factor must be echoed exactly, not collapsed to 1.0.
    payload = make_payload(30.0, [700, 720, 710])
    payload["calibration_factor"] = "1.0000000000000001"
    response = evaluate(payload)
    assert response.status_code == 200
    body = json.loads(response.text, parse_float=Decimal)
    assert body["applied_calibration_factor"] == Decimal("1.0000000000000001")


def test_high_precision_calibration_factor_is_applied_exactly():
    # 707.62499999999999775 kN / 22500 mm² = 31.449999... MPa -> 31.4 MPa.
    # Multiplying by 1.0000000000000001 first lifts the strength just past
    # the 31.45 MPa rounding boundary -> 31.5 MPa.
    uncalibrated = evaluate(make_payload(30.0, ["707.62499999999999775", 720, 720]))
    assert uncalibrated.status_code == 200
    assert uncalibrated.json()["strengths_mpa"] == [31.4, 32.0, 32.0]

    payload = make_payload(30.0, ["707.62499999999999775", 720, 720])
    payload["calibration_factor"] = "1.0000000000000001"
    response = evaluate(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["strengths_mpa"] == [31.5, 32.0, 32.0]
    assert body["mean_strength_mpa"] == 31.8


INVALID_CALIBRATION_FACTORS = {
    "factor_below_range": 0.9499,
    "factor_above_range": 1.0501,
    "factor_non_numeric": "abc",
    "factor_explicit_null": None,
}


@pytest.mark.parametrize(
    "factor", INVALID_CALIBRATION_FACTORS.values(), ids=INVALID_CALIBRATION_FACTORS.keys()
)
def test_invalid_calibration_factor_returns_422_without_partial_results(factor):
    payload = make_payload(30.0, [700, 720, 710])
    payload["calibration_factor"] = factor
    response = evaluate(payload)
    assert response.status_code == 422
    body = response.json()
    assert "strengths_mpa" not in body
    assert "mean_strength_mpa" not in body
    assert "passed" not in body
    assert "applied_calibration_factor" not in body
    locations = [error["loc"] for error in body["detail"]]
    assert any(loc[-1] == "calibration_factor" for loc in locations)


INVALID_PAYLOADS = {
    "missing_design_strength": {
        "specimens": [{"area_mm2": 22500, "load_kn": 700}] * 3,
    },
    "missing_specimens": {"design_strength_mpa": 30.0},
    "specimen_missing_load": {
        "design_strength_mpa": 30.0,
        "specimens": [
            {"area_mm2": 22500, "load_kn": 700},
            {"area_mm2": 22500},
            {"area_mm2": 22500, "load_kn": 710},
        ],
    },
    "two_specimens": {
        "design_strength_mpa": 30.0,
        "specimens": [{"area_mm2": 22500, "load_kn": 700}] * 2,
    },
    "four_specimens": {
        "design_strength_mpa": 30.0,
        "specimens": [{"area_mm2": 22500, "load_kn": 700}] * 4,
    },
    "zero_load": {
        "design_strength_mpa": 30.0,
        "specimens": [
            {"area_mm2": 22500, "load_kn": 0},
            {"area_mm2": 22500, "load_kn": 700},
            {"area_mm2": 22500, "load_kn": 710},
        ],
    },
    "negative_area": {
        "design_strength_mpa": 30.0,
        "specimens": [
            {"area_mm2": -22500, "load_kn": 700},
            {"area_mm2": 22500, "load_kn": 700},
            {"area_mm2": 22500, "load_kn": 710},
        ],
    },
    "zero_design_strength": {
        "design_strength_mpa": 0,
        "specimens": [{"area_mm2": 22500, "load_kn": 700}] * 3,
    },
    "non_numeric_area": {
        "design_strength_mpa": 30.0,
        "specimens": [
            {"area_mm2": "abc", "load_kn": 700},
            {"area_mm2": 22500, "load_kn": 700},
            {"area_mm2": 22500, "load_kn": 710},
        ],
    },
}


@pytest.mark.parametrize("payload", INVALID_PAYLOADS.values(), ids=INVALID_PAYLOADS.keys())
def test_invalid_payloads_return_422_without_partial_strengths(payload):
    response = evaluate(payload)
    assert response.status_code == 422
    body = response.json()
    assert "strengths_mpa" not in body
    assert "mean_strength_mpa" not in body
    assert "passed" not in body


def test_non_finite_number_returns_422_not_internal_error():
    # 1e309 overflows float64 when the server parses the JSON body; the
    # resulting non-finite value is not a legal positive number, so the
    # contractual 422 (not a 500) must come back.
    response = httpx.post(
        f"{BASE_URL}/evaluate",
        content=(
            '{"design_strength_mpa": 30.0, "specimens": ['
            '{"area_mm2": 22500, "load_kn": 1e309},'
            '{"area_mm2": 22500, "load_kn": 700},'
            '{"area_mm2": 22500, "load_kn": 710}]}'
        ),
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    assert response.status_code == 422
    body = response.json()
    assert "strengths_mpa" not in body
    assert "mean_strength_mpa" not in body
    assert "passed" not in body
    locations = [error["loc"] for error in body["detail"]]
    assert any(loc[-1] == "load_kn" for loc in locations)
