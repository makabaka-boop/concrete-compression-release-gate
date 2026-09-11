"""Request/response contracts for the batch strength evaluation API."""

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, WithJsonSchema

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


class Specimen(BaseModel):
    """A single concrete specimen: loaded area and failure load."""

    area_mm2: Decimal = Field(gt=0, description="受压面积，单位 mm²，必须大于 0")
    load_kn: Decimal = Field(gt=0, description="破坏载荷，单位 kN，必须大于 0")


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
