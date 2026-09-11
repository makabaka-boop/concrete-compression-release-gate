"""FastAPI application exposing the batch strength evaluation endpoint."""

import json
import math
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

from .schemas import EvaluationRequest, EvaluationResponse
from .service import SpecimenInput, evaluate_batch

app = FastAPI(title="Concrete Batch Strength Evaluation", version="1.0.0")


def _dumps_exact(value: Any) -> str:
    """Serialize to JSON, emitting Decimal as a raw JSON number.

    str() of a finite Decimal is always a valid JSON number (plain or
    exponent notation) and preserves every significant digit, unlike
    pydantic's JSON mode (stringifies Decimal) or jsonable_encoder
    (truncates Decimal to float64).
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_dumps_exact(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(str(key), ensure_ascii=False) + ":" + _dumps_exact(item)
            for key, item in value.items()
        ) + "}"
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class ExactDecimalJSONResponse(JSONResponse):
    """JSON response that keeps Decimal values exact on the wire."""

    def render(self, content: Any) -> bytes:
        return _dumps_exact(content).encode("utf-8")


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats so an error body stays valid JSON.

    A JSON number such as 1e309 parses to inf and fails finite-number
    validation; echoing that input verbatim would make json.dumps raise
    ("Out of range float values are not JSON compliant") and turn the
    contractual 422 into a 500.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": _json_safe(jsonable_encoder(exc.errors()))},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvaluationResponse, response_model_exclude_none=True)
def evaluate(request: EvaluationRequest) -> Response:
    result = evaluate_batch(
        request.design_strength_mpa,
        [SpecimenInput(area_mm2=s.area_mm2, load_kn=s.load_kn) for s in request.specimens],
        calibration_factor=request.calibration_factor,
    )
    response = EvaluationResponse(
        strengths_mpa=list(result.strengths_mpa),
        mean_strength_mpa=result.mean_strength_mpa,
        passed=result.passed,
        reasons=list(result.reasons),
        # Echo the factor only when the caller explicitly sent one; when it
        # was omitted the response keeps the original field set.
        applied_calibration_factor=(
            request.calibration_factor
            if "calibration_factor" in request.model_fields_set
            else None
        ),
    )
    # Render directly so Decimal fields keep their exact value as JSON
    # numbers; the stock pipeline would stringify or truncate them.
    return ExactDecimalJSONResponse(
        response.model_dump(mode="python", exclude_none=True)
    )
