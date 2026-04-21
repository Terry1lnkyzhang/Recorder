from __future__ import annotations

import json
from pathlib import Path

from src.common.display_utils import prepare_image_path_for_ai
from src.recorder.models import format_recorded_action, normalize_event_type


def build_step_observation_prompt() -> str:
    instruction = {
        "task": "观察当前桌面自动化步骤，仅基于截图提取事实层描述",
        "requirements": [
            "你只能基于截图和红色框选区域判断，不要假设还能看到历史步骤、结构化事件数据或其他上下文。",
            "请针对每一步输出 5 个字段：control_type、label、relative_position、need_scroll、is_table。不要输出其他字段。",
            "红框就是当前操作目标区域。control_type 表示红框对应控件的类型，例如 button、editbox、combobox、checkbox、radiobutton、tab、menuitem、table、row、cell、list、listitem、dialog、panel；无法确定时可用 unknown。",
            "label 表示红框目标控件自身的文字标签，或与该控件最直接对应的字段标签。对于 combobox 和 editbox，label 不应取控件内部当前显示的值或输入内容，而应优先取最直接对应的字段标签。必须准确使用截图原文，不要猜测、改写、翻译、缩写或替换成其他近似词；没有明确 label 时返回空字符串。",
            "relative_position 只能是 self、up、down、left、right 之一，表示目标控件位于 label 的哪个方向。例如控件在 label 右边时返回 right，在 label 左边时返回 left，在 label 上方时返回 up，在 label 下方时返回 down；若 label 就在目标控件自身上，则返回 self。",
            "若按钮、复选框、单选框、tab、菜单项等控件自身已带明确 label，则 label 直接写该控件文字，relative_position 必须为 self，不要再借用附近字段标签。",
            "need_scroll 只能是 true 或 false。只要从截图中可以判断，红框目标控件所属的容器支持通过滚动来调整显示内容，例如位于可滚动列表、表格、滚动面板或下拉项区域中，就返回 true；否则返回 false。",
            "is_table 只能是 true 或 false。只有当从截图中可以明确看出，红框目标位于具有明显行列结构的表格区域中，并且各列有清晰列名或表头时，才返回 true；普通列表、菜单、树、下拉项、无明确列表头的成组项目都返回 false。",
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
                "is_table": False
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
            "current_step_observations 中已经包含 control_type、label、relative_position、need_scroll、is_table 以及基于这些字段生成的 observation。step_insights.description 应优先基于这些结构化字段生成最终语义。",
            "若按钮、复选框、单选框、tab、菜单项等控件自身有 label，应直接写点击该控件，不要再写成某字段右侧的按钮。若是下拉值选择，应优先归一为选中某字段右侧combobox中的某值。",
            "recent_step_observations 提供当前步骤前最多 20 步的局部上下文，可据此判断当前步骤是否属于某个可组合模块。",
            "previous_memory 提供更早步骤的摘要，只用于补充历史阶段和状态变化，不要把它误当成当前步骤。",
            "如能判断局部步骤可组合，可输出 reusable_modules；如发现当前或相关步骤可疑，可输出 invalid_steps；如需要等待条件，可输出 wait_suggestions。没有则输出空数组。",
            "可输出 notes 和 batch_summary，帮助记录这一批的阶段、状态变化与后续摘要。",
            "只输出 JSON。",
        ],
        "json_schema_hint": {
            "step_insights": [
                {
                    "step_id": 21,
                    "description": "选中“mAs(mA)”右侧combobox中的“6”",
                }
            ],
            "invalid_steps": [],
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


def collect_observation_inputs(
    session_dir: Path,
    events: list[dict[str, object]],
    start_index: int,
    batch_size: int,
    display_layout: dict[str, object] | None = None,
    send_fullscreen: bool = False,
) -> tuple[list[dict[str, object]], list[Path], list[int], dict[str, int]]:
    rows: list[dict[str, object]] = []
    image_paths: list[Path] = []
    step_ids: list[int] = []
    seen: set[Path] = set()
    cropped_count = 0
    cache_dir = session_dir / "ai_preprocessed" / "monitors"
    sliced = events[start_index : start_index + batch_size]
    for offset, event in enumerate(sliced, start=start_index + 1):
        if not _should_send_event_to_observation(event):
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
        prepared_path, was_cropped = prepare_image_path_for_ai(
            path,
            event,
            display_layout,
            cache_dir,
            send_fullscreen=send_fullscreen,
            cache_key=f"step_{offset:04d}",
        )
        if was_cropped:
            cropped_count += 1
        rows.append(row)
        step_ids.append(offset)
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
) -> tuple[list[Path], dict[str, int]]:
    _, image_paths, _, image_stats = collect_observation_inputs(
        session_dir,
        events,
        start_index,
        batch_size,
        display_layout=display_layout,
        send_fullscreen=send_fullscreen,
    )
    return image_paths, image_stats


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


def _should_send_event_to_observation(event: dict[str, object]) -> bool:
    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", "")).strip().lower()
    action = format_recorded_action(event.get("action", "")).strip().lower()
    if event_type in {"comment", "checkpoint", "getscreenshot"}:
        return False
    if event_type == "wait" or action in {"manual_comment", "ai_checkpoint", "getscreenshot", "manual_screenshot"}:
        return False
    return True
