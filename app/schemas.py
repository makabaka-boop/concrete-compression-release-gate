"""Request/response contracts for the batch strength evaluation API."""

import re
from decimal import MAX_EMAX, MAX_PREC, Decimal, Overflow, localcontext
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    ValidationError,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

ReasonCode = Literal["MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT"]

# Response Decimals are rendered as JSON numbers on the wire (see
# main.ExactDecimalJSONResponse); keep the documented contract as "number"
# even though pydantic's default serialization schema for Decimal is
# "string". Non-finite values are allowed: a strength whose exact value no
# Decimal can represent saturates to +Infinity so the verdict stays
# complete.
JsonNumber = Annotated[
    Decimal,
    Field(allow_inf_nan=True),
    WithJsonSchema({"type": "number"}, mode="serialization"),
]

# evaluation_id is an opaque client-supplied idempotency key. It must be
# non-blank when present (an empty id would silently share one ledger slot
# across unrelated requests) and is length-capped so the ledger stays sane.
EvaluationId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=re.compile(r"\S")),
]


def _reject_numeric_strings(value: Any) -> Any:
    """Reject numeric strings so specimen numerics stay JSON numbers.

    The contract types every specimen numeric as ``number``; pydantic's
    lax mode would silently coerce strings such as ``"150"`` or
    ``"22500.00"`` into Decimals, letting wrongly typed requests reach
    the evaluation. Reject them here so the type violation is reported
    (422) before any calculation. Non-string values flow through to the
    usual Decimal validation unchanged — ints, floats (converted via
    their shortest repr, so 1.01 stays exactly 1.01) and Decimals remain
    valid, and booleans keep failing the stock Decimal type check.
    """
    if isinstance(value, str):
        raise PydanticCustomError(
            "decimal_type",
            "必须是 JSON 数字（number 类型），不接受数字字符串",
        )
    return value


# Specimen numeric fields are contractually JSON numbers: without this
# annotation, lax Decimal coercion would let "150"-style strings through
# the type contract and into the strength evaluation.
SpecimenNumber = Annotated[Decimal, BeforeValidator(_reject_numeric_strings)]


def _exact_decimal_product(left: Decimal, right: Decimal) -> Decimal:
    """Multiply two Decimals without context-precision or exponent-range
    rounding.

    The default context (28 digits, exponents within ±999999) would round
    the product of long measurements — or reject extreme-but-legal ones,
    e.g. a 1e1000000 mm face — before it ever reaches the evaluation,
    silently changing the area or turning the request into a 500. Widen
    precision and exponent range to the decimal implementation's hard
    limits so the converted area is the exact product whenever that
    product is representable at all (multiplication cost scales with the
    operands' digits, not the precision cap). A product beyond even those
    limits saturates — +Infinity on overflow, 0 on underflow — instead of
    raising, so the batch still evaluates: an unrepresentably large area
    legitimately yields zero strengths, an unrepresentably small one
    yields unbounded strengths.
    """
    with localcontext() as ctx:
        required_prec = len(left.as_tuple().digits) + len(right.as_tuple().digits)
        ctx.prec = max(MAX_PREC, required_prec)
        ctx.Emax = MAX_EMAX
        ctx.Emin = -MAX_EMAX
        ctx.traps[Overflow] = False
        return left * right


