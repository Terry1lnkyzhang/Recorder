from __future__ import annotations

import copy
import math
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


# --- Cleaning signal categories (advisory, do not auto-delete) -----------------
# These are heuristic markers fed to the AI reasoning prompt so the LLM can do
# trial-and-error step detection with structured hints instead of guessing from
# screenshots alone. They DO NOT change cleaning-suggestion behavior.
SIGNAL_SPATIAL_CLUSTER_CLICKS = "spatial_cluster_clicks"
SIGNAL_BACKSPACE_RUN = "backspace_run"
SIGNAL_MENU_OPEN_CLOSE = "menu_open_close"
SIGNAL_WINDOW_VISIT_LEAVE = "window_visit_leave"

# Default thresholds — kept liberal to avoid over-flagging. Callers can re-derive
# values from a session config in the future without breaking the public API.
SPATIAL_CLUSTER_MAX_DISTANCE_PX = 200
SPATIAL_CLUSTER_MIN_RUN = 3
BACKSPACE_RUN_MIN_INPUT_EVENTS = 2
WINDOW_VISIT_MAX_LEN = 5


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


# === Cleaning signals ========================================================
# A "signal" is an advisory hint identifying steps that look like trial-and-error
# operations (clicked the wrong place then tried again, typed something then
# backspaced and re-typed, opened a menu then closed it, walked into a window
# then left). Signals do NOT mutate the event stream by themselves — they are
# attached to events via ``annotate_events_with_cleaning_signals`` so the AI
# reasoning pass can decide whether to mark the steps as ``invalid_steps``.


