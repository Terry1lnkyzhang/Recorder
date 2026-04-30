from __future__ import annotations

import json
from pathlib import Path

from src.ai.method_mapping import resolve_method_name_for_event
from src.common.display_utils import prepare_image_path_for_ai
from src.recorder.models import format_recorded_action, normalize_event_type


def build_step_observation_prompt() -> str:
    instruction = {
        "task": "观察当前桌面自动化步骤，仅基于截图提取事实层描述",
        "requirements": [
            "截图上红色框选区域是当前操作目标。",
            "请针对每一步输出 6 个字段：control_type、label、relative_position、need_scroll、is_table、action。不要输出其他字段。",
            "control_type 表示红框对应控件的类型，例如 button、editbox、combobox、checkbox、radiobutton、tab、menuitem、table、row、cell、list、listitem、dialog、panel；无法确定时可用 unknown。",
            "label 表示红框目标控件自身的文字标签，或与该控件最直接对应的字段标签。对于 combobox 和 editbox，label 不应取控件内部当前显示的值或输入内容，而应优先取最直接对应的字段标签。必须准确使用截图原文，不要猜测、改写、翻译、缩写或替换成其他近似词；没有明确 label 时返回空字符串。",
            "relative_position 只能是 self、up、down、left、right 之一，表示目标控件位于 label 的哪个方向。例如控件在 label 右边时返回 right，在 label 左边时返回 left，在 label 上方时返回 up，在 label 下方时返回 down；若 label 就在目标控件自身上，则返回 self。",
            "若按钮、复选框、单选框、tab、菜单项等控件自身已带明确 label，则 label 直接写该控件文字，relative_position 必须为 self，不要再借用附近字段标签。",
            "need_scroll 只能是 true 或 false。只要从截图中可以判断，红框目标控件所属的容器支持通过滚动来调整显示内容，例如位于可滚动列表、表格、滚动面板或下拉项区域中，就返回 true；否则返回 false。",
            "is_table 只能是 true 或 false。只有当从截图中可以明确看出，红框目标位于具有明显行列结构的表格区域中，并且各列有清晰列名或表头时，才返回 true；普通列表、菜单、树、下拉项、无明确列表头的成组项目都返回 false。",
            "action 表示当前步骤对红框目标的动作。普通点击统一返回 click；若红框目标是 checkbox，如果截图中该 checkbox 当前已勾选，返回off， checkbox 当前未勾选，返回 on",
            "不要输出分析过程，不要输出自然语言总结，只输出 JSON。",
            "只输出 JSON。",
        ],
        "json_schema_hint": {
        "step_observations": [
            {
                "control_type": "button",
                "label": "Save",
                "relative_position": "self",
                "need_scroll": False,
                "is_table": False,
                "action": "click"
            }
        ]
        },
    }
    return json.dumps(instruction, ensure_ascii=False, indent=2)


