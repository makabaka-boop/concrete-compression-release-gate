"""FastAPI application exposing the batch strength evaluation endpoint."""

from fastapi import FastAPI

from .schemas import EvaluationRequest, EvaluationResponse
from .service import SpecimenInput, evaluate_batch

app = FastAPI(title="Concrete Batch Strength Evaluation", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvaluationResponse, response_model_exclude_none=True)
def evaluate(request: EvaluationRequest) -> EvaluationResponse:
    result = evaluate_batch(
        request.design_strength_mpa,
        [SpecimenInput(area_mm2=s.area_mm2, load_kn=s.load_kn) for s in request.specimens],
        calibration_factor=request.calibration_factor,
    )
    return EvaluationResponse(
        strengths_mpa=[float(s) for s in result.strengths_mpa],
        mean_strength_mpa=float(result.mean_strength_mpa),
        passed=result.passed,
        reasons=list(result.reasons),
        # Echo the factor only when the caller explicitly sent one; when it
        # was omitted the response keeps the original field set.
        applied_calibration_factor=(
            float(request.calibration_factor)
            if "calibration_factor" in request.model_fields_set
            else None
        ),
    )
