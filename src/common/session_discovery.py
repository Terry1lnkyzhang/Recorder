from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator


SessionCandidateCache = dict[str, dict[str, object]]


def scan_session_candidates(
    base_dir: Path,
    *,
    cache: SessionCandidateCache | None = None,
    force_refresh: bool = False,
) -> list[dict[str, object]]:
    if not base_dir.exists():
        return []

    candidates: list[dict[str, object]] = []
    resolved_cache = cache if cache is not None else {}

    for session_dir, session_json, events_log in iter_session_candidate_files(base_dir):
        try:
            session_stat = session_json.stat() if session_json.exists() else None
            events_stat = events_log.stat() if events_log.exists() else None
            dir_stat = session_dir.stat()
        except OSError:
            continue

        stamp: tuple[object, ...] | None = None
        cache_key = str(session_dir.resolve())
        if session_stat is not None:
            stamp = ("session", session_stat.st_mtime_ns, session_stat.st_size)
        elif events_stat is not None:
            stamp = ("events", events_stat.st_mtime_ns, events_stat.st_size)

        cached = None if force_refresh else resolved_cache.get(cache_key)
        if cached and cached.get("stamp") == stamp:
            event_count = cached.get("events", "")
        else:
            event_count: str | int = ""
            try:
                if session_stat is not None:
                    payload = json.loads(session_json.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        events = payload.get("events", [])
                        if isinstance(events, list):
                            event_count = len(events)
                elif events_stat is not None:
                    with events_log.open("r", encoding="utf-8") as handle:
                        event_count = sum(1 for line in handle if line.strip())
            except Exception:
                event_count = "?"
            resolved_cache[cache_key] = {
                "stamp": stamp,
                "events": event_count,
            }

        candidates.append(
            {
                "name": format_session_candidate_name(base_dir, session_dir),
                "modified": datetime.fromtimestamp(dir_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "modified_ts": dir_stat.st_mtime,
                "events": event_count,
                "path": str(session_dir),
            }
        )

    candidates.sort(key=lambda item: float(item.get("modified_ts", 0.0) or 0.0), reverse=True)
    for item in candidates:
        item.pop("modified_ts", None)
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
    try:
        testcase_dirs = [item for item in base_dir.iterdir() if item.is_dir()]
    except Exception:
        return

    for testcase_dir in testcase_dirs:
        try:
            child_dirs = [item for item in testcase_dir.iterdir() if item.is_dir()]
        except Exception:
            continue

        for child_dir in child_dirs:
            session_json = child_dir / "session.json"
            events_log = child_dir / "events.jsonl"
            if session_json.exists() or events_log.exists():
                yield child_dir, session_json, events_log
                continue

            try:
                session_dirs = [item for item in child_dir.iterdir() if item.is_dir()]
            except Exception:
                continue
            for session_dir in session_dirs:
                session_json = session_dir / "session.json"
                events_log = session_dir / "events.jsonl"
                if session_json.exists() or events_log.exists():
                    yield session_dir, session_json, events_log


def format_session_candidate_name(base_dir: Path, session_dir: Path) -> str:
    try:
        return session_dir.relative_to(base_dir).as_posix()
    except ValueError:
        return session_dir.name
