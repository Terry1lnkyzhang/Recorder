from __future__ import annotations

from typing import Any

from .models import MethodSelectionSuggestion, SuggestionGenerationResult


SCRIPT_SUGGESTION_MIN_SCORE = 2.0


def build_method_selection_result(session_id: str, retrieval_preview: dict[str, Any]) -> SuggestionGenerationResult:
    steps = retrieval_preview.get("steps", []) if isinstance(retrieval_preview, dict) else []
    suggestions: list[MethodSelectionSuggestion] = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        method_candidates = item.get("top_method_candidates", []) if isinstance(item.get("top_method_candidates", []), list) else []
        script_candidates = item.get("top_script_candidates", []) if isinstance(item.get("top_script_candidates", []), list) else []
        top_method = method_candidates[0] if method_candidates and isinstance(method_candidates[0], dict) else {}
        top_script = script_candidates[0] if script_candidates and isinstance(script_candidates[0], dict) else {}
        top_script_score = float(top_script.get("score", 0.0) or 0.0)
        selected_script = top_script if top_script_score >= SCRIPT_SUGGESTION_MIN_SCORE else {}
        suggestions.append(
            MethodSelectionSuggestion(
                step_id=int(item.get("step_id", 0) or 0),
                method_name=str(top_method.get("name", "")),
                score=float(top_method.get("score", 0.0) or 0.0),
                confidence=_estimate_confidence(float(top_method.get("score", 0.0) or 0.0)),
                reason=str(top_method.get("reason", "")),
                step_description=str(item.get("description", "")),
                step_conclusion=str(item.get("conclusion", "")),
                method_summary=str(top_method.get("summary", "")),
                script_name=str(selected_script.get("name", "")),
                script_summary=str(selected_script.get("summary", "")),
                candidate_payload=dict(top_method.get("payload", {})) if isinstance(top_method.get("payload", {}), dict) else {},
            )
        )
    notes = [
        "方法选择结果来源于程序化 Top-K 检索。",
        "后续如启用 AI 参数推荐，应仅基于少量候选方法继续推理。",
    ]
    return SuggestionGenerationResult(session_id=session_id, suggestions=suggestions, notes=notes)


def _estimate_confidence(score: float) -> float:
    if score <= 0:
        return 0.0
    if score >= 8.0:
        return 0.95
    if score >= 6.0:
        return 0.85
    if score >= 4.0:
        return 0.7
    if score >= 2.0:
        return 0.5
    return 0.3