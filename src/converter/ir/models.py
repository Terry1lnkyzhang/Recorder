from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PlannedStep:
    step_id: str
    step_type: str
    target_name: str
    args: dict[str, Any] = field(default_factory=dict)
    source_step_ids: list[int] = field(default_factory=list)
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    retrieval_reason: str = ""


@dataclass(slots=True)
class ConversionIR:
    ir_version: str = "1.0"
    source_session_id: str = ""
    source: str = "recorder"
    steps: list[PlannedStep] = field(default_factory=list)
    unresolved_steps: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)