from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from src.common.media_utils import file_md5
from src.recorder.models import format_recorded_action, normalize_event_type, normalize_keyboard_key_name


@dataclass(slots=True)
class CleaningSuggestion:
    kind: str
    row_indexes: list[int]
    replacement_event: dict[str, object] | None
    reason: str


def build_cleaning_suggestions(session_dir: Path, events: list[dict[str, object]]) -> list[CleaningSuggestion]:
    suggestions: list[CleaningSuggestion] = []
    suggestions.extend(_find_noop_mouse_clicks(session_dir, events))
    suggestions.extend(_find_noop_scrolls(session_dir, events))
    suggestions.extend(_find_reverted_state_pairs(session_dir, events))
    suggestions.extend(_find_key_press_runs(events))
    return suggestions


def apply_cleaning_suggestions(
    events: list[dict[str, object]],
    suggestions: list[CleaningSuggestion],
) -> list[dict[str, object]]:
    delete_indexes: set[int] = set()
    replacements: dict[int, dict[str, object]] = {}

    for suggestion in suggestions:
        if suggestion.kind in {"drop_noop", "drop_noop_scroll"}:
            delete_indexes.update(suggestion.row_indexes)
            continue
        if suggestion.kind == "merge_keypress" and suggestion.replacement_event:
            first_index = suggestion.row_indexes[0]
            replacements[first_index] = suggestion.replacement_event
            delete_indexes.update(suggestion.row_indexes[1:])

    cleaned_events: list[dict[str, object]] = []
    for index, event in enumerate(events):
        if index in replacements:
            cleaned_events.append(replacements[index])
            continue
        if index in delete_indexes:
            continue
        cleaned_events.append(event)
    return cleaned_events


def _find_noop_mouse_clicks(session_dir: Path, events: list[dict[str, object]]) -> list[CleaningSuggestion]:
    suggestions: list[CleaningSuggestion] = []
    state_hashes = [_resolve_state_hash(session_dir, event) for event in events]

    for index, event in enumerate(events[:-1]):
        screenshot_path = _resolve_primary_image_path(session_dir, event)
        current_hash = state_hashes[index]
        event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
        next_hash = state_hashes[index + 1]
        next_path = _resolve_primary_image_path(session_dir, events[index + 1])

        if event_type == "controlOperation" and current_hash and next_hash and screenshot_path and next_path:
            screenshots_match = current_hash == next_hash
            if not screenshots_match:
                screenshots_match = _images_match_ignoring_highlights(screenshot_path, next_path)

            if screenshots_match:
                reason = "与下一条可视状态相同，疑似点击无响应或无效点击。"
                if current_hash != next_hash:
                    reason = "与下一条可视状态仅红框不同，疑似点击无响应或无效点击。"
                suggestions.append(
                    CleaningSuggestion(
                        kind="drop_noop",
                        row_indexes=[index],
                        replacement_event=None,
                        reason=reason,
                    )
                )

    return suggestions


