"""Core strength calculation and batch release decision.

All arithmetic uses Decimal. Strengths and the mean are quantized to
0.1 MPa with ROUND_HALF_UP; comparisons against the design strength and
the 85.0% minimum-single-value threshold are inclusive (equality passes).
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple, Sequence

MPA_RESOLUTION = Decimal("0.1")
MIN_SINGLE_RATIO = Decimal("0.85")

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
    """Round to 0.1 MPa using ROUND_HALF_UP."""
    return value.quantize(MPA_RESOLUTION, rounding=ROUND_HALF_UP)


def specimen_strength_mpa(load_kn: Decimal, area_mm2: Decimal) -> Decimal:
    """Single specimen strength: load_kn * 1000 / area_mm2, rounded to 0.1 MPa."""
    return round_to_tenth(load_kn * 1000 / area_mm2)


def evaluate_batch(
    design_strength_mpa: Decimal, specimens: Sequence[SpecimenInput]
) -> EvaluationResult:
    """Evaluate one batch of specimens against the release criteria.

    Pass requires both:
      * mean of the three rounded strengths >= design strength
      * lowest single strength >= 85.0% of the design strength
    Reasons are reported in the fixed order MEAN_BELOW_DESIGN,
    MIN_BELOW_85_PERCENT.
    """
    strengths = tuple(
        specimen_strength_mpa(s.load_kn, s.area_mm2) for s in specimens
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