def build_step_reasoning_prompt(
    session_id: str,
    current_step_observations: list[dict[str, object]],
    recent_step_observations: list[dict[str, object]],
    previous_memory: list[dict[str, object]],
) -> str:
    instruction = {
        "task": "基于当前步骤观察结果、最近步骤上下文和历史摘要，输出当前步骤的最终语义与局部多步骤分析结果",
        "session_id": session_id,
        "requirements": [
            "你只能基于 current_step_observations、recent_step_observations 和 previous_memory 推理，不要假设能再次看到截图。",
            "step_insights 只针对 current_step_observations 中的步骤输出，数组顺序必须与 current_step_observations 一致。",
            "current_step_observations 中已经包含 control_type、label、relative_position、need_scroll、is_table、action 以及基于这些字段生成的 observation。step_insights.description 应优先基于这些结构化字段生成最终语义。",
            "若按钮、复选框、单选框、tab、菜单项等控件自身有 label，应直接写点击该控件，不要再写成某字段右侧的按钮。若是下拉值选择，应优先归一为选中某字段右侧combobox中的某值。",
            "recent_step_observations 提供当前步骤前最多 20 步的局部上下文，可据此判断当前步骤是否属于某个可组合模块。",
            "previous_memory 提供更早步骤的摘要，只用于补充历史阶段和状态变化，不要把它误当成当前步骤。",
            "如能判断局部步骤可组合，可输出 reusable_modules；如发现当前或相关步骤可疑，可输出 invalid_steps；如需要等待条件，可输出 wait_suggestions。没有则输出空数组。",
            "可输出 notes 和 batch_summary，帮助记录这一批的阶段、状态变化与后续摘要。",
            "若 current_step_observations 中的某条带有 cleaning_signals 字段，说明本地启发式检测器已发现该步可能属于试错废操作，请优先据此判断是否将其放入 invalid_steps，并把对应 kind 写到 invalid_steps[].category。",
            "判定试错废操作时遵循以下规则：(1) 同一窗口内出现连续邻近点击但只有最后一次产生了真正的状态变化时，前面的点击均为 spatial_cluster_clicks，标 decision=delete 并把最终生效的步骤号写入 kept_step_id；(2) 输入序列中先输错再退格重输的，中间过程标 decision=delete、category=backspace_run，保留最终输入；(3) 打开菜单/下拉/弹出后未选择即按 Esc 关闭的整个开-关对标 decision=delete、category=menu_open_close；(4) 短时间内进入另一窗口又退回原窗口且中间无任何输入或保存的，整段 decision=delete、category=window_visit_leave；(5) 状态回退 A→B→A 仅在 B 没有产生业务后果（保存/提交/字段值变更）时才标 delete，否则 decision=review。",
            "invalid_steps[].decision 只能是 delete 或 review；confidence 取 0~1；若不确定则用 review，并附上 reason 说明依据。",
            "只输出 JSON。",
        ],
        "json_schema_hint": {
            "step_insights": [
                {
                    "step_id": 21,
                    "description": "选中“mAs(mA)”右侧combobox中的“6”",
                }
            ],
            "invalid_steps": [
                {
                    "step_ids": [12, 13, 14],
                    "decision": "delete",
                    "category": "spatial_cluster_clicks",
                    "confidence": 0.85,
                    "kept_step_id": 15,
                    "reason": "前 3 次点击均落在按钮附近但未触发动作，只有第 15 步真正打开了窗口。",
                }
            ],
            "reusable_modules": [
                {
                    "start_step": 18,
                    "end_step": 21,
                    "module_name": "Exposure_Parameter_Setup",
                    "reason": "连续设置曝光相关参数，界面上下文一致",
                    "parameterization": ["kV", "mAs"],
                }
            ],
            "wait_suggestions": [],
            "notes": ["当前步骤属于曝光参数设置过程。"],
            "batch_summary": {
                "current_phase": "参数设置",
                "notable_state_changes": ["曝光参数逐步完成设置"],
                "carry_over_notes": ["后续可能继续设置相关参数"],
            },
        },
        "current_step_observations": current_step_observations,
        "recent_step_observations": recent_step_observations[-20:],
        "previous_memory": previous_memory[-8:],
    }
    return json.dumps(instruction, ensure_ascii=False, indent=2)


