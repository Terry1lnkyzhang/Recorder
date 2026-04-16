from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import yaml

from .models import RecordedEvent, RecordingSessionData, format_recorded_action


def _event_signature(event: RecordedEvent) -> str:
    if event.event_type == "mouse_click":
        control = event.ui_element.control_type or "unknown_control"
        window = event.window.title or "unknown_window"
        return f"click::{window}::{control}::{format_recorded_action(event.action)}"
    if event.event_type == "mouse_drag":
        control = event.ui_element.control_type or "unknown_control"
        window = event.window.title or "unknown_window"
        return f"drag::{window}::{control}::{format_recorded_action(event.action)}"
    if event.event_type == "key_press":
        key_name = event.keyboard.get("key_name", "unknown_key")
        return f"key::{key_name}"
    if event.event_type == "scroll":
        dy = event.scroll.get("dy") if isinstance(event.scroll, dict) else None
        direction = "down" if isinstance(dy, int) and dy < 0 else "up"
        return f"scroll::{direction}"
    return f"{event.event_type}::{format_recorded_action(event.action)}"


def build_reuse_suggestions(session: RecordingSessionData) -> dict[str, object]:
    signatures = [_event_signature(event) for event in session.events]
    counts = Counter(signatures)

    repeated_actions = []
    for signature, count in counts.items():
        if count >= 3:
            repeated_actions.append({
                "signature": signature,
                "count": count,
                "suggestion": "该动作出现次数较多，建议评估是否封装为公共步骤。",
            })

    repeated_sequences = []
    max_window = min(5, max(2, len(signatures) // 2))
    for window_size in range(2, max_window + 1):
        buckets: defaultdict[tuple[str, ...], list[int]] = defaultdict(list)
        for index in range(len(signatures) - window_size + 1):
            chunk = tuple(signatures[index : index + window_size])
            buckets[chunk].append(index)

        for chunk, positions in buckets.items():
            if len(positions) >= 2:
                repeated_sequences.append({
                    "sequence": list(chunk),
                    "occurrences": len(positions),
                    "positions": positions,
                    "suggestion": "检测到重复步骤片段，后续转换 YAML 时可提取为可复用流程。",
                })

    repeated_sequences.sort(key=lambda item: (item["occurrences"], len(item["sequence"])), reverse=True)
    repeated_actions.sort(key=lambda item: item["count"], reverse=True)

    return {
        "session_id": session.session_id,
        "repeated_actions": repeated_actions,
        "repeated_sequences": repeated_sequences[:20],
        "next_step": "后续可把规则分析替换为 LLM 语义分析，并映射到你的自动化 YAML 模板。",
    }


def write_suggestions(output_dir: Path, suggestions: dict[str, object]) -> Path:
    output_path = output_dir / "suggestions.yaml"
    output_path.write_text(
        yaml.safe_dump(suggestions, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return output_path