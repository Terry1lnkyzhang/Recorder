from __future__ import annotations

import ast
import json
import re
import threading
from pathlib import Path
from collections.abc import Callable
from typing import Any

import yaml

from src.recorder.models import format_recorded_action, normalize_event_type
from src.recorder.settings import AISettings

from .client import OpenAICompatibleAIClient
from .errors import AIClientError
from .memory import load_carry_memory, save_carry_memory
from .models import AnalysisBatchRecord, SessionAnalysisResult
from .prompt_builder import (
    build_step_observation_prompt,
    build_step_reasoning_prompt,
    build_workflow_aggregation_prompt,
    collect_observation_inputs,
)


class SessionWorkflowAnalyzer:
    def __init__(self, settings: AISettings) -> None:
        self.settings = settings
        self.client = OpenAICompatibleAIClient(settings)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.client.cancel()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def analyze(
        self,
        session_dir: Path,
        session_data: dict[str, object],
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> SessionAnalysisResult:
        session_id = str(session_data.get("session_id", session_dir.name))
        events = list(session_data.get("events", []))
        environment = session_data.get("environment", {}) if isinstance(session_data.get("environment", {}), dict) else {}
        display_layout = environment.get("display_layout") if isinstance(environment, dict) else None
        batch_size = max(1, int(self.settings.analysis_batch_size))
        memory_path = session_dir / "ai_batch_memory.json"
        carry_memory = load_carry_memory(memory_path)
        total_batches = max(1, (len(events) + batch_size - 1) // batch_size) if events else 0
        prior_step_observations: list[dict[str, object]] = []

        result = SessionAnalysisResult(session_id=session_id, batch_size=batch_size, status="running", carry_memory=list(carry_memory))

        if progress_callback:
            progress_callback(
                "start",
                {
                    "session_id": session_id,
                    "event_count": len(events),
                    "batch_size": batch_size,
                    "total_batches": total_batches,
                },
            )

        for start_index in range(0, len(events), batch_size):
            if self.is_cancelled():
                raise AIClientError("AI 分析已取消。")
            batch_id = f"batch_{start_index + 1:04d}_{min(len(events), start_index + batch_size):04d}"
            current_batch = (start_index // batch_size) + 1

            if progress_callback:
                progress_callback(
                    "batch_preprocess_start",
                    {
                        "session_id": session_id,
                        "batch_id": batch_id,
                        "current_batch": current_batch,
                        "total_batches": total_batches,
                        "start_step": start_index + 1,
                        "end_step": min(len(events), start_index + batch_size),
                    },
                )

            batch_events, image_paths, observation_step_ids, image_stats = collect_observation_inputs(
                session_dir,
                events,
                start_index,
                batch_size,
                display_layout=display_layout,
                send_fullscreen=self.settings.send_fullscreen_screenshots,
            )
            observation_prompt = build_step_observation_prompt()

            if progress_callback:
                progress_callback(
                    "batch_preprocess_done",
                    {
                        "session_id": session_id,
                        "batch_id": batch_id,
                        "current_batch": current_batch,
                        "total_batches": total_batches,
                        "start_step": start_index + 1,
                        "end_step": min(len(events), start_index + batch_size),
                        "image_count": len(image_paths),
                        "cropped_monitor_count": image_stats.get("cropped_monitor_count", 0),
                    },
                )

            observation_response_text = ""
            observation_parsed: dict[str, object] = {"step_observations": []}
            if image_paths:
                observation_response = self.client.query(
                    user_prompt=observation_prompt,
                    image_paths=image_paths,
                    system_prompt=self.settings.analysis_system_prompt,
                    progress_callback=(
                        None
                        if not progress_callback
                        else lambda stage, payload, batch_id=batch_id, current_batch=current_batch: progress_callback(
                            stage,
                            {
                                "session_id": session_id,
                                "batch_id": batch_id,
                                "current_batch": current_batch,
                                "total_batches": total_batches,
                                **payload,
                            },
                        )
                    ),
                    cancel_callback=self.is_cancelled,
                )

                if progress_callback:
                    progress_callback(
                        "batch_parse",
                        {
                            "session_id": session_id,
                            "batch_id": batch_id,
                            "current_batch": current_batch,
                            "total_batches": total_batches,
                        },
                    )
                observation_response_text = str(observation_response.get("response_text", ""))
                try:
                    observation_parsed = _parse_ai_json(observation_response_text)
                except Exception as exc:
                    result.status = "partial_failed" if result.step_insights else "failed"
                    result.failure_message = str(exc)
                    self._persist_partial_result(session_dir, result, carry_memory)
                    self._write_parse_failure_files(session_dir, batch_id, observation_response_text, observation_prompt)
                    raise AIClientError(
                        f"AI 返回无法解析为 JSON。原始返回已保存到 {session_dir / 'ai_parse_error_last_response.txt'}"
                    ) from exc
            current_step_observations = _normalize_step_observations(observation_parsed, observation_step_ids, batch_events)
            if not current_step_observations:
                current_step_observations = _build_fallback_step_observations(batch_events, observation_step_ids)
            if not current_step_observations:
                continue
            result.step_observations.extend(current_step_observations)

            reasoning_prompt = build_step_reasoning_prompt(
                session_id,
                current_step_observations,
                prior_step_observations[-20:],
                carry_memory,
            )
            reasoning_response = self.client.query(
                user_prompt=reasoning_prompt,
                system_prompt=(
                    "你是桌面自动化多步骤推理助手。"
                    "你基于当前步骤观察结果、最近步骤上下文和历史摘要做局部推理。"
                    "输出必须是 JSON，且 step_insights 只针对当前步骤。"
                ),
                progress_callback=(
                    None
                    if not progress_callback
                    else lambda stage, payload, batch_id=batch_id, current_batch=current_batch: progress_callback(
                        stage,
                        {
                            "session_id": session_id,
                            "batch_id": batch_id,
                            "current_batch": current_batch,
                            "total_batches": total_batches,
                            "analysis_phase": "step_reasoning",
                            **payload,
                        },
                    )
                ),
                cancel_callback=self.is_cancelled,
            )
            reasoning_response_text = str(reasoning_response.get("response_text", ""))
            try:
                parsed = _parse_ai_json(reasoning_response_text)
            except Exception as exc:
                result.status = "partial_failed" if result.step_insights else "failed"
                result.failure_message = str(exc)
                self._persist_partial_result(session_dir, result, carry_memory)
                self._write_parse_failure_files(session_dir, batch_id, reasoning_response_text, reasoning_prompt)
                raise AIClientError(
                    f"AI 返回无法解析为 JSON。原始返回已保存到 {session_dir / 'ai_parse_error_last_response.txt'}"
                ) from exc
            batch_record = AnalysisBatchRecord(
                batch_id=batch_id,
                start_step=start_index + 1,
                end_step=min(len(events), start_index + batch_size),
                event_indexes=list(observation_step_ids),
                image_paths=[path.relative_to(session_dir).as_posix() for path in image_paths],
                prompt_preview=(
                    "[Observation Prompt]\n"
                    f"{observation_prompt[:1000]}\n\n"
                    "[Reasoning Prompt]\n"
                    f"{reasoning_prompt[:1000]}"
                ),
                response_text=(
                    "[Observation Response]\n"
                    f"{observation_response_text}\n\n"
                    "[Reasoning Response]\n"
                    f"{reasoning_response_text}"
                ),
                parsed_result={
                    "observation_round": observation_parsed,
                    "reasoning_round": parsed,
                },
            )
            result.batches.append(batch_record)

            step_insights = _normalize_step_insights(parsed, current_step_observations)
            step_notes = _normalize_notes(parsed)
            result.step_insights.extend(step_insights)
            result.analysis_notes.extend(step_notes)
            result.invalid_steps = _merge_unique_dict_items(result.invalid_steps, _normalize_invalid_steps(parsed))
            result.reusable_modules = _merge_unique_dict_items(result.reusable_modules, _normalize_reusable_modules(parsed))
            result.wait_suggestions = _merge_unique_dict_items(result.wait_suggestions, _normalize_wait_suggestions(parsed))

            batch_summary = parsed.get("batch_summary", {}) if isinstance(parsed, dict) else {}
            carry_entry = {
                "batch_id": batch_id,
                "step_range": [batch_record.start_step, batch_record.end_step],
                "current_phase": batch_summary.get("current_phase", "") if isinstance(batch_summary, dict) else "",
                "notable_state_changes": batch_summary.get("notable_state_changes", []) if isinstance(batch_summary, dict) else [],
                "carry_over_notes": batch_summary.get("carry_over_notes", []) if isinstance(batch_summary, dict) else [],
            }
            if not carry_entry["current_phase"] and step_insights:
                carry_entry["current_phase"] = str(step_insights[0].get("description", ""))
            if not carry_entry["notable_state_changes"] and step_insights:
                descriptions = [str(item.get("description", "")) for item in step_insights if str(item.get("description", "")).strip()]
                carry_entry["notable_state_changes"] = descriptions[:3]
            if step_notes:
                carry_entry["carry_over_notes"] = [*carry_entry["carry_over_notes"], *step_notes][:6]
            carry_memory.append(carry_entry)
            result.carry_memory = carry_memory
            prior_step_observations.extend(current_step_observations)
            result.status = "running"
            result.failure_message = ""
            self._persist_partial_result(session_dir, result, carry_memory)

            if progress_callback:
                progress_callback(
                    "batch_done",
                    {
                        "session_id": session_id,
                        "batch_id": batch_id,
                        "current_batch": current_batch,
                        "total_batches": total_batches,
                        "step_insight_count": len(result.step_insights),
                    },
                )

        result.carry_memory = carry_memory
        if self.is_cancelled():
            raise AIClientError("AI 分析已取消。")
        if result.step_insights:
            aggregation_prompt = build_workflow_aggregation_prompt(session_id, result.step_insights, carry_memory)
            if progress_callback:
                progress_callback(
                    "workflow_aggregate_start",
                    {
                        "session_id": session_id,
                        "step_count": len(result.step_insights),
                    },
                )
            aggregation_response = self.client.query(
                user_prompt=aggregation_prompt,
                system_prompt=(
                    "你是桌面自动化流程聚合分析助手。"
                    "你只基于给定 step_insights 做跨步骤推理，输出给人阅读的中文 Markdown 流程总结。"
                    "不要输出 JSON，不要输出 markdown 代码块，不要输出任何解释性前后缀。"
                    "必须按要求的标题顺序输出，并在无内容时明确写无。"
                ),
                progress_callback=(
                    None
                    if not progress_callback
                    else lambda stage, payload: progress_callback(
                        stage,
                        {
                            "session_id": session_id,
                            "analysis_phase": "workflow_aggregate",
                            **payload,
                        },
                    )
                ),
                cancel_callback=self.is_cancelled,
            )
            if progress_callback:
                progress_callback(
                    "workflow_aggregate_parse",
                    {
                        "session_id": session_id,
                        "step_count": len(result.step_insights),
                    },
                )
            aggregation_response_text = str(aggregation_response.get("response_text", ""))
            result.batches.append(
                AnalysisBatchRecord(
                    batch_id="workflow_aggregation",
                    start_step=1 if events else 0,
                    end_step=len(events),
                    event_indexes=list(range(1, len(events) + 1)),
                    image_paths=[],
                    prompt_preview=aggregation_prompt[:2000],
                    response_text=aggregation_response_text,
                    parsed_result={},
                )
            )
            result.workflow_report_markdown = aggregation_response_text.strip()
            result.analysis_notes = [note for note in result.analysis_notes if isinstance(note, str)]
            if progress_callback:
                progress_callback(
                    "workflow_aggregate_done",
                    {
                        "session_id": session_id,
                        "invalid_count": len(result.invalid_steps),
                        "module_count": len(result.reusable_modules),
                        "wait_count": len(result.wait_suggestions),
                    },
                )
        if progress_callback:
            progress_callback(
                "write_result",
                {
                    "session_id": session_id,
                    "total_batches": total_batches,
                },
            )
        result.status = "completed"
        result.failure_message = ""
        save_carry_memory(memory_path, carry_memory)
        self._write_result_files(session_dir, result)
        if progress_callback:
            progress_callback(
                "done",
                {
                    "session_id": session_id,
                    "total_batches": total_batches,
                    "invalid_count": len(result.invalid_steps),
                    "module_count": len(result.reusable_modules),
                    "wait_count": len(result.wait_suggestions),
                },
            )
        return result

    def _write_result_files(self, session_dir: Path, result: SessionAnalysisResult) -> None:
        json_path = session_dir / "ai_analysis.json"
        yaml_path = session_dir / "ai_analysis.yaml"
        payload = result.to_dict()
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        yaml_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _persist_partial_result(
        self,
        session_dir: Path,
        result: SessionAnalysisResult,
        carry_memory: list[dict[str, Any]],
    ) -> None:
        save_carry_memory(session_dir / "ai_batch_memory.json", carry_memory)
        self._write_result_files(session_dir, result)

    def _write_parse_failure_files(
        self,
        session_dir: Path,
        batch_id: str,
        response_text: str,
        prompt: str,
    ) -> None:
        (session_dir / "ai_parse_error_last_response.txt").write_text(response_text, encoding="utf-8")
        (session_dir / "ai_parse_error_last_prompt.txt").write_text(prompt, encoding="utf-8")
        (session_dir / "ai_parse_error_meta.json").write_text(
            json.dumps({"batch_id": batch_id}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _parse_ai_json(response_text: str) -> dict[str, object]:
    candidates = _build_json_candidates(response_text)
    for candidate in candidates:
        payload = _try_load_json_object(candidate)
        if payload is not None:
            return payload
    preview = response_text[:800]
    if len(response_text) > 800:
        preview += "..."
    raise AIClientError(f"AI 返回无法解析为 JSON: {preview}")


def _try_load_json_object(candidate: str) -> dict[str, object] | None:
    attempts = [candidate]
    repaired = _repair_json_candidate(candidate)
    if repaired != candidate:
        attempts.append(repaired)

    for attempt in attempts:
        try:
            payload = json.loads(attempt)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload

    for attempt in attempts:
        payload = _try_load_python_dict(attempt)
        if payload is not None:
            return payload
    return None


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


def _try_load_python_dict(candidate: str) -> dict[str, object] | None:
    try:
        payload = ast.literal_eval(candidate)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    normalized = _normalize_python_literal(payload)
    return normalized if isinstance(normalized, dict) else None


def _normalize_python_literal(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_python_literal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_python_literal(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_python_literal(item) for item in value]
    return value


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

    fence_matches = re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    for match in fence_matches:
        add(match)

    without_think = re.sub(r"<think>.*?</think>", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
    add(without_think)

    for source in list(candidates):
        extracted = _extract_first_json_object(source)
        if extracted:
            add(extracted)

    return candidates


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _normalize_invalid_steps(parsed: dict[str, object]) -> list[dict[str, object]]:
    values = parsed.get("invalid_steps", []) if isinstance(parsed, dict) else []
    return [item for item in values if isinstance(item, dict)]


def _normalize_step_observations(
    parsed: dict[str, object],
    step_ids: list[int],
    batch_events: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    values = parsed.get("step_observations", []) if isinstance(parsed, dict) else []
    if not isinstance(values, list) or not values:
        values = parsed.get("step_insights", []) if isinstance(parsed, dict) else []
    normalized: list[dict[str, object]] = []
    if not isinstance(values, list):
        return normalized
    for offset, item in enumerate(values[: len(step_ids)]):
        if not isinstance(item, dict):
            continue
        event = batch_events[offset] if isinstance(batch_events, list) and offset < len(batch_events) and isinstance(batch_events[offset], dict) else {}
        control_type = str(item.get("control_type", "")).strip()
        label = str(item.get("label", "")).strip()
        relative_position = _normalize_relative_position(item.get("relative_position"))
        need_scroll = _normalize_optional_bool(item.get("need_scroll"))
        is_table = _normalize_optional_bool(item.get("is_table"))
        override_label, override_control_type = _pick_ai_analysis_override_fields(event)
        if override_label:
            label = override_label
            relative_position = "self"
            if override_control_type:
                control_type = override_control_type
        observation = str(item.get("observation", item.get("description", ""))).strip()
        observation = _build_observation_text(
            control_type=control_type,
            label=label,
            relative_position=relative_position,
            need_scroll=need_scroll,
            is_table=is_table,
        )
        if not observation:
            continue
        normalized_item = {
            "step_id": step_ids[offset],
            "observation": observation,
        }
        if control_type:
            normalized_item["control_type"] = control_type
        if label:
            normalized_item["label"] = label
        if relative_position:
            normalized_item["relative_position"] = relative_position
        if need_scroll is not None:
            normalized_item["need_scroll"] = need_scroll
        if is_table is not None:
            normalized_item["is_table"] = is_table
        normalized.append(normalized_item)
    return normalized


def _normalize_step_insights(parsed: dict[str, object], current_step_observations: list[dict[str, object]]) -> list[dict[str, object]]:
    values = parsed.get("step_insights", []) if isinstance(parsed, dict) else []
    normalized: list[dict[str, object]] = []
    if not isinstance(values, list):
        return normalized
    for observation, item in zip(current_step_observations, values):
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        normalized_item: dict[str, object] = {
            "step_id": int(observation.get("step_id", 0) or 0),
            "description": description,
        }
        conclusion = str(item.get("conclusion", "")).strip()
        if conclusion:
            normalized_item["conclusion"] = conclusion
        normalized.append(normalized_item)
    return normalized


def _normalize_reusable_modules(parsed: dict[str, object]) -> list[dict[str, object]]:
    values = parsed.get("reusable_modules", []) if isinstance(parsed, dict) else []
    return [item for item in values if isinstance(item, dict)]


def _normalize_wait_suggestions(parsed: dict[str, object]) -> list[dict[str, object]]:
    values = parsed.get("wait_suggestions", []) if isinstance(parsed, dict) else []
    return [item for item in values if isinstance(item, dict)]


def _normalize_notes(parsed: dict[str, object]) -> list[str]:
    values = parsed.get("notes", []) if isinstance(parsed, dict) else []
    return [str(item) for item in values if isinstance(item, str)]


def _build_fallback_step_observations(batch_events: list[dict[str, object]], step_ids: list[int]) -> list[dict[str, object]]:
    fallback: list[dict[str, object]] = []
    for step_id, event in zip(step_ids, batch_events):
        event_type = normalize_event_type(event.get("event_type", ""), event.get("action", "")).strip()
        action = str(event.get("action", "")).strip()
        ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
        label = _pick_ui_element_label(ui_element)
        control_type = str(ui_element.get("control_type", "")).strip()
        observation = _build_observation_text(
            control_type=control_type or event_type,
            label=label,
            relative_position="self" if label else "",
            need_scroll=True if event_type == "mouseAction" and format_recorded_action(action).strip().lower() == "mouse_scroll" else False,
            is_table=_infer_is_table_from_batch_event(event),
        )
        fallback.append(
            {
                "step_id": step_id,
                "observation": observation or "未提取到明确观察结果",
                "control_type": control_type or event_type,
                "label": label,
                "relative_position": "self" if label else "",
                "need_scroll": True if event_type == "mouseAction" and format_recorded_action(action).strip().lower() == "mouse_scroll" else False,
                "is_table": _infer_is_table_from_batch_event(event),
            }
        )
    return fallback


def _normalize_relative_position(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in {"self", "up", "down", "left", "right"} else ""


def _normalize_optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _infer_is_table_from_batch_event(event: dict[str, object]) -> bool:
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    control_type = str(ui_element.get("control_type", "")).strip().lower()
    label = _pick_ui_element_label(ui_element).lower()
    observation_text = " ".join(part for part in [control_type, label, str(event.get("action", ""))] if str(part).strip())
    return any(token in observation_text for token in ["table", "grid", "row", "cell", "list", "表格", "列表"])


def _pick_ui_element_label(ui_element: dict[str, object]) -> str:
    help_text = str(ui_element.get("help_text", "")).strip()
    if _is_meaningful_ui_label(help_text):
        return help_text

    help_text_fallback = str(ui_element.get("help_text_fallback", "")).strip()
    if _is_meaningful_ui_label(help_text_fallback):
        return help_text_fallback

    name_fallbacks = ui_element.get("name_fallbacks", [])
    if isinstance(name_fallbacks, list):
        for item in name_fallbacks:
            text = str(item).strip()
            if _is_meaningful_ui_label(text):
                return text

    name = str(ui_element.get("name", "")).strip()
    return name if _is_meaningful_ui_label(name) else ""


def _pick_ai_analysis_override_fields(event: dict[str, object]) -> tuple[str, str]:
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    label = _pick_ui_element_label(ui_element)
    control_type = str(ui_element.get("control_type", "")).strip()
    return label, control_type


def _is_meaningful_ui_label(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False

    if len(text) == 1 and not text.isalnum() and not _contains_cjk(text):
        return False

    if all(not char.isalnum() and not _contains_cjk(char) for char in text):
        return False

    if len(text) >= 32 and _looks_like_noisy_technical_label(text):
        return False

    return True


def _looks_like_noisy_technical_label(text: str) -> bool:
    if _contains_embedded_technical_descriptor(text):
        return True

    if _looks_like_namespace_identifier(text):
        return True

    if _looks_like_internal_key(text):
        return True

    if _looks_like_guid_or_identifier(text):
        return True

    separator_count = sum(text.count(separator) for separator in [".", "_", "-", "/", "\\", ":"])
    if separator_count < 2:
        return False

    if " " in text or _contains_cjk(text):
        return False

    alnum_count = sum(1 for char in text if char.isalnum())
    if not alnum_count:
        return True

    punctuation_ratio = 1 - (alnum_count / max(len(text), 1))
    return punctuation_ratio >= 0.18


def _looks_like_namespace_identifier(text: str) -> bool:
    if " " in text or _contains_cjk(text):
        return False

    parts = [part for part in text.split(".") if part]
    if len(parts) < 2:
        return False

    if not all(part.replace("_", "").isalnum() for part in parts):
        return False

    if max(len(part) for part in parts) < 6:
        return False

    capitalized_parts = sum(1 for part in parts if part[:1].isupper())
    if len(parts) >= 3:
        return capitalized_parts >= 2
    return capitalized_parts >= 2 and len(text) >= 16


def _looks_like_internal_key(text: str) -> bool:
    if " " in text or _contains_cjk(text):
        return False

    for separator in ["_", "-"]:
        parts = [part for part in text.split(separator) if part]
        if len(parts) < 3:
            continue
        if not all(part.isalnum() for part in parts):
            continue
        if len(text) >= 24:
            return True
    return False


def _contains_embedded_technical_descriptor(text: str) -> bool:
    normalized = text.strip()
    if not normalized or _contains_cjk(normalized):
        return False

    if re.search(r"\bColumn\s+Display\s+Index\s*:", normalized, flags=re.IGNORECASE):
        return True

    technical_match = re.search(r"\b(?:[A-Z][A-Za-z0-9_]*\.){2,}[A-Z][A-Za-z0-9_]*\b", normalized)
    if technical_match:
        return True

    if re.search(r"\bItem\s*:\s*(?:[A-Z][A-Za-z0-9_]*\.){2,}[A-Z][A-Za-z0-9_]*\b", normalized, flags=re.IGNORECASE):
        return True

    return False


def _looks_like_guid_or_identifier(text: str) -> bool:
    guid_pattern = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    if re.fullmatch(guid_pattern, text):
        return True

    if " " in text or _contains_cjk(text):
        return False

    if len(text) >= 32 and text.isalnum() and not any(char.islower() for char in text):
        return True

    return False


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _build_observation_text(
    *,
    control_type: str,
    label: str,
    relative_position: str,
    need_scroll: bool | None,
    is_table: bool | None,
) -> str:
    parts: list[str] = []
    if label:
        parts.append(f"label={label}")
    if relative_position:
        parts.append(f"direction={relative_position}")
    if control_type:
        parts.append(f"control_type={control_type}")
    if need_scroll is not None:
        parts.append(f"scroll={str(need_scroll).lower()}")
    if is_table is not None:
        parts.append(f"table={str(is_table).lower()}")
    return " | ".join(parts)


def _merge_unique_dict_items(base_items: list[dict[str, object]], new_items: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in [*base_items, *new_items]:
        if not isinstance(item, dict):
            continue
        try:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        except Exception:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged