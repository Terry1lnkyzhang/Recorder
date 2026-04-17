from __future__ import annotations

import copy
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable

from PIL import Image, ImageGrab

from pynput import keyboard, mouse

from .analyzer import build_reuse_suggestions, write_suggestions
from src.common.app_logging import get_logger
from .models import RecordedEvent, UIElementInfo, WindowInfo
from .settings import AISettings, SettingsStore
from .session import SessionStore
from .system_info import get_active_window_info, get_ui_element_at_point, get_window_info_at_point, utc_now_iso


class RecorderEngine:
    def __init__(
        self,
        output_dir: Path,
        status_callback: Callable[[str], None] | None = None,
        settings_store: SettingsStore | None = None,
        ai_checkpoint_request_callback: Callable[[], None] | None = None,
        manual_screenshot_request_callback: Callable[[], None] | None = None,
    ) -> None:
        self.store = SessionStore(output_dir)
        self.status_callback = status_callback or (lambda _: None)
        self.settings_store = settings_store
        self.ai_checkpoint_request_callback = ai_checkpoint_request_callback
        self.manual_screenshot_request_callback = manual_screenshot_request_callback
        self.keyboard_listener: keyboard.Listener | None = None
        self.mouse_listener: mouse.Listener | None = None
        self.is_recording = False
        self.is_paused = False
        self._transient_suspend_count = 0
        self._lock = threading.Lock()
        self._event_queue: queue.Queue[dict[str, object] | None] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._cached_window = None
        self._cached_window_at = 0.0
        self._current_process_id = os.getpid()
        self._exclude_recorder_process_windows = True
        self._excluded_process_patterns: list[str] = []
        self._excluded_window_patterns: list[str] = []
        self._input_state_lock = threading.Lock()
        self._pressed_modifiers: set[str] = set()
        self._pending_modifier_presses: dict[str, dict[str, object]] = {}
        self._mouse_button_states: dict[str, dict[str, object]] = {}
        self._pending_scroll_job: dict[str, object] | None = None
        self._pending_scroll_timer: threading.Timer | None = None
        self._scroll_flush_interval_seconds = 0.18
        self._drag_threshold_pixels = 8
        self.logger = get_logger("engine")

    def start(self, metadata: dict[str, object] | None = None) -> str:
        with self._lock:
            if self.is_recording:
                return "Recorder is already running."

            self.reload_capture_filters()
            session = self.store.start(metadata=metadata)
            self._activate_listeners()
            self.logger.info("Recording started | session_id=%s | session_dir=%s", session.session_id, self.store.session_dir)
            self.status_callback(f"Recording started: {session.session_id}")
            return f"Recording started: {session.session_id}"

    def continue_recording(self, session_dir: Path) -> str:
        with self._lock:
            if self.is_recording:
                raise RuntimeError("Recorder is already running.")

            self.reload_capture_filters()
            session = self.store.resume(session_dir)
            self._activate_listeners()
            self.logger.info("Recording resumed | session_id=%s | session_dir=%s", session.session_id, session_dir)
            self.status_callback(f"继续录制: {session.session_id}")
            return f"继续录制: {session.session_id}"

    def stop(self) -> tuple[Path, Path]:
        with self._lock:
            if not self.is_recording:
                raise RuntimeError("Recorder is not running.")

            self._flush_pending_scroll_event()

            if self.keyboard_listener:
                self.keyboard_listener.stop()
            if self.mouse_listener:
                self.mouse_listener.stop()

            pending_jobs = self._event_queue.qsize()
            if pending_jobs:
                self.status_callback(f"正在停止，等待处理 {pending_jobs} 个后台事件...")
            self._event_queue.join()
            self._event_queue.put(None)
            if self._worker_thread:
                self._worker_thread.join(timeout=3)

            self.is_recording = False
            self.is_paused = False
            self._transient_suspend_count = 0
            session_dir = self.store.stop()
            suggestions = build_reuse_suggestions(self.store.data)
            suggestions_path = write_suggestions(session_dir, suggestions)
            self.logger.info("Recording stopped | session_dir=%s | suggestions=%s", session_dir, suggestions_path)
            self.status_callback(f"Recording stopped: {session_dir}")
            return session_dir, suggestions_path

    def save_snapshot(self) -> tuple[Path, Path]:
        with self._lock:
            if not self.is_recording:
                raise RuntimeError("Recorder is not running.")

            self._transient_suspend_count += 1
            try:
                self._flush_pending_scroll_event()
                pending_jobs = self._event_queue.qsize()
                if pending_jobs:
                    self.status_callback(f"正在保存，等待处理 {pending_jobs} 个后台事件...")
                self._event_queue.join()
                session_dir = self.store.save_snapshot()
                suggestions = build_reuse_suggestions(self.store.data)
                suggestions_path = write_suggestions(session_dir, suggestions)
            finally:
                self._transient_suspend_count = max(0, self._transient_suspend_count - 1)

            self.logger.info("Snapshot saved | session_dir=%s | suggestions=%s", session_dir, suggestions_path)
            self.status_callback(f"录制内容已保存: {session_dir}")
            return session_dir, suggestions_path

    def suspend(self) -> None:
        self._transient_suspend_count += 1

    def resume(self) -> None:
        self._transient_suspend_count = max(0, self._transient_suspend_count - 1)

    def pause_recording(self) -> str:
        with self._lock:
            if not self.is_recording:
                raise RuntimeError("Recorder is not running.")
            if self.is_paused:
                return "录制已暂停"
            self._flush_pending_scroll_event()
            self.is_paused = True
            self.logger.info("Recording paused")
            self.status_callback("录制已暂停")
            return "录制已暂停"

    def resume_recording(self) -> str:
        with self._lock:
            if not self.is_recording:
                raise RuntimeError("Recorder is not running.")
            if not self.is_paused:
                return "录制已继续"
            self.is_paused = False
            self.logger.info("Recording resumed from pause")
            self.status_callback("录制已继续")
            return "录制已继续"

    def reload_capture_filters(self) -> None:
        settings = self.settings_store.load() if self.settings_store else AISettings()
        self._exclude_recorder_process_windows = settings.exclude_recorder_process_windows
        self._excluded_process_patterns = [
            item.lower() for item in SettingsStore.parse_pattern_list(settings.excluded_process_names)
        ]
        self._excluded_window_patterns = [
            item.lower() for item in SettingsStore.parse_pattern_list(settings.excluded_window_keywords)
        ]

    def add_comment(self, note: str) -> None:
        if not self.is_recording:
            raise RuntimeError("Recorder is not running.")

        screenshot = self.store.capture_screenshot("comment")
        event = RecordedEvent(
            event_id=self.store.next_event_id("comment"),
            timestamp=utc_now_iso(),
            event_type="comment",
            action="manual_comment",
            screenshot=screenshot,
            note=note,
            window=get_active_window_info(),
            additional_details={"source": "user"},
        )
        self.store.append_event(event)
        self.store.add_comment(event.to_dict())
        self.logger.info("Comment added | event_id=%s", event.event_id)
        self.status_callback("Comment added.")

    def add_comment_with_media(
        self,
        note: str,
        image: Image.Image,
        region: dict[str, int],
    ) -> None:
        if not self.is_recording:
            raise RuntimeError("Recorder is not running.")

        screenshot = self.store.save_image(image, "comment")
        event = RecordedEvent(
            event_id=self.store.next_event_id("comment"),
            timestamp=utc_now_iso(),
            event_type="comment",
            action="manual_comment",
            screenshot=screenshot,
            note=note,
            window=get_active_window_info(),
            media=[{"type": "image", "path": screenshot, "region": region}],
            additional_details={"source": "user", "selection_region": region},
        )
        self.store.append_event(event)
        self.store.add_comment(event.to_dict())
        self.logger.info("Comment with media added | event_id=%s | region=%s", event.event_id, region)
        self.status_callback("Comment added.")

    def add_wait_for_image_with_media(
        self,
        note: str,
        image: Image.Image,
        region: dict[str, int],
        timeout_seconds: int = 120,
    ) -> None:
        if not self.is_recording:
            raise RuntimeError("Recorder is not running.")

        screenshot = self.store.save_image(image, "wait")
        event = RecordedEvent(
            event_id=self.store.next_event_id("wait"),
            timestamp=utc_now_iso(),
            event_type="wait",
            action="wait_for_image",
            screenshot=screenshot,
            note=note,
            window=get_active_window_info(),
            media=[{"type": "image", "path": screenshot, "region": region}],
            additional_details={
                "source": "user",
                "wait_timeout_seconds": timeout_seconds,
            },
        )
        self.store.append_event(event)
        self.logger.info("Wait-for-image added | event_id=%s | region=%s | timeout=%s", event.event_id, region, timeout_seconds)
        self.status_callback("Wait step added.")

    def add_checkpoint(self, title: str, expectation: str) -> None:
        if not self.is_recording:
            raise RuntimeError("Recorder is not running.")

        screenshot = self.store.capture_screenshot("checkpoint")
        checkpoint = {
            "title": title,
            "expectation": expectation,
            "created_at": utc_now_iso(),
        }
        event = RecordedEvent(
            event_id=self.store.next_event_id("checkpoint"),
            timestamp=utc_now_iso(),
            event_type="checkpoint",
            action="ai_checkpoint",
            screenshot=screenshot,
            note=title,
            checkpoint=checkpoint,
            window=get_active_window_info(),
            additional_details={"source": "user", "analysis_ready": True},
        )
        self.store.append_event(event)
        self.store.add_checkpoint(event.to_dict())
        self.logger.info("Checkpoint added | event_id=%s | title=%s", event.event_id, title)
        self.status_callback("AI checkpoint added.")

    def add_checkpoint_with_media(
        self,
        title: str,
        query: str,
        prompt: str,
        response_text: str,
        media: list[dict[str, object]],
        query_payload: dict[str, object] | None = None,
        prompt_template_key: str = "ct_validation",
        design_steps: str = "",
        step_description: str = "",
        step_comment: str = "",
    ) -> None:
        if not self.is_recording:
            raise RuntimeError("Recorder is not running.")

        primary_screenshot = None
        for item in media:
            if item.get("type") == "image":
                primary_screenshot = item.get("path")
                break

        checkpoint = {
            "title": title,
            "query": query,
            "prompt": prompt,
            "response": response_text,
            "prompt_template_key": prompt_template_key,
            "design_steps": design_steps,
            "step_description": step_description or step_comment,
            "step_comment": step_comment,
            "media_count": len(media),
            "created_at": utc_now_iso(),
        }
        event = RecordedEvent(
            event_id=self.store.next_event_id("checkpoint"),
            timestamp=utc_now_iso(),
            event_type="checkpoint",
            action="ai_checkpoint",
            screenshot=str(primary_screenshot) if primary_screenshot else None,
            note=title,
            checkpoint=checkpoint,
            media=media,
            ai_result={
                **(query_payload or {"response": response_text}),
                "design_steps": design_steps,
                "step_description": step_description or step_comment,
                "step_comment": step_comment,
            },
            window=get_active_window_info(),
            additional_details={"source": "user", "analysis_ready": True},
        )
        self.store.append_event(event)
        self.store.add_checkpoint(event.to_dict())
        self.logger.info(
            "Checkpoint with media added | event_id=%s | title=%s | media_count=%s",
            event.event_id,
            title,
            len(media),
        )
        self.status_callback("AI checkpoint added.")

    def save_manual_image(self, image: Image.Image, prefix: str) -> str | None:
        return self.store.save_image(image, prefix)

    def allocate_media_path(self, prefix: str, extension: str, folder_name: str = "media") -> Path:
        return self.store.allocate_media_path(prefix, extension, folder_name)

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if not self.is_recording or self._is_capture_paused():
            return

        self._flush_pending_scroll_event()

        button_name = str(button)

        if pressed:
            window_info = get_window_info_at_point(x, y)
            if not isinstance(window_info, WindowInfo) or not window_info.handle:
                window_info = self._refresh_cached_window_info()
            if self._should_exclude_window(window_info):
                return
            timestamp = utc_now_iso()
            with self._input_state_lock:
                active_modifiers = self._snapshot_pressed_modifiers_locked()
                if active_modifiers:
                    self._mark_modifier_presses_consumed_locked(active_modifiers)
                self._mouse_button_states[button_name] = {
                    "button": button_name,
                    "start_x": x,
                    "start_y": y,
                    "last_x": x,
                    "last_y": y,
                    "start_timestamp": timestamp,
                    "start_monotonic": time.monotonic(),
                    "move_count": 0,
                    "max_distance": 0,
                    "window": window_info,
                    "modifiers": active_modifiers,
                }
            return

        with self._input_state_lock:
            state = self._mouse_button_states.pop(button_name, None)

        if not state:
            return

        window_info = get_window_info_at_point(x, y)
        if not isinstance(window_info, WindowInfo) or not window_info.handle:
            window_info = self._refresh_cached_window_info()
        press_window = state.get("window")
        if not isinstance(window_info, WindowInfo) or self._should_exclude_window(window_info):
            window_info = press_window if isinstance(press_window, WindowInfo) else None
        if not isinstance(window_info, WindowInfo) or self._should_exclude_window(window_info):
            return

        start_x = int(state.get("start_x", x))
        start_y = int(state.get("start_y", y))
        delta_x = x - start_x
        delta_y = y - start_y
        max_distance = max(abs(delta_x), abs(delta_y), int(state.get("max_distance", 0) or 0))
        move_count = int(state.get("move_count", 0) or 0)
        modifiers = sorted(
            set(state.get("modifiers", [])) | set(self._snapshot_pressed_modifiers())
        )
        duration_ms = int(max(0.0, time.monotonic() - float(state.get("start_monotonic", time.monotonic()))) * 1000)
        is_drag = max_distance >= self._drag_threshold_pixels

        job = {
            "kind": "mouse_drag" if is_drag else "mouse_click",
            "event_id": self.store.next_event_id(),
            "timestamp": utc_now_iso(),
            "button": button_name,
            "window": window_info,
            "modifiers": modifiers,
            "duration_ms": duration_ms,
            "start_timestamp": str(state.get("start_timestamp", "")),
            "start_x": start_x,
            "start_y": start_y,
            "end_x": x,
            "end_y": y,
            "delta_x": delta_x,
            "delta_y": delta_y,
            "move_count": move_count,
            "max_distance": max_distance,
        }

        ui_element, highlight_rect, captured_image = self._capture_visual_context(x, y)
        job["ui_element"] = ui_element
        job["highlight_rect"] = highlight_rect
        job["captured_image"] = captured_image
        self._event_queue.put(job)

    def _on_move(self, x: int, y: int) -> None:
        if not self.is_recording or self._is_capture_paused():
            return

        with self._input_state_lock:
            if not self._mouse_button_states:
                return
            for state in self._mouse_button_states.values():
                start_x = int(state.get("start_x", x))
                start_y = int(state.get("start_y", y))
                state["last_x"] = x
                state["last_y"] = y
                state["move_count"] = int(state.get("move_count", 0) or 0) + 1
                state["max_distance"] = max(
                    int(state.get("max_distance", 0) or 0),
                    abs(x - start_x),
                    abs(y - start_y),
                )

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if not self.is_recording or self._is_capture_paused():
            return

        window_info = self._refresh_cached_window_info()
        if self._should_exclude_window(window_info):
            return

        now = time.monotonic()
        timestamp = utc_now_iso()
        modifiers = self._snapshot_pressed_modifiers()
        pending_job_to_flush: dict[str, object] | None = None

        with self._input_state_lock:
            current = self._pending_scroll_job
            if modifiers:
                self._mark_modifier_presses_consumed_locked(modifiers)
            if self._can_merge_scroll_job(current, window_info, x, y, modifiers, now):
                assert current is not None
                current["dx"] = int(current.get("dx", 0)) + dx
                current["dy"] = int(current.get("dy", 0)) + dy
                current["end_x"] = x
                current["end_y"] = y
                current["end_timestamp"] = timestamp
                current["last_monotonic"] = now
                current["step_count"] = int(current.get("step_count", 0) or 0) + 1
            else:
                pending_job_to_flush = self._consume_pending_scroll_job_locked(cancel_timer=False)
                self._pending_scroll_job = {
                    "kind": "scroll",
                    "window": window_info,
                    "dx": dx,
                    "dy": dy,
                    "start_x": x,
                    "start_y": y,
                    "end_x": x,
                    "end_y": y,
                    "start_timestamp": timestamp,
                    "end_timestamp": timestamp,
                    "last_monotonic": now,
                    "step_count": 1,
                    "modifiers": modifiers,
                }
            self._schedule_scroll_flush_locked()

        if pending_job_to_flush:
            self._queue_scroll_job(pending_job_to_flush)

    def _on_key_press(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if not self.is_recording or self._is_capture_paused():
            return

        self._flush_pending_scroll_event()

        key_name = self._normalize_key(key)
        modifier_name = self._normalize_modifier_key(key_name)
        key_char = getattr(key, "char", None)
        modifier_window_info = self._refresh_cached_window_info() if modifier_name else None

        with self._input_state_lock:
            if modifier_name:
                modifiers_before_press = self._snapshot_pressed_modifiers_locked()
                was_already_pressed = modifier_name in self._pressed_modifiers
                self._pressed_modifiers.add(modifier_name)
                if not was_already_pressed:
                    self._pending_modifier_presses[modifier_name] = {
                        "key_name": key_name,
                        "char": key_char,
                        "timestamp": utc_now_iso(),
                        "window": copy.deepcopy(modifier_window_info) if isinstance(modifier_window_info, WindowInfo) else None,
                        "modifiers": modifiers_before_press,
                        "consumed": False,
                    }
            else:
                active_modifiers = self._snapshot_pressed_modifiers_locked()
                if active_modifiers:
                    self._mark_modifier_presses_consumed_locked(active_modifiers)

        if modifier_name:
            return

        if self._is_ai_checkpoint_shortcut(key_name, active_modifiers):
            self.logger.info("AI checkpoint shortcut triggered")
            if self.ai_checkpoint_request_callback:
                try:
                    self.ai_checkpoint_request_callback()
                except Exception:
                    self.logger.exception("Failed to invoke AI checkpoint shortcut callback")
            return
        if self._is_manual_screenshot_shortcut(key_name, active_modifiers):
            self.logger.info("Manual screenshot shortcut triggered")
            if self.manual_screenshot_request_callback:
                try:
                    self.manual_screenshot_request_callback()
                except Exception:
                    self.logger.exception("Failed to invoke manual screenshot shortcut callback")
            return

        window_info = self._refresh_cached_window_info()
        self._append_key_press_event(
            key_name=key_name,
            key_char=key_char,
            window_info=window_info,
            active_modifiers=active_modifiers,
        )

    def _on_key_release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        key_name = self._normalize_key(key)
        modifier_name = self._normalize_modifier_key(key_name)
        if not modifier_name:
            return

        with self._input_state_lock:
            self._pressed_modifiers.discard(modifier_name)
            pending_press = self._pending_modifier_presses.pop(modifier_name, None)

        if not isinstance(pending_press, dict) or pending_press.get("consumed"):
            return

        window_info = pending_press.get("window")
        if not isinstance(window_info, WindowInfo):
            return
        self._append_key_press_event(
            key_name=str(pending_press.get("key_name") or key_name),
            key_char=pending_press.get("char"),
            window_info=window_info,
            active_modifiers=list(pending_press.get("modifiers", [])),
            timestamp=str(pending_press.get("timestamp") or utc_now_iso()),
        )

    def _process_event_jobs(self) -> None:
        while True:
            job = self._event_queue.get()
            if job is None:
                self._event_queue.task_done()
                break

            try:
                kind = job["kind"]
                if kind == "mouse_click":
                    self._record_mouse_click(job)
                elif kind == "mouse_drag":
                    self._record_mouse_drag(job)
                elif kind == "scroll":
                    self._record_scroll(job)
            except Exception:
                self.logger.exception("Failed to process background event job | kind=%s", job.get("kind"))
            finally:
                self._event_queue.task_done()

    def _record_mouse_click(self, job: dict[str, object]) -> None:
        x = int(job.get("end_x", job.get("x", 0)) or 0)
        y = int(job.get("end_y", job.get("y", 0)) or 0)
        window_info = job.get("window")
        if not isinstance(window_info, WindowInfo) or self._should_exclude_window(window_info):
            return
        ui_element = job.get("ui_element")
        if not isinstance(ui_element, UIElementInfo):
            ui_element = get_ui_element_at_point(x, y)
        highlight_rect = job.get("highlight_rect") if isinstance(job.get("highlight_rect"), dict) else None
        if not highlight_rect:
            highlight_rect = ui_element.rectangle if ui_element.rectangle else None
        captured_image = job.get("captured_image")
        if isinstance(captured_image, Image.Image):
            screenshot = self.store.save_image(captured_image, "step", highlight_rect=highlight_rect)
        else:
            screenshot = self.store.capture_screenshot("step", highlight_rect=highlight_rect)
        visual_focus_hint = {
            "red_box_marks_target": bool(highlight_rect),
            "target_control_name": ui_element.name,
            "target_control_type": ui_element.control_type,
            "target_rect": dict(highlight_rect) if highlight_rect else {},
        }
        event = RecordedEvent(
            event_id=str(job["event_id"]),
            timestamp=str(job["timestamp"]),
            event_type="mouse_click",
            action=self._build_action_payload(str(job["button"]), list(job.get("modifiers", []))),
            screenshot=screenshot,
            mouse={"x": x, "y": y, "button": str(job["button"])} ,
            keyboard=self._build_modifier_keyboard_payload(list(job.get("modifiers", []))),
            window=copy.deepcopy(window_info),
            ui_element=ui_element,
            additional_details={
                "capture_reason": "mouse_click",
                "processed_async": True,
                "visual_focus_hint": visual_focus_hint,
                "modifiers": list(job.get("modifiers", [])),
                "combined_action": self._build_combined_mouse_action_label(
                    button_name=str(job.get("button", "")),
                    modifiers=list(job.get("modifiers", [])),
                    is_drag=False,
                ),
                "duration_ms": int(job.get("duration_ms", 0) or 0),
            },
        )
        self.store.append_event(event)

    def _record_mouse_drag(self, job: dict[str, object]) -> None:
        start_x = int(job["start_x"])
        start_y = int(job["start_y"])
        end_x = int(job["end_x"])
        end_y = int(job["end_y"])
        window_info = job.get("window")
        if not isinstance(window_info, WindowInfo) or self._should_exclude_window(window_info):
            return

        ui_element = job.get("ui_element")
        if not isinstance(ui_element, UIElementInfo):
            ui_element = get_ui_element_at_point(end_x, end_y)
        highlight_rect = job.get("highlight_rect") if isinstance(job.get("highlight_rect"), dict) else None
        if not highlight_rect:
            highlight_rect = ui_element.rectangle if ui_element.rectangle else None
        captured_image = job.get("captured_image")
        if isinstance(captured_image, Image.Image):
            screenshot = self.store.save_image(captured_image, "step", highlight_rect=highlight_rect)
        else:
            screenshot = self.store.capture_screenshot("step", highlight_rect=highlight_rect)
        visual_focus_hint = {
            "red_box_marks_target": bool(highlight_rect),
            "target_control_name": ui_element.name,
            "target_control_type": ui_element.control_type,
            "target_rect": dict(highlight_rect) if highlight_rect else {},
        }
        event = RecordedEvent(
            event_id=str(job["event_id"]),
            timestamp=str(job["timestamp"]),
            event_type="mouse_drag",
            action=self._build_action_payload(str(job["button"]), list(job.get("modifiers", []))),
            screenshot=screenshot,
            mouse={
                "button": str(job["button"]),
                "x": end_x,
                "y": end_y,
                "start_x": start_x,
                "start_y": start_y,
                "end_x": end_x,
                "end_y": end_y,
                "delta_x": int(job.get("delta_x", 0) or 0),
                "delta_y": int(job.get("delta_y", 0) or 0),
                "move_count": int(job.get("move_count", 0) or 0),
            },
            keyboard=self._build_modifier_keyboard_payload(list(job.get("modifiers", []))),
            window=copy.deepcopy(window_info),
            ui_element=ui_element,
            additional_details={
                "capture_reason": "mouse_drag",
                "processed_async": True,
                "visual_focus_hint": visual_focus_hint,
                "modifiers": list(job.get("modifiers", [])),
                "combined_action": self._build_combined_mouse_action_label(
                    button_name=str(job.get("button", "")),
                    modifiers=list(job.get("modifiers", [])),
                    is_drag=True,
                ),
                "duration_ms": int(job.get("duration_ms", 0) or 0),
                "start_timestamp": str(job.get("start_timestamp", "")),
                "max_distance": int(job.get("max_distance", 0) or 0),
            },
        )
        self.store.append_event(event)

    def _record_scroll(self, job: dict[str, object]) -> None:
        x = int(job["end_x"])
        y = int(job["end_y"])
        window_info = job.get("window")
        if not isinstance(window_info, WindowInfo) or self._should_exclude_window(window_info):
            return
        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=str(job.get("end_timestamp", utc_now_iso())),
            event_type="scroll",
            action=self._build_action_payload("mouse_scroll", list(job.get("modifiers", []))),
            mouse={"x": x, "y": y},
            keyboard=self._build_modifier_keyboard_payload(list(job.get("modifiers", []))),
            scroll={
                "dx": int(job["dx"]),
                "dy": int(job["dy"]),
                "step_count": int(job.get("step_count", 1) or 1),
                "start_x": int(job.get("start_x", x) or x),
                "start_y": int(job.get("start_y", y) or y),
                "end_x": x,
                "end_y": y,
            },
            window=copy.deepcopy(window_info),
            ui_element=get_ui_element_at_point(x, y),
            additional_details={
                "capture_reason": "scroll",
                "processed_async": True,
                "modifiers": list(job.get("modifiers", [])),
                "combined_action": self._build_combined_scroll_action_label(
                    modifiers=list(job.get("modifiers", [])),
                    delta_y=int(job.get("dy", 0) or 0),
                ),
                "start_timestamp": str(job.get("start_timestamp", "")),
                "end_timestamp": str(job.get("end_timestamp", "")),
            },
        )
        self.store.append_event(event)

    def _should_exclude_window(self, window: WindowInfo | None) -> bool:
        if window is None:
            return False

        if self._exclude_recorder_process_windows and window.process_id == self._current_process_id:
            return True

        process_name = (window.process_name or "").lower()
        title = (window.title or "").lower()
        class_name = (window.class_name or "").lower()

        if any(pattern in process_name for pattern in self._excluded_process_patterns):
            return True
        if any(pattern in title or pattern in class_name for pattern in self._excluded_window_patterns):
            return True
        return False

    def _activate_listeners(self) -> None:
        self._event_queue = queue.Queue()
        self._worker_thread = threading.Thread(target=self._process_event_jobs, daemon=True)
        self._worker_thread.start()
        self._cached_window = None
        self._cached_window_at = 0.0
        with self._input_state_lock:
            self._pressed_modifiers.clear()
            self._pending_modifier_presses.clear()
            self._mouse_button_states.clear()
            self._consume_pending_scroll_job_locked(cancel_timer=True)
        self.is_recording = True
        self.is_paused = False
        self._transient_suspend_count = 0
        self.keyboard_listener = keyboard.Listener(on_press=self._on_key_press, on_release=self._on_key_release)
        self.mouse_listener = mouse.Listener(on_click=self._on_click, on_move=self._on_move, on_scroll=self._on_scroll)
        self.keyboard_listener.start()
        self.mouse_listener.start()

    def _is_capture_paused(self) -> bool:
        return self.is_paused or self._transient_suspend_count > 0

    def _get_cached_window_info(self):
        now = time.monotonic()
        if self._cached_window is None or now - self._cached_window_at > 0.3:
            return self._refresh_cached_window_info()
        return copy.deepcopy(self._cached_window)

    def _refresh_cached_window_info(self):
        self._cached_window = get_active_window_info()
        self._cached_window_at = time.monotonic()
        return copy.deepcopy(self._cached_window)

    @staticmethod
    def _normalize_key(key: keyboard.Key | keyboard.KeyCode) -> str:
        if hasattr(key, "char") and key.char:
            return str(key.char)
        return str(key)

    @staticmethod
    def _normalize_modifier_key(key_name: str) -> str | None:
        normalized = key_name.split(".", 1)[1].lower() if key_name.startswith("Key.") else key_name.lower()
        if normalized in {"ctrl", "ctrl_l", "ctrl_r"}:
            return "ctrl"
        if normalized in {"shift", "shift_l", "shift_r"}:
            return "shift"
        if normalized in {"alt", "alt_l", "alt_r", "alt_gr"}:
            return "alt"
        if normalized in {"cmd", "cmd_l", "cmd_r"}:
            return "cmd"
        return None

    def _snapshot_pressed_modifiers(self) -> list[str]:
        with self._input_state_lock:
            return self._snapshot_pressed_modifiers_locked()

    def _snapshot_pressed_modifiers_locked(self) -> list[str]:
        return sorted(self._pressed_modifiers)

    def _mark_modifier_presses_consumed_locked(self, modifiers: list[str]) -> None:
        for modifier_name in modifiers:
            pending_press = self._pending_modifier_presses.get(modifier_name)
            if isinstance(pending_press, dict):
                pending_press["consumed"] = True

    def _append_key_press_event(
        self,
        *,
        key_name: str,
        key_char: object,
        window_info: WindowInfo,
        active_modifiers: list[str],
        timestamp: str | None = None,
    ) -> None:
        if self._should_exclude_window(window_info):
            return

        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=timestamp or utc_now_iso(),
            event_type="key_press",
            action="press",
            keyboard={
                "key_name": key_name,
                "char": key_char,
                "modifiers": list(active_modifiers),
            },
            window=window_info,
            additional_details={
                "capture_reason": "key_press",
                "modifiers": list(active_modifiers),
                "combined_action": self._build_combined_key_action_label(key_name, key_char, active_modifiers),
            },
        )
        self.store.append_event(event)

    @staticmethod
    def _build_modifier_keyboard_payload(modifiers: list[str]) -> dict[str, object]:
        normalized = [str(item).strip() for item in modifiers if str(item).strip()]
        if not normalized:
            return {}
        return {"modifiers": normalized}

    @staticmethod
    def _build_action_payload(primary_action: str, modifiers: list[str]) -> str | list[str]:
        if any(str(item).strip() for item in modifiers):
            return ["press", primary_action]
        return primary_action

    def _build_combined_mouse_action_label(self, button_name: str, modifiers: list[str], *, is_drag: bool) -> str:
        readable_modifiers = self._format_modifier_names(modifiers)
        action_label = str(button_name or "").strip()
        if not readable_modifiers:
            return action_label
        return f"{' + '.join(readable_modifiers)} + {action_label}"

    def _build_combined_scroll_action_label(self, modifiers: list[str], delta_y: int) -> str:
        readable_modifiers = self._format_modifier_names(modifiers)
        action_label = "mouse_scroll"
        if not readable_modifiers:
            return action_label
        return f"{' + '.join(readable_modifiers)} + {action_label}"

    def _build_combined_key_action_label(self, key_name: str, key_char: object, modifiers: list[str]) -> str:
        readable_modifiers = self._format_modifier_names(modifiers)
        readable_key = self._normalize_display_key_name(key_name, key_char)
        if not readable_modifiers:
            return readable_key
        return f"{' + '.join(readable_modifiers)} + {readable_key}"

    def _format_modifier_names(self, modifiers: list[str]) -> list[str]:
        return [self._normalize_display_modifier_name(item) for item in modifiers if self._normalize_display_modifier_name(item)]

    @staticmethod
    def _normalize_display_modifier_name(modifier_name: object) -> str:
        normalized = str(modifier_name or "").strip().lower()
        mapping = {
            "ctrl": "Ctrl",
            "shift": "Shift",
            "alt": "Alt",
            "cmd": "Win",
        }
        return mapping.get(normalized, normalized)

    def _normalize_display_key_name(self, key_name: str, key_char: object) -> str:
        if isinstance(key_char, str) and len(key_char) == 1 and key_char.isprintable():
            return key_char.upper()
        normalized = key_name.split(".", 1)[1] if key_name.startswith("Key.") else key_name
        modifier_name = self._normalize_modifier_key(normalized)
        if modifier_name:
            return self._normalize_display_modifier_name(modifier_name)
        mapping = {
            "enter": "Enter",
            "tab": "Tab",
            "esc": "Esc",
            "space": "Space",
            "backspace": "Backspace",
            "delete": "Delete",
        }
        lowered = normalized.lower()
        if lowered in mapping:
            return mapping[lowered]
        return normalized.upper() if len(normalized) == 1 else normalized

    @staticmethod
    def _is_ai_checkpoint_shortcut(key_name: str, active_modifiers: list[str]) -> bool:
        normalized_key = key_name.split(".", 1)[1].lower() if key_name.startswith("Key.") else key_name.lower()
        return normalized_key == "f5" and "ctrl" in active_modifiers

    @staticmethod
    def _is_manual_screenshot_shortcut(key_name: str, active_modifiers: list[str]) -> bool:
        normalized_key = key_name.split(".", 1)[1].lower() if key_name.startswith("Key.") else key_name.lower()
        return normalized_key == "f4" and "ctrl" in active_modifiers

    def _can_merge_scroll_job(
        self,
        current: dict[str, object] | None,
        window_info: WindowInfo,
        x: int,
        y: int,
        modifiers: list[str],
        now: float,
    ) -> bool:
        if not current:
            return False
        current_window = current.get("window")
        if not isinstance(current_window, WindowInfo):
            return False
        if current_window.handle != window_info.handle:
            return False
        if list(current.get("modifiers", [])) != list(modifiers):
            return False
        last_monotonic = float(current.get("last_monotonic", 0.0) or 0.0)
        if now - last_monotonic > self._scroll_flush_interval_seconds:
            return False
        end_x = int(current.get("end_x", x) or x)
        end_y = int(current.get("end_y", y) or y)
        return abs(x - end_x) <= 64 and abs(y - end_y) <= 64

    def _schedule_scroll_flush_locked(self) -> None:
        if self._pending_scroll_timer:
            self._pending_scroll_timer.cancel()
        timer = threading.Timer(self._scroll_flush_interval_seconds, self._flush_pending_scroll_event)
        timer.daemon = True
        self._pending_scroll_timer = timer
        timer.start()

    def _consume_pending_scroll_job_locked(self, cancel_timer: bool) -> dict[str, object] | None:
        if cancel_timer and self._pending_scroll_timer:
            self._pending_scroll_timer.cancel()
        self._pending_scroll_timer = None
        job = self._pending_scroll_job
        self._pending_scroll_job = None
        return copy.deepcopy(job) if job else None

    def _flush_pending_scroll_event(self) -> None:
        with self._input_state_lock:
            job = self._consume_pending_scroll_job_locked(cancel_timer=True)
        if job:
            self._queue_scroll_job(job)

    def _queue_scroll_job(self, job: dict[str, object]) -> None:
        if not self.is_recording or self._is_capture_paused():
            return
        self._event_queue.put(job)

    def _capture_visual_context(self, x: int, y: int) -> tuple[UIElementInfo, dict[str, int] | None, Image.Image | None]:
        try:
            image = ImageGrab.grab(all_screens=True)
        except Exception:
            image = None
        ui_element = get_ui_element_at_point(x, y)
        highlight_rect = ui_element.rectangle if ui_element.rectangle else None
        return ui_element, highlight_rect, image