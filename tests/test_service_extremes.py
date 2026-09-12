"""Unit tests for extreme-magnitude and high-precision evaluation paths.

The extreme scenarios were previously exercised over HTTP with numeric
strings (e.g. ``"1e1000000"`` dimensions, ``"707.62499999999999775"``
loads). The request contract now requires specimen numerics to be JSON
numbers, and float64 cannot carry such magnitudes or precision, so these
guarantees of the normalization and adjudication layers are verified
directly against the Decimal service.
"""

from decimal import Decimal

from app.schemas import Specimen
from app.service import SpecimenInput, evaluate_batch

DESIGN_30 = Decimal("30.0")


def evaluate_dimensions(width: str, depth: str, loads=(700, 720, 710)):
    """Evaluate a batch whose faces are given as exact Decimal dimensions."""
    specimens = [
        SpecimenInput(
            area_mm2=Specimen(
                width_mm=Decimal(width), depth_mm=Decimal(depth), load_kn=load
            ).effective_area_mm2,
            load_kn=Decimal(load),
        )
        for load in loads
    ]
    return evaluate_batch(DESIGN_30, specimens)


def test_extremely_large_dimensions_return_complete_verdict():
    # 1e1000000 mm faces are legal (positive) but their product 1e2000000
    # mm² overflows the default decimal context's exponent range (Emax
    # 999999); the area conversion must stay exact and the batch must get a
    # complete verdict instead of an internal error. 700 kN on a 1e2000000
    # mm² face is effectively 0 MPa, so the batch fails on both criteria.
    result = evaluate_dimensions("1e1000000", "1e1000000")
    assert result.strengths_mpa == (Decimal("0.0"),) * 3
    assert result.mean_strength_mpa == Decimal("0.0")
    assert result.passed is False
    assert result.reasons == ("MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT")


def test_dimensions_beyond_decimal_exponent_limit_return_complete_verdict():
    # 1e999999999999999999 mm is the largest exponent a Decimal can hold;
    # the exact face product exceeds even the implementation's exponent
    # limit. The evaluation must still complete: an unrepresentably large
    # area legitimately yields zero strengths and a failing verdict.
    result = evaluate_dimensions("1e999999999999999999", "1e999999999999999999")
    assert result.strengths_mpa == (Decimal("0.0"),) * 3
    assert result.mean_strength_mpa == Decimal("0.0")
    assert result.passed is False
    assert result.reasons == ("MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT")


def test_extremely_small_dimensions_return_complete_verdict():
    # 1e-1000000 mm faces are legal (positive) but their product
    # 1e-2000000 mm² underflows the default decimal context's exponent
    # range (Emin -999999) to zero, turning the strength computation into
    # a division by zero; the verdict must still be computed exactly.
    # 700_000 N / 1e-2000000 mm² = 7e2000005 MPa, quantized to 0.1 MPa.
    result = evaluate_dimensions("1e-1000000", "1e-1000000")
    assert result.strengths_mpa == (
        Decimal("7" + "0" * 2000005 + ".0"),
        Decimal("72" + "0" * 2000004 + ".0"),
        Decimal("71" + "0" * 2000004 + ".0"),
    )
    assert result.mean_strength_mpa == Decimal("71" + "0" * 2000004 + ".0")
    assert result.passed is True
    assert result.reasons == ()


def test_dimensions_at_decimal_lower_limit_return_complete_verdict():
    # 1e-999999999999999999 mm is the smallest normal magnitude a Decimal
    # can hold; the exact face product (1e-1999999999999999998 mm²) and
    # the exact strengths exceed even the implementation's limits, so no
    # Decimal can represent them. The evaluation must still complete:
    # strengths saturate to +Infinity and the batch trivially passes.
    result = evaluate_dimensions("1e-999999999999999999", "1e-999999999999999999")
    assert result.strengths_mpa == (Decimal("Infinity"),) * 3
    assert result.mean_strength_mpa == Decimal("Infinity")
    assert result.passed is True
    assert result.reasons == ()


def test_dimensions_at_decimal_subnormal_limit_return_complete_verdict():
    # 1e-1999999999999999997 mm is the smallest positive Decimal of all
    # (the subnormal quantum); the same saturation contract applies.
    result = evaluate_dimensions("1e-1999999999999999997", "1e-1999999999999999997")
    assert result.strengths_mpa == (Decimal("Infinity"),) * 3
    assert result.mean_strength_mpa == Decimal("Infinity")
    assert result.passed is True
    assert result.reasons == ()


def test_saturated_specimen_does_not_corrupt_ordinary_siblings():
    # A batch mixing one unrepresentably strong specimen with two ordinary
    # ones: the ordinary strengths stay exact, the mean saturates, and the
    # minimum-single-value check still sees the ordinary strengths.
    saturated = Specimen(
        width_mm=Decimal("1e-999999999999999999"),
        depth_mm=Decimal("1e-999999999999999999"),
        load_kn=Decimal(700),
    )
    specimens = [
        SpecimenInput(area_mm2=saturated.effective_area_mm2, load_kn=Decimal(700)),
        SpecimenInput(area_mm2=Decimal(22500), load_kn=Decimal(720)),
        SpecimenInput(area_mm2=Decimal(22500), load_kn=Decimal(710)),
    ]
    result = evaluate_batch(DESIGN_30, specimens)
    assert result.strengths_mpa == (
        Decimal("Infinity"),
        Decimal("32.0"),
        Decimal("31.6"),
    )
    assert result.mean_strength_mpa == Decimal("Infinity")
    assert result.passed is True
    assert result.reasons == ()


def test_high_precision_calibration_factor_is_applied_exactly():
    # 707.62499999999999775 kN / 22500 mm² = 31.449999... MPa -> 31.4 MPa.
    # Multiplying by 1.0000000000000001 first lifts the strength just past
    # the 31.45 MPa rounding boundary -> 31.5 MPa. Both values exceed
    # float64 precision, so they are fed to the service as Decimals.
    loads = [Decimal("707.62499999999999775"), Decimal(720), Decimal(720)]
    specimens = [
        SpecimenInput(area_mm2=Decimal(22500), load_kn=load) for load in loads
    ]

    uncalibrated = evaluate_batch(DESIGN_30, specimens)
    assert uncalibrated.strengths_mpa == (
        Decimal("31.4"),
        Decimal("32.0"),
        Decimal("32.0"),
    )

    calibrated = evaluate_batch(
        DESIGN_30, specimens, calibration_factor=Decimal("1.0000000000000001")
    )
    assert calibrated.strengths_mpa == (
        Decimal("31.5"),
        Decimal("32.0"),
        Decimal("32.0"),
    )
    assert calibrated.mean_strength_mpa == Decimal("31.8")
