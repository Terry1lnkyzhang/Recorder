from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageGrab

from src.common.display_utils import get_display_layout_snapshot

from .models import RecordedEvent, RecordingSessionData, SessionMetadata
from .system_info import build_environment_snapshot, safe_relpath, utc_now_iso


class SessionStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir: Path | None = None
        self.screenshots_dir: Path | None = None
        self.media_dir: Path | None = None
        self.events_log_path: Path | None = None
        self.data: RecordingSessionData | None = None
        self._event_counter = 0
        self._event_count = 0
        self._screenshot_counter = 0
        self._lock = threading.Lock()
        self._in_memory_event_limit = 1500

    def resume(self, session_dir: Path) -> RecordingSessionData:
        session_dir = session_dir.resolve()
        session_path = session_dir / "session.json"
        events_log_path = session_dir / "events.jsonl"
        screenshots_dir = session_dir / "screenshots"
        media_dir = session_dir / "media"

        if not session_path.exists() and not events_log_path.exists():
            raise RuntimeError(f"未找到可恢复的录制内容: {session_dir}")

        self.session_dir = session_dir
        self.screenshots_dir = screenshots_dir
        self.media_dir = media_dir
        self.events_log_path = events_log_path
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)

        if session_path.exists():
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"session.json 格式无效: {session_path}")
            self.data = RecordingSessionData.from_dict(payload)
        else:
            self.data = RecordingSessionData(
                session_id=session_dir.name,
                created_at=utc_now_iso(),
                output_dir=str(session_dir),
                screenshots_dir=str(screenshots_dir),
                media_dir=str(media_dir),
                environment=build_environment_snapshot(),
            )

        self.data.session_id = self.data.session_id or session_dir.name
        self.data.output_dir = str(session_dir)
        self.data.screenshots_dir = str(screenshots_dir)
        self.data.media_dir = str(media_dir)
        self.data.stopped_at = None

        if events_log_path.exists():
            self._materialize_all_events(force_reload=True)
        else:
            events_log_path.write_text("", encoding="utf-8")
            for event in self.data.events:
                self._append_event_to_log(event)

        self._event_count = len(self.data.events)
        self._event_counter = self._infer_event_counter(self.data.events)
        self._screenshot_counter = self._infer_screenshot_counter()
        return self.data

    def start(self, metadata: dict[str, object] | None = None) -> RecordingSessionData:
        metadata_model = SessionMetadata.from_dict(metadata if isinstance(metadata, dict) else {})
        if not metadata_model.is_prs_recording:
            raise RuntimeError("当前版本仅支持 PRS 用例录制。")

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        testcase_id = self._sanitize_session_name_part(metadata_model.testcase_id, "Testcase ID")
        project = self._sanitize_session_name_part(metadata_model.project or "Taichi", "Project")
        version_number = self._sanitize_session_name_part(metadata_model.version_number, "Version Number")
        recorder_person = self._sanitize_session_name_part(metadata_model.recorder_person, "录制人员")
        session_id = f"{testcase_id}_{version_number}_{recorder_person}_{timestamp}"
        self.session_dir = self.base_dir / testcase_id / project / session_id
        self.screenshots_dir = self.session_dir / "screenshots"
        self.media_dir = self.session_dir / "media"
        self.events_log_path = self.session_dir / "events.jsonl"
        self._event_counter = 0
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.events_log_path.write_text("", encoding="utf-8")
        self._event_count = 0
        self._screenshot_counter = 0

        self.data = RecordingSessionData(
            session_id=session_id,
            created_at=utc_now_iso(),
            output_dir=str(self.session_dir),
            screenshots_dir=str(self.screenshots_dir),
            media_dir=str(self.media_dir),
            metadata=metadata_model,
            environment=build_environment_snapshot(),
        )
        return self.data

    def stop(self) -> Path:
        if not self.data or not self.session_dir:
            raise RuntimeError("No active session to stop.")

        self.data.stopped_at = utc_now_iso()
        self._write_session_files()
        return self.session_dir

    def save_snapshot(self) -> Path:
        if not self.data or not self.session_dir:
            raise RuntimeError("No active session to save.")
        self._write_session_files()
        return self.session_dir

    def capture_screenshot(
        self,
        prefix: str,
        highlight_rect: dict[str, int] | None = None,
        focus_point: tuple[int, int] | None = None,
    ) -> str | None:
        if not self.screenshots_dir or not self.session_dir:
            return None

        with self._lock:
            self._screenshot_counter += 1
            file_name = f"{prefix}_{self._screenshot_counter:04d}.png"
        screenshot_path = self.screenshots_dir / file_name
        try:
            image = ImageGrab.grab(all_screens=True)
            image, origin = self._prepare_event_image(image, highlight_rect, focus_point)
            if highlight_rect:
                self._draw_highlight_rect(image, highlight_rect, origin)
            image.save(screenshot_path)
            return safe_relpath(screenshot_path, self.session_dir)
        except Exception:
            return None

    @staticmethod
    def _draw_highlight_rect(
        image: Image.Image,
        highlight_rect: dict[str, int],
        origin: tuple[int, int] | None = None,
    ) -> None:
        left = int(highlight_rect.get("left", 0) or 0)
        top = int(highlight_rect.get("top", 0) or 0)
        right = int(highlight_rect.get("right", 0) or 0)
        bottom = int(highlight_rect.get("bottom", 0) or 0)
        if right <= left or bottom <= top:
            return

        layout = get_display_layout_snapshot()
        virtual_screen = layout.get("virtual_screen", {}) if isinstance(layout, dict) else {}
        if not isinstance(virtual_screen, dict):
            return
        if origin is None:
            screen_left = int(virtual_screen.get("left", 0) or 0)
            screen_top = int(virtual_screen.get("top", 0) or 0)
        else:
            screen_left, screen_top = origin
        padding = 4
        translated = (
            left - screen_left - padding,
            top - screen_top - padding,
            right - screen_left + padding,
            bottom - screen_top + padding,
        )

        draw = ImageDraw.Draw(image)
        for offset in range(4):
            draw.rectangle(
                (
                    translated[0] - offset,
                    translated[1] - offset,
                    translated[2] + offset,
                    translated[3] + offset,
                ),
                outline="#ff2b2b",
                width=2,
            )

    def save_image(
        self,
        image: Image.Image,
        prefix: str,
        folder_name: str = "screenshots",
        highlight_rect: dict[str, int] | None = None,
        focus_point: tuple[int, int] | None = None,
    ) -> str | None:
        if not self.session_dir:
            return None

        target_dir = self._get_media_folder(folder_name)
        with self._lock:
            self._screenshot_counter += 1
            file_name = f"{prefix}_{self._screenshot_counter:04d}.png"
        output_path = target_dir / file_name
        try:
            image, origin = self._prepare_event_image(image, highlight_rect, focus_point)
            if highlight_rect:
                self._draw_highlight_rect(image, highlight_rect, origin)
            image.save(output_path)
            return safe_relpath(output_path, self.session_dir)
        except Exception:
            return None

    @staticmethod
    def _prepare_event_image(
        image: Image.Image,
        highlight_rect: dict[str, int] | None,
        focus_point: tuple[int, int] | None,
    ) -> tuple[Image.Image, tuple[int, int]]:
        layout = get_display_layout_snapshot()
        virtual_screen = layout.get("virtual_screen", {}) if isinstance(layout, dict) else {}
        monitors = layout.get("monitors", []) if isinstance(layout, dict) else []
        if not isinstance(virtual_screen, dict):
            return image, (0, 0)

        origin = (
            int(virtual_screen.get("left", 0) or 0),
            int(virtual_screen.get("top", 0) or 0),
        )

        if not focus_point and isinstance(highlight_rect, dict):
            left = highlight_rect.get("left")
            top = highlight_rect.get("top")
            right = highlight_rect.get("right")
            bottom = highlight_rect.get("bottom")
            if all(isinstance(value, int) for value in [left, top, right, bottom]):
                focus_point = (int((left + right) / 2), int((top + bottom) / 2))

        if not focus_point or not isinstance(monitors, list) or len(monitors) <= 1:
            return image, origin

        monitor = SessionStore._find_monitor_for_point(focus_point[0], focus_point[1], monitors)
        if not isinstance(monitor, dict):
            return image, origin

        monitor_left = int(monitor.get("left", 0) or 0)
        monitor_top = int(monitor.get("top", 0) or 0)
        monitor_width = int(monitor.get("width", 0) or 0)
        monitor_height = int(monitor.get("height", 0) or 0)
        virtual_left = origin[0]
        virtual_top = origin[1]
        crop_box = (
            monitor_left - virtual_left,
            monitor_top - virtual_top,
            monitor_left - virtual_left + monitor_width,
            monitor_top - virtual_top + monitor_height,
        )
        if crop_box == (0, 0, image.width, image.height):
            return image, origin
        return image.crop(crop_box), (monitor_left, monitor_top)

    @staticmethod
    def _find_monitor_for_point(x: int, y: int, monitors: list[dict[str, object]]) -> dict[str, object] | None:
        for monitor in monitors:
            left = monitor.get("left")
            top = monitor.get("top")
            width = monitor.get("width")
            height = monitor.get("height")
            if not all(isinstance(value, int) for value in [left, top, width, height]):
                continue
            if left <= x < left + width and top <= y < top + height:
                return monitor
        return None

    def allocate_media_path(self, prefix: str, extension: str, folder_name: str = "media") -> Path:
        if not self.session_dir:
            raise RuntimeError("No active session.")
        target_dir = self._get_media_folder(folder_name)
        with self._lock:
            self._screenshot_counter += 1
            file_name = f"{prefix}_{self._screenshot_counter:04d}{extension}"
        return target_dir / file_name

    def next_event_id(self, prefix: str = "evt") -> str:
        with self._lock:
            self._event_counter += 1
            return f"{prefix}_{self._event_counter:04d}"

    def append_event(self, event: RecordedEvent) -> None:
        if not self.data:
            raise RuntimeError("No active session.")
        with self._lock:
            self._append_event_to_log(event)
            self._event_count += 1
            if len(self.data.events) < self._in_memory_event_limit:
                self.data.events.append(event)

    def add_comment(self, payload: dict[str, object]) -> None:
        if not self.data:
            raise RuntimeError("No active session.")
        with self._lock:
            self.data.comments.append(payload)

    def add_checkpoint(self, payload: dict[str, object]) -> None:
        if not self.data:
            raise RuntimeError("No active session.")
        with self._lock:
            self.data.checkpoints.append(payload)

    def _write_session_files(self) -> None:
        if not self.data or not self.session_dir:
            return

        self._materialize_all_events(force_reload=True)

        session_payload = self.data.to_dict()
        json_path = self.session_dir / "session.json"
        yaml_path = self.session_dir / "session.yaml"

        json_path.write_text(
            json.dumps(session_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        yaml_path.write_text(
            yaml.safe_dump(session_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _get_media_folder(self, folder_name: str) -> Path:
        if not self.session_dir:
            raise RuntimeError("No active session.")

        if folder_name == "screenshots":
            target_dir = self.screenshots_dir
        elif folder_name == "media":
            target_dir = self.media_dir
        else:
            target_dir = self.session_dir / folder_name
            target_dir.mkdir(parents=True, exist_ok=True)

        if target_dir is None:
            raise RuntimeError("Session media directory is not ready.")
        return target_dir

    def _append_event_to_log(self, event: RecordedEvent) -> None:
        if not self.events_log_path:
            raise RuntimeError("Session event log is not ready.")
        with self.events_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def _materialize_all_events(self, force_reload: bool = False) -> None:
        if not self.data or not self.events_log_path:
            return
        if not force_reload and self._event_count == len(self.data.events):
            return

        loaded_events: list[RecordedEvent] = []
        with self.events_log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                payload = json.loads(text)
                if isinstance(payload, dict):
                    loaded_events.append(RecordedEvent.from_dict(payload))
        self.data.events = loaded_events
        self._event_count = len(loaded_events)

    @staticmethod
    def _infer_event_counter(events: list[RecordedEvent]) -> int:
        max_counter = 0
        for event in events:
            max_counter = max(max_counter, SessionStore._extract_counter(event.event_id))
        return max_counter

    def _infer_screenshot_counter(self) -> int:
        if not self.session_dir:
            return 0

        max_counter = 0
        for path in self.session_dir.rglob("*"):
            if path.is_file():
                max_counter = max(max_counter, self._extract_counter(path.stem))
        return max_counter

    @staticmethod
    def _extract_counter(value: str) -> int:
        match = re.search(r"_(\d+)$", value)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    @staticmethod
    def _sanitize_session_name_part(value: str, field_name: str) -> str:
        cleaned = str(value or "").strip()
        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", cleaned)
        cleaned = re.sub(r"\s+", "_", cleaned)
        cleaned = re.sub(r"_+", "_", cleaned)
        cleaned = cleaned.strip("._ ")
        if not cleaned:
            raise RuntimeError(f"{field_name} 无法用于创建录制目录，请检查填写内容。")
        return cleaned