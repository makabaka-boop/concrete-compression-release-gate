"""FastAPI application exposing the batch strength evaluation endpoint."""

import json
import math
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_ENTITY,
)

from . import store
from .schemas import EvaluationRecordResponse, EvaluationRequest, EvaluationResponse
from .service import SpecimenInput, evaluate_batch


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Create the ledger schema and apply compatible migrations (e.g. the
    # request_snapshot column on pre-snapshot databases) before the first
    # request arrives.
    store.init_db()
    yield


app = FastAPI(
    title="Concrete Batch Strength Evaluation", version="1.0.0", lifespan=_lifespan
)


def _dumps_exact(value: Any) -> str:
    """Serialize to JSON, emitting Decimal as a raw JSON number.

    str() of a finite Decimal is always a valid JSON number (plain or
    exponent notation) and preserves every significant digit, unlike
    pydantic's JSON mode (stringifies Decimal) or jsonable_encoder
    (truncates Decimal to float64). A non-finite Decimal — a strength
    saturated to +Infinity because no Decimal can represent it — is
    emitted as the ``Infinity`` literal, which Python's json parser
    accepts and maps to float inf.
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
    # Normalize each specimen to its effective loaded area (an explicit
    # area_mm2, or the exact Decimal product of width_mm x depth_mm) before
    # anything downstream: calibration, strength rounding and the ledger
    # fingerprint all work from the same uniform area value.
    effective_specimens = [
        SpecimenInput(area_mm2=s.effective_area_mm2, load_kn=s.load_kn)
        for s in request.specimens
    ]
    result = evaluate_batch(
        request.design_strength_mpa,
        effective_specimens,
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
    body = response.model_dump(mode="python", exclude_none=True)

    if request.evaluation_id is None:
        # No idempotency key: compute and respond as before, persist nothing.
        return ExactDecimalJSONResponse(body)

    fingerprint = store.build_fingerprint(
        request.design_strength_mpa,
        [(specimen.area_mm2, specimen.load_kn) for specimen in effective_specimens],
        request.calibration_factor,
        "calibration_factor" in request.model_fields_set,
    )
    record = store.lookup(request.evaluation_id)
    replayed = record is not None
    if record is None:
        # First sight of this id: persist the verdict together with a
        # snapshot of the normalized input, so the record stays auditable
        # without trusting any caller-side cache. A racing request may win
        # the insert; the stored record always stands.
        snapshot = _dumps_exact(
            {
                "design_strength_mpa": request.design_strength_mpa,
                "specimens": [
                    {"area_mm2": specimen.area_mm2, "load_kn": specimen.load_kn}
                    for specimen in effective_specimens
                ],
                "calibration_factor": request.calibration_factor,
                "calibration_explicit": "calibration_factor"
                in request.model_fields_set,
            }
        )
        record, inserted = store.store_if_absent(
            request.evaluation_id, fingerprint, _dumps_exact(body), snapshot
        )
        replayed = not inserted

    if record.fingerprint != fingerprint:
        # Same id but different business input: reject without touching the
        # stored record.
        return JSONResponse(
            status_code=HTTP_409_CONFLICT,
            content={
                "detail": (
                    "evaluation_id 冲突：该标识已对应不同的业务输入，"
                    "已保存的首次结果未被覆盖"
                ),
                "evaluation_id": request.evaluation_id,
            },
        )

    # Replay the stored body verbatim. parse_float=Decimal keeps every
    # significant digit through the round trip (a float parse would
    # truncate extreme values such as 1e30-scale strengths).
    stored_body = json.loads(record.response_json, parse_float=Decimal)
    stored_body["replayed"] = replayed
    return ExactDecimalJSONResponse(stored_body)


@app.get(
    "/evaluations/{evaluation_id}",
    response_model=EvaluationRecordResponse,
    response_model_exclude_none=True,
)
def get_evaluation(evaluation_id: str) -> Response:
    """Return the ledger record for one evaluation_id: first verdict plus,
    when a request snapshot exists, the normalized input that produced it.
    """
    record = store.lookup(evaluation_id)
    if record is None:
        # Unknown id (or a request that never carried one and was therefore
        # never persisted): no record is fabricated.
        return JSONResponse(
            status_code=HTTP_404_NOT_FOUND,
            content={
                "detail": "evaluation_id 不存在：台账中没有该标识的首次裁决记录",
                "evaluation_id": evaluation_id,
            },
        )
    body: dict[str, Any] = {
        "evaluation_id": evaluation_id,
        "created_at": record.created_at,
        "snapshot_available": record.request_snapshot is not None,
    }
    if record.request_snapshot is not None:
        # The snapshot is the normalized first input (design strength,
        # effective area + load per specimen, calibration factor and its
        # submission mode); merge its keys next to the record metadata.
        body.update(json.loads(record.request_snapshot, parse_float=Decimal))
    # The first verdict exactly as originally computed; parse_float=Decimal
    # keeps every significant digit, same as the replay path.
    body["result"] = json.loads(record.response_json, parse_float=Decimal)
    return ExactDecimalJSONResponse(body)
