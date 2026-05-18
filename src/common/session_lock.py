from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import socket
import threading
import uuid


SESSION_LOCK_FILE_NAME = ".recorder_session.lock"
_LOCAL_LOCKS: dict[str, _SharedSessionLockState] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


@dataclass(slots=True)
class SessionLockInfo:
    token: str
    owner_kind: str
    owner_label: str
    username: str
    hostname: str
    pid: int
    acquired_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> SessionLockInfo | None:
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                token=str(payload.get("token", "") or ""),
                owner_kind=str(payload.get("owner_kind", "") or ""),
                owner_label=str(payload.get("owner_label", "") or ""),
                username=str(payload.get("username", "") or ""),
                hostname=str(payload.get("hostname", "") or ""),
                pid=int(payload.get("pid", 0) or 0),
                acquired_at=str(payload.get("acquired_at", "") or ""),
            )
        except Exception:
            return None


class SessionLockError(RuntimeError):
    def __init__(self, session_dir: Path, lock_path: Path, lock_info: SessionLockInfo | None = None) -> None:
        self.session_dir = session_dir
        self.lock_path = lock_path
        self.lock_info = lock_info
        super().__init__(build_session_lock_message(session_dir, lock_info, lock_path))


class _SharedSessionLockState:
    def __init__(self, session_dir: Path, lock_path: Path, lock_info: SessionLockInfo) -> None:
        self.session_dir = session_dir
        self.session_key = _session_dir_key(session_dir)
        self.lock_path = lock_path
        self.lock_info = lock_info
        self.ref_count = 0


class SessionLockHandle:
    def __init__(self, shared_state: _SharedSessionLockState) -> None:
        self._shared_state = shared_state
        self.session_dir = shared_state.session_dir
        self.lock_path = shared_state.lock_path
        self.lock_info = shared_state.lock_info
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _LOCAL_LOCKS_GUARD:
            state = _LOCAL_LOCKS.get(self._shared_state.session_key)
            if state is None:
                return
            state.ref_count = max(0, state.ref_count - 1)
            if state.ref_count > 0:
                return
            _LOCAL_LOCKS.pop(self._shared_state.session_key, None)
        try:
            current_info = read_session_lock_info(self.lock_path)
            if current_info is None or current_info.token != self.lock_info.token:
                return
            try:
                os.remove(_lock_path_str(self.session_dir))
            except FileNotFoundError:
                pass
        except Exception:
            return


def acquire_session_lock(session_dir: Path, owner_kind: str, owner_label: str) -> SessionLockHandle:
    resolved_dir = _normalize_session_dir(session_dir)
    session_key = _session_dir_key(resolved_dir)
    lock_path = resolved_dir / SESSION_LOCK_FILE_NAME
    with _LOCAL_LOCKS_GUARD:
        shared_state = _LOCAL_LOCKS.get(session_key)
        if shared_state is not None:
            shared_state.ref_count += 1
            return SessionLockHandle(shared_state)

    existing_lock_info = read_session_lock_info(lock_path)
    if existing_lock_info is not None and is_session_lock_stale(existing_lock_info):
        force_release_session_lock(resolved_dir)
        existing_lock_info = None

    lock_info = SessionLockInfo(
        token=uuid.uuid4().hex,
        owner_kind=str(owner_kind or "").strip(),
        owner_label=str(owner_label or "").strip(),
        username=_safe_get_username(),
        hostname=socket.gethostname().strip(),
        pid=os.getpid(),
        acquired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(lock_info.to_dict(), ensure_ascii=False, indent=2))
    except FileExistsError as exc:
        raise SessionLockError(resolved_dir, lock_path, inspect_session_lock(resolved_dir, auto_clear_stale=True)) from exc
    shared_state = _SharedSessionLockState(resolved_dir, lock_path, lock_info)
    shared_state.ref_count = 1
    with _LOCAL_LOCKS_GUARD:
        _LOCAL_LOCKS[session_key] = shared_state
    return SessionLockHandle(shared_state)


