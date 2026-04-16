from __future__ import annotations

import json
from pathlib import Path

from src.common.display_utils import prepare_image_path_for_ai


def build_step_observation_prompt(
    batch_events: list[dict[str, object]],
) -> str:
    instruction = {
        "task": "观察当前桌面自动化步骤，基于截图和 batch_events 提取事实层描述",
        "requirements": [
            "请结合截图中的红色框选区域和 batch_events 数据，识别用户实际操作的目标区域和目标控件。红框表示本步操作聚焦的区域，不等于一律要描述成点击。",
            "如果只有batch_events没有截图，就根据batch_events描述操作",
            "描述要简洁、专业，优先输出用户真实意图和结果语义，而不是机械描述鼠标动作。比如 combobox 里选择值应描述为选中某字段中的某值；checkbox 应根据界面状态描述为勾选或取消勾选；tab、列表项、菜单项等应描述为切换、选中或打开，而不是统一写成点击。",
            "如果能从截图或界面视觉上下文识别到控件对应的 label，就要把这个 label 作为操作控件的标签写出来，例如：点击“Patient ID”右侧的 Editbox。",
            "如果从截图里能识别到可用 label，必须准确使用这个 label 原文，不要猜测、改写、翻译、缩写或替换成其他近似词。",
            "如果 batch_events 没有可用 label，但有 helpText，就直接使用 helpText 描述控件，不需要再强行补 label。",
            "只有在无法判断更准确语义时，才使用单击、双击、右击、拖拽、滚动等表层鼠标动作描述。",
            "每个 step_observation 必须同时输出 observation 和 semantic_kind。",
            "semantic_kind 只能从以下枚举中选择一个: wait、mouseAction、tableOperation、controlOperation、input、comment、checkpoint。不要输出其他值。",
            "controlOperation 用于对普通控件的操作，例如 button、checkbox、editcontrol、combobox、radiobutton、tab、菜单项等控件上的点击、切换、勾选、取消勾选、展开、收起、打开、关闭、选择值等；mouseAction 用于更偏表层的鼠标动作，例如拖拽、滚动、框选、右击空白区域等；tableOperation 用于表格、列表、grid、row、cell 上的选择、编辑、增删改等操作；input 用于文本输入、键盘录入、快捷键输入；comment 用于添加备注；checkpoint 用于添加 AI checkpoint；wait 用于明确的等待类动作或等待界面状态变化。",
            "不要输出分析过程，只输出最终的操作描述",
            "只输出 JSON。",
        ],
        "json_schema_hint": {
        "step_observations": [
            {
                "observation": "操作描述",
                "semantic_kind": "controlOperation"
            }
        ]
        },
        "batch_events": batch_events,
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
            "step_insights.description 使用最终输出范式，聚焦用户实际操作；若是下拉值选择，应优先归一为选中某字段右侧combobox中的某值。",
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
        event_type = str(event.get("event_type", ""))
        keyboard = event.get("keyboard", {}) if event_type == "key_press" else {}
        mouse = event.get("mouse", {}) if event_type == "mouse_click" else {}
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


def collect_batch_images(
    session_dir: Path,
    events: list[dict[str, object]],
    start_index: int,
    batch_size: int,
    display_layout: dict[str, object] | None = None,
    send_fullscreen: bool = False,
) -> tuple[list[Path], dict[str, int]]:
    image_paths: list[Path] = []
    seen: set[Path] = set()
    cropped_count = 0
    cache_dir = session_dir / "ai_preprocessed" / "monitors"
    sliced = events[start_index : start_index + batch_size]
    for offset, event in enumerate(sliced, start=start_index + 1):
        primary = _resolve_primary_image(session_dir, event)
        if not primary:
            continue
        path = session_dir / str(primary)
        if not path.exists():
            continue
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
        if prepared_path not in seen:
            image_paths.append(prepared_path)
            seen.add(prepared_path)
    return image_paths, {"image_count": len(image_paths), "cropped_monitor_count": cropped_count}


def _build_prompt_ui_element(event: dict[str, object]) -> dict[str, object]:
    ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
    name = str(ui_element.get("name", "")).strip()
    control_type = str(ui_element.get("control_type", "")).strip()
    help_text = str(ui_element.get("help_text", "")).strip()

    prompt_ui_element: dict[str, object] = {}
    if name:
        prompt_ui_element["name"] = name
    elif help_text:
        prompt_ui_element["help_text"] = help_text
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
