from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


SESSION_SUMMARY_FILE_NAME = ".recorder_session.summary.json"
SESSION_REVIEW_STATUS_EMPTY = ""
SESSION_REVIEW_STATUS_CHECKPOINT_COMPLETE = "checkpoint_completed"
SESSION_REVIEW_STATUS_DEBUG_COMPLETE = "debug_completed"


def get_session_summary_path(session_dir: Path) -> Path:
    return Path(session_dir) / SESSION_SUMMARY_FILE_NAME


def normalize_session_review_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "": SESSION_REVIEW_STATUS_EMPTY,
        "empty": SESSION_REVIEW_STATUS_EMPTY,
        "none": SESSION_REVIEW_STATUS_EMPTY,
        "空": SESSION_REVIEW_STATUS_EMPTY,
        "checkpoint_completed": SESSION_REVIEW_STATUS_CHECKPOINT_COMPLETE,
        "checkpoint complete": SESSION_REVIEW_STATUS_CHECKPOINT_COMPLETE,
        "checkpoint": SESSION_REVIEW_STATUS_CHECKPOINT_COMPLETE,
        "检查点完成": SESSION_REVIEW_STATUS_CHECKPOINT_COMPLETE,
        "debug_completed": SESSION_REVIEW_STATUS_DEBUG_COMPLETE,
        "debug complete": SESSION_REVIEW_STATUS_DEBUG_COMPLETE,
        "debug": SESSION_REVIEW_STATUS_DEBUG_COMPLETE,
        "调试完成": SESSION_REVIEW_STATUS_DEBUG_COMPLETE,
    }
    return mapping.get(normalized, SESSION_REVIEW_STATUS_EMPTY)


def read_session_summary(session_dir: Path) -> dict[str, Any] | None:
    summary_path = get_session_summary_path(session_dir)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    event_count = payload.get("event_count")
    try:
        normalized_event_count = int(event_count) if event_count is not None else None
    except Exception:
        normalized_event_count = None
    return {
        "schema_version": 1,
        "testcase_id": str(payload.get("testcase_id", "") or "").strip(),
        "project": str(payload.get("project", "") or "").strip(),
        "recorder_person": str(payload.get("recorder_person", "") or "").strip(),
        "converter_person": str(payload.get("converter_person", "") or "").strip(),
        "review_status": normalize_session_review_status(payload.get("review_status", "")),
        "review_comments": str(payload.get("review_comments", "") or "").strip(),
        "event_count": normalized_event_count,
    }


def write_session_summary(
    session_dir: Path,
    *,
    metadata_payload: dict[str, Any] | None = None,
    event_count: int | None = None,
) -> dict[str, Any]:
    existing = read_session_summary(session_dir) or {}
    metadata = metadata_payload if isinstance(metadata_payload, dict) else {}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "testcase_id": str(metadata.get("testcase_id", existing.get("testcase_id", "")) or "").strip(),
        "project": str(metadata.get("project", existing.get("project", "")) or "").strip(),
        "recorder_person": str(metadata.get("recorder_person", existing.get("recorder_person", "")) or "").strip(),
        "converter_person": str(metadata.get("converter_person", existing.get("converter_person", "")) or "").strip(),
        "review_status": normalize_session_review_status(metadata.get("review_status", existing.get("review_status", ""))),
        "review_comments": str(metadata.get("review_comments", existing.get("review_comments", "")) or "").strip(),
    }
    resolved_event_count = event_count if isinstance(event_count, int) else existing.get("event_count")
    if isinstance(resolved_event_count, int):
        payload["event_count"] = resolved_event_count
    summary_path = get_session_summary_path(session_dir)
    summary_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def write_session_summary_from_session_payload(session_dir: Path, session_payload: dict[str, Any], event_count: int | None = None) -> dict[str, Any]:
    metadata = session_payload.get("metadata") if isinstance(session_payload.get("metadata"), dict) else {}
    resolved_event_count = event_count
    if resolved_event_count is None:
        events = session_payload.get("events", [])
        if isinstance(events, list):
            resolved_event_count = len(events)
    return write_session_summary(session_dir, metadata_payload=metadata, event_count=resolved_event_count)


def update_session_summary_event_count(session_dir: Path, event_count: int) -> dict[str, Any]:
    return write_session_summary(session_dir, event_count=event_count)


def count_session_events(session_dir: Path) -> int | str:
    events_log_path = Path(session_dir) / "events.jsonl"
    session_path = Path(session_dir) / "session.json"
    try:
        if events_log_path.exists():
            with events_log_path.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        if session_path.exists():
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                events = payload.get("events", [])
                if isinstance(events, list):
                    return len(events)
    except Exception:
        return "?"
    return ""


def update_session_review_fields(
    session_dir: Path,
    *,
    review_status: Any,
    review_comments: Any,
    converter_person: Any | None = None,
) -> dict[str, Any]:
    session_path = Path(session_dir) / "session.json"
    yaml_path = Path(session_dir) / "session.yaml"
    session_payload: dict[str, Any] = {}

    if session_path.exists():
        try:
            loaded = json.loads(session_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                session_payload = loaded
        except Exception:
            session_payload = {}

    metadata_payload = session_payload.get("metadata") if isinstance(session_payload.get("metadata"), dict) else {}
    metadata_payload = dict(metadata_payload)
    metadata_payload["review_status"] = normalize_session_review_status(review_status)
    metadata_payload["review_comments"] = str(review_comments or "").strip()
    if converter_person is not None:
        metadata_payload["converter_person"] = str(converter_person or "").strip()

    if session_payload:
        session_payload["metadata"] = metadata_payload
        session_path.write_text(json.dumps(session_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        yaml_path.write_text(yaml.safe_dump(session_payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        write_session_summary_from_session_payload(session_dir, session_payload)
    else:
        write_session_summary(session_dir, metadata_payload=metadata_payload)

    return metadata_payload