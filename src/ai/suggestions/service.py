from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from src.ai.client import OpenAICompatibleAIClient
from src.ai.errors import AIClientError
from src.ai.method_mapping import resolve_method_name_for_event
from src.converter.pipeline.method_candidates import build_retrieval_preview_from_files
from src.converter.registry.loader import load_method_registry
from src.converter.retrieval.models import SemanticStep
from src.recorder.models import format_recorded_action, normalize_event_type, normalize_keyboard_key_name

from .method_selection import build_method_selection_result
from .models import MethodParameterSuggestion, MethodSelectionSuggestion, SuggestionGenerationResult
from .parameter_recommendation import parse_parameter_recommendation_payload, parse_parameter_recommendation_response_text
from .prompt_builder import build_parameter_recommendation_prompt, build_parameter_recommendation_system_prompt


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
            mapped_method_name, mapped_reason = self._resolve_method_name_for_event(step, event)
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
                    candidate_payload=asdict(top_entry) if top_entry else {},
                )
            )

        return SuggestionGenerationResult(
            session_id=session_id,
            suggestions=suggestions,
            notes=[
                "方法建议来源于清洗后事件。",
                "方法建议使用写死映射关系表生成，不再依赖 pilot_methods.yaml 中的 aliases。",
                "mouseAction 中：scroll 映射到 Wheel，带 start/end 坐标的拖动映射到 DragDrop，其他 mouseAction 当前不自动推荐方法。",
            ],
        )

    def _resolve_method_name_for_event(
        self,
        step: SemanticStep,
        event: dict[str, Any],
    ) -> tuple[str, str]:
        event_type = str(step.event_type or "").strip()
        if event_type == "mouseAction":
            method_name = resolve_method_name_for_event(event)
            if method_name == "Wheel":
                return method_name, "统一映射表：mouseAction 且 action 包含 scroll，映射到 Wheel"
            if method_name == "DragDrop":
                return method_name, "统一映射表：mouseAction 且 mouse 含 start/end 坐标，映射到 DragDrop"
            return "", "统一映射表：当前 mouseAction 不满足 Wheel/DragDrop 条件，暂不推荐方法"

        method_name = resolve_method_name_for_event(event)
        if method_name:
            return method_name, f"统一映射表：event_type={event_type} -> {method_name}"
        return "", f"统一映射表中未配置 event_type={event_type} 的方法"

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
    ) -> list[str]:
        method_name = str(suggestion.method_name or "").strip()
        derived_values, evidence_map, missing_map = _derive_parameter_values_for_method(method_name, event, ai_observation_text)
        parameter_suggestions = _build_parameter_suggestions_from_schema(
            suggestion=suggestion,
            derived_values=derived_values,
            evidence_map=evidence_map,
            missing_map=missing_map,
        )
        suggestion.parameters = parameter_suggestions
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
            suggestion.parameters = parameters
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
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    normalized_method = method_name.strip().lower()
    if normalized_method == "performscan":
        return {}, {}, {}
    if normalized_method == "findcontrolbyname":
        return _derive_find_control_by_name_values(event, ai_observation_text)
    if normalized_method == "getscreenshot":
        return _derive_get_screenshot_values(event)
    if normalized_method == "waitforexists":
        return _derive_wait_for_exists_values(event)
    if normalized_method == "agentinterface":
        return _derive_agent_interface_values(event)
    if normalized_method == "manualcheck":
        return _derive_manual_check_values(event)
    if normalized_method == "sendkeys":
        return _derive_send_keys_values(event)
    if normalized_method == "click":
        return _derive_click_values(event)
    if normalized_method == "wheel":
        return _derive_wheel_values(event)
    if normalized_method == "dragdrop":
        return _derive_drag_drop_values(event)
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

    help_text = str(observation.get("helptext", "")).strip()
    label = str(observation.get("label", "")).strip()
    if help_text:
        derived_values["HelpText"] = help_text
        evidence_map["HelpText"] = [f"AI看图: helptext={help_text}"]
    elif label:
        derived_values["Name"] = label
        evidence_map["Name"] = [f"AI看图: label={label}"]
    else:
        ui_help_text = str(ui_element.get("help_text", "") or ui_element.get("help_text_fallback", "")).strip()
        ui_name = str(ui_element.get("name", "")).strip()
        if ui_help_text:
            derived_values["HelpText"] = ui_help_text
            evidence_map["HelpText"] = [f"事件明细.ui_element.help_text={ui_help_text}"]
        elif ui_name:
            derived_values["Name"] = ui_name
            evidence_map["Name"] = [f"事件明细.ui_element.name={ui_name}"]
        else:
            missing_map["Name"] = "AI看图和事件明细中都没有可用的 Name/HelpText。"

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

    click_point = _extract_click_point(event)
    if click_point is not None:
        derived_values["clickPoint"] = click_point
        evidence_map["clickPoint"] = [f"事件明细.mouse=({click_point[0]}, {click_point[1]})"]

    cell_value = _derive_cell_value(event, observation)
    if cell_value is not None and cell_value != "":
        derived_values["cellValue"] = cell_value
        evidence_map["cellValue"] = [_build_cell_value_evidence(event, cell_value, observation)]
    else:
        missing_map["cellValue"] = "当前事件明细无法确定 cellValue。"

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

    return derived_values, evidence_map, missing_map


