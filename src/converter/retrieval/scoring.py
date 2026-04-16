from __future__ import annotations

import re

from src.recorder.models import format_recorded_action
from collections import Counter
from typing import Iterable

from .models import SemanticStep
from ..registry.models import MethodRegistryEntry, ScriptRegistryEntry


def score_method_candidate(step: SemanticStep, entry: MethodRegistryEntry) -> tuple[float, str]:
    if entry.name.lower() == "manualcheck" and step.event_type.lower() != "checkpoint":
        return 0.0, "ManualCheck 仅允许用于 checkpoint 类型步骤"
    score = _score_text_match(_build_method_step_text(step), _build_method_candidate_texts(entry))
    score += _score_method_heuristics(step, entry)
    return score, _build_method_reason(step, entry)


def score_script_candidate(step: SemanticStep, entry: ScriptRegistryEntry) -> tuple[float, str]:
    score = _score_text_match(_build_full_step_text(step), [entry.name, entry.summary, *entry.tags, *entry.aliases, *entry.covers_steps])
    if entry.script_type == "business_flow":
        score += 0.4
    score += max(0.0, (200 - entry.priority) / 1000)
    return score, _build_reason(step, entry.name, entry.tags, entry.aliases)


def _score_text_match(step_text: str, candidate_texts: Iterable[str]) -> float:
    step_tokens = _tokenize(step_text)
    candidate_tokens = _tokenize(" ".join(candidate_texts))
    if not step_tokens or not candidate_tokens:
        return 0.0
    overlap = set(step_tokens) & set(candidate_tokens)
    overlap_score = float(len(overlap))
    weighted = sum(min(Counter(step_tokens)[token], Counter(candidate_tokens)[token]) for token in overlap)
    return overlap_score + weighted * 0.15


def _tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    normalized = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", " ", text.lower())
    return [token for token in normalized.split() if len(token) >= 2]


def _build_method_step_text(step: SemanticStep) -> str:
    event = step.context.get("event", {}) if isinstance(step.context, dict) else {}
    ui_element = step.context.get("ui_element", {}) if isinstance(step.context, dict) else {}
    window = step.context.get("window", {}) if isinstance(step.context, dict) else {}
    keyboard = event.get("keyboard", {}) if isinstance(event, dict) else {}
    parts = [
        step.description,
        step.window_title,
        step.control_type,
        step.event_type,
        format_recorded_action(event.get("action", "")),
        str(event.get("note", "")),
        str(ui_element.get("name", "")),
        str(ui_element.get("control_type", "")),
        str(ui_element.get("automation_id", "")),
        str(ui_element.get("class_name", "")),
        str(window.get("title", "")),
        str(window.get("class_name", "")),
        str(keyboard.get("key_name", "")),
        str(keyboard.get("char", "")),
    ]
    return " ".join(part for part in parts if str(part).strip())


def _build_full_step_text(step: SemanticStep) -> str:
    event = step.context.get("event", {}) if isinstance(step.context, dict) else {}
    ui_element = step.context.get("ui_element", {}) if isinstance(step.context, dict) else {}
    window = step.context.get("window", {}) if isinstance(step.context, dict) else {}
    keyboard = event.get("keyboard", {}) if isinstance(event, dict) else {}
    parts = [
        step.description,
        step.conclusion,
        step.raw_text,
        step.window_title,
        step.control_type,
        step.event_type,
        *step.tags,
        format_recorded_action(event.get("action", "")),
        str(event.get("note", "")),
        str(ui_element.get("name", "")),
        str(ui_element.get("control_type", "")),
        str(ui_element.get("automation_id", "")),
        str(ui_element.get("class_name", "")),
        str(window.get("title", "")),
        str(window.get("class_name", "")),
        str(keyboard.get("key_name", "")),
        str(keyboard.get("char", "")),
    ]
    return " ".join(part for part in parts if str(part).strip())


def _build_method_candidate_texts(entry: MethodRegistryEntry) -> list[str]:
    values = [
        entry.name,
        entry.exposed_keyword,
        entry.summary,
        entry.description,
        entry.returns,
        *entry.tags,
        *entry.aliases,
        *entry.when_to_use,
        *entry.when_not_to_use,
    ]
    for parameter in entry.parameters:
        values.extend([parameter.name, parameter.type, parameter.description])
        for field in parameter.schema_fields:
            values.extend([field.name, field.type, field.description])
    return [str(value) for value in values if str(value).strip()]


