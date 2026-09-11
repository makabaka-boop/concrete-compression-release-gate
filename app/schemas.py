"""Request/response contracts for the batch strength evaluation API."""

import re
from decimal import Decimal, DefaultContext, localcontext
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError

ReasonCode = Literal["MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT"]

# Response Decimals are rendered as JSON numbers on the wire (see
# main.ExactDecimalJSONResponse); keep the documented contract as "number"
# even though pydantic's default serialization schema for Decimal is
# "string".
JsonNumber = Annotated[Decimal, WithJsonSchema({"type": "number"}, mode="serialization")]

# evaluation_id is an opaque client-supplied idempotency key. It must be
# non-blank when present (an empty id would silently share one ledger slot
# across unrelated requests) and is length-capped so the ledger stays sane.
EvaluationId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=re.compile(r"\S")),
]


def _exact_decimal_product(left: Decimal, right: Decimal) -> Decimal:
    """Multiply two Decimals without context-precision rounding.

    The default 28-digit context would round the product of long
    measurements (e.g. high-precision ``width_mm``/``depth_mm`` readings)
    before it ever reaches the evaluation, silently changing the area;
    widen the context so the converted area is the exact product.
    """
    with localcontext() as ctx:
        required_prec = len(left.as_tuple().digits) + len(right.as_tuple().digits)
        ctx.prec = max(DefaultContext.prec, required_prec)
        return left * right


class Specimen(BaseModel):
    """A single concrete specimen: failure load plus its loaded face.

    The loaded face is expressed exactly one of two ways:
      * ``area_mm2`` directly, or
      * the ``width_mm``/``depth_mm`` pair (both present and positive).

    The two forms must not be mixed on one specimen. Requests are
    normalized to :attr:`effective_area_mm2` before evaluation, so the
    dimensions form is converted to an exact Decimal product and the rest
    of the pipeline sees one uniform area value.
    """

    area_mm2: Decimal | None = Field(
        default=None,
        gt=0,
        description="受压面积，单位 mm²，必须大于 0；与 width_mm/depth_mm 二选一，不能混用",
    )
    width_mm: Decimal | None = Field(
        default=None,
        gt=0,
        description="受压面长度，单位 mm，必须大于 0；与 depth_mm 成对出现，换算面积采用 Decimal 精确乘积",
    )
    depth_mm: Decimal | None = Field(
        default=None,
        gt=0,
        description="受压面宽度，单位 mm，必须大于 0；与 width_mm 成对出现",
    )
    load_kn: Decimal = Field(gt=0, description="破坏载荷，单位 kN，必须大于 0")

    @model_validator(mode="after")
    def _validate_loaded_face_expression(self) -> "Specimen":
        has_area = self.area_mm2 is not None
        has_width = self.width_mm is not None
        has_depth = self.depth_mm is not None

        if has_area and (has_width or has_depth):
            error = PydanticCustomError(
                "mixed_area_and_dimensions",
                "area_mm2 与 width_mm/depth_mm 不能混用，受压面只能选择其中一种表达方式",
            )
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [{"type": error, "loc": ("area_mm2",), "input": self.area_mm2}],
            )
        if not has_area and (has_width != has_depth):
            missing_field = "depth_mm" if has_width else "width_mm"
            error = PydanticCustomError(
                "missing_loaded_face_dimension",
                "width_mm 与 depth_mm 必须成对提供受压面尺寸，缺少 {missing_field}",
                {"missing_field": missing_field},
            )
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [{"type": error, "loc": (missing_field,), "input": None}],
            )
        if not has_area and not has_width and not has_depth:
            error = PydanticCustomError(
                "missing_loaded_face",
                "必须提供 area_mm2，或成对提供 width_mm 与 depth_mm",
            )
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [{"type": error, "loc": ("area_mm2",), "input": None}],
            )
        return self

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
