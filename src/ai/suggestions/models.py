from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class MethodParameterSuggestion:
    name: str
    suggested_value: Any = None
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    missing_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MethodParameterSuggestion:
        return cls(
            name=str(payload.get("name", "")),
            suggested_value=payload.get("suggested_value"),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            evidence=[str(item) for item in payload.get("evidence", []) if str(item).strip()],
            missing_reason=str(payload.get("missing_reason", "")),
        )


@dataclass(slots=True)
class MethodSelectionSuggestion:
    step_id: int
    method_name: str
    score: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    step_description: str = ""
    step_conclusion: str = ""
    method_summary: str = ""
    script_name: str = ""
    script_summary: str = ""
    candidate_payload: dict[str, Any] = field(default_factory=dict)
    parameters: list[MethodParameterSuggestion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "method_name": self.method_name,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
            "step_description": self.step_description,
            "step_conclusion": self.step_conclusion,
            "method_summary": self.method_summary,
            "script_name": self.script_name,
            "script_summary": self.script_summary,
            "candidate_payload": self.candidate_payload,
            "parameters": [item.to_dict() for item in self.parameters],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MethodSelectionSuggestion:
        return cls(
            step_id=int(payload.get("step_id", 0) or 0),
            method_name=str(payload.get("method_name", "")),
            score=float(payload.get("score", 0.0) or 0.0),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            reason=str(payload.get("reason", "")),
            step_description=str(payload.get("step_description", "")),
            step_conclusion=str(payload.get("step_conclusion", "")),
            method_summary=str(payload.get("method_summary", "")),
            script_name=str(payload.get("script_name", "")),
            script_summary=str(payload.get("script_summary", "")),
            candidate_payload=dict(payload.get("candidate_payload", {})) if isinstance(payload.get("candidate_payload", {}), dict) else {},
            parameters=[MethodParameterSuggestion.from_dict(item) for item in payload.get("parameters", []) if isinstance(item, dict)],
        )


@dataclass(slots=True)
class SuggestionGenerationResult:
    session_id: str
    suggestions: list[MethodSelectionSuggestion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "suggestions": [item.to_dict() for item in self.suggestions],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SuggestionGenerationResult:
        return cls(
            session_id=str(payload.get("session_id", "")),
            suggestions=[MethodSelectionSuggestion.from_dict(item) for item in payload.get("suggestions", []) if isinstance(item, dict)],
            notes=[str(item) for item in payload.get("notes", []) if str(item).strip()],
        )