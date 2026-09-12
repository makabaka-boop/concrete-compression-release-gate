"""Core strength calculation and batch release decision.

All arithmetic uses Decimal. Strengths and the mean are quantized to
0.1 MPa with ROUND_HALF_UP; comparisons against the design strength and
the 85.0% minimum-single-value threshold are inclusive (equality passes).

An optional press calibration factor scales each specimen's load before
the strength rounding; the calibrated load itself is never rounded.
"""

from decimal import (
    MAX_EMAX,
    MAX_PREC,
    ROUND_HALF_UP,
    Decimal,
    DefaultContext,
    DivisionByZero,
    Overflow,
    localcontext,
)
from typing import NamedTuple, Sequence

MPA_RESOLUTION = Decimal("0.1")
MIN_SINGLE_RATIO = Decimal("0.85")
NO_CALIBRATION = Decimal("1")
DEFAULT_PRECISION = DefaultContext.prec
POSITIVE_INFINITY = Decimal("Infinity")

REASON_MEAN_BELOW_DESIGN = "MEAN_BELOW_DESIGN"
REASON_MIN_BELOW_85_PERCENT = "MIN_BELOW_85_PERCENT"


class SpecimenInput(NamedTuple):
    area_mm2: Decimal
    load_kn: Decimal


class EvaluationResult(NamedTuple):
    strengths_mpa: tuple[Decimal, ...]
    mean_strength_mpa: Decimal
    passed: bool
    reasons: tuple[str, ...]


def round_to_tenth(value: Decimal) -> Decimal:
    """Round to 0.1 MPa using ROUND_HALF_UP.

    quantize() raises InvalidOperation when the rounded result needs more
    significant digits than the context precision (default 28) provides —
    e.g. strengths from extremely large loads or tiny areas. Widen the
    precision to the value's magnitude so any finite value rounds cleanly.
    The exponent range is likewise widened from the default ±999999 to the
    implementation's hard limits, so extreme-but-representable strengths
    (e.g. from 1e-1000000 mm² faces) quantize instead of overflowing.
    Non-finite values (strengths saturated beyond the exponent limit) have
    nothing to round and pass through unchanged.
    """
    if not value.is_finite():
        return value
    with localcontext() as ctx:
        ctx.prec = max(ctx.prec, value.adjusted() + 2)
        ctx.Emax = MAX_EMAX
        ctx.Emin = -MAX_EMAX
        return value.quantize(MPA_RESOLUTION, rounding=ROUND_HALF_UP)


def specimen_strength_mpa(load_kn: Decimal, area_mm2: Decimal) -> Decimal:
    """Single specimen strength: load_kn * 1000 / area_mm2, rounded to 0.1 MPa.

    When the exact strength's exponent exceeds the decimal
    implementation's hard limit (e.g. a 700 kN load on a
    1e-999999999999999999 mm x 1e-999999999999999999 mm face), no Decimal
    can represent it; the strength saturates to +Infinity — every input
    is positive, so the quotient can only grow without bound — and the
    batch still produces a complete verdict. The division itself runs
    with Overflow and DivisionByZero untrapped for the same reason: a
    zero effective area can only arise from an unrepresentably tiny
    width x depth product, and a boundary quotient can only overflow
    upwards. Precision is sized per specimen so one extreme specimen
    cannot inflate (or exhaust) the precision of its ordinary siblings.
    """
    if (
        load_kn.is_finite()
        and area_mm2.is_finite()
        and load_kn.adjusted() + 3 - area_mm2.adjusted() > MAX_EMAX + 1
    ):
        return POSITIVE_INFINITY
    with localcontext() as ctx:
        ctx.prec = min(
            max(
                DEFAULT_PRECISION,
                load_kn.adjusted()
                + 3
                - area_mm2.adjusted()
                + len(load_kn.as_tuple().digits)
                + len(area_mm2.as_tuple().digits)
                + 10,
            ),
            MAX_PREC,
        )
        ctx.Emax = MAX_EMAX
        ctx.Emin = -MAX_EMAX
        ctx.traps[Overflow] = False
        ctx.traps[DivisionByZero] = False
        return round_to_tenth(load_kn * 1000 / area_mm2)


def _required_precision(
    design_strength_mpa: Decimal,
    specimens: Sequence[SpecimenInput],
    calibration_factor: Decimal,
) -> int:
    """Context precision that keeps extreme-but-legal magnitudes exact.

    The default 28-digit context cannot even represent strengths with more
    than 28 significant digits (huge loads, tiny areas), and divisions such
    as sum/3 need spare digits beyond the 0.1 MPa place. The bound below
    covers the largest possible result magnitude (a strength is at most
    load * 1000 / area, so its most significant digit sits at
    adjusted(load) + 3 - adjusted(area)) plus every significant input digit
    plus a safety margin, so rounding at 0.1 MPa stays exact.
    """
    operands = [design_strength_mpa, calibration_factor]
    for s in specimens:
        operands += [s.load_kn, s.area_mm2]
    input_digits = sum(len(o.as_tuple().digits) for o in operands)
    max_result_exponent = max(
        [design_strength_mpa.adjusted()]
        + [s.load_kn.adjusted() + 3 - s.area_mm2.adjusted() for s in specimens]
    )
    return max(DEFAULT_PRECISION, max_result_exponent + input_digits + 10)


def evaluate_batch(
    design_strength_mpa: Decimal,
    specimens: Sequence[SpecimenInput],
    calibration_factor: Decimal = NO_CALIBRATION,
) -> EvaluationResult:
    """Evaluate one batch of specimens against the release criteria.

    Each specimen's load_kn is first multiplied by calibration_factor
    (Decimal multiplication, no intermediate rounding of the calibrated
    load); the result then flows through the usual strength rounding.

    Pass requires both:
      * mean of the three rounded strengths >= design strength
      * lowest single strength >= 85.0% of the design strength
    Reasons are reported in the fixed order MEAN_BELOW_DESIGN,
    MIN_BELOW_85_PERCENT.
    """
    precision = _required_precision(design_strength_mpa, specimens, calibration_factor)
    with localcontext() as ctx:
        # Precision alone is not enough for extreme-but-legal inputs: the
        # default context caps exponents at ±999999, so strengths from
        # 1e-1000000 mm² faces or 1e1000000 mm² areas would overflow or
        # underflow mid-calculation. Widen the exponent range to the
        # implementation's hard limits as well, cap precision at what the
        # implementation supports, and let the calibration multiplication
        # saturate to +Infinity rather than raise when no Decimal can
        # hold the calibrated load.
        ctx.prec = min(precision, MAX_PREC)
        ctx.Emax = MAX_EMAX
        ctx.Emin = -MAX_EMAX
        ctx.traps[Overflow] = False
        strengths = tuple(
            specimen_strength_mpa(s.load_kn * calibration_factor, s.area_mm2)
            for s in specimens
        )
        mean = round_to_tenth(sum(strengths) / Decimal(len(strengths)))

        reasons: list[str] = []
        if mean < design_strength_mpa:
            reasons.append(REASON_MEAN_BELOW_DESIGN)
        if min(strengths) < design_strength_mpa * MIN_SINGLE_RATIO:
            reasons.append(REASON_MIN_BELOW_85_PERCENT)

    return EvaluationResult(
        strengths_mpa=strengths,
        mean_strength_mpa=mean,
        passed=not reasons,
        reasons=tuple(reasons),
    )