def build_workflow_aggregation_prompt(
    session_id: str,
    step_insights: list[dict[str, object]],
    previous_memory: list[dict[str, object]],
) -> str:
    instruction = {
        "task": "基于逐步分析结果做桌面自动化流程聚合分析",
        "session_id": session_id,
        "requirements": [
            "输入已经是逐步分析后的 step_insights，不要再假设你能看到截图",
            "输出面向人阅读的中文 Markdown 总结，不需要 JSON",
            "按固定顺序输出以下一级标题：# 流程阶段、# 关键状态变化、# 可疑步骤、# 可复用模块、# 等待建议、# 结论与后续建议",
            "如果某一部分没有内容，也必须保留标题，并在标题下写“无”",
            "每个部分使用简洁中文；需要列举时使用 Markdown 列表",
            "可疑步骤请写明步骤号、建议（删除/保留/复核）和原因",
            "可复用模块请写明步骤范围、模块名、原因和可参数化点",
            "等待建议请写明步骤号、建议等待目标和原因",
            "不要输出 markdown 代码块，不要输出 JSON，不要输出额外前后缀",
        ],
        "markdown_outline_hint": [
            "# 流程阶段\n- 当前处于什么阶段",
            "# 关键状态变化\n- 状态变化 1\n- 状态变化 2",
            "# 可疑步骤\n- 步骤 5｜复核｜原因...",
            "# 可复用模块\n- 步骤 8-10｜Exam_Configuration｜原因...｜参数化点: exam_type, protocol, orientation",
            "# 等待建议\n- 步骤 11｜等待目标: xxx｜原因...",
            "# 结论与后续建议\n- 建议 1\n- 建议 2",
        ],
        "previous_memory": previous_memory[-8:],
        "step_insights": step_insights,
    }
    return json.dumps(instruction, ensure_ascii=False, indent=2)


def build_grouped_step_summary_prompt(
    session_id: str,
    step_ids: list[int],
    step_events: list[dict[str, object]],
) -> str:
    instruction = {
        "task": "基于一小批连续步骤截图与事件元数据，总结这一批步骤中用户实际进行了哪些操作",
        "session_id": session_id,
        "step_ids": step_ids,
        "requirements": [
            "这些步骤不适合按单张截图逐控件识别，请改用多步骤操作理解方式。",
            "你会同时看到这一批连续步骤的多张截图，以及每一步的事件元数据。",
            "请输出详细中文总结，说明用户在这批步骤里依次做了什么、界面发生了什么变化、是否有输入、选择、滚动、切换、确认等操作。",
            "只能基于提供的截图和事件元数据，不要猜测截图外的信息。",
            "如果某一步看不清，也要结合前后连续步骤做谨慎描述，但不要编造。",
            "只输出 JSON。",
        ],
        "json_schema_hint": {
            "window_summary": "步骤 11-15：先进入某个区域，再连续滚动并选择目标项，最后确认设置。",
            "step_highlights": [
                {
                    "step_id": 11,
                    "description": "进入相关设置区域并开始定位目标内容。",
                }
            ],
        },
        "events": step_events,
    }
    return json.dumps(instruction, ensure_ascii=False, indent=2)


def build_grouped_step_merge_prompt(
    session_id: str,
    step_ids: list[int],
    step_events: list[dict[str, object]],
    window_summaries: list[dict[str, object]],
) -> str:
    instruction = {
        "task": "基于多批重叠窗口总结，生成连续步骤区间的最终操作总结",
        "session_id": session_id,
        "step_ids": step_ids,
        "requirements": [
            "输入里的 window_summaries 来自同一连续步骤区间，且相邻窗口会有 1 个重叠步骤。",
            "请先对重叠内容去重，再总结整个区间的真实操作过程。",
            "segment_summary 需要是详细中文自然语言描述，说明这一整段步骤总体完成了什么。",
            "step_summaries 必须覆盖 step_ids 中的每一个步骤号，并按顺序输出。",
            "非最后一步的 step_summaries.description 要写成简短分步描述，通常 1 句，聚焦该步的直接作用，不要重复整段总结。",
            "最后一个步骤的 description 需要更完整一些，用于承载整段操作的最终收束，可以带上整段  step_ids 的总结。",
            "只输出 JSON。",
        ],
        "json_schema_hint": {
            "segment_summary": "步骤 11-17：用户先浏览并定位目标区域，随后连续滚动和切换候选项，最终完成目标项选择与确认。",
            "step_summaries": [
                {
                    "step_id": 11,
                    "description": "开始浏览相关界面区域，定位后续要操作的内容。",
                },
                {
                    "step_id": 17,
                    "description": "完成本段连续操作的最后确认；整段来看，步骤 11-17 主要是在浏览、定位并完成目标项处理。",
                },
            ],
        },
        "events": step_events,
        "window_summaries": window_summaries,
    }
    return json.dumps(instruction, ensure_ascii=False, indent=2)


