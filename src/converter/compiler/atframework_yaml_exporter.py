from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path
from typing import Any

import yaml


def export_suggestions_to_atframework_yaml(suggestion_result: Any, output_path: Path, source_root: Path | None = None) -> int:
    payload = build_atframework_yaml_dict(suggestion_result)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if source_root is not None:
        _rewrite_wait_for_exists_screenshot_paths(payload, source_root, output_path.parent)
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
        check_parameter_raw = _remove_check_text_parameters(export_parameter_raw)
        return {
            "ControlName": control_name,
            "Action": "Null",
            "Parameter Value": "",
            "Check": method_name,
            "Check Parameter Value": _stringify_param_dict(check_parameter_raw),
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
    try:
        parsed_json = json.loads(raw_override)
    except Exception:
        parsed_json = None
    if isinstance(parsed_json, dict):
        return {str(key): value for key, value in parsed_json.items() if str(key).strip()}

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


def _remove_check_text_parameters(value: Any) -> Any:
    if isinstance(value, dict):
        filtered = {
            key: item
            for key, item in value.items()
            if str(key or "").strip().lower() not in {"description", "expect"}
        }
        return filtered or None
    if isinstance(value, str):
        parsed = _parse_param_dict_text(value)
        if isinstance(parsed, dict):
            return _remove_check_text_parameters(parsed)
    return value


def _parse_param_dict_text(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return ast.literal_eval(text)
    except Exception:
        return None


def _rewrite_wait_for_exists_screenshot_paths(payload: dict[str, Any], source_root: Path, export_dir: Path) -> None:
    steps = payload.get("Steps")
    if not isinstance(steps, list):
        return

    screenshot_dir = export_dir / "screenshot"
    copied_targets_by_source: dict[str, str] = {}
    used_targets: set[str] = set()

    for step in steps:
        if not isinstance(step, dict):
            continue
        for method_key, payload_key in (("Action", "Parameter Value"), ("Check", "Check Parameter Value")):
            method_name = str(step.get(method_key, "") or "").strip().lower()
            if method_name not in {"waitforexists", "matchingclick", "wheel"}:
                continue
            step[payload_key] = _rewrite_wait_for_exists_parameter_blob(
                step.get(payload_key),
                source_root,
                export_dir,
                screenshot_dir,
                copied_targets_by_source,
                used_targets,
            )


def _rewrite_wait_for_exists_parameter_blob(
    raw_value: Any,
    source_root: Path,
    export_dir: Path,
    screenshot_dir: Path,
    copied_targets_by_source: dict[str, str],
    used_targets: set[str],
) -> Any:
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return raw_value
        try:
            parsed = json.loads(text)
        except Exception:
            return raw_value
        rewritten = _rewrite_wait_for_exists_parameter_value(
            parsed,
            source_root,
            export_dir,
            screenshot_dir,
            copied_targets_by_source,
            used_targets,
        )
        if rewritten is parsed:
            return raw_value
        return json.dumps(rewritten, ensure_ascii=False)

    return _rewrite_wait_for_exists_parameter_value(
        raw_value,
        source_root,
        export_dir,
        screenshot_dir,
        copied_targets_by_source,
        used_targets,
    )


def _rewrite_wait_for_exists_parameter_value(
    value: Any,
    source_root: Path,
    export_dir: Path,
    screenshot_dir: Path,
    copied_targets_by_source: dict[str, str],
    used_targets: set[str],
    parent_key: str = "",
) -> Any:
    parent_key_lower = parent_key.strip().lower()
    if parent_key_lower in {"sourcepath", "targetpath"}:
        return _copy_wait_for_exists_sources(
            value,
            source_root,
            export_dir,
            screenshot_dir,
            copied_targets_by_source,
            used_targets,
        )

    if isinstance(value, dict):
        changed = False
        converted_dict: dict[Any, Any] = {}
        for key, item in value.items():
            converted_item = _rewrite_wait_for_exists_parameter_value(
                item,
                source_root,
                export_dir,
                screenshot_dir,
                copied_targets_by_source,
                used_targets,
                parent_key=str(key),
            )
            if converted_item is not item:
                changed = True
            converted_dict[key] = converted_item
        return converted_dict if changed else value

    if isinstance(value, list):
        changed = False
        converted_list: list[Any] = []
        for item in value:
            converted_item = _rewrite_wait_for_exists_parameter_value(
                item,
                source_root,
                export_dir,
                screenshot_dir,
                copied_targets_by_source,
                used_targets,
                parent_key=parent_key,
            )
            if converted_item is not item:
                changed = True
            converted_list.append(converted_item)
        return converted_list if changed else value

    return value


def _copy_wait_for_exists_sources(
    value: Any,
    source_root: Path,
    export_dir: Path,
    screenshot_dir: Path,
    copied_targets_by_source: dict[str, str],
    used_targets: set[str],
) -> Any:
    if isinstance(value, str):
        return _copy_wait_for_exists_source(
            value,
            source_root,
            export_dir,
            screenshot_dir,
            copied_targets_by_source,
            used_targets,
        )
    if isinstance(value, list):
        changed = False
        converted_list: list[Any] = []
        for item in value:
            if isinstance(item, str):
                converted_item = _copy_wait_for_exists_source(
                    item,
                    source_root,
                    export_dir,
                    screenshot_dir,
                    copied_targets_by_source,
                    used_targets,
                )
            else:
                converted_item = item
            if converted_item is not item:
                changed = True
            converted_list.append(converted_item)
        return converted_list if changed else value
    return value


def _copy_wait_for_exists_source(
    raw_value: str,
    source_root: Path,
    export_dir: Path,
    screenshot_dir: Path,
    copied_targets_by_source: dict[str, str],
    used_targets: set[str],
) -> str:
    source_path = _resolve_wait_for_exists_source_path(raw_value, source_root)
    if source_path is None or not source_path.exists() or not source_path.is_file():
        return raw_value

    source_key = str(source_path.resolve()).casefold()
    cached_target = copied_targets_by_source.get(source_key)
    if cached_target:
        return cached_target

    target_relative = _build_wait_for_exists_target_relative(raw_value, source_path, used_targets)
    target_path = export_dir / target_relative
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)

    target_value = target_relative.as_posix()
    copied_targets_by_source[source_key] = target_value
    used_targets.add(target_value.casefold())
    return target_value


def _resolve_wait_for_exists_source_path(raw_value: str, source_root: Path) -> Path | None:
    candidate_text = str(raw_value or "").strip().strip('"')
    if not candidate_text:
        return None

    candidate_path = Path(candidate_text)
    if candidate_path.is_absolute():
        return candidate_path
    return (source_root / candidate_path).resolve()


def _build_wait_for_exists_target_relative(raw_value: str, source_path: Path, used_targets: set[str]) -> Path:
    raw_parts = [part for part in str(raw_value or "").replace("\\", "/").split("/") if part and part != "."]
    while raw_parts and raw_parts[0].lower() in {"screenshot", "screenshots", "media"}:
        raw_parts = raw_parts[1:]
    if not raw_parts or any(part == ".." for part in raw_parts):
        raw_parts = [source_path.name]

    sanitized_parts = [_sanitize_export_path_segment(part) for part in raw_parts]
    candidate = Path("screenshot", *sanitized_parts)
    deduplicated = candidate
    counter = 1
    while deduplicated.as_posix().casefold() in used_targets:
        suffix = deduplicated.suffix
        stem = deduplicated.stem
        deduplicated = deduplicated.with_name(f"{stem}_{counter}{suffix}")
        counter += 1
    return deduplicated


def _sanitize_export_path_segment(value: str) -> str:
    sanitized = "".join("_" if char in '<>:"\\|?*' else char for char in str(value or "").strip())
    return sanitized or "file"


def _normalize_optional_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""