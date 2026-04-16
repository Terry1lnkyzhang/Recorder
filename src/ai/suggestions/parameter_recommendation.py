from __future__ import annotations

import json
import re
from typing import Any

from .models import MethodParameterSuggestion


def parse_parameter_recommendation_payload(payload: dict[str, Any]) -> tuple[str, str, list[MethodParameterSuggestion], list[str]]:
    selected_method = str(payload.get("selected_method", ""))
    reason = str(payload.get("reason", ""))
    parameters: list[MethodParameterSuggestion] = []
    for item in payload.get("parameters", []):
        if not isinstance(item, dict):
            continue
        parameters.append(
            MethodParameterSuggestion(
                name=str(item.get("name", "")),
                suggested_value=item.get("suggested_value"),
                confidence=float(item.get("confidence", 0.0) or 0.0),
                evidence=[str(value) for value in item.get("evidence", []) if str(value).strip()],
                missing_reason=str(item.get("missing_reason", "")),
            )
        )
    notes = [str(item) for item in payload.get("notes", []) if str(item).strip()]
    return selected_method, reason, parameters, notes


def parse_parameter_recommendation_response_text(response_text: str) -> dict[str, Any]:
    for candidate in _build_json_candidates(response_text):
        for attempt in (candidate, _repair_json_candidate(candidate)):
            try:
                payload = json.loads(attempt)
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
    preview = response_text[:600]
    if len(response_text) > 600:
        preview += "..."
    raise ValueError(f"参数推荐返回无法解析为 JSON: {preview}")


def _build_json_candidates(response_text: str) -> list[str]:
    stripped = response_text.strip()
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    add(stripped)
    add(re.sub(r"<think>.*?</think>", "", stripped, flags=re.IGNORECASE | re.DOTALL))
    for match in re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL):
        add(match)
    for source in list(candidates):
        start = source.find("{")
        end = source.rfind("}")
        if start >= 0 and end > start:
            add(source[start : end + 1])
    return candidates


def _repair_json_candidate(candidate: str) -> str:
    repaired = candidate.strip().lstrip("\ufeff")
    repaired = re.sub(r"^json\s*", "", repaired, flags=re.IGNORECASE)
    repaired = repaired.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = _escape_unescaped_inner_quotes(repaired)
    return repaired


def _escape_unescaped_inner_quotes(text: str) -> str:
    result: list[str] = []
    in_string = False
    escape = False

    for index, char in enumerate(text):
        if not in_string:
            if char == '"':
                in_string = True
            result.append(char)
            continue

        if escape:
            result.append(char)
            escape = False
            continue

        if char == "\\":
            result.append(char)
            escape = True
            continue

        if char == '"':
            next_sig = _next_significant_char(text, index + 1)
            if next_sig in {",", "}", "]", ":", None}:
                in_string = False
                result.append(char)
            else:
                result.append(r'\"')
            continue

        result.append(char)

    return "".join(result)


def _next_significant_char(text: str, start: int) -> str | None:
    for index in range(start, len(text)):
        if not text[index].isspace():
            return text[index]
    return None