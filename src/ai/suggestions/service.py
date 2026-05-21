from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import asdict
import json
from pathlib import Path
import re
from typing import Any

from PIL import Image

from src.ai.client import OpenAICompatibleAIClient
from src.ai.errors import AIClientError
from src.ai.method_mapping import MethodSuggestionOption, resolve_method_name_for_event, resolve_method_options_for_event
from src.converter.pipeline.method_candidates import build_retrieval_preview_from_files
from src.converter.registry.loader import load_method_registry
from src.converter.retrieval.models import SemanticStep
from src.recorder.models import format_recorded_action, normalize_event_type, normalize_keyboard_key_name

from .method_selection import build_method_selection_result
from .models import MethodParameterSuggestion, MethodSelectionSuggestion, SuggestionGenerationResult
from .parameter_recommendation import parse_parameter_recommendation_payload, parse_parameter_recommendation_response_text
from .prompt_builder import build_parameter_recommendation_prompt, build_parameter_recommendation_system_prompt


ParameterDerivationResult = tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]
ParameterDeriver = Callable[[dict[str, Any], str], ParameterDerivationResult]
SessionAwareParameterDeriver = Callable[[dict[str, Any], str, Path | None], ParameterDerivationResult]


class AISuggestionService:
    def build_retrieval_preview_from_files(
        self,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 5,
        top_k_scripts: int = 3,
    ) -> dict[str, Any]:
        return build_retrieval_preview_from_files(
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )

    def build_method_selection_from_files(
        self,
        session_id: str,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 5,
        top_k_scripts: int = 3,
    ) -> SuggestionGenerationResult:
        preview = self.build_retrieval_preview_from_files(
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )
        return build_method_selection_result(session_id=session_id, retrieval_preview=preview)

    def build_method_selection_from_session_data(
        self,
        session_id: str,
        session_data: dict[str, Any],
        methods_registry_path: Path,
    ) -> SuggestionGenerationResult:
        events = session_data.get("events", []) if isinstance(session_data.get("events", []), list) else []
        registry = load_method_registry(methods_registry_path)
        suggestions: list[MethodSelectionSuggestion] = []

        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                continue
            step = SemanticStep(
                step_id=index,
                description="",
                conclusion="",
                raw_text="",
                tags=[],
                window_title="",
                control_type="",
                event_type=normalize_event_type(event.get("event_type", ""), event.get("action", "")),
                context={"event": event},
            )
            mapped_method_name, mapped_reason, method_options = self._resolve_method_name_for_event(step, event)
            top_entry = _find_registry_entry_by_name(registry.entries, mapped_method_name) if mapped_method_name else None
            top_score = 100.0 if mapped_method_name else 0.0
            top_reason = mapped_reason
            method_name = mapped_method_name
            if top_entry is None and not method_name:
                method_name = ""
            elif top_entry is None and method_name:
                top_reason = f"{mapped_reason}；但 registry 中未找到 {method_name} 的元数据"
                top_score = 60.0
            else:
                method_name = top_entry.name

            candidate_payload = _build_method_candidate_payload(
                registry_entries=registry.entries,
                selected_entry=top_entry,
                selected_method_name=method_name,
                method_options=method_options,
                event_type=step.event_type,
            )

            suggestions.append(
                MethodSelectionSuggestion(
                    step_id=index,
                    method_name=method_name,
                    score=top_score,
                    confidence=1.0 if method_name else 0.0,
                    reason=top_reason,
                    step_description="",
                    step_conclusion="",
                    method_summary=top_entry.summary if top_entry else "",
                    script_name="",
                    script_summary="",
                    candidate_payload=candidate_payload,
                )
            )

        return SuggestionGenerationResult(
            session_id=session_id,
            suggestions=suggestions,
            notes=[
                "方法建议来源于清洗后事件。",
                "方法建议使用统一映射表生成；每个事件类型可配置默认方法和多个备选方法，不再依赖 pilot_methods.yaml 中的 aliases。",
                "mouseAction 中：scroll 映射到 Wheel，带 start/end 坐标的拖动映射到 DragDrop，其他 mouseAction 当前不自动推荐方法。",
            ],
        )

    def _resolve_method_name_for_event(
        self,
        step: SemanticStep,
        event: dict[str, Any],
    ) -> tuple[str, str, list[MethodSuggestionOption]]:
        event_type = str(step.event_type or "").strip()
        method_options = resolve_method_options_for_event(event)
        method_name = resolve_method_name_for_event(event)
        if method_name and method_options:
            default_reason = method_options[0].reason or f"统一映射表：event_type={event_type} -> {method_name}"
            alternative_names = [option.name for option in method_options[1:] if option.name]
            if alternative_names:
                return method_name, f"{default_reason} 可选方法：{', '.join(alternative_names)}。", method_options
            return method_name, default_reason, method_options
        if event_type == "mouseAction":
            return "", "统一映射表：当前 mouseAction 不满足 Wheel/DragDrop 条件，暂不推荐方法", []
        return "", f"统一映射表中未配置 event_type={event_type} 的方法", []

    def build_parameter_prompt_for_step_from_files(
        self,
        suggestion: MethodSelectionSuggestion,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 3,
        top_k_scripts: int = 2,
    ) -> tuple[str, dict[str, Any]]:
        preview = self.build_retrieval_preview_from_files(
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )
        return self.build_parameter_prompt_for_step(suggestion, preview), preview

    def write_result_file(self, output_path: Path, result: SuggestionGenerationResult) -> None:
        output_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_result_file(self, path: Path) -> SuggestionGenerationResult:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Suggestion result file must contain an object: {path}")
        return SuggestionGenerationResult.from_dict(payload)

    def build_parameter_prompt_for_step(
        self,
        suggestion: MethodSelectionSuggestion,
        retrieval_preview: dict[str, Any],
    ) -> str:
        step_result = next(
            (
                item
                for item in retrieval_preview.get("steps", [])
                if isinstance(item, dict) and int(item.get("step_id", 0) or 0) == suggestion.step_id
            ),
            {},
        )
        top_candidates = step_result.get("top_method_candidates", []) if isinstance(step_result.get("top_method_candidates", []), list) else []
        return build_parameter_recommendation_prompt(suggestion.to_dict(), top_candidates)

    def recommend_parameters_from_context(
        self,
        suggestion: MethodSelectionSuggestion,
        event: dict[str, Any],
        ai_observation_text: str = "",
        session_dir: Path | None = None,
    ) -> list[str]:
        method_name = str(suggestion.method_name or "").strip()
        derived_values, evidence_map, missing_map = _derive_parameter_values_for_method(method_name, event, ai_observation_text, session_dir=session_dir)
        parameter_suggestions = _build_parameter_suggestions_from_schema(
            suggestion=suggestion,
            derived_values=derived_values,
            evidence_map=evidence_map,
            missing_map=missing_map,
        )
        suggestion.parameters = _normalize_parameter_suggestions_for_method(method_name, parameter_suggestions)
        return [
            "参数推荐基于方法建议、事件明细和 AI看图内容生成。",
            "当前参数推荐未调用 AI。",
        ]

    def apply_parameter_recommendation(
        self,
        suggestion: MethodSelectionSuggestion,
        payload: dict[str, Any],
    ) -> list[str]:
        selected_method, reason, parameters, notes = parse_parameter_recommendation_payload(payload)
        if selected_method:
            suggestion.method_name = selected_method
        if reason:
            suggestion.reason = reason
        if parameters:
            method_name = selected_method or str(suggestion.method_name or "")
            suggestion.parameters = _normalize_parameter_suggestions_for_method(method_name, parameters)
        return notes

    def recommend_parameters_for_suggestion(
        self,
        client: OpenAICompatibleAIClient,
        suggestion: MethodSelectionSuggestion,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 3,
        top_k_scripts: int = 2,
        system_prompt: str | None = None,
    ) -> tuple[list[str], dict[str, Any], str, str]:
        prompt, preview = self.build_parameter_prompt_for_step_from_files(
            suggestion=suggestion,
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )
        try:
            response = client.query(
                user_prompt=prompt,
                system_prompt=system_prompt or build_parameter_recommendation_system_prompt(),
            )
        except Exception as exc:
            if isinstance(exc, AIClientError):
                raise
            raise AIClientError(str(exc)) from exc
        response_text = str(response.get("response_text", ""))
        payload = parse_parameter_recommendation_response_text(response_text)
        notes = self.apply_parameter_recommendation(suggestion, payload)
        return notes, preview, prompt, response_text


def _derive_parameter_values_for_method(
    method_name: str,
    event: dict[str, Any],
    ai_observation_text: str,
    session_dir: Path | None = None,
) -> ParameterDerivationResult:
    normalized_method = method_name.strip().lower()
    session_aware_deriver = SESSION_AWARE_METHOD_PARAMETER_DERIVERS.get(normalized_method)
    if session_aware_deriver is not None:
        return session_aware_deriver(event, ai_observation_text, session_dir)
    deriver = METHOD_PARAMETER_DERIVERS.get(normalized_method)
    if deriver is not None:
        return deriver(event, ai_observation_text)
    return _derive_generic_parameter_values(event, ai_observation_text)


