from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REMOTE_SERVICE_API_VERSION = "2026-04-remote-v1"


@dataclass(slots=True)
class RemoteServiceBundle:
    root_dir_name: str
    bundle_name: str
    zip_bytes: bytes


def build_bundle_member_name(root_dir_name: str, relative_path: str) -> str:
    clean_root = Path(root_dir_name).name or "session"
    normalized = relative_path.replace("\\", "/").lstrip("/")
    return f"{clean_root}/{normalized}"
