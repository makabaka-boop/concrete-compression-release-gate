"""Request/response contracts for the batch strength evaluation API."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

ReasonCode = Literal["MEAN_BELOW_DESIGN", "MIN_BELOW_85_PERCENT"]


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


class EvaluationResponse(BaseModel):
    """Release decision for a batch."""

    strengths_mpa: list[float] = Field(description="三个试件的单块强度，MPa，保留 0.1")
    mean_strength_mpa: float = Field(description="三项强度的算术平均值，MPa，保留 0.1")
    passed: bool = Field(description="批次是否放行")
    reasons: list[ReasonCode] = Field(
        description="未通过原因，固定顺序 MEAN_BELOW_DESIGN、MIN_BELOW_85_PERCENT；通过时为空"
    )
