from __future__ import annotations

import os
import platform
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import psutil

from src.common.display_utils import get_display_layout_snapshot
from .models import UIElementInfo, WindowInfo

try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None

try:
    from pywinauto import Desktop
except ImportError:
    Desktop = None


_HELP_TEXT_PARENT_FALLBACK_DEPTH = 3
_HELP_TEXT_FALLBACK_BUDGET_SECONDS = 0.03
_TEXT_NAME_FALLBACK_DEPTH = 2
_TEXT_NAME_FALLBACK_BUDGET_SECONDS = 0.03
_TEXT_NAME_FALLBACK_LIMIT = 8


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def build_environment_snapshot() -> dict[str, object]:
    return {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "hostname": platform.node(),
        "cwd": os.getcwd(),
        "display_layout": get_display_layout_snapshot(),
    }


def safe_relpath(path: Path, start: Path) -> str:
    return str(path.relative_to(start)).replace("\\", "/")


def get_active_window_info() -> WindowInfo:
    if not win32gui:
        return WindowInfo()

    try:
        handle = win32gui.GetForegroundWindow()
        return _build_window_info_from_handle(handle)
    except Exception:
        return WindowInfo()


def get_window_info_at_point(x: int, y: int) -> WindowInfo:
    if not win32gui:
        return WindowInfo()

    try:
        handle = win32gui.WindowFromPoint((x, y))
        return _build_window_info_from_handle(handle)
    except Exception:
        return WindowInfo()


def get_ui_element_at_point(x: int, y: int) -> UIElementInfo:
    if Desktop is None:
        return UIElementInfo()

    try:
        element = Desktop(backend="uia").from_point(x, y)
        rect = element.rectangle()
        info = element.element_info
        current_control_type = str(getattr(info, "control_type", "") or "")
        current_help_text = _extract_help_text(element, info) if current_control_type.strip().lower() == "image" else ""
        return UIElementInfo(
            name=getattr(info, "name", "") or "",
            control_type=current_control_type,
            automation_id=getattr(info, "automation_id", "") or "",
            class_name=getattr(info, "class_name", "") or "",
            help_text=current_help_text,
            help_text_fallback="" if current_help_text or current_control_type.strip().lower() != "image" else _extract_parent_help_text(element),
            name_fallbacks=_extract_text_child_names(element, info),
            rectangle={
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
            },
        )
    except Exception:
        return UIElementInfo()


def _extract_parent_help_text(element: object) -> str:
    deadline = time.perf_counter() + _HELP_TEXT_FALLBACK_BUDGET_SECONDS
    current = element
    for _ in range(_HELP_TEXT_PARENT_FALLBACK_DEPTH):
        if time.perf_counter() >= deadline:
            break
        parent = _get_parent_element(current)
        if parent is None:
            break
        parent_info = getattr(parent, "element_info", None)
        text = _extract_help_text(parent, parent_info)
        if text:
            return text
        current = parent

    return ""


def _extract_help_text(element: object, info: object) -> str:
    text = str(getattr(info, "help_text", "") or "").strip()
    if text:
        return text

    try:
        legacy_properties = getattr(element, "legacy_properties", None)
        if callable(legacy_properties):
            payload = legacy_properties()
            if isinstance(payload, dict):
                text = str(payload.get("Help", "") or "").strip()
                if text:
                    return text
    except Exception:
        pass

    return ""


def _get_parent_element(element: object) -> object | None:
    try:
        parent_method = getattr(element, "parent", None)
        if callable(parent_method):
            parent = parent_method()
            if parent is not None:
                return parent
    except Exception:
        pass

    try:
        info = getattr(element, "element_info", None)
        if info is not None:
            parent_info = getattr(info, "parent", None)
            if parent_info is not None:
                return getattr(parent_info, "wrapper_object", lambda: None)()
    except Exception:
        pass

    return None


def _extract_text_child_names(element: object, info: object) -> list[str]:
    control_type = str(getattr(info, "control_type", "") or "").strip().lower()
    if control_type == "text":
        return []

    deadline = time.perf_counter() + _TEXT_NAME_FALLBACK_BUDGET_SECONDS
    matches: list[str] = []
    seen: set[str] = set()
    queue: list[tuple[object, int]] = [(element, 0)]

    while queue and time.perf_counter() < deadline and len(matches) < _TEXT_NAME_FALLBACK_LIMIT:
        current, depth = queue.pop(0)
        if depth >= _TEXT_NAME_FALLBACK_DEPTH:
            continue
        for child in _get_child_elements(current):
            child_info = getattr(child, "element_info", None)
            child_control_type = str(getattr(child_info, "control_type", "") or "").strip().lower()
            child_name = str(getattr(child_info, "name", "") or "").strip()
            if child_control_type == "text" and child_name and child_name not in seen:
                matches.append(child_name)
                seen.add(child_name)
                if len(matches) >= _TEXT_NAME_FALLBACK_LIMIT:
                    break
            queue.append((child, depth + 1))

    return matches


def _get_child_elements(element: object) -> list[object]:
    children_method = getattr(element, "children", None)
    if callable(children_method):
        try:
            children = children_method()
            if isinstance(children, list):
                return [child for child in children if child is not None]
        except Exception:
            pass

    info = getattr(element, "element_info", None)
    info_children = getattr(info, "children", None) if info is not None else None
    if isinstance(info_children, list):
        wrappers: list[object] = []
        for child_info in info_children:
            try:
                wrapper = getattr(child_info, "wrapper_object", lambda: None)()
            except Exception:
                wrapper = None
            if wrapper is not None:
                wrappers.append(wrapper)
        return wrappers

    return []


def serialize_window_info(window: WindowInfo) -> dict[str, object]:
    return asdict(window)


def serialize_ui_element_info(ui_element: UIElementInfo) -> dict[str, object]:
    return asdict(ui_element)


def _build_window_info_from_handle(handle: int) -> WindowInfo:
    if not handle:
        return WindowInfo()

    title = win32gui.GetWindowText(handle)
    class_name = win32gui.GetClassName(handle)
    process_id = None
    process_name = ""
    if win32process:
        _, process_id = win32process.GetWindowThreadProcessId(handle)
        if process_id:
            process_name = psutil.Process(process_id).name()
    return WindowInfo(
        title=title,
        class_name=class_name,
        handle=hex(handle),
        process_id=process_id,
        process_name=process_name,
    )