def _derive_find_control_by_name_values(
    event: dict[str, Any],
    ai_observation_text: str,
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    observation = _parse_ai_observation_text(ai_observation_text)
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    ui_help_text = str(ui_element.get("help_text", "")).strip()
    ui_help_text_fallback = str(ui_element.get("help_text_fallback", "")).strip()
    ui_name = str(ui_element.get("name", "")).strip()
    ui_name_fallbacks = [
        str(item).strip()
        for item in ui_element.get("name_fallbacks", [])
        if str(item).strip()
    ] if isinstance(ui_element.get("name_fallbacks", []), list) else []
    label = str(observation.get("label", "")).strip()
    if ui_help_text:
        derived_values["HelpText"] = ui_help_text
        evidence_map["HelpText"] = [f"事件明细.ui_element.help_text={ui_help_text}"]
    elif ui_help_text_fallback:
        derived_values["HelpText"] = ui_help_text_fallback
        evidence_map["HelpText"] = [f"事件明细.ui_element.help_text_fallback={ui_help_text_fallback}"]
    elif ui_name:
        derived_values["Name"] = ui_name
        evidence_map["Name"] = [f"事件明细.ui_element.name={ui_name}"]
    elif len(ui_name_fallbacks) == 1:
        derived_values["Name"] = ui_name_fallbacks[0]
        evidence_map["Name"] = [f"事件明细.ui_element.name_fallbacks[0]={ui_name_fallbacks[0]}"]
    elif label:
        derived_values["Name"] = label
        evidence_map["Name"] = [f"AI看图: label={label}"]
    else:
        missing_map["Name"] = "事件明细中没有可用的 help_text/help_text_fallback/name/单个 name_fallbacks，AI看图中也没有可用的 label。"

    direction = str(observation.get("direction", "")).strip().lower()
    if direction and direction != "self":
        derived_values["direction"] = direction
        evidence_map["direction"] = [f"AI看图: direction={observation.get('direction', '')}"]

    control_type = str(ui_element.get("control_type", "")).strip()
    if control_type:
        evidence_map["controlTypeList"] = [f"事件明细.ui_element.control_type={control_type}"]
    else:
        control_type = str(observation.get("control_type", "")).strip()
        if control_type:
            evidence_map["controlTypeList"] = [f"AI看图: control_type={control_type}"]
    if control_type:
        derived_values["controlTypeList"] = [control_type]

    scrollable = observation.get("scroll")
    if isinstance(scrollable, bool):
        derived_values["scrollable"] = scrollable
        evidence_map["scrollable"] = [f"AI看图: scroll={str(scrollable).lower()}"]

    table = observation.get("table")
    if scrollable is True or table is True:
        click_point = _extract_click_point(event)
        if click_point is not None:
            derived_values["clickPoint"] = click_point
            click_point_evidence = []
            if scrollable is True:
                click_point_evidence.append("AI看图: scroll=true")
            if table is True:
                click_point_evidence.append("AI看图: table=true")
            click_point_evidence.append(f"事件明细.mouse=({click_point[0]}, {click_point[1]})")
            evidence_map["clickPoint"] = click_point_evidence
        else:
            missing_map["clickPoint"] = "AI看图判断 scroll=true 或 table=true，但事件明细中没有可用的 mouse.x/mouse.y，无法生成 clickPoint。"

    cell_value = _derive_cell_value(event, observation)
    if cell_value is not None and cell_value != "":
        derived_values["cellValue"] = cell_value
        evidence_map["cellValue"] = [_build_cell_value_evidence(event, cell_value, observation)]

    return derived_values, evidence_map, missing_map


def _derive_generic_parameter_values(
    event: dict[str, Any],
    ai_observation_text: str,
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    observation = _parse_ai_observation_text(ai_observation_text)
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    label = str(observation.get("label", "")).strip() or str(ui_element.get("name", "")).strip()
    if label:
        derived_values["Name"] = label
        evidence_map["Name"] = [f"AI看图/事件明细 label={label}"]

    help_text = str(observation.get("helptext", "")).strip() or str(ui_element.get("help_text", "") or ui_element.get("help_text_fallback", "")).strip()
    if help_text:
        derived_values["HelpText"] = help_text
        evidence_map["HelpText"] = [f"AI看图/事件明细 helptext={help_text}"]

    control_type = str(observation.get("control_type", "")).strip() or str(ui_element.get("control_type", "")).strip()
    if control_type:
        derived_values["controlTypeList"] = [control_type]
        evidence_map["controlTypeList"] = [f"AI看图/事件明细 control_type={control_type}"]

    click_point = _extract_click_point(event)
    if click_point is not None:
        derived_values["clickPoint"] = click_point
        evidence_map["clickPoint"] = [f"事件明细.mouse=({click_point[0]}, {click_point[1]})"]

    cell_value = _derive_cell_value(event, observation)
    if cell_value is not None and cell_value != "":
        derived_values["cellValue"] = cell_value
        evidence_map["cellValue"] = [_build_cell_value_evidence(event, cell_value, observation)]

    return derived_values, evidence_map, missing_map


def _derive_click_by_ocr_windows_values(
    event: dict[str, Any],
    ai_observation_text: str,
) -> ParameterDerivationResult:
    observation = _parse_ai_observation_text(ai_observation_text)
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    query_candidates: list[tuple[str, str]] = []
    for key in ("query", "label", "text", "name", "helptext", "help_text"):
        value = str(observation.get(key, "")).strip()
        if _is_useful_ocr_query(value):
            query_candidates.append((value, f"AI看图.{key}={value}"))

    for key in ("name", "help_text", "help_text_fallback"):
        value = str(ui_element.get(key, "")).strip()
        if _is_useful_ocr_query(value):
            query_candidates.append((value, f"事件明细.ui_element.{key}={value}"))

    name_fallbacks = ui_element.get("name_fallbacks", [])
    if isinstance(name_fallbacks, list):
        fallback_values = [str(item).strip() for item in name_fallbacks if _is_useful_ocr_query(str(item).strip())]
        if len(fallback_values) == 1:
            query_candidates.append((fallback_values[0], f"事件明细.ui_element.name_fallbacks[0]={fallback_values[0]}"))

    if query_candidates:
        query, evidence = query_candidates[0]
        derived_values["query"] = query
        evidence_map["query"] = [evidence]
    else:
        missing_map["query"] = "事件明细和 AI看图结果中没有可用于 OCR 的目标文本。"

    return derived_values, evidence_map, missing_map


def _derive_select_data_grid_rows_values(
    event: dict[str, Any],
    ai_observation_text: str,
) -> ParameterDerivationResult:
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    row_value = _extract_datagrid_row_value_from_ai_text(ai_observation_text)
    if row_value:
        derived_values["rowValue"] = row_value
        evidence_map["rowValue"] = [f"AI看图提取红框表格行定位列值={json.dumps(row_value, ensure_ascii=False)}"]
    else:
        missing_map["rowValue"] = "AI看图结果中没有可解析的 rowValue/row_locator 字典，无法生成 SelectDataGridRows.rowValue。"

    derived_values["multiSelect"] = False
    evidence_map["multiSelect"] = ["SelectDataGridRows 默认按当前红框选中行定位单行，因此 multiSelect=false。"]

    click_point = _extract_click_point(event)
    if click_point is not None:
        derived_values["clickPoint"] = click_point
        evidence_map["clickPoint"] = [f"事件明细.mouse=({click_point[0]}, {click_point[1]})"]
    else:
        missing_map["clickPoint"] = "事件明细中没有可用的 mouse.x/mouse.y，无法生成 SelectDataGridRows.clickPoint。"

    return derived_values, evidence_map, missing_map


def _derive_matching_click_values(
    event: dict[str, Any],
    _ai_observation_text: str,
    session_dir: Path | None,
) -> ParameterDerivationResult:
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    if session_dir is None:
        missing_map["sourcePath"] = "缺少 Session 目录，无法根据截图裁剪 MatchingClick 模板图。"
        return derived_values, evidence_map, missing_map

    source_screenshot = _resolve_matching_click_source_screenshot(event, session_dir)
    if source_screenshot is None:
        missing_map["sourcePath"] = "事件明细中没有可用的 screenshot/media 图片，无法裁剪 MatchingClick 模板图。"
        return derived_values, evidence_map, missing_map

    rect = _extract_matching_click_rectangle(event)
    if rect is None:
        missing_map["sourcePath"] = "事件明细中没有可用的 ui_element.rectangle/target_rect，无法裁剪 MatchingClick 模板图。"
        return derived_values, evidence_map, missing_map

    try:
        relative_template_path, crop_box = _create_matching_click_template_image(source_screenshot, session_dir, event, rect)
    except ValueError as exc:
        missing_map["sourcePath"] = str(exc)
        return derived_values, evidence_map, missing_map
    except Exception as exc:
        missing_map["sourcePath"] = f"裁剪 MatchingClick 模板图失败: {exc}"
        return derived_values, evidence_map, missing_map

    derived_values["sourcePath"] = relative_template_path
    evidence_map["sourcePath"] = [
        f"事件截图={_format_session_relative_path(source_screenshot, session_dir)}",
        f"事件 rectangle={rect}，为去除红框内缩后裁剪 crop_box={list(crop_box)}",
        f"模板图已保存到 {relative_template_path}",
    ]
    return derived_values, evidence_map, missing_map


def _derive_scan_dll_values(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    comment_text = _extract_event_comment_text(event)
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}
    parameter_names = ["funcName"]

    if not comment_text:
        for name in parameter_names:
            missing_map[name] = f"当前行 Comment 列为空，无法生成 ScanDll.{name}。"
        return derived_values, evidence_map, missing_map

    parsed_values = _parse_scan_dll_comment_values(comment_text)
    for name in parameter_names:
        if name not in parsed_values:
            continue
        value = _normalize_scan_dll_parameter_value(name, parsed_values[name])
        if value is None or value == "":
            continue
        derived_values[name] = value
        evidence_map[name] = [f"Comment列: {name}={parsed_values[name]}"]

    if "funcName" not in derived_values:
        func_name = _infer_scan_dll_func_name_from_comment(comment_text)
        if func_name:
            derived_values["funcName"] = func_name
            evidence_map["funcName"] = [f"Comment列文本包含 {func_name} 相关关键词。"]

    for name in parameter_names:
        if name not in derived_values:
            missing_map[name] = f"Comment列未提供可解析的 ScanDll.{name}。"

    return derived_values, evidence_map, missing_map


def _derive_get_screenshot_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    media_items = event.get("media", []) if isinstance(event.get("media", []), list) else []
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    file_name = _extract_first_media_file_name(media_items)
    if file_name:
        derived_values["filePath"] = file_name
        evidence_map["filePath"] = [f"事件明细.media[0].path 文件名={file_name}"]
    else:
        missing_map["filePath"] = "事件明细中没有可用的 media.path。"

    rect = _extract_agent_interface_rect(media_items)
    if rect is not None:
        derived_values["rect"] = rect
        evidence_map["rect"] = [f"事件明细.media[0].region={rect}"]

    return derived_values, evidence_map, missing_map


def _derive_wait_for_exists_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    media_items = event.get("media", []) if isinstance(event.get("media", []), list) else []
    note_text = str(event.get("note", "")).strip()
    wait_condition = _extract_wait_condition(event)
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    source_paths = [
        str(item.get("path", "")).strip()
        for item in media_items
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    ]
    if len(source_paths) == 1:
        derived_values["sourcePath"] = source_paths[0]
        evidence_map["sourcePath"] = [f"事件明细.media[0].path={source_paths[0]}"]
    elif len(source_paths) > 1:
        derived_values["sourcePath"] = source_paths
        evidence_map["sourcePath"] = [f"事件明细.media.path 列表={source_paths}"]
    else:
        missing_map["sourcePath"] = "事件明细中没有可用的 media.path。"

    if note_text:
        derived_values["Description"] = note_text
        evidence_map["Description"] = [f"事件明细.note={note_text}"]
    else:
        missing_map["Description"] = "事件明细.note 为空，无法生成 WaitForExists.Description。"

    if wait_condition == "disappear":
        derived_values["exist"] = False
        evidence_map["exist"] = ["事件明细.additional_details.wait_condition=disappear，因此 WaitForExists.exist=false。"]

    return derived_values, evidence_map, missing_map


def _extract_wait_condition(event: dict[str, Any]) -> str:
    details = event.get("additional_details", {}) if isinstance(event.get("additional_details", {}), dict) else {}
    raw_condition = str(details.get("wait_condition", "") or "").strip().lower()
    if raw_condition in {"disappear", "disappearance", "hidden", "absent", "not_exists", "not_exist", "wait_for_disappearance"}:
        return "disappear"
    if raw_condition in {"appear", "appearance", "visible", "present", "exists", "exist", "wait_for_appearance"}:
        return "appear"

    raw_appearance = details.get("wait_for_appearance")
    if isinstance(raw_appearance, bool):
        return "appear" if raw_appearance else "disappear"
    if isinstance(raw_appearance, str):
        lowered = raw_appearance.strip().lower()
        if lowered in {"false", "0", "no", "n", "disappear", "disappearance"}:
            return "disappear"
        if lowered in {"true", "1", "yes", "y", "appear", "appearance"}:
            return "appear"

    return "appear"


def _derive_agent_interface_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    checkpoint = event.get("checkpoint", {}) if isinstance(event.get("checkpoint", {}), dict) else {}
    media_items = event.get("media", []) if isinstance(event.get("media", []), list) else []
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    step_comment = str(checkpoint.get("step_comment", "")).strip()
    if step_comment:
        derived_values["Description"] = step_comment
        evidence_map["Description"] = [f"事件明细.checkpoint.step_comment={step_comment}"]

    title = str(checkpoint.get("title", "")).strip()
    if title:
        derived_values["Expect"] = title
        evidence_map["Expect"] = [f"事件明细.checkpoint.title={title}"]

    query = str(checkpoint.get("query", "")).strip()
    if query:
        derived_values["query"] = query
        evidence_map["query"] = [f"事件明细.checkpoint.query={query}"]

    raw_enable_thinking = checkpoint.get("enableThinking", checkpoint.get("enable_thinking"))
    if raw_enable_thinking is False:
        derived_values["enableThinking"] = False
        evidence_map["enableThinking"] = ["事件明细.checkpoint.enableThinking=false，关闭 Thinking 时显式传 enableThinking=False"]
    elif isinstance(raw_enable_thinking, str) and raw_enable_thinking.strip().lower() in {"0", "false", "no", "off"}:
        derived_values["enableThinking"] = False
        evidence_map["enableThinking"] = [f"事件明细.checkpoint.enableThinking={raw_enable_thinking}，关闭 Thinking 时显式传 enableThinking=False"]

    rect = _extract_agent_interface_rect(media_items)
    if rect is not None:
        derived_values["rect"] = rect
        evidence_map["rect"] = [f"事件明细.media[0].region={rect}"]

    image_list = _extract_agent_interface_image_list(media_items)
    if image_list:
        derived_values["imageList"] = image_list
        evidence_map["imageList"] = [f"事件明细.media[1:] 路径文件名={image_list}"]

    screenshot_path = str(event.get("screenshot", "") or "").strip()
    if screenshot_path:
        derived_values["imageSaveFile"] = screenshot_path
        evidence_map["imageSaveFile"] = [f"事件明细.screenshot={screenshot_path}"]

    return derived_values, evidence_map, missing_map


def _derive_manual_check_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    note_text = str(event.get("note", "")).strip()
    checkpoint = event.get("checkpoint", {}) if isinstance(event.get("checkpoint", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    if note_text:
        derived_values["Description"] = note_text
        derived_values["Expect"] = note_text
        evidence_map["Description"] = [f"事件明细.note={note_text}"]
        evidence_map["Expect"] = [f"事件明细.note={note_text}"]
    elif checkpoint:
        step_comment = str(checkpoint.get("step_comment", "")).strip()
        title = str(checkpoint.get("title", "")).strip()
        query = str(checkpoint.get("query", "")).strip()
        description_text = step_comment or title or query
        expect_text = title or query or step_comment
        if description_text:
            derived_values["Description"] = description_text
            source_key = "step_comment" if step_comment else "title" if title else "query"
            evidence_map["Description"] = [f"事件明细.checkpoint.{source_key}={description_text}"]
        if expect_text:
            derived_values["Expect"] = expect_text
            source_key = "title" if title else "query" if query else "step_comment"
            evidence_map["Expect"] = [f"事件明细.checkpoint.{source_key}={expect_text}"]
    else:
        empty_text = "Current content is empty"
        derived_values["Description"] = empty_text
        derived_values["Expect"] = empty_text
        evidence_map["Description"] = ["事件明细.note 为空，使用固定英文占位。"]
        evidence_map["Expect"] = ["事件明细.note 为空，使用固定英文占位。"]

    if "Description" not in derived_values and "Expect" not in derived_values:
        empty_text = "Current content is empty"
        derived_values["Description"] = empty_text
        derived_values["Expect"] = empty_text
        evidence_map["Description"] = ["事件明细.note/checkpoint 文本为空，使用固定英文占位。"]
        evidence_map["Expect"] = ["事件明细.note/checkpoint 文本为空，使用固定英文占位。"]
    elif "Description" not in derived_values and "Expect" in derived_values:
        derived_values["Description"] = derived_values["Expect"]
        evidence_map["Description"] = ["ManualCheck.Description 使用 Expect 同文本。"]
    elif "Expect" not in derived_values and "Description" in derived_values:
        derived_values["Expect"] = derived_values["Description"]
        evidence_map["Expect"] = ["ManualCheck.Expect 使用 Description 同文本。"]

    return derived_values, evidence_map, missing_map


def _derive_send_keys_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    keyboard = event.get("keyboard", {}) if isinstance(event.get("keyboard", {}), dict) else {}
    additional_details = event.get("additional_details", {}) if isinstance(event.get("additional_details", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    text_value = _derive_send_keys_text_value(keyboard)
    key_value = _derive_send_keys_key_value(keyboard)

    if text_value:
        derived_values["text"] = text_value
        evidence_map["text"] = [_build_send_keys_text_evidence(keyboard, text_value)]
    elif key_value:
        derived_values["key"] = key_value
        evidence_map["key"] = [_build_send_keys_key_evidence(keyboard, key_value)]

    combined_action = str(additional_details.get("combined_action", "")).strip()
    if combined_action:
        evidence_map.setdefault("text", evidence_map.get("text", []))
        evidence_map.setdefault("key", evidence_map.get("key", []))
        if "text" in evidence_map:
            evidence_map["text"].append(f"事件明细.additional_details.combined_action={combined_action}")
        if "key" in evidence_map:
            evidence_map["key"].append(f"事件明细.additional_details.combined_action={combined_action}")

    return derived_values, evidence_map, missing_map


def _derive_click_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    button_name = _normalize_mouse_button(str(mouse.get("button", "")).strip())
    if button_name:
        derived_values["button"] = button_name
        evidence_map["button"] = [f"事件明细.mouse.button={mouse.get('button', '')}"]
    else:
        missing_map["button"] = "事件明细缺少可识别的鼠标按键。"

    x = mouse.get("x")
    y = mouse.get("y")
    if isinstance(x, int):
        derived_values["x"] = x
        evidence_map["x"] = [f"事件明细.mouse.x={x}"]
    else:
        missing_map["x"] = "事件明细缺少 x 坐标。"
    if isinstance(y, int):
        derived_values["y"] = y
        evidence_map["y"] = [f"事件明细.mouse.y={y}"]
    else:
        missing_map["y"] = "事件明细缺少 y 坐标。"

    if "x" in derived_values and "y" in derived_values:
        derived_values["absolute"] = True
        evidence_map["absolute"] = ["Click 类型按屏幕绝对坐标点击，参数推荐使用 absolute=True"]

    return derived_values, evidence_map, missing_map


def _derive_wheel_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    scroll = event.get("scroll", {}) if isinstance(event.get("scroll", {}), dict) else {}
    mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    dy = scroll.get("dy")
    if isinstance(dy, int):
        derived_values["isDown"] = dy < 0
        evidence_map["isDown"] = [f"事件明细.scroll.dy={dy}，dy<0 视为向下滚动"]
    else:
        missing_map["isDown"] = "事件明细缺少 scroll.dy，无法判断滚动方向。"

    wheel_times = scroll.get("step_count")
    if isinstance(wheel_times, int) and wheel_times > 0:
        derived_values["wheelTimes"] = wheel_times
        evidence_map["wheelTimes"] = [f"事件明细.scroll.step_count={wheel_times}"]
    else:
        missing_map["wheelTimes"] = "事件明细缺少有效的 scroll.step_count。"

    x, y = _extract_scroll_point(scroll, mouse)
    if x is not None and y is not None:
        derived_values["x"] = x
        derived_values["y"] = y
        evidence_map["x"] = [f"事件明细滚轮坐标=({x}, {y})"]
        evidence_map["y"] = [f"事件明细滚轮坐标=({x}, {y})"]
    else:
        missing_map["x"] = "事件明细缺少滚轮坐标。"
        missing_map["y"] = "事件明细缺少滚轮坐标。"

    return derived_values, evidence_map, missing_map


def _derive_wheel_values_for_session(
    event: dict[str, Any],
    _ai_observation_text: str,
    session_dir: Path | None,
) -> ParameterDerivationResult:
    derived_values, evidence_map, missing_map = _derive_wheel_values(event)
    if session_dir is None:
        return derived_values, evidence_map, missing_map

    source_screenshot = _resolve_matching_click_source_screenshot(event, session_dir)
    rect = _extract_matching_click_rectangle(event)
    if source_screenshot is None:
        missing_map["targetPath"] = "事件明细中没有可用的 screenshot/media 图片，无法裁剪 Wheel.targetPath。"
        return derived_values, evidence_map, missing_map
    if rect is None:
        missing_map["targetPath"] = "事件明细中没有可用的 ui_element.rectangle/target_rect/rect，无法裁剪 Wheel.targetPath。"
        return derived_values, evidence_map, missing_map

    try:
        relative_target_path, crop_box = _create_wheel_target_image(source_screenshot, session_dir, event, rect)
    except Exception as exc:
        missing_map["targetPath"] = f"根据事件截图和 rect 裁剪 Wheel.targetPath 失败: {exc}"
        return derived_values, evidence_map, missing_map

    derived_values["targetPath"] = relative_target_path
    derived_values.pop("wheelTimes", None)
    evidence_map.pop("wheelTimes", None)
    missing_map.pop("wheelTimes", None)
    derived_values["clickBeforeWheel"] = True
    evidence_map["targetPath"] = [
        f"事件截图={_format_session_relative_path(source_screenshot, session_dir)}",
        f"事件 rect={rect}，按 MatchingClick 规则内缩后裁剪 crop_box={list(crop_box)}",
        f"Wheel 目标图已保存到 {relative_target_path}",
    ]
    evidence_map["clickBeforeWheel"] = ["已生成 Wheel.targetPath，因此默认先点击目标图位置再滚轮，clickBeforeWheel=True。"]
    return derived_values, evidence_map, missing_map


def _derive_drag_drop_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    coordinate_pairs = {
        "x1": ("start_x", mouse.get("start_x")),
        "y1": ("start_y", mouse.get("start_y")),
        "x2": ("end_x", mouse.get("end_x")),
        "y2": ("end_y", mouse.get("end_y")),
    }
    for name, (source_name, value) in coordinate_pairs.items():
        if isinstance(value, int):
            derived_values[name] = value
            evidence_map[name] = [f"事件明细.mouse.{source_name}={value}"]
        else:
            missing_map[name] = f"事件明细缺少 {name} 对应坐标。"

    has_all_coordinates = all(name in derived_values for name in ("x1", "y1", "x2", "y2"))
    if has_all_coordinates:
        derived_values["absolute"] = True
        evidence_map["absolute"] = ["录制事件中的拖拽坐标为屏幕绝对坐标，参数推荐使用 absolute=True"]

    button_name = _normalize_mouse_button(str(mouse.get("button", "")).strip())
    if button_name:
        derived_values["button"] = button_name
        evidence_map["button"] = [f"事件明细.mouse.button={mouse.get('button', '')}"]
    else:
        missing_map["button"] = "事件明细缺少可识别的鼠标按键。"

    return derived_values, evidence_map, missing_map


def _derive_empty_values(_event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return {}, {}, {}


def _derive_get_screenshot_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_get_screenshot_values(event)


def _derive_wait_for_exists_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_wait_for_exists_values(event)


def _derive_agent_interface_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_agent_interface_values(event)


def _derive_manual_check_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_manual_check_values(event)


def _derive_send_keys_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_send_keys_values(event)


def _derive_click_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_click_values(event)


def _derive_wheel_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_wheel_values(event)


def _derive_drag_drop_values_from_context(event: dict[str, Any], _ai_observation_text: str) -> ParameterDerivationResult:
    return _derive_drag_drop_values(event)


METHOD_PARAMETER_DERIVERS: dict[str, ParameterDeriver] = {
    "performscan": _derive_empty_values,
    "findcontrolbyname": _derive_find_control_by_name_values,
    "clickbyocrwindows": _derive_click_by_ocr_windows_values,
    "selectdatagridrows": _derive_select_data_grid_rows_values,
    "scandll": _derive_scan_dll_values,
    "getscreenshot": _derive_get_screenshot_values_from_context,
    "waitforexists": _derive_wait_for_exists_values_from_context,
    "agentinterface": _derive_agent_interface_values_from_context,
    "manualcheck": _derive_manual_check_values_from_context,
    "sendkeys": _derive_send_keys_values_from_context,
    "click": _derive_click_values_from_context,
    "wheel": _derive_wheel_values_from_context,
    "dragdrop": _derive_drag_drop_values_from_context,
}


SESSION_AWARE_METHOD_PARAMETER_DERIVERS: dict[str, SessionAwareParameterDeriver] = {
    "matchingclick": _derive_matching_click_values,
    "wheel": _derive_wheel_values_for_session,
}


def _build_parameter_suggestions_from_schema(
    suggestion: MethodSelectionSuggestion,
    derived_values: dict[str, Any],
    evidence_map: dict[str, list[str]],
    missing_map: dict[str, str],
) -> list[MethodParameterSuggestion]:
    schema_fields = _extract_schema_fields(suggestion)
    ordered_names = [str(item.get("name", "")).strip() for item in schema_fields if str(item.get("name", "")).strip()]
    if not ordered_names:
        ordered_names = list(derived_values.keys())
    ordered_names = _reorder_parameter_names_for_method(str(suggestion.method_name or ""), ordered_names)

    suggestions: list[MethodParameterSuggestion] = []
    seen_names: set[str] = set()
    for name in ordered_names:
        seen_names.add(name)
        required = any(str(item.get("name", "")).strip() == name and bool(item.get("required", False)) for item in schema_fields)
        value = derived_values.get(name)
        missing_reason = missing_map.get(name, "")
        if _should_skip_missing_parameter(suggestion, name, value, missing_reason):
            continue
        if value is None and not missing_reason and required:
            missing_reason = f"未能从当前步骤中提取必填参数 {name}。"
        if value is None and not missing_reason:
            continue
        confidence = 1.0 if value is not None else 0.0
        suggestions.append(
            MethodParameterSuggestion(
                name=name,
                suggested_value=value,
                confidence=confidence,
                evidence=evidence_map.get(name, []),
                missing_reason=missing_reason,
            )
        )

    for name, value in derived_values.items():
        if name in seen_names:
            continue
        suggestions.append(
            MethodParameterSuggestion(
                name=name,
                suggested_value=value,
                confidence=1.0,
                evidence=evidence_map.get(name, []),
                missing_reason="",
            )
        )
    return _reorder_parameter_suggestions(str(suggestion.method_name or ""), suggestions)


def _normalize_parameter_suggestions_for_method(
    method_name: str,
    suggestions: list[MethodParameterSuggestion],
) -> list[MethodParameterSuggestion]:
    normalized_method = str(method_name or "").strip().lower()
    if normalized_method != "findcontrolbyname":
        return suggestions

    filtered: list[MethodParameterSuggestion] = []
    for item in suggestions:
        normalized_item = _normalize_find_control_parameter_item(item)
        if normalized_item is None:
            continue
        filtered.append(normalized_item)
    return filtered


def _normalize_find_control_parameter_item(item: MethodParameterSuggestion) -> MethodParameterSuggestion | None:
    if _should_skip_find_control_parameter(item):
        return None

    name = str(item.name or "").strip().lower()
    if name != "paramdict" or not isinstance(item.suggested_value, dict):
        return item

    filtered_value: dict[str, Any] = {}
    for key, value in item.suggested_value.items():
        nested_item = MethodParameterSuggestion(name=str(key), suggested_value=value)
        if _should_skip_find_control_parameter(nested_item):
            continue
        filtered_value[key] = value

    if not filtered_value:
        return None

    return MethodParameterSuggestion(
        name=item.name,
        suggested_value=filtered_value,
        confidence=item.confidence,
        evidence=list(item.evidence),
        missing_reason=item.missing_reason,
    )


def _should_skip_find_control_parameter(item: MethodParameterSuggestion) -> bool:
    name = str(item.name or "").strip().lower()
    if not name:
        return False
    if name in {"point", "x", "y", "absolute", "coordinate", "coordinates"}:
        return True
    if name in {"scrollable", "scroll"}:
        return _is_false_like_value(item.suggested_value)
    if name in {"table", "is_table", "istable"}:
        return _is_false_like_value(item.suggested_value)
    return False


def _is_false_like_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value.strip().lower() == "false"
    return False


def _reorder_parameter_names_for_method(method_name: str, ordered_names: list[str]) -> list[str]:
    priority_names = ["Name", "HelpText", "direction", "scrollable", "cellValue"]
    reordered = list(ordered_names)
    for name in reversed(priority_names):
        reordered = _move_name_to_front(reordered, name)
    normalized_method = str(method_name or "").strip().lower()
    if normalized_method == "clickbyocrwindows":
        for name in reversed(["query", "singleMatch", "matchIndex", "returnMatch"]):
            reordered = _move_name_to_front(reordered, name)
        return reordered
    if normalized_method == "selectdatagridrows":
        for name in reversed(["rowValue", "clickPoint", "rowIndex", "multiSelect", "headerList"]):
            reordered = _move_name_to_front(reordered, name)
        return reordered
    if normalized_method == "matchingclick":
        for name in reversed(["sourcePath", "sigleMatch", "rect"]):
            reordered = _move_name_to_front(reordered, name)
        return reordered
    if normalized_method == "wheel":
        for name in reversed(["targetPath", "clickBeforeWheel", "isDown", "wheelTimes", "x", "y"]):
            if name not in reordered:
                reordered.insert(0, name)
                continue
            reordered = _move_name_to_front(reordered, name)
        return reordered
    if normalized_method == "scandll":
        for name in reversed(["funcName"]):
            reordered = _move_name_to_front(reordered, name)
        return reordered
    if normalized_method == "findcontrolbyname":
        return reordered
    if normalized_method == "click":
        for name in reversed(["button", "x", "y", "absolute"]):
            reordered = _move_name_to_front(reordered, name)
        return reordered
    return reordered


def _reorder_parameter_suggestions(method_name: str, suggestions: list[MethodParameterSuggestion]) -> list[MethodParameterSuggestion]:
    priority_order = {"Name": 0, "HelpText": 1, "direction": 2, "scrollable": 3, "cellValue": 4}
    normalized_method = str(method_name or "").strip().lower()
    if normalized_method == "clickbyocrwindows":
        priority_order = {"query": 0, "singleMatch": 1, "matchIndex": 2, "returnMatch": 3}
    if normalized_method == "selectdatagridrows":
        priority_order = {"rowValue": 0, "clickPoint": 1, "rowIndex": 2, "multiSelect": 3, "headerList": 4}
    if normalized_method == "matchingclick":
        priority_order = {"sourcePath": 0, "sigleMatch": 1, "rect": 2}
    if normalized_method == "wheel":
        priority_order = {"targetPath": 0, "clickBeforeWheel": 1, "isDown": 2, "wheelTimes": 3, "x": 4, "y": 5}
    if normalized_method == "scandll":
        priority_order = {"funcName": 0}
    if normalized_method == "click":
        priority_order = {"button": 0, "x": 1, "y": 2, "absolute": 3}
    indexed = list(enumerate(suggestions))
    indexed.sort(key=lambda item: (priority_order.get(str(item[1].name), 999), item[0]))
    return [item for _index, item in indexed]


def _move_name_to_front(names: list[str], target_name: str) -> list[str]:
    target_indexes = [index for index, item in enumerate(names) if item == target_name]
    if not target_indexes:
        return names
    target_index = target_indexes[0]
    if target_index == 0:
        return names
    reordered = list(names)
    target_value = reordered.pop(target_index)
    reordered.insert(0, target_value)
    return reordered


def _extract_schema_fields(suggestion: MethodSelectionSuggestion) -> list[dict[str, Any]]:
    candidate_payload = suggestion.candidate_payload if isinstance(suggestion.candidate_payload, dict) else {}
    parameters = candidate_payload.get("parameters", []) if isinstance(candidate_payload.get("parameters", []), list) else []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if str(parameter.get("name", "")).strip() != "paramDict":
            continue
        schema_fields = parameter.get("schema_fields", []) if isinstance(parameter.get("schema_fields", []), list) else []
        filtered_fields = [item for item in schema_fields if isinstance(item, dict)]
        if str(suggestion.method_name or "").strip().lower() == "sendkeys":
            return [item for item in filtered_fields if str(item.get("name", "")).strip() in {"text", "key"}]
        if str(suggestion.method_name or "").strip().lower() == "click":
            return [item for item in filtered_fields if str(item.get("name", "")).strip() in {"absolute", "x", "y"}]
        if str(suggestion.method_name or "").strip().lower() == "scandll":
            return [item for item in filtered_fields if str(item.get("name", "")).strip() == "funcName"]
        return filtered_fields
    return []


def _parse_ai_observation_text(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for part in str(text or "").split("|"):
        segment = part.strip()
        if not segment or "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        normalized_key = key.strip().lower()
        normalized_value = value.strip()
        if not normalized_key:
            continue
        if normalized_key in {"scroll", "table"}:
            lowered = normalized_value.lower()
            if lowered in {"true", "false"}:
                payload[normalized_key] = lowered == "true"
                continue
        payload[normalized_key] = normalized_value
    return payload


def _extract_event_comment_text(event: dict[str, Any]) -> str:
    details = event.get("additional_details", {}) if isinstance(event.get("additional_details", {}), dict) else {}
    checkpoint = event.get("checkpoint", {}) if isinstance(event.get("checkpoint", {}), dict) else {}
    candidates = [
        details.get("viewer_comment"),
        details.get("comment"),
        details.get("note"),
        event.get("comment"),
        event.get("note"),
        checkpoint.get("step_comment"),
        checkpoint.get("title"),
    ]
    for item in candidates:
        value = str(item or "").strip()
        if value:
            return value
    return ""


def _parse_scan_dll_comment_values(comment_text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    parsed_payload = _parse_mapping_text(comment_text)
    if isinstance(parsed_payload, dict):
        _collect_scan_dll_mapping_values(parsed_payload, result)

    key_alias_pattern = "|".join(re.escape(key) for key in sorted(_SCAN_DLL_KEY_ALIASES.keys(), key=len, reverse=True))
    pattern = re.compile(
        rf"(?P<key>{key_alias_pattern})\s*[:=]\s*(?P<value>.*?)(?=(?:\s|[,;|])(?:{key_alias_pattern})\s*[:=]|[;|\n\r]+|$)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(comment_text):
        canonical_name = _normalize_scan_dll_comment_key(match.group("key"))
        if not canonical_name or canonical_name in result:
            continue
        value = match.group("value").strip().strip(",，;；| ")
        if value:
            result[canonical_name] = value
    return result


def _collect_scan_dll_mapping_values(payload: dict[Any, Any], result: dict[str, Any]) -> None:
    nested_payloads: list[dict[Any, Any]] = []
    for key, value in payload.items():
        canonical_name = _normalize_scan_dll_comment_key(str(key))
        if canonical_name and canonical_name not in result:
            result[canonical_name] = value
        elif isinstance(value, dict) and str(key).strip().lower() in {"paramdict", "params", "parameters", "scandll"}:
            nested_payloads.append(value)
    for item in nested_payloads:
        _collect_scan_dll_mapping_values(item, result)


_SCAN_DLL_KEY_ALIASES: dict[str, str] = {
    "funcName": "funcName",
    "funcname": "funcName",
    "functionName": "funcName",
    "functionname": "funcName",
    "function": "funcName",
    "func": "funcName",
    "dataHex": "dataHex",
    "datahex": "dataHex",
    "hex": "dataHex",
    "hexData": "dataHex",
    "hexdata": "dataHex",
    "data": "dataHex",
    "portRadio": "portRadio",
    "portradio": "portRadio",
    "port": "portRadio",
    "baud": "portRadio",
    "baudRate": "portRadio",
    "baudrate": "portRadio",
    "timeOut": "timeOut",
    "timeout": "timeOut",
    "waitPropertyName": "waitPropertyName",
    "waitpropertyname": "waitPropertyName",
    "propertyName": "waitPropertyName",
    "propertyname": "waitPropertyName",
    "waitPropertyVlaue": "waitPropertyVlaue",
    "waitpropertyvlaue": "waitPropertyVlaue",
    "waitPropertyValue": "waitPropertyVlaue",
    "waitpropertyvalue": "waitPropertyVlaue",
    "propertyValue": "waitPropertyVlaue",
    "propertyvalue": "waitPropertyVlaue",
    "waitTime": "waitTime",
    "waittime": "waitTime",
    "sleep": "waitTime",
}


def _normalize_scan_dll_comment_key(raw_key: str) -> str:
    stripped = str(raw_key or "").strip()
    if not stripped:
        return ""
    if stripped in _SCAN_DLL_KEY_ALIASES:
        return _SCAN_DLL_KEY_ALIASES[stripped]
    compact = re.sub(r"[^0-9A-Za-z]+", "", stripped).lower()
    return _SCAN_DLL_KEY_ALIASES.get(compact, "")


def _normalize_scan_dll_parameter_value(name: str, value: Any) -> Any:
    if name == "funcName":
        return _normalize_scan_dll_func_name(str(value or ""))
    if name == "dataHex":
        return _normalize_scan_dll_data_hex(str(value or ""))
    if name == "portRadio":
        return _coerce_int(value)
    if name in {"timeOut", "waitTime"}:
        return _coerce_float(value)
    return str(value or "").strip()


def _normalize_scan_dll_func_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "", str(value or "")).lower()
    mapping = {
        "enable": "Enable",
        "scankeydown": "ScanKeyDown",
        "scanbuttondown": "ScanKeyDown",
        "scan": "ScanKeyDown",
        "stop": "Stop",
    }
    return mapping.get(normalized, str(value or "").strip())


def _normalize_scan_dll_data_hex(value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    bytes_found = re.findall(r"(?:0x)?([0-9A-Fa-f]{2})", raw_value)
    if bytes_found and re.fullmatch(r"[\s,;:xXA-Fa-f0-9-]+", raw_value):
        return " ".join(item.upper() for item in bytes_found)
    return raw_value


def _infer_scan_dll_func_name_from_comment(comment_text: str) -> str:
    lowered = str(comment_text or "").lower()
    compact = re.sub(r"[^0-9a-z]+", "", lowered)
    if "scankeydown" in compact or "scanbuttondown" in compact or "pressscan" in compact:
        return "ScanKeyDown"
    if re.search(r"\bscan\b", lowered):
        return "ScanKeyDown"
    if "enable" in compact:
        return "Enable"
    if re.search(r"\bstop\b", lowered):
        return "Stop"
    return ""


def _extract_scan_dll_data_hex_from_comment(comment_text: str) -> str:
    match = re.search(r"(?<![0-9A-Fa-f])(?:0x)?[0-9A-Fa-f]{2}(?:[\s,;:-]+(?:0x)?[0-9A-Fa-f]{2})+(?![0-9A-Fa-f])", str(comment_text or ""))
    if match is None:
        return ""
    return _normalize_scan_dll_data_hex(match.group(0))


def _extract_datagrid_row_value_from_ai_text(text: str) -> dict[str, str]:
    raw_text = str(text or "").strip()
    if not raw_text:
        return {}

    parsed_payload = _parse_mapping_text(raw_text)
    row_value = _extract_datagrid_row_value_from_mapping(parsed_payload)
    if row_value:
        return row_value

    observation = _parse_ai_observation_text(raw_text)
    for key in ("rowvalue", "row_value", "rowlocator", "row_locator", "locator"):
        value = observation.get(key)
        row_value = _normalize_datagrid_row_value_payload(_parse_mapping_text(str(value or "")))
        if row_value:
            return row_value

    for key in ("rowValue", "row_value", "rowLocator", "row_locator", "locator"):
        value_text = _extract_mapping_text_after_key(raw_text, key)
        row_value = _normalize_datagrid_row_value_payload(_parse_mapping_text(value_text))
        if row_value:
            return row_value

    return {}


def _extract_datagrid_row_value_from_mapping(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    for key in ("rowValue", "row_value", "rowLocator", "row_locator", "locator"):
        row_value = _normalize_datagrid_row_value_payload(payload.get(key))
        if row_value:
            return row_value
    metadata_keys = {"step_id", "analysis_mode", "method_name", "observation", "description", "missing_reason", "reason", "confidence"}
    if not any(str(key) in metadata_keys for key in payload):
        return _normalize_datagrid_row_value_payload(payload)
    return {}


def _normalize_datagrid_row_value_payload(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if not key or raw_value is None:
            continue
        if isinstance(raw_value, str):
            cell_value = raw_value.strip()
        elif isinstance(raw_value, (int, float, bool)):
            cell_value = str(raw_value).strip()
        else:
            cell_value = json.dumps(raw_value, ensure_ascii=False, default=str).strip()
        if cell_value:
            result[key] = cell_value
    return result


def _parse_mapping_text(text: str) -> object:
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        pass
    try:
        return ast.literal_eval(value)
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start != -1 and end > start:
        segment = value[start : end + 1]
        try:
            return json.loads(segment)
        except Exception:
            pass
        try:
            return ast.literal_eval(segment)
        except Exception:
            pass
    return None


def _extract_mapping_text_after_key(text: str, key: str) -> str:
    pattern = re.compile(rf"\b{re.escape(key)}\b\s*[:=]\s*", re.IGNORECASE)
    match = pattern.search(text)
    if match is None:
        return ""
    start = text.find("{", match.end())
    if start == -1:
        return ""
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {'"', "'"}:
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def _resolve_matching_click_source_screenshot(event: dict[str, Any], session_dir: Path) -> Path | None:
    screenshot = str(event.get("screenshot", "") or "").strip()
    if screenshot:
        resolved = _resolve_session_artifact_path(screenshot, session_dir)
        if resolved.exists() and resolved.is_file():
            return resolved

    media_items = event.get("media", []) if isinstance(event.get("media", []), list) else []
    for item in media_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "image") or "image").strip().lower() != "image":
            continue
        raw_path = str(item.get("path", "") or "").strip()
        if not raw_path:
            continue
        resolved = _resolve_session_artifact_path(raw_path, session_dir)
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _resolve_session_artifact_path(raw_path: str, session_dir: Path) -> Path:
    candidate = Path(str(raw_path or "").strip().strip('"'))
    if candidate.is_absolute():
        return candidate
    return (session_dir / candidate).resolve()


def _extract_matching_click_rectangle(event: dict[str, Any]) -> list[int] | None:
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    candidates: list[object] = [
        ui_element.get("rectangle"),
        ui_element.get("rect"),
        event.get("rectangle"),
        event.get("rect"),
        event.get("target_rectangle"),
    ]
    additional_details = event.get("additional_details", {}) if isinstance(event.get("additional_details", {}), dict) else {}
    visual_focus_hint = additional_details.get("visual_focus_hint", {}) if isinstance(additional_details.get("visual_focus_hint", {}), dict) else {}
    candidates.extend(
        [
            visual_focus_hint.get("target_rect"),
            visual_focus_hint.get("rectangle"),
            visual_focus_hint.get("rect"),
            additional_details.get("target_rect"),
            additional_details.get("rectangle"),
            additional_details.get("rect"),
        ]
    )
    media_items = event.get("media", []) if isinstance(event.get("media", []), list) else []
    if media_items and isinstance(media_items[0], dict):
        candidates.append(media_items[0].get("region"))

    for candidate in candidates:
        rect = _normalize_rectangle_payload(candidate)
        if rect is not None:
            return rect
    return None


def _normalize_rectangle_payload(value: object) -> list[int] | None:
    if isinstance(value, dict):
        left = _coerce_int(value.get("left", value.get("x")))
        top = _coerce_int(value.get("top", value.get("y")))
        right = _coerce_int(value.get("right"))
        bottom = _coerce_int(value.get("bottom"))
        width = _coerce_int(value.get("width"))
        height = _coerce_int(value.get("height"))
        if right is None and left is not None and width is not None:
            right = left + width
        if bottom is None and top is not None and height is not None:
            bottom = top + height
        if all(item is not None for item in (left, top, right, bottom)) and right > left and bottom > top:
            return [int(left), int(top), int(right), int(bottom)]
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        values = [_coerce_int(item) for item in value[:4]]
        if all(item is not None for item in values) and int(values[2]) > int(values[0]) and int(values[3]) > int(values[1]):
            return [int(values[0]), int(values[1]), int(values[2]), int(values[3])]
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str) and value.strip():
        try:
            return int(round(float(value.strip())))
        except Exception:
            return None
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except Exception:
            return None
    return None


def _create_matching_click_template_image(
    source_screenshot: Path,
    session_dir: Path,
    event: dict[str, Any],
    rect: list[int],
) -> tuple[str, tuple[int, int, int, int]]:
    return _create_rect_template_image(source_screenshot, session_dir, event, rect, "matchingclick")


def _create_wheel_target_image(
    source_screenshot: Path,
    session_dir: Path,
    event: dict[str, Any],
    rect: list[int],
) -> tuple[str, tuple[int, int, int, int]]:
    return _create_rect_template_image(source_screenshot, session_dir, event, rect, "wheel_target")


def _create_rect_template_image(
    source_screenshot: Path,
    session_dir: Path,
    event: dict[str, Any],
    rect: list[int],
    template_kind: str,
) -> tuple[str, tuple[int, int, int, int]]:
    with Image.open(source_screenshot) as image:
        crop_box = _build_rect_crop_box_for_source_image(event, source_screenshot, session_dir, rect, image.size)
        if crop_box is None:
            raise ValueError("事件 rect 与截图尺寸不匹配，无法裁剪目标模板图。")
        cropped = image.crop(crop_box).convert("RGB")

    relative_path = _build_rect_template_relative_path(event, source_screenshot, template_kind)
    output_path = session_dir / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path, format="PNG")
    return relative_path.as_posix(), crop_box


def _build_rect_crop_box_for_source_image(
    event: dict[str, Any],
    source_screenshot: Path,
    session_dir: Path,
    rect: list[int],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    for candidate_rect in _build_image_space_rect_candidates(event, source_screenshot, session_dir, rect):
        crop_box = _build_matching_click_crop_box(candidate_rect, image_size)
        if crop_box is not None:
            return crop_box
    return None


def _build_image_space_rect_candidates(
    event: dict[str, Any],
    source_screenshot: Path,
    session_dir: Path,
    rect: list[int],
) -> list[list[int]]:
    candidates: list[list[int]] = []
    seen: set[tuple[int, int, int, int]] = set()

    def add_candidate(candidate: list[int] | tuple[int, int, int, int]) -> None:
        values = [int(item) for item in candidate[:4]]
        key = tuple(values)
        if key in seen:
            return
        seen.add(key)
        candidates.append(values)

    def add_origin_adjusted(origin: tuple[int, int] | None) -> None:
        if origin is None:
            return
        left_offset, top_offset = origin
        add_candidate([rect[0] - left_offset, rect[1] - top_offset, rect[2] - left_offset, rect[3] - top_offset])

    manual_origin = _extract_manual_rectangle_image_origin(event, source_screenshot, session_dir)
    add_origin_adjusted(manual_origin)
    add_origin_adjusted(_extract_matching_media_image_origin(event, source_screenshot, session_dir))
    add_candidate(rect)
    return candidates


def _extract_manual_rectangle_image_origin(
    event: dict[str, Any],
    source_screenshot: Path,
    session_dir: Path,
) -> tuple[int, int] | None:
    details = event.get("additional_details", {}) if isinstance(event.get("additional_details", {}), dict) else {}
    manual_edit = details.get("rectangle_manual_edit", {}) if isinstance(details.get("rectangle_manual_edit", {}), dict) else {}
    if not manual_edit:
        return None
    raw_image_path = str(manual_edit.get("image_path", "") or "").strip()
    if raw_image_path and not _session_artifact_path_matches(raw_image_path, source_screenshot, session_dir):
        return None
    return _extract_origin_tuple(manual_edit.get("image_origin"))


def _extract_matching_media_image_origin(
    event: dict[str, Any],
    source_screenshot: Path,
    session_dir: Path,
) -> tuple[int, int] | None:
    media_items = event.get("media", []) if isinstance(event.get("media", []), list) else []
    for item in media_items:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path", "") or "").strip()
        if not raw_path or not _session_artifact_path_matches(raw_path, source_screenshot, session_dir):
            continue
        return _extract_origin_tuple(item.get("region"))
    return None


def _session_artifact_path_matches(raw_path: str, target_path: Path, session_dir: Path) -> bool:
    try:
        resolved = _resolve_session_artifact_path(raw_path, session_dir).resolve()
    except Exception:
        resolved = _resolve_session_artifact_path(raw_path, session_dir)
    try:
        target = target_path.resolve()
    except Exception:
        target = target_path
    return resolved == target


def _extract_origin_tuple(value: object) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    left = _coerce_int(value.get("left", value.get("x")))
    top = _coerce_int(value.get("top", value.get("y")))
    if left is None or top is None:
        return None
    return int(left), int(top)


def _build_matching_click_crop_box(rect: list[int], image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    image_width, image_height = image_size
    if image_width <= 1 or image_height <= 1:
        return None
    left, top, right, bottom = [int(item) for item in rect]
    width = right - left
    height = bottom - top
    if width <= 1 or height <= 1:
        return None

    max_inset = max(0, (min(width, height) - 2) // 2)
    inset = min(4, max_inset)
    left += inset
    top += inset
    right -= inset
    bottom -= inset

    if right <= 0 or bottom <= 0 or left >= image_width or top >= image_height:
        return None

    left = max(0, min(image_width - 1, left))
    top = max(0, min(image_height - 1, top))
    right = max(left + 1, min(image_width, right))
    bottom = max(top + 1, min(image_height, bottom))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _build_matching_click_template_relative_path(event: dict[str, Any], source_screenshot: Path) -> Path:
    return _build_rect_template_relative_path(event, source_screenshot, "matchingclick")


def _build_rect_template_relative_path(event: dict[str, Any], source_screenshot: Path, template_kind: str) -> Path:
    source_stem = _sanitize_matching_click_path_segment(source_screenshot.stem)
    event_id = _sanitize_matching_click_path_segment(str(event.get("event_id", "") or ""))
    kind = _sanitize_matching_click_path_segment(template_kind)
    name_parts = [source_stem]
    if event_id and event_id.lower() not in source_stem.lower():
        name_parts.append(event_id)
    name_parts.append(kind)
    return Path("screenshots", "matching_click", "_".join(name_parts) + ".png")


def _sanitize_matching_click_path_segment(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
    sanitized = sanitized.strip("._-")
    return sanitized or "template"


def _format_session_relative_path(path: Path, session_dir: Path) -> str:
    try:
        return path.relative_to(session_dir).as_posix()
    except Exception:
        return str(path)


def _normalize_find_control_direction(value: str) -> str:
    lowered = value.strip().lower()
    mapping = {
        "up": "above",
        "down": "below",
        "left": "left",
        "right": "right",
        "above": "above",
        "below": "below",
        "any": "any",
    }
    return mapping.get(lowered, "")


def _extract_click_point(event: dict[str, Any]) -> list[int] | None:
    mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
    x = mouse.get("x")
    y = mouse.get("y")
    if isinstance(x, int) and isinstance(y, int):
        return [x, y]
    return None


def _extract_scroll_point(scroll: dict[str, Any], mouse: dict[str, Any]) -> tuple[int | None, int | None]:
    end_x = scroll.get("end_x")
    end_y = scroll.get("end_y")
    if isinstance(end_x, int) and isinstance(end_y, int):
        return end_x, end_y

    mouse_x = mouse.get("x")
    mouse_y = mouse.get("y")
    if isinstance(mouse_x, int) and isinstance(mouse_y, int):
        return mouse_x, mouse_y
    return None, None


def _extract_agent_interface_rect(media_items: list[Any]) -> list[int] | None:
    if not media_items:
        return None
    first_item = media_items[0] if isinstance(media_items[0], dict) else {}
    region = first_item.get("region", {}) if isinstance(first_item.get("region", {}), dict) else {}
    left = region.get("left")
    top = region.get("top")
    right = region.get("right")
    bottom = region.get("bottom")
    if all(isinstance(value, int) for value in (left, top, right, bottom)):
        return [left, top, right, bottom]
    return None


def _extract_agent_interface_image_list(media_items: list[Any]) -> list[str]:
    if len(media_items) <= 1:
        return []

    image_names: list[str] = []
    for item in media_items[1:]:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path", "")).strip()
        if not raw_path:
            continue
        image_names.append(Path(raw_path).name)
    return image_names


def _extract_first_media_file_name(media_items: list[Any]) -> str:
    if not media_items:
        return ""
    first_item = media_items[0] if isinstance(media_items[0], dict) else {}
    raw_path = str(first_item.get("path", "")).strip()
    if not raw_path:
        return ""
    return Path(raw_path).name


def _derive_send_keys_text_value(keyboard: dict[str, Any]) -> str:
    sequence = keyboard.get("sequence", [])
    if isinstance(sequence, list):
        sequence_text = _convert_keyboard_sequence_to_send_keys_text(sequence)
        if sequence_text:
            return sequence_text

    keyboard_text = keyboard.get("text")
    if isinstance(keyboard_text, str) and keyboard_text:
        plain_text = _convert_plain_text_to_send_keys_text(keyboard_text)
        if plain_text:
            return plain_text

    modifiers = _normalize_keyboard_modifiers(keyboard.get("modifiers", []))
    char = keyboard.get("char")
    key_name = normalize_keyboard_key_name(keyboard.get("key_name", ""))

    if modifiers:
        modifier_macro = "".join(f"{{{token}}}" for token in modifiers)
        if isinstance(char, str) and len(char) == 1 and char.isprintable():
            return f"{modifier_macro}{char}"
        special_token = _to_send_keys_special_token(key_name)
        if special_token:
            return f"{modifier_macro}{special_token}"
        if key_name:
            return f"{modifier_macro}{key_name}"
        return ""

    if isinstance(char, str) and len(char) == 1:
        return char
    if key_name.lower() == "space":
        return " "
    return ""


def _derive_send_keys_key_value(keyboard: dict[str, Any]) -> str:
    modifiers = _normalize_keyboard_modifiers(keyboard.get("modifiers", []))
    if modifiers:
        return ""

    key_name = normalize_keyboard_key_name(keyboard.get("key_name", ""))
    char = keyboard.get("char")
    if isinstance(char, str) and len(char) == 1 and char.isprintable():
        return ""
    return _to_virtual_key_name(key_name)


def _normalize_keyboard_modifiers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    mapping = {
        "ctrl": "Ctrl",
        "ctrl_l": "Ctrl",
        "ctrl_r": "Ctrl",
        "shift": "Shift",
        "shift_l": "Shift",
        "shift_r": "Shift",
        "alt": "Alt",
        "alt_l": "Alt",
        "alt_r": "Alt",
        "alt_gr": "Alt",
        "cmd": "Win",
        "cmd_l": "Win",
        "cmd_r": "Win",
        "win": "Win",
    }
    normalized: list[str] = []
    for item in value:
        key = normalize_keyboard_key_name(item).strip().lower()
        mapped = mapping.get(key)
        if mapped and mapped not in normalized:
            normalized.append(mapped)
    return normalized


def _to_send_keys_special_token(key_name: str) -> str:
    lowered = str(key_name or "").strip().lower()
    mapping = {
        "enter": "{Enter}",
        "tab": "{Tab}",
        "esc": "{Esc}",
        "escape": "{Esc}",
        "space": " ",
        "backspace": "{Backspace}",
        "delete": "{Delete}",
        "home": "{Home}",
        "end": "{End}",
        "page_up": "{PageUp}",
        "page_down": "{PageDown}",
        "up": "{Up}",
        "down": "{Down}",
        "left": "{Left}",
        "right": "{Right}",
    }
    if lowered in mapping:
        return mapping[lowered]
    if lowered.startswith("f") and lowered[1:].isdigit():
        return "{" + lowered.upper() + "}"
    return ""


def _to_virtual_key_name(key_name: str) -> str:
    lowered = str(key_name or "").strip().lower()
    mapping = {
        "enter": "VK_RETURN",
        "tab": "VK_TAB",
        "esc": "VK_ESCAPE",
        "escape": "VK_ESCAPE",
        "space": "VK_SPACE",
        "backspace": "VK_BACK",
        "delete": "VK_DELETE",
        "home": "VK_HOME",
        "end": "VK_END",
        "page_up": "VK_PRIOR",
        "page_down": "VK_NEXT",
        "up": "VK_UP",
        "down": "VK_DOWN",
        "left": "VK_LEFT",
        "right": "VK_RIGHT",
        "cmd": "VK_LWIN",
        "win": "VK_LWIN",
        "ctrl": "VK_CONTROL",
        "shift": "VK_SHIFT",
        "alt": "VK_MENU",
    }
    if lowered in mapping:
        return mapping[lowered]
    if lowered.startswith("f") and lowered[1:].isdigit():
        return "VK_" + lowered.upper()
    return ""


def _build_send_keys_text_evidence(keyboard: dict[str, Any], text_value: str) -> str:
    sequence = keyboard.get("sequence", [])
    if isinstance(sequence, list) and sequence:
        return f"事件明细.keyboard.sequence={sequence}，转换为 SendKeys.text={text_value}"
    keyboard_text = keyboard.get("text")
    if isinstance(keyboard_text, str) and keyboard_text:
        return f"事件明细.keyboard.text={keyboard_text!r}，转换为 SendKeys.text={text_value}"

    char = keyboard.get("char")
    key_name = normalize_keyboard_key_name(keyboard.get("key_name", ""))
    modifiers = _normalize_keyboard_modifiers(keyboard.get("modifiers", []))
    if modifiers:
        return f"事件明细.keyboard.modifiers={modifiers}，key_name={key_name}，组合为 SendKeys.text={text_value}"
    if isinstance(char, str) and char:
        return f"事件明细.keyboard.char={char}"
    return f"事件明细.keyboard.key_name={key_name}"


def _build_send_keys_key_evidence(keyboard: dict[str, Any], key_value: str) -> str:
    key_name = normalize_keyboard_key_name(keyboard.get("key_name", ""))
    return f"事件明细.keyboard.key_name={key_name}，映射为 SendKeys.key={key_value}"


def _should_skip_missing_parameter(
    suggestion: MethodSelectionSuggestion,
    name: str,
    value: Any,
    missing_reason: str,
) -> bool:
    method_name = str(suggestion.method_name or "").strip().lower()
    parameter_name = str(name or "").strip()
    if method_name == "sendkeys" and parameter_name in {"text", "key"} and value is None and not missing_reason:
        return True
    if method_name == "waitforexists" and value is None:
        return True
    return False


def _convert_keyboard_sequence_to_send_keys_text(sequence: list[Any]) -> str:
    converted_parts: list[str] = []
    for item in sequence:
        token = _convert_sequence_token_to_send_keys_text(item)
        if token is None:
            return ""
        converted_parts.append(token)
    return "".join(converted_parts)


def _convert_sequence_token_to_send_keys_text(token: Any) -> str | None:
    value = str(token or "")
    if not value:
        return None
    mapping = {
        "[Enter]": "{Enter}",
        "[Tab]": "{Tab}",
        "[Space]": " ",
        "[Backspace]": "{Backspace}",
        "[Delete]": "{Delete}",
        "[Esc]": "{Esc}",
        "[Escape]": "{Esc}",
        "[Up]": "{Up}",
        "[Down]": "{Down}",
        "[Left]": "{Left}",
        "[Right]": "{Right}",
        "[Home]": "{Home}",
        "[End]": "{End}",
    }
    if value in mapping:
        return mapping[value]
    if len(value) == 1:
        return _escape_send_keys_literal(value)
    return None


def _convert_plain_text_to_send_keys_text(text: str) -> str:
    converted_parts: list[str] = []
    for char in text:
        if char == "\n":
            converted_parts.append("{Enter}")
        elif char == "\t":
            converted_parts.append("{Tab}")
        else:
            converted_parts.append(_escape_send_keys_literal(char))
    return "".join(converted_parts)


def _escape_send_keys_literal(value: str) -> str:
    if value == "{":
        return "{{}"
    if value == "}":
        return "{}}"
    return value




def _derive_cell_value(event: dict[str, Any], observation: dict[str, Any] | None = None) -> Any:
    parsed_observation = observation if isinstance(observation, dict) else {}
    observed_action = _normalize_observation_action(str(parsed_observation.get("action", "")).strip())
    if observed_action:
        return observed_action

    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
    action_value = format_recorded_action(event.get("action", "")).strip().lower()
    keyboard = event.get("keyboard", {}) if isinstance(event.get("keyboard", {}), dict) else {}

    if event_type in {"controlOperation", "Click"}:
        return "click"
    if event_type == "mouseAction" and action_value != "mouse_scroll":
        return "drag"
    if event_type == "mouseAction" and action_value == "mouse_scroll":
        scroll = event.get("scroll", {}) if isinstance(event.get("scroll", {}), dict) else {}
        dy = scroll.get("dy")
        if isinstance(dy, int):
            return "scroll_down" if dy < 0 else "scroll_up"
        return "scroll"
    if event_type == "input" and action_value == "type_input":
        text = keyboard.get("text", "")
        if isinstance(text, str):
            return text
        return str(text)
    if event_type == "input" and action_value == "press":
        char = keyboard.get("char")
        if isinstance(char, str) and char:
            return char
        key_name = keyboard.get("key_name", "")
        if str(key_name).strip():
            return str(key_name)
    return None


def _build_cell_value_evidence(event: dict[str, Any], cell_value: Any, observation: dict[str, Any] | None = None) -> str:
    parsed_observation = observation if isinstance(observation, dict) else {}
    observed_action = _normalize_observation_action(str(parsed_observation.get("action", "")).strip())
    if observed_action:
        return f"AI看图.action={observed_action}"

    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
    action_value = format_recorded_action(event.get("action", "")).strip().lower()
    if event_type in {"controlOperation", "Click"}:
        return f"事件类型={event_type}，按点击处理为 cellValue=click"
    if event_type == "input" and action_value == "type_input":
        return f"事件明细.keyboard.text={cell_value}"
    if event_type == "input" and action_value == "press":
        return f"事件明细.keyboard 单键输入={cell_value}"
    return f"由事件类型 {event_type} 和 action={action_value} 推断得到"


def _normalize_observation_action(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"click", "on", "off", "search"}:
        return lowered
    return ""


def _is_useful_ocr_query(value: str) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    return normalized.lower() not in {"anonymous", "unknown", "none", "null", "n/a"}


def _normalize_mouse_button(value: str) -> str:
    lowered = value.strip().lower()
    mapping = {
        "button.left": "left",
        "button.right": "right",
        "button.middle": "middle",
        "left": "left",
        "right": "right",
        "middle": "middle",
    }
    return mapping.get(lowered, "")


def _find_registry_entry_by_name(registry_entries: list[Any], name: str) -> Any | None:
    target = str(name).strip().lower()
    for entry in registry_entries:
        entry_name = str(getattr(entry, "name", "")).strip().lower()
        if entry_name == target:
            return entry
    return None


def _build_method_candidate_payload(
    registry_entries: list[Any],
    selected_entry: Any | None,
    selected_method_name: str,
    method_options: list[MethodSuggestionOption],
    event_type: str,
) -> dict[str, Any]:
    payload = asdict(selected_entry) if selected_entry is not None else {}
    option_payloads = _build_method_option_payloads(registry_entries, method_options)
    if option_payloads:
        payload["method_options"] = option_payloads
        payload["default_method_name"] = option_payloads[0].get("name", "")
    if selected_method_name:
        payload["selected_method_name"] = selected_method_name
    if event_type:
        payload["source_event_type"] = event_type
    return payload


def _build_method_option_payloads(
    registry_entries: list[Any],
    method_options: list[MethodSuggestionOption],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, option in enumerate(method_options):
        name = str(option.name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        entry = _find_registry_entry_by_name(registry_entries, name)
        payload = option.to_dict(is_default=index == 0)
        payload["available_in_registry"] = entry is not None
        payload["summary"] = str(getattr(entry, "summary", "") or "") if entry is not None else ""
        payloads.append(payload)
    return payloads