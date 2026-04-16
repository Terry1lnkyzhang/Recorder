from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class AnalysisBatchRecord:
    batch_id: str
    start_step: int
    end_step: int
    event_indexes: list[int] = field(default_factory=list)
    image_paths: list[str] = field(default_factory=list)
    prompt_preview: str = ""
    response_text: str = ""
    parsed_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SessionAnalysisResult:
    session_id: str
    batch_size: int
    status: str = "completed"
    failure_message: str = ""
    carry_memory: list[dict[str, Any]] = field(default_factory=list)
    batches: list[AnalysisBatchRecord] = field(default_factory=list)
    step_insights: list[dict[str, Any]] = field(default_factory=list)
    invalid_steps: list[dict[str, Any]] = field(default_factory=list)
    reusable_modules: list[dict[str, Any]] = field(default_factory=list)
    wait_suggestions: list[dict[str, Any]] = field(default_factory=list)
    analysis_notes: list[str] = field(default_factory=list)
    workflow_report_markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "batch_size": self.batch_size,
            "status": self.status,
            "failure_message": self.failure_message,
            "carry_memory": self.carry_memory,
            "batches": [batch.to_dict() for batch in self.batches],
            "step_insights": self.step_insights,
            "invalid_steps": self.invalid_steps,
            "reusable_modules": self.reusable_modules,
            "wait_suggestions": self.wait_suggestions,
            "analysis_notes": self.analysis_notes,
            "workflow_report_markdown": self.workflow_report_markdown,
        }