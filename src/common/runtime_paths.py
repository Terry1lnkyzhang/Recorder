from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "Recorder"
NETWORK_RECORDINGS_ROOT = Path(r"\\130.147.129.203\AutomaticShared\Recordings")


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
    path = NETWORK_RECORDINGS_ROOT
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_logs_dir(app_name: str = APP_NAME) -> Path:
    path = get_user_data_root(app_name) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_settings_path(app_name: str = APP_NAME) -> Path:
    return get_user_data_root(app_name) / "recorder_settings.json"