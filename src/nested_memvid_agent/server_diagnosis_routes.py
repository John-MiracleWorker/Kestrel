from __future__ import annotations

from typing import Any

from .server_models import DiagnosisRequest
from .server_support import tool_response_payload


def register_diagnosis_routes(app: Any, *, runs: Any) -> None:
    @app.post("/api/diagnosis/classify")  # type: ignore[untyped-decorator]
    def classify_diagnosis(request: DiagnosisRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="diagnosis.classify",
            arguments={"failure_text": request.failure_text, "source": request.source or "api"},
            session_id="api",
        )
        return tool_response_payload(execution)

    @app.post("/api/diagnosis/recall")  # type: ignore[untyped-decorator]
    def recall_diagnosis(request: DiagnosisRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="diagnosis.recall",
            arguments={
                "failure_text": request.failure_text,
                "source": request.source or "api",
                "k": request.k,
            },
            session_id="api",
        )
        return tool_response_payload(execution)