def _derive_wait_for_exists_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    media_items = event.get("media", []) if isinstance(event.get("media", []), list) else []
    note_text = str(event.get("note", "")).strip()
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

    return derived_values, evidence_map, missing_map


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

    rect = _extract_agent_interface_rect(media_items)
    if rect is not None:
        derived_values["rect"] = rect
        evidence_map["rect"] = [f"事件明细.media[0].region={rect}"]

    image_list = _extract_agent_interface_image_list(media_items)
    if image_list:
        derived_values["imageList"] = image_list
        evidence_map["imageList"] = [f"事件明细.media[1:] 路径文件名={image_list}"]

    return derived_values, evidence_map, missing_map


def _derive_manual_check_values(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, str]]:
    note_text = str(event.get("note", "")).strip()
    derived_values: dict[str, Any] = {}
    evidence_map: dict[str, list[str]] = {}
    missing_map: dict[str, str] = {}

    if note_text:
        derived_values["Description"] = note_text
        derived_values["Expect"] = note_text
        evidence_map["Description"] = [f"事件明细.note={note_text}"]
        evidence_map["Expect"] = [f"事件明细.note={note_text}"]
    else:
        empty_text = "Current content is empty"
        derived_values["Description"] = empty_text
        derived_values["Expect"] = empty_text
        evidence_map["Description"] = ["事件明细.note 为空，使用固定英文占位。"]
        evidence_map["Expect"] = ["事件明细.note 为空，使用固定英文占位。"]

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


def _reorder_parameter_names_for_method(method_name: str, ordered_names: list[str]) -> list[str]:
    priority_names = ["Name", "HelpText", "direction", "scrollable", "cellValue"]
    reordered = list(ordered_names)
    for name in reversed(priority_names):
        reordered = _move_name_to_front(reordered, name)
    normalized_method = str(method_name or "").strip().lower()
    if normalized_method == "findcontrolbyname":
        return reordered
    if normalized_method == "click":
        for name in reversed(["absolute", "y", "x", "button"]):
            reordered = _move_name_to_front(reordered, name)
        return reordered
    return reordered


def _reorder_parameter_suggestions(method_name: str, suggestions: list[MethodParameterSuggestion]) -> list[MethodParameterSuggestion]:
    priority_order = {"Name": 0, "HelpText": 1, "direction": 2, "scrollable": 3, "cellValue": 4}
    normalized_method = str(method_name or "").strip().lower()
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

    if event_type == "controlOperation":
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
    if event_type == "controlOperation":
        return "事件类型=controlOperation，按点击处理为 cellValue=click"
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