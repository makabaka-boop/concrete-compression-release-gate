"""FastAPI application exposing the batch strength evaluation endpoint."""

from fastapi import FastAPI

from .schemas import EvaluationRequest, EvaluationResponse
from .service import SpecimenInput, evaluate_batch

app = FastAPI(title="Concrete Batch Strength Evaluation", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvaluationResponse)
def evaluate(request: EvaluationRequest) -> EvaluationResponse:
    result = evaluate_batch(
        request.design_strength_mpa,
        [SpecimenInput(area_mm2=s.area_mm2, load_kn=s.load_kn) for s in request.specimens],
    )
    return EvaluationResponse(
        strengths_mpa=[float(s) for s in result.strengths_mpa],
        mean_strength_mpa=float(result.mean_strength_mpa),
        passed=result.passed,
        reasons=list(result.reasons),
    )
