from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.recorder.models import format_recorded_action, normalize_event_type


@dataclass(frozen=True, slots=True)
class MethodSuggestionOption:
    name: str
    reason: str = ""
    score: float = 100.0
    confidence: float = 1.0

    def to_dict(self, *, is_default: bool = False) -> dict[str, Any]:
        return {
            "name": self.name,
            "reason": self.reason,
            "score": self.score,
            "confidence": self.confidence,
            "is_default": is_default,
        }


EVENT_METHOD_SUGGESTION_OPTIONS: dict[str, tuple[MethodSuggestionOption, ...]] = {
    "controlOperation": (
        MethodSuggestionOption(
            name="FindControlByName",
            reason="默认建议：controlOperation 优先按 UIA 控件属性定位并执行操作。",
            score=100.0,
            confidence=1.0,
        ),
        MethodSuggestionOption(
            name="ClickByOCRWindows",
            reason="备选建议：当控件属性不稳定或只适合通过可见文本定位时，可改用 Windows OCR 点击。",
            score=90.0,
            confidence=0.9,
        ),
        MethodSuggestionOption(
            name="SelectDataGridRows",
            reason="备选建议：当点击目标是表格/DataGrid 中的一行时，可改用列值组合定位并选择行。",
            score=88.0,
            confidence=0.88,
        ),
        MethodSuggestionOption(
            name="MatchingClick",
            reason="备选建议：当控件属性/OCR 不稳定但目标外观稳定时，可截取目标小图并通过图像匹配点击。",
            score=86.0,
            confidence=0.86,
        ),
    ),
    "Click": (
        MethodSuggestionOption(
            name="Click",
            reason="默认建议：Click 类型按录制坐标/鼠标按键执行点击。",
        ),
    ),
    "PerformScan": (
        MethodSuggestionOption(
            name="PerformScan",
            reason="默认建议：PerformScan 类型映射到 PerformScan。",
        ),
        MethodSuggestionOption(
            name="ScanDll",
            reason="备选建议：当该步骤只需要直接触发扫描器 DLL/串口命令时，可根据 Comment 列内容改用 ScanDll。",
            score=88.0,
            confidence=0.88,
        ),
    ),
    "input": (
        MethodSuggestionOption(
            name="SendKeys",
            reason="默认建议：input 类型映射到 SendKeys。",
        ),
    ),
    "wait": (
        MethodSuggestionOption(
            name="WaitForExists",
            reason="默认建议：wait 类型映射到 WaitForExists。",
        ),
        MethodSuggestionOption(
            name="WaitTime",
            reason="备选建议：当该 wait 步骤只需要固定等待时间、不需要等待图片或控件出现/消失时，可改用 WaitTime。",
            score=86.0,
            confidence=0.86,
        ),
    ),
    "comment": (
        MethodSuggestionOption(
            name="ManualCheck",
            reason="默认建议：comment 类型映射到 ManualCheck。",
        ),
    ),
    "checkpoint": (
        MethodSuggestionOption(
            name="AgentInterface",
            reason="默认建议：checkpoint 类型映射到 AgentInterface。",
        ),
        MethodSuggestionOption(
            name="ManualCheck",
            reason="备选建议：当 checkpoint 只需要人工确认、不需要 AI 视觉判断时，可改用 ManualCheck。",
            score=88.0,
            confidence=0.88,
        ),
    ),
    "getScreenshot": (
        MethodSuggestionOption(
            name="GetScreenShot",
            reason="默认建议：getScreenshot 类型映射到 GetScreenShot。",
        ),
    ),
}


METHOD_SUGGESTION_NAME_MAP: dict[str, str] = {
    event_type: options[0].name
    for event_type, options in EVENT_METHOD_SUGGESTION_OPTIONS.items()
    if options
}


def resolve_method_name_for_event(event: dict[str, Any]) -> str:
    options = resolve_method_options_for_event(event)
    return options[0].name if options else ""


def resolve_method_options_for_event(event: dict[str, Any]) -> list[MethodSuggestionOption]:
    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
    if event_type == "mouseAction":
        return resolve_mouse_action_method_options(event)
    if event_type == "checkpoint":
        return resolve_checkpoint_method_options(event)
    return list(EVENT_METHOD_SUGGESTION_OPTIONS.get(event_type, ()))


def resolve_checkpoint_method_options(event: dict[str, Any]) -> list[MethodSuggestionOption]:
    checkpoint = event.get("checkpoint", {}) if isinstance(event.get("checkpoint", {}), dict) else {}
    query = str(checkpoint.get("query", "") or "").strip()
    if query:
        return list(EVENT_METHOD_SUGGESTION_OPTIONS.get("checkpoint", ()))
    return [
        MethodSuggestionOption(
            name="ManualCheck",
            reason="默认建议：checkpoint.query 为空，无法进行 AI 视觉判断，映射到 ManualCheck。",
            score=100.0,
            confidence=1.0,
        ),
        MethodSuggestionOption(
            name="AgentInterface",
            reason="备选建议：如果后续补充 checkpoint.query，可改用 AgentInterface 进行 AI 视觉判断。",
            score=88.0,
            confidence=0.88,
        ),
    ]


def resolve_method_option_dicts_for_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        option.to_dict(is_default=index == 0)
        for index, option in enumerate(resolve_method_options_for_event(event))
    ]


def resolve_mouse_action_method_name(event: dict[str, Any]) -> str:
    options = resolve_mouse_action_method_options(event)
    return options[0].name if options else ""


def resolve_mouse_action_method_options(event: dict[str, Any]) -> list[MethodSuggestionOption]:
    action_value = format_recorded_action(event.get("action", "")).strip().lower()
    mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
    if "scroll" in action_value:
        return [
            MethodSuggestionOption(
                name="Wheel",
                reason="默认建议：mouseAction 且 action 包含 scroll，映射到 Wheel。",
            )
        ]
    has_drag_points = all(
        isinstance(mouse.get(key), int)
        for key in ("start_x", "start_y", "end_x", "end_y")
    )
    if has_drag_points:
        return [
            MethodSuggestionOption(
                name="DragDrop",
                reason="默认建议：mouseAction 且 mouse 含 start/end 坐标，映射到 DragDrop。",
            )
        ]
    return []