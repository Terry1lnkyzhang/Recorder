from __future__ import annotations

from pathlib import Path

import mss
from PIL import Image


def get_display_layout_snapshot() -> dict[str, object]:
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
    except Exception:
        return {"virtual_screen": {}, "monitors": []}

    if not monitors:
        return {"virtual_screen": {}, "monitors": []}

    virtual = monitors[0]
    normalized_monitors: list[dict[str, int]] = []
    for index, monitor in enumerate(monitors[1:], start=1):
        normalized_monitors.append(
            {
                "index": index,
                "left": int(monitor["left"]),
                "top": int(monitor["top"]),
                "width": int(monitor["width"]),
                "height": int(monitor["height"]),
            }
        )

    return {
        "virtual_screen": {
            "left": int(virtual["left"]),
            "top": int(virtual["top"]),
            "width": int(virtual["width"]),
            "height": int(virtual["height"]),
        },
        "monitors": normalized_monitors,
    }


def prepare_image_path_for_ai(
    image_path: Path,
    event: dict[str, object],
    display_layout: dict[str, object] | None,
    cache_dir: Path,
    send_fullscreen: bool,
    cache_key: str,
) -> tuple[Path, bool]:
    if send_fullscreen:
        return image_path, False

    layout = _normalize_display_layout(display_layout) or get_display_layout_snapshot()
    virtual_screen = layout.get("virtual_screen", {}) if isinstance(layout, dict) else {}
    monitors = layout.get("monitors", []) if isinstance(layout, dict) else []
    if not isinstance(virtual_screen, dict) or not isinstance(monitors, list) or len(monitors) <= 1:
        return image_path, False

    focus_point = _extract_event_focus_point(event)
    if focus_point is None:
        return image_path, False

    monitor = _find_monitor_for_point(focus_point[0], focus_point[1], monitors)
    if monitor is None:
        return image_path, False

    try:
        with Image.open(image_path) as image:
            virtual_width = int(virtual_screen.get("width", 0))
            virtual_height = int(virtual_screen.get("height", 0))
            if image.size != (virtual_width, virtual_height):
                return image_path, False

            crop_box = _build_crop_box(monitor, virtual_screen)
            if crop_box == (0, 0, virtual_width, virtual_height):
                return image_path, False

            cache_dir.mkdir(parents=True, exist_ok=True)
            output_path = cache_dir / f"{cache_key}_{image_path.stem}_monitor.png"
            if output_path.exists():
                return output_path, True

            image.crop(crop_box).save(output_path)
            return output_path, True
    except Exception:
        return image_path, False


def _normalize_display_layout(raw_layout: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(raw_layout, dict):
        return None
    if not isinstance(raw_layout.get("virtual_screen"), dict):
        return None
    if not isinstance(raw_layout.get("monitors"), list):
        return None
    return raw_layout


def _extract_event_focus_point(event: dict[str, object]) -> tuple[int, int] | None:
    media = event.get("media", {})
    if isinstance(media, list):
        for item in media:
            if not isinstance(item, dict):
                continue
            region = item.get("region")
            if isinstance(region, dict):
                left = region.get("left")
                top = region.get("top")
                width = region.get("width")
                height = region.get("height")
                if all(isinstance(value, int) for value in [left, top, width, height]):
                    return int(left + width / 2), int(top + height / 2)

    additional = event.get("additional_details", {})
    if isinstance(additional, dict):
        region = additional.get("selection_region")
        if isinstance(region, dict):
            left = region.get("left")
            top = region.get("top")
            width = region.get("width")
            height = region.get("height")
            if all(isinstance(value, int) for value in [left, top, width, height]):
                return int(left + width / 2), int(top + height / 2)

    mouse = event.get("mouse", {})
    if isinstance(mouse, dict):
        x = mouse.get("x")
        y = mouse.get("y")
        if isinstance(x, int) and isinstance(y, int):
            return x, y

    ui_element = event.get("ui_element", {})
    if isinstance(ui_element, dict):
        rectangle = ui_element.get("rectangle", {})
        if isinstance(rectangle, dict):
            left = rectangle.get("left")
            top = rectangle.get("top")
            right = rectangle.get("right")
            bottom = rectangle.get("bottom")
            if all(isinstance(value, int) for value in [left, top, right, bottom]):
                return int((left + right) / 2), int((top + bottom) / 2)
    return None


def _find_monitor_for_point(x: int, y: int, monitors: list[dict[str, object]]) -> dict[str, object] | None:
    for monitor in monitors:
        left = monitor.get("left")
        top = monitor.get("top")
        width = monitor.get("width")
        height = monitor.get("height")
        if not all(isinstance(value, int) for value in [left, top, width, height]):
            continue
        if left <= x < left + width and top <= y < top + height:
            return monitor
    return None


def _build_crop_box(monitor: dict[str, object], virtual_screen: dict[str, object]) -> tuple[int, int, int, int]:
    monitor_left = int(monitor.get("left", 0))
    monitor_top = int(monitor.get("top", 0))
    monitor_width = int(monitor.get("width", 0))
    monitor_height = int(monitor.get("height", 0))
    virtual_left = int(virtual_screen.get("left", 0))
    virtual_top = int(virtual_screen.get("top", 0))
    left = monitor_left - virtual_left
    top = monitor_top - virtual_top
    return left, top, left + monitor_width, top + monitor_height