def build_cleaning_signals(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Compute trial-and-error advisory signals for the given event list.

    Returns a list of signal dicts of shape::

        {
            "kind": "spatial_cluster_clicks" | "backspace_run"
                   | "menu_open_close" | "window_visit_leave",
            "row_indexes": [int, ...],     # 0-based positions in ``events``
            "kept_row_index": int | None,  # which step is likely the intentional one
            "reason": str,                 # human-readable Chinese explanation
        }

    The function is intentionally cheap (no PIL / no file IO) so it is safe to
    call from the AI service layer.
    """

    if not events:
        return []
    signals: list[dict[str, object]] = []
    signals.extend(_find_spatial_cluster_clicks(events))
    signals.extend(_find_backspace_runs(events))
    signals.extend(_find_menu_open_close_pairs(events))
    signals.extend(_find_window_visit_then_leave(events))
    return signals


def annotate_events_with_cleaning_signals(
    events: list[dict[str, object]],
    signals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return a deep copy of ``events`` with signals attached.

    Each affected event gets a ``additional_details.cleaning_signals`` list
    appended (created if missing) describing every signal it participates in.
    The original ``events`` argument is not modified.
    """

    annotated = [copy.deepcopy(event) if isinstance(event, dict) else event for event in events]
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        row_indexes = signal.get("row_indexes", [])
        if not isinstance(row_indexes, list):
            continue
        for row_index in row_indexes:
            if not isinstance(row_index, int) or row_index < 0 or row_index >= len(annotated):
                continue
            event = annotated[row_index]
            if not isinstance(event, dict):
                continue
            additional = event.get("additional_details")
            if not isinstance(additional, dict):
                additional = {}
                event["additional_details"] = additional
            existing = additional.get("cleaning_signals")
            if not isinstance(existing, list):
                existing = []
                additional["cleaning_signals"] = existing
            existing.append(
                {
                    "kind": signal.get("kind", ""),
                    "row_indexes": list(row_indexes),
                    "kept_row_index": signal.get("kept_row_index"),
                    "reason": signal.get("reason", ""),
                }
            )
    return annotated


# --- Individual detectors ---------------------------------------------------


def _find_spatial_cluster_clicks(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Detect runs of >=3 consecutive clicks in the same window whose
    successive coordinates are within ``SPATIAL_CLUSTER_MAX_DISTANCE_PX``.

    The last click in the run is treated as the likely-intentional step;
    the earlier ones get flagged as suspect.
    """

    signals: list[dict[str, object]] = []
    run: list[int] = []

    def flush() -> None:
        nonlocal run
        if len(run) >= SPATIAL_CLUSTER_MIN_RUN:
            kept = run[-1]
            suspect_indexes = run[:-1]
            signals.append(
                {
                    "kind": SIGNAL_SPATIAL_CLUSTER_CLICKS,
                    "row_indexes": suspect_indexes,
                    "kept_row_index": kept,
                    "reason": (
                        f"在同一窗口内出现 {len(run)} 次邻近点击（≤{SPATIAL_CLUSTER_MAX_DISTANCE_PX}px）。"
                        "疑似找位置/点错重试，最后一次可能才是真正生效的操作。"
                    ),
                }
            )
        run = []

    last_xy: tuple[float, float] | None = None
    last_window_key: tuple[str, str] | None = None
    for index, event in enumerate(events):
        if not _is_click_event(event):
            flush()
            last_xy = None
            last_window_key = None
            continue
        xy = _click_coords(event)
        window_key = _window_key(event)
        if xy is None:
            flush()
            last_xy = None
            last_window_key = None
            continue
        if last_xy is None or last_window_key != window_key:
            flush()
            run = [index]
            last_xy = xy
            last_window_key = window_key
            continue
        distance = math.hypot(xy[0] - last_xy[0], xy[1] - last_xy[1])
        if distance <= SPATIAL_CLUSTER_MAX_DISTANCE_PX:
            run.append(index)
        else:
            flush()
            run = [index]
        last_xy = xy
        last_window_key = window_key

    flush()
    return signals


def _find_backspace_runs(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Detect a run of consecutive ``input`` events that contains at least one
    Backspace token, indicating the user typed something, deleted it, and
    typed again."""

    signals: list[dict[str, object]] = []
    run: list[int] = []
    has_backspace = False
    has_non_backspace = False

    def flush() -> None:
        nonlocal run, has_backspace, has_non_backspace
        if (
            len(run) >= BACKSPACE_RUN_MIN_INPUT_EVENTS
            and has_backspace
            and has_non_backspace
        ):
            signals.append(
                {
                    "kind": SIGNAL_BACKSPACE_RUN,
                    "row_indexes": run[:],
                    "kept_row_index": run[-1],
                    "reason": (
                        "连续输入序列中出现退格删除并重新输入，疑似输错重输；"
                        "最终生效的输入应保留，中间过程可考虑剔除。"
                    ),
                }
            )
        run = []
        has_backspace = False
        has_non_backspace = False

    for index, event in enumerate(events):
        event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
        if event_type != "input":
            flush()
            continue
        is_backspace = _input_event_has_backspace(event)
        if is_backspace:
            has_backspace = True
        if _input_event_has_non_backspace_text(event):
            has_non_backspace = True
        run.append(index)
    flush()
    return signals


def _find_menu_open_close_pairs(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Detect a click that appears to open a menu / popup / dropdown which is
    then dismissed by Escape (or by another click in a different window)
    without selecting any menuitem.
    """

    signals: list[dict[str, object]] = []
    for index, event in enumerate(events[:-1]):
        if not _is_click_event(event):
            continue
        if not _looks_like_menu_opener(event):
            continue
        next_event = events[index + 1]
        if not _is_escape_press(next_event):
            continue
        signals.append(
            {
                "kind": SIGNAL_MENU_OPEN_CLOSE,
                "row_indexes": [index, index + 1],
                "kept_row_index": None,
                "reason": (
                    "打开菜单/下拉/弹出后立即按下 Esc 关闭，未选择任何项，"
                    "疑似探索性打开后放弃，对自动化脚本无贡献。"
                ),
            }
        )
    return signals


def _find_window_visit_then_leave(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Detect a transition into a different window followed shortly by a
    return to the original window, with no input or save action in between.
    Such excursions usually represent the user walking down a wrong path
    before backtracking to the correct one.
    """

    signals: list[dict[str, object]] = []
    n = len(events)
    for start in range(n - 1):
        prev_window = _window_key(events[start])
        if not prev_window:
            continue
        next_window = _window_key(events[start + 1]) if start + 1 < n else None
        if not next_window or next_window == prev_window:
            continue
        # Found A -> B at index ``start+1``; look for return to A within window.
        for end in range(start + 2, min(n, start + 2 + WINDOW_VISIT_MAX_LEN)):
            current_window = _window_key(events[end])
            if not current_window:
                continue
            if current_window == next_window:
                continue
            if current_window == prev_window:
                visited_indexes = list(range(start + 1, end))
                if not visited_indexes:
                    break
                if any(
                    normalize_event_type(events[i].get("event_type", ""), events[i].get("action", "")) == "input"
                    for i in visited_indexes
                ):
                    break
                signals.append(
                    {
                        "kind": SIGNAL_WINDOW_VISIT_LEAVE,
                        "row_indexes": visited_indexes,
                        "kept_row_index": None,
                        "reason": (
                            f"进入窗口后于 {len(visited_indexes)} 步内未做任何输入便返回原窗口，"
                            "疑似走错路径后折回，对自动化脚本无贡献。"
                        ),
                    }
                )
                break
            # Window changed yet again before returning — give up on this start.
            break
    return signals


# --- Helpers -----------------------------------------------------------------


def _is_click_event(event: dict[str, object]) -> bool:
    if not isinstance(event, dict):
        return False
    event_type = normalize_event_type(event.get("event_type", ""), event.get("action", ""))
    if event_type not in {"controlOperation", "Click"}:
        return False
    action_value = format_recorded_action(event.get("action", "")).strip().lower()
    # Ignore wheel scrolls and drags — they are not point-and-click attempts.
    if "scroll" in action_value or "drag" in action_value:
        return False
    return True


def _click_coords(event: dict[str, object]) -> tuple[float, float] | None:
    mouse = event.get("mouse")
    if not isinstance(mouse, dict):
        return None
    x = mouse.get("x")
    y = mouse.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return (float(x), float(y))


def _window_key(event: dict[str, object]) -> tuple[str, str] | None:
    window = event.get("window")
    if not isinstance(window, dict):
        return None
    handle = str(window.get("handle", "")).strip()
    title = str(window.get("title", "")).strip()
    if not handle and not title:
        return None
    return (handle, title)


def _looks_like_menu_opener(event: dict[str, object]) -> bool:
    ui_element = event.get("ui_element")
    if not isinstance(ui_element, dict):
        return False
    control_type = str(ui_element.get("control_type", "")).lower()
    return any(token in control_type for token in ("menu", "popup", "dropdown", "combobox", "split"))


def _is_escape_press(event: dict[str, object]) -> bool:
    if not isinstance(event, dict):
        return False
    if normalize_event_type(event.get("event_type", ""), event.get("action", "")) != "input":
        return False
    keyboard = event.get("keyboard")
    if not isinstance(keyboard, dict):
        return False
    key_name = normalize_keyboard_key_name(keyboard.get("key_name", "")).lower()
    if key_name == "escape" or key_name == "esc":
        return True
    sequence = keyboard.get("sequence")
    if isinstance(sequence, list):
        for token in sequence:
            if str(token).strip().lower() in {"[escape]", "[esc]"}:
                return True
    return False


def _input_event_has_backspace(event: dict[str, object]) -> bool:
    keyboard = event.get("keyboard")
    if not isinstance(keyboard, dict):
        return False
    key_name = normalize_keyboard_key_name(keyboard.get("key_name", "")).lower()
    if key_name == "backspace":
        return True
    sequence = keyboard.get("sequence")
    if isinstance(sequence, list):
        for token in sequence:
            if str(token).strip().lower() == "[backspace]":
                return True
    return False


def _input_event_has_non_backspace_text(event: dict[str, object]) -> bool:
    keyboard = event.get("keyboard")
    if not isinstance(keyboard, dict):
        return False
    text = keyboard.get("text")
    if isinstance(text, str) and text.strip():
        return True
    char = keyboard.get("char")
    if isinstance(char, str) and char.strip():
        return True
    sequence = keyboard.get("sequence")
    if isinstance(sequence, list):
        for token in sequence:
            token_str = str(token).strip()
            if not token_str:
                continue
            if token_str.lower() == "[backspace]":
                continue
            return True
    key_name = normalize_keyboard_key_name(keyboard.get("key_name", "")).lower()
    if key_name and key_name != "backspace":
        return True
    return False