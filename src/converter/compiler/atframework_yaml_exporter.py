from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def export_suggestions_to_atframework_yaml(suggestion_result: Any, output_path: Path) -> int:
    payload = build_atframework_yaml_dict(suggestion_result)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    steps = payload.get("Steps", []) if isinstance(payload, dict) else []
    return len(steps) if isinstance(steps, list) else 0


def build_atframework_yaml_dict(suggestion_result: Any) -> dict[str, Any]:
    suggestions = list(getattr(suggestion_result, "suggestions", []) or [])
    ordered = sorted(suggestions, key=lambda item: int(getattr(item, "step_id", 0) or 0))
    steps = [_build_atframework_step(item) for item in ordered if str(getattr(item, "method_name", "")).strip()]
    return {"Steps": steps}


def _build_atframework_step(suggestion: Any) -> dict[str, Any]:
    method_name = str(getattr(suggestion, "method_name", "") or "")
    parameter_map = _build_parameter_map(getattr(suggestion, "parameters", []) or [])
    control_name = _normalize_control_name(parameter_map.get("uiControl"))
    param_dict_value = _stringify_param_dict(parameter_map.get("paramDict"))

    if method_name == "ManualCheck":
        return {
            "ControlName": control_name,
            "Action": "Null",
            "Parameter Value": "",
            "Check": method_name,
            "Check Parameter Value": param_dict_value,
            "Step Description": "",
            "Expect result": "",
        }

    return {
        "ControlName": control_name,
        "Action": method_name or "Null",
        "Parameter Value": param_dict_value,
        "Check": "Null",
        "Check Parameter Value": "",
        "Step Description": "",
        "Expect result": "",
    }


def _build_parameter_map(parameters: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in parameters:
        name = str(getattr(item, "name", "") or "").strip()
        if not name:
            continue
        result[name] = getattr(item, "suggested_value", None)
    return result


def _normalize_control_name(value: Any) -> str:
    if value is None:
        return "Null"
    if isinstance(value, str):
        return value.strip() or "Null"
    if isinstance(value, (int, float, bool)):
        return str(value)
    return "Null"


def _stringify_param_dict(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, default=str)