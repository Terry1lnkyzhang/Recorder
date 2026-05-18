from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "Recorder"
NETWORK_RECORDINGS_ROOT = Path(r"\\130.147.129.203\AutomaticShared\Recordings")
LOCAL_RECORDINGS_ROOT_NAME = "recording"
LOCAL_RECORDING_STAGING_ROOT_NAME = "recording_staging"


@dataclass(frozen=True)
class RecordingsDirResolution:
    path: Path
    using_network_share: bool
    fallback_reason: str | None = None


def get_resource_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def get_user_data_root(app_name: str = APP_NAME) -> Path:
    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            root = Path(local_app_data) / app_name
        else:
            root = Path.home() / "AppData" / "Local" / app_name
    else:
        root = get_resource_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_recordings_dir(app_name: str = APP_NAME) -> Path:
    return resolve_recordings_dir(app_name).path


def resolve_recordings_dir(app_name: str = APP_NAME) -> RecordingsDirResolution:
    try:
        NETWORK_RECORDINGS_ROOT.mkdir(parents=True, exist_ok=True)
        return RecordingsDirResolution(path=NETWORK_RECORDINGS_ROOT, using_network_share=True)
    except Exception as exc:
        fallback = get_local_recordings_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        return RecordingsDirResolution(path=fallback, using_network_share=False, fallback_reason=str(exc))


def get_local_recordings_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / LOCAL_RECORDINGS_ROOT_NAME
    return get_resource_root() / LOCAL_RECORDINGS_ROOT_NAME


def get_local_recording_staging_dir(app_name: str = APP_NAME) -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / app_name
    else:
        root = Path.home() / "AppData" / "Local" / app_name
    path = root / LOCAL_RECORDING_STAGING_ROOT_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_logs_dir(app_name: str = APP_NAME) -> Path:
    path = get_user_data_root(app_name) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_settings_path(app_name: str = APP_NAME) -> Path:
    return get_user_data_root(app_name) / "recorder_settings.json"