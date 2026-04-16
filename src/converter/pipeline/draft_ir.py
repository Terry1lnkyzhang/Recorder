from __future__ import annotations

from ..ir.models import ConversionIR, PlannedStep
from ..retrieval.models import CandidateMatch, SemanticStep


class DraftIRBuilder:
    def __init__(self, script_threshold: float = 2.4, method_threshold: float = 1.8) -> None:
        self.script_threshold = script_threshold
        self.method_threshold = method_threshold

    def build(
        self,
        session_id: str,
        steps: list[SemanticStep],
        retrieval_results: dict[int, dict[str, list[CandidateMatch]]],
    ) -> ConversionIR:
        ir = ConversionIR(source_session_id=session_id)
        for step in steps:
            matches = retrieval_results.get(step.step_id, {})
            top_script = _first_match(matches.get("scripts", []))
            top_method = _first_match(matches.get("methods", []))

            if top_script and top_script.score >= self.script_threshold:
                ir.steps.append(
                    PlannedStep(
                        step_id=f"step_{step.step_id:04d}",
                        step_type="script_call",
                        target_name=top_script.name,
                        source_step_ids=[step.step_id],
                        confidence=top_script.score,
                        notes=[step.description, step.conclusion] if step.conclusion else [step.description],
                        retrieval_reason=top_script.reason,
                    )
                )
                continue

            if top_method and top_method.score >= self.method_threshold:
                ir.steps.append(
                    PlannedStep(
                        step_id=f"step_{step.step_id:04d}",
                        step_type="method_call",
                        target_name=top_method.name,
                        source_step_ids=[step.step_id],
                        confidence=top_method.score,
                        notes=[step.description, step.conclusion] if step.conclusion else [step.description],
                        retrieval_reason=top_method.reason,
                    )
                )
                continue

            ir.unresolved_steps.append(
                {
                    "source_step_id": step.step_id,
                    "description": step.description,
                    "conclusion": step.conclusion,
                    "reason": "No candidate crossed the confidence threshold.",
                    "top_method": top_method.name if top_method else "",
                    "top_script": top_script.name if top_script else "",
                }
            )
        return ir


def _first_match(matches: list[CandidateMatch]) -> CandidateMatch | None:
    return matches[0] if matches else None