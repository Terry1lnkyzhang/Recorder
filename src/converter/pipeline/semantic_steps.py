from __future__ import annotations

from typing import Any

from src.recorder.models import format_recorded_action

from ..retrieval.models import SemanticStep


class SemanticStepExtractor:
    @staticmethod
    def from_ai_analysis(ai_analysis: dict[str, Any], session_data: dict[str, Any] | None = None) -> list[SemanticStep]:
        step_insights = ai_analysis.get("step_insights", []) if isinstance(ai_analysis, dict) else []
        events = session_data.get("events", []) if isinstance(session_data, dict) else []
        result: list[SemanticStep] = []
        for item in step_insights:
            if not isinstance(item, dict):
                continue
            step_id = item.get("step_id")
            if not isinstance(step_id, int):
                continue
            event = events[step_id - 1] if isinstance(events, list) and 0 < step_id <= len(events) and isinstance(events[step_id - 1], dict) else {}
            ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
            window = event.get("window", {}) if isinstance(event.get("window", {}), dict) else {}
            result.append(
                SemanticStep(
                    step_id=step_id,
                    description=str(item.get("description", "")).strip(),
                    conclusion=str(item.get("conclusion", "")).strip(),
                    raw_text=f"{item.get('description', '')} {item.get('conclusion', '')}".strip(),
                    tags=_extract_tags(item, event),
                    window_title=str(window.get("title", "")),
                    control_type=str(ui_element.get("control_type", "")),
                    event_type=str(event.get("event_type", "")),
                    context={
                        "event": event,
                        "ui_element": ui_element,
                        "window": window,
                    },
                )
            )
        return result


def _extract_tags(step_insight: dict[str, Any], event: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    description = str(step_insight.get("description", ""))
    conclusion = str(step_insight.get("conclusion", ""))
    for token in [description, conclusion, str(event.get("event_type", "")), format_recorded_action(event.get("action", ""))]:
        cleaned = token.strip()
        if cleaned:
            tags.append(cleaned)
    return tags