def _loaded_face_expression_errors(
    has_area: bool,
    has_width: bool,
    has_depth: bool,
    area_input: Any,
) -> list[InitErrorDetails]:
    """Contract errors for the loaded-face expression, if any.

    Exactly one of ``area_mm2`` or the ``width_mm``/``depth_mm`` pair must
    be present; mixing forms, giving only one dimension, or giving neither
    is a contract violation. Error locations follow the contract: a
    missing paired side points at the missing dimension, mixing or no
    expression at all points at ``area_mm2``.
    """
    if has_area and (has_width or has_depth):
        return [
            {
                "type": PydanticCustomError(
                    "mixed_area_and_dimensions",
                    "area_mm2 与 width_mm/depth_mm 不能混用，受压面只能选择其中一种表达方式",
                ),
                "loc": ("area_mm2",),
                "input": area_input,
            }
        ]
    if not has_area and (has_width != has_depth):
        missing_field = "depth_mm" if has_width else "width_mm"
        return [
            {
                "type": PydanticCustomError(
                    "missing_loaded_face_dimension",
                    "width_mm 与 depth_mm 必须成对提供受压面尺寸，缺少 {missing_field}",
                    {"missing_field": missing_field},
                ),
                "loc": (missing_field,),
                "input": None,
            }
        ]
    if not has_area and not has_width and not has_depth:
        return [
            {
                "type": PydanticCustomError(
                    "missing_loaded_face",
                    "必须提供 area_mm2，或成对提供 width_mm 与 depth_mm",
                ),
                "loc": ("area_mm2",),
                "input": None,
            }
        ]
    return []


class Specimen(BaseModel):
    """A single concrete specimen: failure load plus its loaded face.

    The loaded face is expressed exactly one of two ways:
      * ``area_mm2`` directly, or
      * the ``width_mm``/``depth_mm`` pair (both present and positive).

    The two forms must not be mixed on one specimen. Requests are
    normalized to :attr:`effective_area_mm2` before evaluation, so the
    dimensions form is converted to an exact Decimal product and the rest
    of the pipeline sees one uniform area value.

    Every numeric field is a strict JSON ``number``: numeric strings such
    as ``"150"`` are rejected with a type error before any calculation.
    """

    area_mm2: SpecimenNumber | None = Field(
        default=None,
        gt=0,
        description="受压面积，单位 mm²，必须大于 0；与 width_mm/depth_mm 二选一，不能混用；只接受 JSON 数字",
    )
    width_mm: SpecimenNumber | None = Field(
        default=None,
        gt=0,
        description="受压面长度，单位 mm，必须大于 0；与 depth_mm 成对出现，换算面积采用 Decimal 精确乘积；只接受 JSON 数字",
    )
    depth_mm: SpecimenNumber | None = Field(
        default=None,
        gt=0,
        description="受压面宽度，单位 mm，必须大于 0；与 width_mm 成对出现；只接受 JSON 数字",
    )
    load_kn: SpecimenNumber = Field(
        gt=0, description="破坏载荷，单位 kN，必须大于 0；只接受 JSON 数字"
    )

    @model_validator(mode="wrap")
    @classmethod
    def _validate_loaded_face_expression(
        cls, values: Any, handler: Any
    ) -> "Specimen":
        """Enforce the loaded-face contract alongside field constraints.

        Field checks (e.g. ``width_mm > 0``) run inside ``handler``; the
        face-expression rules are applied here so that a specimen breaking
        both reports both problems — e.g. a zero width together with a
        missing depth flags the non-positive ``width_mm`` *and* the
        missing ``depth_mm``, instead of hiding the expression error
        behind the failed field check.
        """
        try:
            model = handler(values)
        except ValidationError as exc:
            # Field validation failed; the expression contract still
            # applies to the raw input, so merge its errors with the
            # field errors instead of dropping them.
            expression_errors = (
                _loaded_face_expression_errors(
                    values.get("area_mm2") is not None,
                    values.get("width_mm") is not None,
                    values.get("depth_mm") is not None,
                    values.get("area_mm2"),
                )
                if isinstance(values, dict)
                else []
            )
            if expression_errors:
                raise ValidationError.from_exception_data(
                    cls.__name__, [*exc.errors(), *expression_errors]
                ) from None
            raise
        expression_errors = _loaded_face_expression_errors(
            model.area_mm2 is not None,
            model.width_mm is not None,
            model.depth_mm is not None,
            model.area_mm2,
        )
        if expression_errors:
            raise ValidationError.from_exception_data(cls.__name__, expression_errors)
        return model

    @property
    def effective_area_mm2(self) -> Decimal:
        """Area used downstream: the given area or width × depth (exact Decimal product)."""
        if self.area_mm2 is not None:
            return self.area_mm2
        assert self.width_mm is not None and self.depth_mm is not None
        return _exact_decimal_product(self.width_mm, self.depth_mm)