def _find_key_press_runs(events: list[dict[str, object]]) -> list[CleaningSuggestion]:
    suggestions: list[CleaningSuggestion] = []
    run_indexes: list[int] = []
    run_sequence_tokens: list[str] = []
    run_text_buffer: list[str] = []

    def flush() -> None:
        nonlocal run_indexes, run_sequence_tokens, run_text_buffer
        if len(run_indexes) < 2:
            run_indexes = []
            run_sequence_tokens = []
            run_text_buffer = []
            return

        merged_text = "".join(run_text_buffer)
        readable_sequence = " ".join(run_sequence_tokens)
        first_event = events[run_indexes[0]].copy()
        first_event["event_type"] = "input"
        first_event["action"] = "type_input"
        first_event["keyboard"] = {
            "text": merged_text,
            "sequence": run_sequence_tokens[:],
        }
        first_event["note"] = f"Merged input: {readable_sequence}"
        additional = dict(first_event.get("additional_details", {}))
        additional["merged_from_input_count"] = len(run_indexes)
        additional["merged_input_sequence"] = run_sequence_tokens[:]
        first_event["additional_details"] = additional

        suggestions.append(
            CleaningSuggestion(
                kind="merge_keypress",
                row_indexes=run_indexes[:],
                replacement_event=first_event,
                reason=f"连续 {len(run_indexes)} 个 input 可合并为一次输入: {readable_sequence}",
            )
        )
        run_indexes = []
        run_sequence_tokens = []
        run_text_buffer = []

    for index, event in enumerate(events):
        if normalize_event_type(event.get("event_type", ""), event.get("action", "")) != "input":
            flush()
            continue

        segment = _normalize_input_merge_segment(event)
        if segment is None:
            flush()
            continue

        run_indexes.append(index)
        run_sequence_tokens.extend(segment["sequence_tokens"])
        for operation in segment["ops"]:
            if operation["op"] == "insert":
                run_text_buffer.append(str(operation["text"]))
            elif operation["op"] == "backspace":
                if run_text_buffer:
                    run_text_buffer.pop()

    flush()
    return suggestions


def _normalize_input_merge_segment(event: dict[str, object]) -> dict[str, object] | None:
    action_value = format_recorded_action(event.get("action", "")).strip().lower()
    if action_value == "press":
        token = _normalize_keypress_token(event)
        if token is None:
            return None
        return {
            "sequence_tokens": [token["sequence_token"]],
            "ops": [{"op": token["op"], "text": token["text"]}],
        }

    if action_value != "type_input":
        return None

    keyboard = event.get("keyboard", {})
    if not isinstance(keyboard, dict):
        return None

    merged_text = keyboard.get("text", "")
    text_value = str(merged_text) if merged_text is not None else ""
    raw_sequence = keyboard.get("sequence", [])
    if isinstance(raw_sequence, list):
        sequence_tokens = [str(item) for item in raw_sequence if str(item).strip()]
    else:
        sequence_tokens = []
    if not sequence_tokens and text_value:
        sequence_tokens = [_format_sequence_char(char) for char in text_value]
    if not sequence_tokens and not text_value:
        return None

    return {
        "sequence_tokens": sequence_tokens,
        "ops": [{"op": "insert", "text": text_value}],
    }


def _normalize_keypress_token(event: dict[str, object]) -> dict[str, object] | None:
    keyboard = event.get("keyboard", {})
    if not isinstance(keyboard, dict):
        return None

    char = keyboard.get("char")
    if isinstance(char, str) and len(char) == 1 and char.isprintable():
        return {"op": "insert", "text": char, "sequence_token": _format_sequence_char(char)}

    normalized_key_name = normalize_keyboard_key_name(keyboard.get("key_name", ""))
    if len(normalized_key_name) == 1 and normalized_key_name.isprintable():
        return {"op": "insert", "text": normalized_key_name, "sequence_token": _format_sequence_char(normalized_key_name)}

    normalized = normalized_key_name.lower()
    if normalized == "space":
        return {"op": "insert", "text": " ", "sequence_token": "[Space]"}
    if normalized == "enter":
        return {"op": "insert", "text": "\n", "sequence_token": "[Enter]"}
    if normalized == "tab":
        return {"op": "insert", "text": "\t", "sequence_token": "[Tab]"}
    if normalized == "backspace":
        return {"op": "backspace", "text": "", "sequence_token": "[Backspace]"}
    return None


def _format_sequence_char(char: str) -> str:
    if char == " ":
        return "[Space]"
    if char == "\n":
        return "[Enter]"
    if char == "\t":
        return "[Tab]"
    return char