def get_session_lock_path(session_dir: Path) -> Path:
    return _normalize_session_dir(session_dir) / SESSION_LOCK_FILE_NAME


def inspect_session_lock(session_dir: Path, *, auto_clear_stale: bool = True) -> SessionLockInfo | None:
    lock_path = get_session_lock_path(session_dir)
    lock_info = read_session_lock_info(lock_path)
    if lock_info is None:
        return None
    if auto_clear_stale and is_session_lock_stale(lock_info):
        force_release_session_lock(session_dir)
        return None
    return lock_info


def force_release_session_lock(session_dir: Path) -> bool:
    resolved_dir = _normalize_session_dir(session_dir)
    with _LOCAL_LOCKS_GUARD:
        _LOCAL_LOCKS.pop(_session_dir_key(resolved_dir), None)
    try:
        try:
            os.remove(_lock_path_str(resolved_dir))
        except FileNotFoundError:
            pass
        return True
    except Exception:
        return False


def is_session_lock_stale(lock_info: SessionLockInfo | None) -> bool:
    if lock_info is None:
        return False
    if not _is_same_host(lock_info.hostname):
        return False
    if lock_info.pid <= 0:
        return False
    return not _is_process_alive(lock_info.pid)


def read_session_lock_info(lock_path: Path) -> SessionLockInfo | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return SessionLockInfo.from_dict(payload)


def build_session_lock_message(session_dir: Path, lock_info: SessionLockInfo | None, lock_path: Path | None = None) -> str:
    if lock_info is None:
        lock_suffix = f"\n锁文件: {lock_path}" if lock_path is not None else ""
        return f"当前 Session 正在被其他实例使用，暂时不能打开：\n{session_dir}{lock_suffix}"

    owner_label = lock_info.owner_label or lock_info.owner_kind or "其他实例"
    owner_parts = []
    if lock_info.username:
        owner_parts.append(lock_info.username)
    if lock_info.hostname:
        owner_parts.append(f"@{lock_info.hostname}")
    owner_text = "".join(owner_parts) if owner_parts else "(未知用户)"
    acquired_at = lock_info.acquired_at or "(未知时间)"
    return (
        f"当前 Session 已打开，暂时不能重复操作。\n\n"
        f"Session: {session_dir}\n"
        f"占用方: {owner_label}\n"
        f"用户: {owner_text}\n"
        f"PID: {lock_info.pid or '(未知)'}\n"
        f"开始时间(UTC): {acquired_at}"
    )


def build_session_lock_status_text(lock_info: SessionLockInfo | None) -> str:
    if lock_info is None:
        return "空闲"
    owner_label = lock_info.owner_label or lock_info.owner_kind or "占用中"
    if is_session_lock_stale(lock_info):
        return f"陈旧锁: {owner_label}"
    return f"占用中: {owner_label}"


def _safe_get_username() -> str:
    try:
        return getpass.getuser().strip()
    except Exception:
        return ""


def _normalize_session_dir(session_dir: Path) -> Path:
    return Path(os.path.abspath(os.fspath(session_dir)))


def _lock_path_str(session_dir: Path) -> str:
    return os.path.join(os.path.abspath(os.fspath(session_dir)), SESSION_LOCK_FILE_NAME)


def _session_dir_key(session_dir: Path) -> str:
    return os.path.abspath(os.fspath(session_dir)).lower()


def _is_same_host(hostname: str) -> bool:
    normalized_host = str(hostname or "").strip().lower()
    if not normalized_host:
        return False
    current_host = socket.gethostname().strip().lower()
    if not current_host:
        return False
    current_short_host = current_host.split(".", 1)[0]
    local_names = {current_host, current_short_host}
    if normalized_host in local_names:
        return True
    return any(normalized_host.startswith(f"{local_name}.") for local_name in local_names if local_name)


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True