class EvaluationRequest(BaseModel):
    """One batch: design strength plus exactly three specimens."""

    design_strength_mpa: Decimal = Field(gt=0, description="设计强度，单位 MPa，必须大于 0")
    specimens: list[Specimen] = Field(
        min_length=3,
        max_length=3,
        description="恰好三个试件",
    )
    calibration_factor: Decimal = Field(
        default=Decimal("1"),
        ge=Decimal("0.9500"),
        le=Decimal("1.0500"),
        description="压力机校准载荷修正系数，可选；省略时按 1 处理，范围 0.9500 至 1.0500，显式 null 视为非法",
    )
    evaluation_id: EvaluationId | None = Field(
        default=None,
        description="幂等标识，可选；携带时相同标识与相同业务输入的重试直接回放首次结果，标识冲突返回 409，显式 null 视为未携带",
    )


class EvaluationResponse(BaseModel):
    """Release decision for a batch.

    Numeric fields stay Decimal so the exact computed values reach the
    wire; converting them to float would silently drop precision (e.g. a
    high-precision calibration factor would collapse to 1.0).
    """

    strengths_mpa: list[JsonNumber] = Field(description="三个试件的单块强度，MPa，保留 0.1")
    mean_strength_mpa: JsonNumber = Field(description="三项强度的算术平均值，MPa，保留 0.1")
    passed: bool = Field(description="批次是否放行")
    reasons: list[ReasonCode] = Field(
        description="未通过原因，固定顺序 MEAN_BELOW_DESIGN、MIN_BELOW_85_PERCENT；通过时为空"
    )
    applied_calibration_factor: JsonNumber | None = Field(
        default=None,
        description="实际应用的校准系数；仅当请求显式携带 calibration_factor 时返回，省略时不出现该字段",
    )
    replayed: bool | None = Field(
        default=None,
        description="是否回放了已保存的首次结果；仅当请求携带 evaluation_id 时返回，省略时不出现该字段",
    )


class SnapshotSpecimen(BaseModel):
    """One specimen as persisted in the ledger snapshot.

    The loaded face is always the normalized effective area: a face
    entered as ``width_mm`` x ``depth_mm`` was converted to its exact
    Decimal product before the snapshot was written.
    """

    area_mm2: JsonNumber = Field(description="归一化后的有效受压面积，单位 mm²")
    load_kn: JsonNumber = Field(description="破坏载荷，单位 kN")


class EvaluationRecordResponse(BaseModel):
    """Ledger record behind ``GET /evaluations/{evaluation_id}``.

    ``result`` is the first stored verdict, exactly as originally
    computed. The normalized input fields are present only when the
    record carries a request snapshot; rows written before snapshots
    existed report ``snapshot_available=false`` and omit them.
    """

    evaluation_id: str = Field(description="幂等标识")
    created_at: str = Field(description="首次裁决的落库时间（UTC）")
    snapshot_available: bool = Field(
        description="请求快照是否可用；旧库记录为 false，表示当时输入不可还原"
    )
    design_strength_mpa: JsonNumber | None = Field(
        default=None, description="规范化的设计强度，单位 MPa；仅快照可用时返回"
    )
    specimens: list[SnapshotSpecimen] | None = Field(
        default=None,
        description="三块试件的有效受压面积与破坏载荷（按提交顺序）；仅快照可用时返回",
    )
    calibration_factor: JsonNumber | None = Field(
        default=None,
        description="裁决使用的校准系数（未显式提交时为 1）；仅快照可用时返回",
    )
    calibration_explicit: bool | None = Field(
        default=None,
        description="调用方是否显式提交了校准系数；仅快照可用时返回",
    )
    result: EvaluationResponse = Field(description="首次裁决结果（不含 replayed 标记）")
