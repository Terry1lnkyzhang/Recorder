from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from src.common.session_lock import build_session_lock_status_text, get_session_lock_path, inspect_session_lock, is_session_lock_stale
from src.common.session_summary import count_session_events, get_session_summary_path, read_session_summary


SessionCandidateCache = dict[str, dict[str, object]]


def scan_session_candidates(
    base_dir: Path,
    *,
    cache: SessionCandidateCache | None = None,
    force_refresh: bool = False,
    include_event_counts: bool = True,
    include_metadata_columns: bool = True,
    diagnostics: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    if diagnostics is not None:
        diagnostics.clear()
    if not base_dir.exists():
        if diagnostics is not None:
            diagnostics.update(
                {
                    "total_seconds": 0.0,
                    "candidate_count": 0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                    "include_event_counts": include_event_counts,
                    "include_metadata_columns": include_metadata_columns,
                    "group_seconds": {
                        "events": 0.0,
                        "lock_status": 0.0,
                        "metadata_columns": 0.0,
                        "name_modified": 0.0,
                        "sorting": 0.0,
                    },
                }
            )
        return []

    scan_started_at = time.perf_counter()
    candidates: list[dict[str, object]] = []
    resolved_cache = cache if cache is not None else {}
    stat_seconds = 0.0
    summary_seconds = 0.0
    event_count_seconds = 0.0
    metadata_seconds = 0.0
    lock_seconds = 0.0
    candidate_build_seconds = 0.0
    sort_seconds = 0.0
    cache_hits = 0
    cache_misses = 0

    for session_dir, session_json, events_log in iter_session_candidate_files(base_dir):
        try:
            stat_started_at = time.perf_counter()
            session_stat = _safe_stat(session_json)
            events_stat = _safe_stat(events_log)
            dir_stat = _safe_stat(session_dir)
            if dir_stat is None:
                continue
            summary_path = get_session_summary_path(session_dir)
            summary_stat = _safe_stat(summary_path)
            lock_stat = _safe_stat(get_session_lock_path(session_dir))
            stat_seconds += time.perf_counter() - stat_started_at
        except OSError:
            continue

        stamp: tuple[object, ...] | None = None
        cache_key = _build_cache_key(session_dir)
        stamp = (
            "full" if include_event_counts else "quick",
            dir_stat.st_mtime_ns,
            dir_stat.st_size,
            session_stat.st_mtime_ns if session_stat is not None else None,
            session_stat.st_size if session_stat is not None else None,
            events_stat.st_mtime_ns if events_stat is not None else None,
            events_stat.st_size if events_stat is not None else None,
            summary_stat.st_mtime_ns if summary_stat is not None else None,
            summary_stat.st_size if summary_stat is not None else None,
            lock_stat.st_mtime_ns if lock_stat is not None else None,
            lock_stat.st_size if lock_stat is not None else None,
        )

        cached = None if force_refresh else resolved_cache.get(cache_key)
        if cached and cached.get("stamp") == stamp:
            cache_hits += 1
            event_count = cached.get("events", "")
            testcase_id = str(cached.get("testcase_id", "") or "")
            project = str(cached.get("project", "") or "")
            recorder_person = str(cached.get("recorder_person", "") or "")
            converter_person = str(cached.get("converter_person", "") or "")
            review_status = str(cached.get("review_status", "") or "")
            review_comments = str(cached.get("review_comments", "") or "")
            lock_status = str(cached.get("lock_status", "") or "")
            lock_owner = str(cached.get("lock_owner", "") or "")
            lock_acquired_at = str(cached.get("lock_acquired_at", "") or "")
            is_locked = bool(cached.get("is_locked", False))
            is_lock_stale = bool(cached.get("is_lock_stale", False))
        else:
            cache_misses += 1
            summary_payload: dict[str, object] = {}
            if include_event_counts or include_metadata_columns:
                summary_started_at = time.perf_counter()
                summary_payload = read_session_summary(session_dir) or {}
                summary_seconds += time.perf_counter() - summary_started_at
            event_count = summary_payload.get("event_count") if isinstance(summary_payload.get("event_count"), int) else ""
            if include_event_counts and event_count == "":
                event_count_started_at = time.perf_counter()
                event_count = count_session_events(session_dir)
                event_count_seconds += time.perf_counter() - event_count_started_at

            if include_metadata_columns:
                metadata_started_at = time.perf_counter()
                metadata = _extract_session_candidate_metadata(base_dir, session_dir, session_json, summary_payload)
                metadata_seconds += time.perf_counter() - metadata_started_at
                testcase_id = metadata["testcase_id"]
                project = metadata["project"]
                recorder_person = metadata["recorder_person"]
                converter_person = metadata["converter_person"]
                review_status = str(summary_payload.get("review_status", "") or "")
                review_comments = str(summary_payload.get("review_comments", "") or "")
            else:
                testcase_id = ""
                project = ""
                recorder_person = ""
                converter_person = ""
                review_status = ""
                review_comments = ""
            lock_started_at = time.perf_counter()
            lock_info = inspect_session_lock(session_dir, auto_clear_stale=True)
            is_locked = lock_info is not None
            is_lock_stale = is_session_lock_stale(lock_info)
            lock_status = build_session_lock_status_text(lock_info)
            lock_owner = ""
            lock_acquired_at = ""
            if lock_info is not None:
                owner_parts = []
                if lock_info.username:
                    owner_parts.append(lock_info.username)
                if lock_info.hostname:
                    owner_parts.append(f"@{lock_info.hostname}")
                lock_owner = "".join(owner_parts)
                lock_acquired_at = lock_info.acquired_at
            lock_seconds += time.perf_counter() - lock_started_at
            resolved_cache[cache_key] = {
                "stamp": stamp,
                "events": event_count,
                "testcase_id": testcase_id,
                "project": project,
                "recorder_person": recorder_person,
                "converter_person": converter_person,
                "review_status": review_status,
                "review_comments": review_comments,
                "lock_status": lock_status,
                "lock_owner": lock_owner,
                "lock_acquired_at": lock_acquired_at,
                "is_locked": is_locked,
                "is_lock_stale": is_lock_stale,
            }

        candidate_started_at = time.perf_counter()
        candidates.append(
            {
                "name": format_session_candidate_name(base_dir, session_dir),
                "modified": datetime.fromtimestamp(dir_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "modified_ts": dir_stat.st_mtime,
                "events": event_count,
                "testcase_id": testcase_id,
                "project": project,
                "recorder_person": recorder_person,
                "converter_person": converter_person,
                "review_status": review_status,
                "review_comments": review_comments,
                "lock_status": lock_status,
                "lock_owner": lock_owner,
                "lock_acquired_at": lock_acquired_at,
                "is_locked": is_locked,
                "is_lock_stale": is_lock_stale,
                "path": str(session_dir),
            }
        )
        candidate_build_seconds += time.perf_counter() - candidate_started_at

    sort_started_at = time.perf_counter()
    candidates.sort(key=lambda item: float(item.get("modified_ts", 0.0) or 0.0), reverse=True)
    for item in candidates:
        item.pop("modified_ts", None)
    sort_seconds += time.perf_counter() - sort_started_at

    if diagnostics is not None:
        diagnostics.update(
            {
                "total_seconds": time.perf_counter() - scan_started_at,
                "candidate_count": len(candidates),
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
                "include_event_counts": include_event_counts,
                "include_metadata_columns": include_metadata_columns,
                "group_seconds": {
                    "events": event_count_seconds + (summary_seconds if not include_metadata_columns else 0.0),
                    "lock_status": lock_seconds,
                    "metadata_columns": (summary_seconds + metadata_seconds) if include_metadata_columns else 0.0,
                    "name_modified": stat_seconds + candidate_build_seconds,
                    "sorting": sort_seconds,
                },
                "detail_seconds": {
                    "stat": stat_seconds,
                    "summary": summary_seconds,
                    "event_count": event_count_seconds,
                    "metadata": metadata_seconds,
                    "lock": lock_seconds,
                    "candidate_build": candidate_build_seconds,
                    "sort": sort_seconds,
                },
            }
        )
    return candidates


def find_latest_session_dir(base_dir: Path) -> Path | None:
    latest_session: Path | None = None
    latest_mtime = float("-inf")
    for session_dir, _session_json, _events_log in iter_session_candidate_files(base_dir):
        try:
            modified = session_dir.stat().st_mtime
        except OSError:
            continue
        if modified > latest_mtime:
            latest_session = session_dir
            latest_mtime = modified
    return latest_session


def iter_session_candidate_files(base_dir: Path) -> Iterator[tuple[Path, Path, Path]]:
    for testcase_dir in _list_subdirectories(base_dir):
        for child_dir in _list_subdirectories(testcase_dir):
            is_session_dir, session_dirs = _classify_session_parent_dir(child_dir)
            if is_session_dir:
                yield child_dir, child_dir / "session.json", child_dir / "events.jsonl"
                continue

            for session_dir in session_dirs:
                if _contains_session_payload_files(session_dir):
                    yield session_dir, session_dir / "session.json", session_dir / "events.jsonl"


def format_session_candidate_name(base_dir: Path, session_dir: Path) -> str:
    try:
        return session_dir.relative_to(base_dir).as_posix()
    except ValueError:
        return session_dir.name


def _extract_session_candidate_metadata(
    base_dir: Path,
    session_dir: Path,
    session_json: Path,
    summary_payload: dict[str, object] | None,
) -> dict[str, str]:
    relative_parts = _resolve_candidate_relative_parts(base_dir, session_dir)

    testcase_id = str(summary_payload.get("testcase_id", "") or "").strip() if isinstance(summary_payload, dict) else ""
    if not testcase_id and relative_parts:
        testcase_id = relative_parts[0]

    project = str(summary_payload.get("project", "") or "").strip() if isinstance(summary_payload, dict) else ""
    if not project and len(relative_parts) >= 3:
        project = relative_parts[1]

    recorder_person = str(summary_payload.get("recorder_person", "") or "").strip() if isinstance(summary_payload, dict) else ""
    if not recorder_person:
        recorder_person = _try_extract_metadata_field_from_session_json(session_json, "recorder_person")

    converter_person = str(summary_payload.get("converter_person", "") or "").strip() if isinstance(summary_payload, dict) else ""
    if not converter_person:
        converter_person = _try_extract_metadata_field_from_session_json(session_json, "converter_person")

    return {
        "testcase_id": testcase_id,
        "project": project,
        "recorder_person": recorder_person,
        "converter_person": converter_person,
    }


def load_session_candidate_metadata(base_dir: Path, session_dir: Path) -> dict[str, str]:
    session_json = session_dir / "session.json"
    summary_payload = read_session_summary(session_dir) or {}
    metadata = _extract_session_candidate_metadata(base_dir, session_dir, session_json, summary_payload)
    return {
        **metadata,
        "review_status": str(summary_payload.get("review_status", "") or ""),
        "review_comments": str(summary_payload.get("review_comments", "") or ""),
    }


def _resolve_candidate_relative_parts(base_dir: Path, session_dir: Path) -> tuple[str, ...]:
    try:
        return session_dir.relative_to(base_dir).parts
    except ValueError:
        return ()


def _try_extract_metadata_field_from_session_json(session_json: Path, field_name: str) -> str:
    if not session_json.exists():
        return ""
    try:
        with session_json.open("r", encoding="utf-8") as handle:
            header_text = handle.read(32768)
    except Exception:
        return ""

    match = re.search(rf'"{re.escape(field_name)}"\s*:\s*"((?:\\.|[^"\\])*)"', header_text)
    if not match:
        return ""
    try:
        return str(json.loads(f'"{match.group(1)}"') or "").strip()
    except Exception:
        return ""


def _build_cache_key(session_dir: Path) -> str:
    return os.path.abspath(os.fspath(session_dir)).lower()


def _safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _list_subdirectories(directory: Path) -> list[Path]:
    subdirectories: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirectories.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return []
    return subdirectories


def _classify_session_parent_dir(directory: Path) -> tuple[bool, list[Path]]:
    session_subdirectories: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if name == "session.json" or name == "events.jsonl":
                    return True, []
                try:
                    if entry.is_dir(follow_symlinks=False):
                        session_subdirectories.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return False, []
    return False, session_subdirectories


def _contains_session_payload_files(directory: Path) -> bool:
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == "session.json" or entry.name == "events.jsonl":
                    return True
    except OSError:
        return False
    return False