def build_batch_events(events: list[dict[str, object]], start_index: int, batch_size: int) -> list[dict[str, object]]:
    rows = []
    sliced = events[start_index : start_index + batch_size]
    for event in sliced:
        event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
        keyboard = event.get("keyboard", {}) if event_type == "input" else {}
        mouse = event.get("mouse", {}) if event_type == "controlOperation" else {}
        row: dict[str, object] = {
            "event_type": event_type,
            "action": event.get("action", ""),
        }
        ui_element = _build_prompt_ui_element(event)
        if ui_element:
            row["ui_element"] = ui_element
        if keyboard:
            row["keyboard"] = keyboard
        if mouse:
            row["mouse"] = mouse
        rows.append(row)
    return rows


def resolve_analysis_step_id(event: dict[str, object], fallback_step_id: int) -> int:
    candidate = event.get("analysis_step_id")
    if isinstance(candidate, int) and candidate > 0:
        return candidate
    return fallback_step_id


def _extract_cleaning_signals(event: dict[str, object]) -> list[dict[str, object]]:
    """Pull cleaning signals (set by ``annotate_events_with_cleaning_signals``)
    out of an event so they can be forwarded to the AI prompt."""

    if not isinstance(event, dict):
        return []
    additional = event.get("additional_details")
    if not isinstance(additional, dict):
        return []
    raw_signals = additional.get("cleaning_signals")
    if not isinstance(raw_signals, list):
        return []
    cleaned: list[dict[str, object]] = []
    for item in raw_signals:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        if not kind:
            continue
        cleaned.append(
            {
                "kind": kind,
                "reason": str(item.get("reason", "")),
            }
        )
    return cleaned


def collect_observation_inputs(
    session_dir: Path,
    events: list[dict[str, object]],
    start_index: int,
    batch_size: int,
    display_layout: dict[str, object] | None = None,
    send_fullscreen: bool = False,
    excluded_process_names: list[str] | None = None,
    include_all_screenshot_events: bool = False,
) -> tuple[list[dict[str, object]], list[Path], list[int], dict[str, int]]:
    rows: list[dict[str, object]] = []
    image_paths: list[Path] = []
    step_ids: list[int] = []
    seen: set[Path] = set()
    cropped_count = 0
    cache_dir = session_dir / "ai_preprocessed" / "monitors"
    normalized_excluded_process_names = _build_normalized_excluded_process_names(excluded_process_names)
    sliced = events[start_index : start_index + batch_size]
    for offset, event in enumerate(sliced, start=start_index + 1):
        analysis_step_id = resolve_analysis_step_id(event, offset)
        if not include_all_screenshot_events and not _should_send_event_to_observation(event, normalized_excluded_process_names):
            continue
        primary = _resolve_primary_image(session_dir, event)
        if not primary:
            continue
        path = session_dir / str(primary)
        if not path.exists():
            continue
        row: dict[str, object] = {
            "event_type": normalize_event_type(event.get("event_type", ""), event.get("action", "")),
            "action": event.get("action", ""),
        }
        ui_element = _build_prompt_ui_element(event)
        if ui_element:
            row["ui_element"] = ui_element
        event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
        keyboard = event.get("keyboard", {}) if event_type == "input" else {}
        mouse = event.get("mouse", {}) if event_type == "controlOperation" else {}
        if keyboard:
            row["keyboard"] = keyboard
        if mouse:
            row["mouse"] = mouse
        cleaning_signals = _extract_cleaning_signals(event)
        if cleaning_signals:
            row["cleaning_signals"] = cleaning_signals
        prepared_path, was_cropped = prepare_image_path_for_ai(
            path,
            event,
            display_layout,
            cache_dir,
            send_fullscreen=send_fullscreen,
            cache_key=f"step_{analysis_step_id:04d}",
        )
        if was_cropped:
            cropped_count += 1
        rows.append(row)
        step_ids.append(analysis_step_id)
        if prepared_path not in seen:
            image_paths.append(prepared_path)
            seen.add(prepared_path)
    return rows, image_paths, step_ids, {"image_count": len(image_paths), "cropped_monitor_count": cropped_count}


