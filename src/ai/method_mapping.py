from __future__ import annotations

from typing import Any

from src.recorder.models import format_recorded_action, normalize_event_type


METHOD_SUGGESTION_NAME_MAP: dict[str, str] = {
    "controlOperation": "FindControlByName",
    "Click": "Click",
    "PerformScan": "PerformScan",
    "input": "SendKeys",
    "wait": "WaitForExists",
    "comment": "ManualCheck",
    "checkpoint": "AgentInterface",
    "getScreenshot": "GetScreenShot",
}


def resolve_method_name_for_event(event: dict[str, Any]) -> str:
    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
    if event_type == "mouseAction":
        return resolve_mouse_action_method_name(event)
    return METHOD_SUGGESTION_NAME_MAP.get(event_type, "")


def resolve_mouse_action_method_name(event: dict[str, Any]) -> str:
    action_value = format_recorded_action(event.get("action", "")).strip().lower()
    mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
    if "scroll" in action_value:
        return "Wheel"
    has_drag_points = all(
        isinstance(mouse.get(key), int)
        for key in ("start_x", "start_y", "end_x", "end_y")
    )
    if has_drag_points:
        return "DragDrop"
    return ""