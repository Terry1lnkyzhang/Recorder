from __future__ import annotations

import ast
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
    parameter_map = _apply_parameter_summary_override(parameter_map, suggestion)
    control_name = _normalize_control_name(parameter_map.get("uiControl"))
    param_dict_raw = parameter_map.get("paramDict")
    export_parameter_raw = _build_export_parameter_payload(parameter_map)
    export_parameter_value = _stringify_param_dict(export_parameter_raw)
    step_description = _extract_text_parameter(parameter_map, param_dict_raw, "Description")
    expect_result = _extract_text_parameter(parameter_map, param_dict_raw, "Expect")

    if method_name in {"ManualCheck", "AgentInterface"}:
        return {
            "ControlName": control_name,
            "Action": "Null",
            "Parameter Value": "",
            "Check": method_name,
            "Check Parameter Value": export_parameter_value,
            "Step Description": step_description,
            "Expect result": expect_result,
        }

    return {
        "ControlName": control_name,
        "Action": method_name or "Null",
        "Parameter Value": export_parameter_value,
        "Check": "Null",
        "Check Parameter Value": "",
        "Step Description": step_description,
        "Expect result": expect_result,
    }


def _build_parameter_map(parameters: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in parameters:
        name = str(getattr(item, "name", "") or "").strip()
        if not name:
            continue
        result[name] = getattr(item, "suggested_value", None)
    return result


def _apply_parameter_summary_override(parameter_map: dict[str, Any], suggestion: Any) -> dict[str, Any]:
    candidate_payload = getattr(suggestion, "candidate_payload", {})
    if not isinstance(candidate_payload, dict):
        return parameter_map
    raw_override = str(candidate_payload.get("viewer_parameter_summary_override", "") or "").strip()
    if not raw_override:
        return parameter_map
    parsed_override = _parse_parameter_summary_override(raw_override)
    if not parsed_override:
        return parameter_map
    return parsed_override


def _parse_parameter_summary_override(raw_override: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for part in _split_parameter_override_segments(raw_override):
        segment = part.strip()
        if not segment or "=" not in segment:
            continue
        key, raw_value = segment.split("=", 1)
        name = key.strip()
        value_text = raw_value.strip()
        if not name:
            continue
        if not value_text:
            result[name] = ""
            continue
        try:
            result[name] = json.loads(value_text)
        except Exception:
            try:
                result[name] = ast.literal_eval(value_text)
            except Exception:
                lowered = value_text.lower()
                if lowered == "true":
                    result[name] = True
                elif lowered == "false":
                    result[name] = False
                elif lowered == "null" or lowered == "none":
                    result[name] = None
                else:
                    result[name] = value_text
    return result


def _split_parameter_override_segments(raw_override: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_string = False
    quote_char = ""
    escape = False

    for char in raw_override:
        if in_string:
            current.append(char)
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == quote_char:
                in_string = False
                quote_char = ""
            continue

        if char in {"\"", "'"}:
            in_string = True
            quote_char = char
            current.append(char)
            continue

        if char in "[{(":
            depth += 1
            current.append(char)
            continue

        if char in "]})":
            depth = max(0, depth - 1)
            current.append(char)
            continue

        if char in {",", ";"} and depth == 0:
            segment = "".join(current).strip()
            if segment:
                parts.append(segment)
            current = []
            continue

        current.append(char)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


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


def _extract_text_parameter(parameter_map: dict[str, Any], param_dict_value: Any, key: str) -> str:
    direct_value = _normalize_optional_text(parameter_map.get(key))
    if direct_value:
        return direct_value
    if isinstance(param_dict_value, dict):
        return _normalize_optional_text(param_dict_value.get(key))
    return ""


def _build_export_parameter_payload(parameter_map: dict[str, Any]) -> Any:
    param_dict_value = parameter_map.get("paramDict")
    merged: dict[str, Any] = {}

    if isinstance(param_dict_value, dict):
        merged.update(param_dict_value)

    for key, value in parameter_map.items():
        if key in {"uiControl", "paramDict"}:
            continue
        if value is None:
            continue
        merged[key] = value

    if merged:
        return merged
    return param_dict_value


def _normalize_optional_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""