def collect_batch_images(
    session_dir: Path,
    events: list[dict[str, object]],
    start_index: int,
    batch_size: int,
    display_layout: dict[str, object] | None = None,
    send_fullscreen: bool = False,
    excluded_process_names: list[str] | None = None,
    include_all_screenshot_events: bool = False,
) -> tuple[list[Path], dict[str, int]]:
    _, image_paths, _, image_stats = collect_observation_inputs(
        session_dir,
        events,
        start_index,
        batch_size,
        display_layout=display_layout,
        send_fullscreen=send_fullscreen,
        excluded_process_names=excluded_process_names,
        include_all_screenshot_events=include_all_screenshot_events,
    )
    return image_paths, image_stats


def collect_group_summary_inputs(
    session_dir: Path,
    events: list[dict[str, object]],
    step_ids: list[int],
    display_layout: dict[str, object] | None = None,
    send_fullscreen: bool = False,
) -> tuple[list[dict[str, object]], list[Path], list[int], dict[str, int]]:
    rows: list[dict[str, object]] = []
    image_paths: list[Path] = []
    available_step_ids: list[int] = []
    seen: set[Path] = set()
    cropped_count = 0
    cache_dir = session_dir / "ai_preprocessed" / "monitors"
    event_lookup = {resolve_analysis_step_id(event, index): event for index, event in enumerate(events, start=1) if isinstance(event, dict)}
    for step_id in step_ids:
        if not isinstance(step_id, int) or step_id < 1:
            continue
        event = event_lookup.get(step_id)
        if not isinstance(event, dict):
            continue
        primary = _resolve_primary_image(session_dir, event)
        if not primary:
            continue
        path = session_dir / str(primary)
        if not path.exists():
            continue
        row: dict[str, object] = {
            "step_id": step_id,
            "event_type": normalize_event_type(event.get("event_type", ""), event.get("action", "")),
            "action": event.get("action", ""),
        }
        ui_element = _build_prompt_ui_element(event)
        if ui_element:
            row["ui_element"] = ui_element
        event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
        keyboard = event.get("keyboard", {}) if event_type == "input" else {}
        mouse = event.get("mouse", {}) if event_type == "controlOperation" else {}
        if keyboard:
            row["keyboard"] = keyboard
        if mouse:
            row["mouse"] = mouse
        cleaning_signals = _extract_cleaning_signals(event)
        if cleaning_signals:
            row["cleaning_signals"] = cleaning_signals
        prepared_path, was_cropped = prepare_image_path_for_ai(
            path,
            event,
            display_layout,
            cache_dir,
            send_fullscreen=send_fullscreen,
            cache_key=f"group_step_{step_id:04d}",
        )
        if was_cropped:
            cropped_count += 1
        rows.append(row)
        available_step_ids.append(step_id)
        if prepared_path not in seen:
            image_paths.append(prepared_path)
            seen.add(prepared_path)
    return rows, image_paths, available_step_ids, {"image_count": len(image_paths), "cropped_monitor_count": cropped_count}


def find_group_summary_segments(
    session_dir: Path,
    events: list[dict[str, object]],
    excluded_process_names: list[str] | None = None,
    include_all_screenshot_events: bool = False,
) -> list[list[int]]:
    if include_all_screenshot_events:
        return []
    normalized_excluded_process_names = _build_normalized_excluded_process_names(excluded_process_names)
    segments: list[list[int]] = []
    current_segment: list[int] = []
    for fallback_step_id, event in enumerate(events, start=1):
        step_id = resolve_analysis_step_id(event, fallback_step_id)
        if _should_send_event_to_group_summary(session_dir, event, normalized_excluded_process_names):
            current_segment.append(step_id)
            continue
        if current_segment:
            segments.append(current_segment)
            current_segment = []
    if current_segment:
        segments.append(current_segment)
    return segments