def _score_method_heuristics(step: SemanticStep, entry: MethodRegistryEntry) -> float:
    score = 0.0
    description = step.description.lower()
    conclusion = step.conclusion.lower()
    method_name = entry.name.lower()
    control_type = step.control_type.lower()
    event_type = step.event_type.lower()
    ui_name = str(step.context.get("ui_element", {}).get("name", "")).lower() if isinstance(step.context, dict) else ""

    if any(keyword in description for keyword in ("等待", "wait")) and method_name.startswith("wait"):
        score += 1.8
    if any(keyword in description for keyword in ("滚动", "scroll")) and "scroll" in method_name:
        score += 1.4
    if any(keyword in description for keyword in ("输入", "填写", "键入")) and any(token in method_name for token in ("edit", "sendkeys")):
        score += 1.8
    if any(keyword in description for keyword in ("按下", "按键", "热键", "快捷键")) and "sendkeys" in method_name:
        score += 2.0
    if any(keyword in description for keyword in ("单击", "点击", "选择")) and "click" in method_name:
        score += 1.3
    if event_type == "mouse_click" and "click" in method_name:
        score += 0.9
    if event_type == "key_press" and any(token in method_name for token in ("sendkeys", "edit")):
        score += 1.1
    if event_type == "key_press" and all(token not in method_name for token in ("sendkeys", "edit", "null", "wait")):
        score -= 0.8
    if control_type == "button" and "click" in method_name:
        score += 0.9
    if control_type == "checkbox" and "checkboxclick" in method_name:
        score += 2.0
    if control_type == "edit" and any(token in method_name for token in ("edit", "sendkeys")):
        score += 1.2
    if control_type in {"list", "listitem", "combobox"} and any(token in method_name for token in ("select", "click")):
        score += 0.8
    if ui_name and ui_name in f"{entry.summary} {entry.description}".lower():
        score += 0.8
    if event_type == "checkpoint" and method_name == "manualcheck":
        score += 3.0
    if "无效" in conclusion and method_name == "null":
        score += 1.5
    return score


def _build_method_reason(step: SemanticStep, entry: MethodRegistryEntry) -> str:
    matched = _collect_matched_tokens(_build_method_step_text(step), [entry.name, entry.exposed_keyword, *entry.aliases])
    hints: list[str] = []
    method_name = entry.name.lower()
    if step.event_type.lower() == "mouse_click" and "click" in method_name:
        hints.append("事件类型与 Click 类方法匹配")
    if step.event_type.lower() == "key_press" and any(token in method_name for token in ("sendkeys", "edit")):
        hints.append("键盘事件与输入类方法匹配")
    if step.control_type.lower() == "edit" and any(token in method_name for token in ("edit", "sendkeys")):
        hints.append("控件类型为 Edit")
    if any(keyword in step.description.lower() for keyword in ("等待", "wait")) and method_name.startswith("wait"):
        hints.append("步骤描述包含等待语义")
    if matched:
        hints.insert(0, f"命中关键词: {', '.join(matched)}")
    if hints:
        return "；".join(dict.fromkeys(hints))
    return "基于步骤语义、控件信息和方法文档相似度排序"


def _collect_matched_tokens(step_text: str, candidates: Iterable[str]) -> list[str]:
    step_tokens = set(_tokenize(step_text))
    matched: list[str] = []
    for value in candidates:
        token = str(value).strip()
        if not token:
            continue
        candidate_tokens = _tokenize(token)
        if candidate_tokens and set(candidate_tokens) & step_tokens:
            matched.append(token)
    return list(dict.fromkeys(matched))


def _build_reason(step: SemanticStep, name: str, tags: list[str], aliases: list[str]) -> str:
    matched = []
    step_text = f"{step.description} {step.conclusion} {step.raw_text}".lower()
    for token in [name, *tags, *aliases]:
        value = str(token).strip()
        if value and value.lower() in step_text:
            matched.append(value)
    if matched:
        return f"命中关键词: {', '.join(dict.fromkeys(matched))}"
    return "基于步骤语义、标签和别名相似度排序"