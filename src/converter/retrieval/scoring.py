from __future__ import annotations

import re

from collections import Counter
from typing import Iterable

from .models import SemanticStep
from ..registry.models import MethodRegistryEntry, ScriptRegistryEntry


def score_method_candidate(step: SemanticStep, entry: MethodRegistryEntry) -> tuple[float, str]:
    event_type = step.event_type.strip().lower()
    aliases = [str(item).strip().lower() for item in entry.aliases if str(item).strip()]
    if not event_type:
        return 0.0, "步骤缺少 event_type，无法按 alias 匹配方法"
    if event_type not in aliases:
        return 0.0, f"方法 aliases 未包含 event_type={step.event_type}"
    return 10.0, f"event_type={step.event_type} 命中方法 alias"


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
        str(event.get("action", "")),
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