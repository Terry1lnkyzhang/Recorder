from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from src.database.config import DB_URL


@dataclass(slots=True)
class Settings:
    endpoint: str = "http://130.147.129.154:8001/v1/chat/completions"
    api_key: str = ""
    model: str = "Qwen/Qwen3-VL-8B-Instruct-FP8"
    timeout_seconds: int = 90
    temperature: float = 0.0
    enable_thinking: bool = False
    default_system_prompt: str = (
        "你是自动化测试分析助手。结合截图、视频抽帧和用户说明，"
        "判断当前界面状态、可自动化定位线索、风险点，以及建议的断言或复用步骤。"
    )
    extra_headers_json: str = "{}"
    video_frame_count: int = 4
    video_fps: int = 5
    send_video_directly: bool = True
    analysis_batch_size: int = 1
    send_fullscreen_screenshots: bool = False
    ai_observation_excluded_process_names: str = "explorer\nmsedge\nnotepad\nwordpad\nwrite"
    analysis_system_prompt: str = (
        "你是桌面自动化操作分析助手。"
    )
    exclude_recorder_process_windows: bool = True
    excluded_process_names: str = ""
    excluded_window_keywords: str = ""
    prompt_db_connection_string: str = DB_URL
    checkpoint_prompt_table: str = "agentprompt"
    checkpoint_prompt_key_column: str = ""
    checkpoint_prompt_label_column: str = ""
    checkpoint_prompt_content_column: str = "PromptContent"
    use_remote_ai_service: bool = False
    remote_ai_service_url: str = "http://127.0.0.1:8010"
    remote_ai_service_api_key: str = ""
    remote_ai_service_timeout_seconds: int = 180
    show_design_steps_overlay: bool = True
    design_steps_overlay_width: int = 520
    design_steps_overlay_height: int = 220
    design_steps_overlay_bg_color: str = "#d7caa3"
    design_steps_overlay_opacity: float = 0.88


AISettings = Settings


class SettingsStore:
    def __init__(self, settings_path: Path) -> None:
        self.settings_path = settings_path

    def load(self) -> Settings:
        if not self.settings_path.exists():
            return Settings()

        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            return Settings()

        defaults = asdict(Settings())
        defaults.update(payload)
        if not str(defaults.get("prompt_db_connection_string", "")).strip():
            defaults["prompt_db_connection_string"] = DB_URL
        return Settings(**defaults)

    def save(self, settings: Settings) -> None:
        self.settings_path.write_text(
            json.dumps(asdict(settings), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def parse_extra_headers(raw_text: str) -> dict[str, str]:
        payload = json.loads(raw_text or "{}")
        if not isinstance(payload, dict):
            raise ValueError("extra headers 必须是 JSON object")
        return {str(key): str(value) for key, value in payload.items()}

    @staticmethod
    def parse_pattern_list(raw_text: str) -> list[str]:
        normalized = (raw_text or "").replace(";", "\n").replace(",", "\n")
        return [item.strip() for item in normalized.splitlines() if item.strip()]