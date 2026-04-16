from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.common.media_utils import file_md5


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
    previous_hash: str | None = None

    for index, event in enumerate(events):
        screenshot_path = _resolve_primary_image_path(session_dir, event)
        current_hash = file_md5(screenshot_path) if screenshot_path else None
        event_type = str(event.get("event_type", ""))

        if event_type == "mouse_click" and current_hash and previous_hash and current_hash == previous_hash:
            suggestions.append(
                CleaningSuggestion(
                    kind="drop_noop",
                    row_indexes=[index],
                    replacement_event=None,
                    reason="与上一条可视状态相同，疑似点击无响应或无效点击。",
                )
            )

        if current_hash:
            previous_hash = current_hash

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
        first_event["event_type"] = "type_input"
        first_event["action"] = "typeinput"
        first_event["keyboard"] = {
            "text": merged_text,
            "sequence": run_sequence_tokens[:],
        }
        first_event["note"] = f"Merged key press input: {readable_sequence}"
        additional = dict(first_event.get("additional_details", {}))
        additional["merged_from_keypress_count"] = len(run_indexes)
        additional["merged_key_sequence"] = run_sequence_tokens[:]
        first_event["additional_details"] = additional

        suggestions.append(
            CleaningSuggestion(
                kind="merge_keypress",
                row_indexes=run_indexes[:],
                replacement_event=first_event,
                reason=f"连续 {len(run_indexes)} 个 key_press 可合并为一次输入: {readable_sequence}",
            )
        )
        run_indexes = []
        run_sequence_tokens = []
        run_text_buffer = []

    for index, event in enumerate(events):
        if str(event.get("event_type", "")) != "key_press":
            flush()
            continue

        token = _normalize_keypress_token(event)
        if token is None:
            flush()
            continue

        run_indexes.append(index)
        run_sequence_tokens.append(token["sequence_token"])
        if token["op"] == "insert":
            run_text_buffer.append(str(token["text"]))
        elif token["op"] == "backspace":
            if run_text_buffer:
                run_text_buffer.pop()

    flush()
    return suggestions


def _normalize_keypress_token(event: dict[str, object]) -> dict[str, object] | None:
    keyboard = event.get("keyboard", {})
    if not isinstance(keyboard, dict):
        return None

    char = keyboard.get("char")
    if isinstance(char, str) and len(char) == 1 and char.isprintable():
        return {"op": "insert", "text": char, "sequence_token": _format_sequence_char(char)}

    key_name = str(keyboard.get("key_name", ""))
    normalized = key_name.split(".", 1)[1].lower() if key_name.startswith("Key.") else key_name.lower()
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
    previous_hash: str | None = None

    for index, event in enumerate(events):
        screenshot_path = _resolve_primary_image_path(session_dir, event)
        current_hash = file_md5(screenshot_path) if screenshot_path else None
        event_type = str(event.get("event_type", ""))

        if event_type == "scroll" and current_hash and previous_hash and current_hash == previous_hash:
            suggestions.append(
                CleaningSuggestion(
                    kind="drop_noop_scroll",
                    row_indexes=[index],
                    replacement_event=None,
                    reason="滚动后界面无变化，疑似无效滚动或滚动未生效。",
                )
            )

        if current_hash:
            previous_hash = current_hash

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

        current_type = str(events[index].get("event_type", ""))
        next_type = str(events[index + 1].get("event_type", ""))
        if current_type not in {"mouse_click", "scroll"} or next_type not in {"mouse_click", "scroll"}:
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