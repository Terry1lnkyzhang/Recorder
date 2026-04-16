from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.ai.client import OpenAICompatibleAIClient

from .settings import SettingsStore


SESSION_METADATA_ANALYSIS_SYSTEM_PROMPT = (
    "你是桌面自动化测试录制元数据整理助手。"
    "你要根据 Design Steps 和用户已经填写的内容，整理出适合测试记录使用的关键词。"
    "输出必须是 JSON，不能包含 markdown 代码块。"
)


@dataclass(slots=True)
class SessionMetadataAIResult:
    preconditions: list[str]
    configuration_requirements: list[str]
    extra_devices: list[str]
    missing_preconditions: list[str]
    missing_configuration_requirements: list[str]
    missing_extra_devices: list[str]
    notes: str = ""


def normalize_keyword_terms(raw_text: str) -> list[str]:
    normalized = (raw_text or "").replace(";", "\n").replace(",", "\n")
    result: list[str] = []
    seen: set[str] = set()
    for item in normalized.splitlines():
        value = item.strip()
        if not value:
            continue
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(value)
    return result


def format_keyword_terms(items: list[str]) -> str:
    return "\n".join(normalize_keyword_terms("\n".join(items)))


def merge_keyword_text(existing_text: str, new_items: list[str]) -> str:
    merged = normalize_keyword_terms(existing_text)
    seen = {item.casefold() for item in merged}
    for item in new_items:
        value = item.strip()
        if not value:
            continue
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        merged.append(value)
    return "\n".join(merged)


def should_prompt_ai_analysis(metadata: dict[str, object]) -> bool:
    return not any(
        str(metadata.get(field_name, "")).strip()
        for field_name in ("preconditions", "configuration_requirements", "extra_devices")
    )


def analyze_session_metadata(settings_store: SettingsStore, metadata: dict[str, object]) -> SessionMetadataAIResult:
    design_steps = str(metadata.get("design_steps", "")).strip()
    if not design_steps:
        raise RuntimeError("Design Steps 为空，无法执行 AI 分析。")

    settings = settings_store.load()
    client = OpenAICompatibleAIClient(settings)
    prompt = _build_metadata_analysis_prompt(metadata)
    result = client.query(
        user_prompt=prompt,
        system_prompt=SESSION_METADATA_ANALYSIS_SYSTEM_PROMPT,
        extra_body={"response_format": {"type": "json_object"}},
    )
    return _parse_metadata_analysis_result(str(result.get("response_text", "")))


def build_missing_summary(result: SessionMetadataAIResult) -> str:
    lines: list[str] = []
    if result.missing_preconditions:
        lines.append("前置条件建议: " + "、".join(result.missing_preconditions))
    if result.missing_configuration_requirements:
        lines.append("配置要求建议: " + "、".join(result.missing_configuration_requirements))
    if result.missing_extra_devices:
        lines.append("额外设备建议: " + "、".join(result.missing_extra_devices))
    if result.notes.strip():
        lines.append("AI备注: " + result.notes.strip())
    return "\n".join(lines)


def _build_metadata_analysis_prompt(metadata: dict[str, object]) -> str:
    payload = {
        "design_steps": str(metadata.get("design_steps", "")).strip(),
        "existing_preconditions": normalize_keyword_terms(str(metadata.get("preconditions", ""))),
        "existing_configuration_requirements": normalize_keyword_terms(str(metadata.get("configuration_requirements", ""))),
        "existing_extra_devices": normalize_keyword_terms(str(metadata.get("extra_devices", ""))),
        "recording_context": {
            "is_prs_recording": bool(metadata.get("is_prs_recording", True)),
            "testcase_id": str(metadata.get("testcase_id", "")).strip(),
            "version_number": str(metadata.get("version_number", "")).strip(),
            "name": str(metadata.get("name", "")).strip(),
        },
    }
    return (
        "请根据下面的 Design Steps，整理 session 元数据中的关键词。\n"
        "要求：\n"
        "1. 前置条件、配置要求、额外设备都必须使用词语或短语，不要写完整句子。\n"
        "2. 关键词可以是中文、英文或界面中的原始术语，例如 第一次启动、倾斜、systemphantom。\n"
        "3. 不要重复已有内容。\n"
        "4. 如果 Design Steps 无法支持某一项，就返回空数组。\n"
        "5. 同时判断用户当前填写的内容是否有遗漏，把缺失但建议补充的项放到 missing_* 字段。\n"
        "6. 严格返回 JSON 对象，字段必须只有: preconditions, configuration_requirements, extra_devices, missing_preconditions, missing_configuration_requirements, missing_extra_devices, notes。\n\n"
        "输入数据:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_metadata_analysis_result(raw_text: str) -> SessionMetadataAIResult:
    normalized = raw_text.strip()
    if normalized.startswith("```"):
        normalized = normalized.strip("`")
        if normalized.startswith("json"):
            normalized = normalized[4:].strip()
    payload = json.loads(normalized)
    if not isinstance(payload, dict):
        raise RuntimeError("AI 返回格式无效，未得到 JSON object。")
    return SessionMetadataAIResult(
        preconditions=_normalize_result_list(payload.get("preconditions")),
        configuration_requirements=_normalize_result_list(payload.get("configuration_requirements")),
        extra_devices=_normalize_result_list(payload.get("extra_devices")),
        missing_preconditions=_normalize_result_list(payload.get("missing_preconditions")),
        missing_configuration_requirements=_normalize_result_list(payload.get("missing_configuration_requirements")),
        missing_extra_devices=_normalize_result_list(payload.get("missing_extra_devices")),
        notes=str(payload.get("notes", "")).strip(),
    )


def _normalize_result_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return normalize_keyword_terms("\n".join(str(item) for item in value))