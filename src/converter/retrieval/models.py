from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SemanticStep:
    step_id: int
    description: str
    conclusion: str = ""
    raw_text: str = ""
    tags: list[str] = field(default_factory=list)
    window_title: str = ""
    control_type: str = ""
    event_type: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CandidateMatch:
    candidate_type: str
    name: str
    score: float
    summary: str
    reason: str
    payload: dict[str, Any]