def _build_prompt_ui_element(event: dict[str, object]) -> dict[str, object]:
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    name = str(ui_element.get("name", "")).strip()
    control_type = str(ui_element.get("control_type", "")).strip()
    help_text = str(ui_element.get("help_text", "")).strip()
    help_text_fallback = str(ui_element.get("help_text_fallback", "")).strip()
    name_fallbacks = [str(item).strip() for item in ui_element.get("name_fallbacks", []) if str(item).strip()] if isinstance(ui_element.get("name_fallbacks", []), list) else []

    prompt_ui_element: dict[str, object] = {}
    if name:
        prompt_ui_element["name"] = name
    elif name_fallbacks:
        prompt_ui_element["name_fallbacks"] = name_fallbacks
    if help_text:
        prompt_ui_element["help_text"] = help_text
    elif help_text_fallback:
        prompt_ui_element["help_text_fallback"] = help_text_fallback
    if control_type:
        prompt_ui_element["control_type"] = control_type
    return prompt_ui_element


def _resolve_primary_image(session_dir: Path, event: dict[str, object]) -> str | None:
    media_items = event.get("media", [])
    if isinstance(media_items, list):
        for item in media_items:
            if isinstance(item, dict) and item.get("type") == "image" and item.get("path"):
                return str(item.get("path"))
    screenshot = event.get("screenshot")
    if screenshot:
        candidate = session_dir / str(screenshot)
        if candidate.exists():
            return str(screenshot)
    return None


def _should_send_event_to_observation(event: dict[str, object], excluded_process_names: set[str] | None = None) -> bool:
    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", "")).strip().lower()
    action = format_recorded_action(event.get("action", "")).strip().lower()
    if event_type in {"comment", "checkpoint", "getscreenshot"}:
        return False
    if event_type == "wait" or action in {"manual_comment", "ai_checkpoint", "getscreenshot", "manual_screenshot"}:
        return False

    method_name = resolve_method_name_for_event(event)
    if method_name != "FindControlByName":
        return False

    window = event.get("window", {}) if isinstance(event.get("window", {}), dict) else {}
    process_name = _normalize_process_name_for_filter(window.get("process_name", ""))
    if process_name in (excluded_process_names or set()):
        return False

    return True


def _should_send_event_to_group_summary(
    session_dir: Path,
    event: dict[str, object],
    excluded_process_names: set[str] | None = None,
) -> bool:
    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", "")).strip().lower()
    action = format_recorded_action(event.get("action", "")).strip().lower()
    if event_type in {"comment", "checkpoint", "getscreenshot"}:
        return False
    if event_type == "wait" or action in {"manual_comment", "ai_checkpoint", "getscreenshot", "manual_screenshot"}:
        return False
    if _should_send_event_to_observation(event, excluded_process_names):
        return False
    return _resolve_primary_image(session_dir, event) is not None


def _build_normalized_excluded_process_names(excluded_process_names: list[str] | None) -> set[str]:
    normalized: set[str] = set()
    for item in excluded_process_names or []:
        alias_names = _expand_process_filter_aliases(item)
        normalized.update(alias_names)
    return normalized


def _expand_process_filter_aliases(process_name: object) -> set[str]:
    normalized = _normalize_process_name_for_filter(process_name)
    if not normalized:
        return set()
    aliases = {normalized}
    if normalized == "wordpad":
        aliases.add("write")
    elif normalized == "write":
        aliases.add("wordpad")
    return aliases


def _normalize_process_name_for_filter(process_name: object) -> str:
    value = str(process_name or "").strip().lower().replace("\\", "/")
    if not value:
        return ""
    value = value.rsplit("/", 1)[-1]
    if value.endswith(".exe"):
        value = value[:-4]
    return value.strip()