def _find_noop_scrolls(session_dir: Path, events: list[dict[str, object]]) -> list[CleaningSuggestion]:
    suggestions: list[CleaningSuggestion] = []
    state_hashes = [_resolve_state_hash(session_dir, event) for event in events]

    for index, event in enumerate(events[:-1]):
        screenshot_path = _resolve_primary_image_path(session_dir, event)
        current_hash = state_hashes[index]
        event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
        action_value = format_recorded_action(event.get("action", "")).strip().lower()
        next_hash = state_hashes[index + 1]
        next_path = _resolve_primary_image_path(session_dir, events[index + 1])

        if event_type == "mouseAction" and action_value == "mouse_scroll" and current_hash and next_hash and screenshot_path and next_path:
            screenshots_match = current_hash == next_hash
            if not screenshots_match:
                screenshots_match = _images_match_ignoring_highlights(screenshot_path, next_path)
            if screenshots_match:
                reason = "滚动后与下一条可视状态相同，疑似无效滚动或滚动未生效。"
                if current_hash != next_hash:
                    reason = "滚动后与下一条可视状态仅红框不同，疑似无效滚动或滚动未生效。"
                suggestions.append(
                    CleaningSuggestion(
                        kind="drop_noop_scroll",
                        row_indexes=[index],
                        replacement_event=None,
                        reason=reason,
                    )
                )

    return suggestions


def _find_reverted_state_pairs(session_dir: Path, events: list[dict[str, object]]) -> list[CleaningSuggestion]:
    suggestions: list[CleaningSuggestion] = []
    state_hashes = [_resolve_state_hash(session_dir, event) for event in events]

    for index in range(1, len(events) - 1):
        previous_hash = state_hashes[index - 1]
        current_hash = state_hashes[index]
        next_hash = state_hashes[index + 1]
        if not previous_hash or not current_hash or not next_hash:
            continue
        if previous_hash != next_hash or previous_hash == current_hash:
            continue

        current_type = normalize_event_type(events[index].get("event_type", ""), events[index].get("action", ""))
        next_type = normalize_event_type(events[index + 1].get("event_type", ""), events[index + 1].get("action", ""))
        if current_type not in {"controlOperation", "mouseAction"} or next_type not in {"controlOperation", "mouseAction"}:
            continue

        suggestions.append(
            CleaningSuggestion(
                kind="review_revert_pair",
                row_indexes=[index, index + 1],
                replacement_event=None,
                reason="检测到状态 A→B→A 回退。可以识别，但这类操作可能是有意义的打开/关闭或试探性点击，默认只提示不自动清洗。",
            )
        )

    return suggestions


def _resolve_primary_image_path(session_dir: Path, event: dict[str, object]) -> Path | None:
    media_items = event.get("media", [])
    if isinstance(media_items, list):
        for item in media_items:
            if isinstance(item, dict) and item.get("type") == "image" and item.get("path"):
                return session_dir / str(item.get("path"))

    screenshot = event.get("screenshot")
    if screenshot:
        return session_dir / str(screenshot)
    return None


def _resolve_state_hash(session_dir: Path, event: dict[str, object]) -> str | None:
    image_path = _resolve_primary_image_path(session_dir, event)
    if not image_path:
        return None
    return file_md5(image_path)


def _images_match_ignoring_highlights(left_path: Path, right_path: Path) -> bool:
    try:
        with Image.open(left_path) as left_image, Image.open(right_path) as right_image:
            left_rgb = left_image.convert("RGB")
            right_rgb = right_image.convert("RGB")
            if left_rgb.size != right_rgb.size:
                return False

            left_pixels = left_rgb.load()
            right_pixels = right_rgb.load()
            width, height = left_rgb.size
            for y in range(height):
                for x in range(width):
                    left_pixel = left_pixels[x, y]
                    right_pixel = right_pixels[x, y]
                    if left_pixel == right_pixel:
                        continue
                    if _is_highlight_red(left_pixel) or _is_highlight_red(right_pixel):
                        continue
                    return False
            return True
    except Exception:
        return False


def _is_highlight_red(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 220 and green <= 90 and blue <= 90