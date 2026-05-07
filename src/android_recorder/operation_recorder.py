from __future__ import annotations

import copy
import math
import queue
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from pynput import keyboard, mouse

from src.common.app_logging import get_logger
from src.recorder.models import RecordedEvent, UIElementInfo, WindowInfo
from src.recorder.session import SessionStore
from src.recorder.system_info import get_active_window_info, get_window_info_at_point, utc_now_iso

from .backend import AndroidDeviceCapture, AndroidRecorderBackend, AndroidRecorderError

try:
    import win32gui
except ImportError:
    win32gui = None


_TAP_MOVE_THRESHOLD_PX = 18.0
_SWIPE_MOVE_THRESHOLD_PX = 36.0
_LONG_PRESS_THRESHOLD_MS = 450


@dataclass(slots=True)
class _QueuedJob:
    kind: str
    payload: dict[str, Any]


@dataclass(slots=True)
class _HierarchyNode:
    text: str
    resource_id: str
    content_desc: str
    class_name: str
    bounds: str
    rectangle: dict[str, int]
    xpath: str
    focused: bool = False


@dataclass(slots=True)
class _PointerGestureState:
    button: str
    started_at_monotonic: float
    started_at_iso: str
    start_screen_x: int
    start_screen_y: int
    last_screen_x: int
    last_screen_y: int
    max_move_distance: float
    window: WindowInfo
    client_rect: dict[str, int]
    pre_capture: AndroidDeviceCapture | None = None
    pre_capture_error: str = ""
    pre_capture_thread: threading.Thread | None = None


class AndroidOperationRecorder:
    def __init__(
        self,
        output_dir: Path,
        backend: AndroidRecorderBackend,
        status_callback=None,
    ) -> None:
        self._owned_store = SessionStore(output_dir)
        self.store = self._owned_store
        self._uses_external_session_store = False
        self.backend = backend
        self.status_callback = status_callback or (lambda _message: None)
        self.is_recording = False
        self.serial = ""
        self.keyboard_listener: keyboard.Listener | None = None
        self.mouse_listener: mouse.Listener | None = None
        self._event_queue: queue.Queue[_QueuedJob | None] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._target_root_handle: int | None = None
        self._target_window_title = ""
        self._active_pointer_gesture: _PointerGestureState | None = None
        self.logger = get_logger("android-operation-recorder")

    @property
    def session_dir(self) -> Path | None:
        return self.store.session_dir

    def refresh_target_window_binding(self, serial: str | None = None) -> int | None:
        target_serial = (serial or self.serial or "").strip()
        if not target_serial:
            self._target_root_handle = None
            return None
        deadline = time.monotonic() + 3.0
        handle: int | None = None
        while time.monotonic() < deadline:
            handle = self._resolve_scrcpy_root_handle(target_serial, allow_generic_fallback=False)
            if handle:
                break
            time.sleep(0.15)
        if handle is None:
            handle = self._resolve_scrcpy_root_handle(target_serial, allow_generic_fallback=True)
        self._target_root_handle = handle
        if self._target_root_handle:
            self.logger.info(
                "Android mirror binding refreshed | serial=%s | root_handle=%s",
                target_serial,
                self._target_root_handle,
            )
        else:
            self.logger.info("Android mirror binding not found yet | serial=%s", target_serial)
        return self._target_root_handle

    def start(
        self,
        serial: str,
        metadata: dict[str, object],
        existing_store: SessionStore | None = None,
    ) -> tuple[str, Path | None]:
        with self._lock:
            if self.is_recording:
                raise AndroidRecorderError("Android 操作录制已经在进行中。")

            if existing_store is not None:
                if existing_store.session_dir is None or existing_store.data is None:
                    raise AndroidRecorderError("主 Recorder 当前没有可复用的活动 Session。")
                self.store = existing_store
                self._uses_external_session_store = True
                session = existing_store.data
            else:
                self.store = self._owned_store
                self._uses_external_session_store = False
                session = self.store.start(metadata=metadata)
            self.serial = serial
            environment = self.store.data.environment if self.store.data else {}
            if isinstance(environment, dict):
                environment["android_serial"] = serial
                environment["android_capture_mode"] = "scrcpy+uiautomator2"
                if not self._uses_external_session_store:
                    environment["recorder_platform"] = "android"

            self._event_queue = queue.Queue()
            self._worker_thread = threading.Thread(target=self._process_jobs, daemon=True)
            self._worker_thread.start()
            self._target_root_handle = self._resolve_scrcpy_root_handle(serial)
            self._target_window_title = f"Recorder Android Mirror [{serial}]"
            self._active_pointer_gesture = None
            self.keyboard_listener = keyboard.Listener(on_press=self._on_key_press)
            self.mouse_listener = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll, on_move=self._on_move)
            self.keyboard_listener.start()
            self.mouse_listener.start()
            self.is_recording = True

        self.logger.info(
            "Android operation recording started | serial=%s | session_id=%s | external_store=%s",
            serial,
            session.session_id,
            self._uses_external_session_store,
        )
        self.logger.info("Android target mirror window | serial=%s | root_handle=%s | title=%s", serial, self._target_root_handle, self._target_window_title)
        self._emit_debug(f"录制器已启动 | serial={serial} | root_handle={self._target_root_handle}")
        self.status_callback(f"Android 操作录制已开始: {session.session_id}")
        return session.session_id, self.store.session_dir

    def stop(self) -> Path:
        with self._lock:
            if not self.is_recording:
                raise AndroidRecorderError("当前没有正在进行的 Android 操作录制。")

            if self.keyboard_listener:
                self.keyboard_listener.stop()
            if self.mouse_listener:
                self.mouse_listener.stop()

        self._event_queue.join()
        self._event_queue.put(None)
        if self._worker_thread:
            self._worker_thread.join(timeout=5)

        with self._lock:
            self.is_recording = False
            if self._uses_external_session_store:
                if self.store.session_dir is None:
                    raise AndroidRecorderError("当前主 Session 不可用，无法结束 Android 操作录制。")
                session_dir = self.store.session_dir
            else:
                session_dir = self.store.stop()
            self.serial = ""
            self._target_root_handle = None
            self._target_window_title = ""
            self._active_pointer_gesture = None
            self.store = self._owned_store
            self._uses_external_session_store = False

        self.logger.info("Android operation recording stopped | session_dir=%s", session_dir)
        self._emit_debug(f"录制器已停止 | session_dir={session_dir}")
        self.status_callback(f"Android 操作录制已停止: {session_dir}")
        return session_dir

    def save_snapshot(self) -> Path:
        if not self.is_recording:
            raise AndroidRecorderError("当前没有正在进行的 Android 操作录制。")
        self._event_queue.join()
        session_dir = self.store.save_snapshot()
        self.status_callback(f"Android 操作录制快照已保存: {session_dir}")
        return session_dir

    def _on_move(self, x: int, y: int) -> None:
        if not self.is_recording or self._active_pointer_gesture is None:
            return
        gesture = self._active_pointer_gesture
        distance = self._distance_between_points(gesture.start_screen_x, gesture.start_screen_y, x, y)
        gesture.last_screen_x = x
        gesture.last_screen_y = y
        gesture.max_move_distance = max(gesture.max_move_distance, distance)

    def _on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if not self.is_recording:
            return

        active_window = get_active_window_info()
        if pressed:
            if not self._is_target_window(active_window):
                self._emit_debug(
                    f"按下已忽略 | screen=({x},{y}) | button={button} | active_title={active_window.title!r} | process={active_window.process_name!r} | handle={active_window.handle!r} | target_root={self._target_root_handle}"
                )
                return
            client_rect = self._get_target_client_rect(active_window)
            if client_rect is None:
                self._emit_debug(
                    f"按下已忽略，未拿到镜像客户区 | screen=({x},{y}) | button={button} | active_title={active_window.title!r} | process={active_window.process_name!r} | handle={active_window.handle!r} | target_root={self._target_root_handle}"
                )
                return
            self._active_pointer_gesture = _PointerGestureState(
                button=str(button),
                started_at_monotonic=time.monotonic(),
                started_at_iso=utc_now_iso(),
                start_screen_x=x,
                start_screen_y=y,
                last_screen_x=x,
                last_screen_y=y,
                max_move_distance=0.0,
                window=active_window,
                client_rect=dict(client_rect),
            )
            self._start_pre_capture(self._active_pointer_gesture)
            return

        gesture = self._active_pointer_gesture
        self._active_pointer_gesture = None
        if gesture is None or gesture.button != str(button):
            self._emit_debug(
                f"释放已忽略，未找到匹配的按下态 | screen=({x},{y}) | button={button} | active_title={active_window.title!r} | process={active_window.process_name!r} | handle={active_window.handle!r}"
            )
            return

        if not self._is_target_window(active_window):
            self._emit_debug(
                f"释放发生在镜像窗口外，按起点继续记录 | screen=({x},{y}) | button={button} | active_title={active_window.title!r} | process={active_window.process_name!r} | handle={active_window.handle!r}"
            )

        duration_ms = int(round((time.monotonic() - gesture.started_at_monotonic) * 1000))
        move_distance = max(
            gesture.max_move_distance,
            self._distance_between_points(gesture.start_screen_x, gesture.start_screen_y, x, y),
        )
        payload = {
            "start_screen_x": gesture.start_screen_x,
            "start_screen_y": gesture.start_screen_y,
            "end_screen_x": x,
            "end_screen_y": y,
            "button": str(button),
            "timestamp": gesture.started_at_iso,
            "released_at": utc_now_iso(),
            "duration_ms": duration_ms,
            "move_distance": move_distance,
            "window": gesture.window,
            "release_window": active_window,
            "client_rect": gesture.client_rect,
            "gesture_state": gesture,
        }

        action_kind = self._classify_pointer_action(button, duration_ms, move_distance)
        if action_kind == "click":
            self._emit_debug(
                f"点击已入队 | start=({gesture.start_screen_x},{gesture.start_screen_y}) | end=({x},{y}) | duration_ms={duration_ms} | active_title={active_window.title!r} | process={active_window.process_name!r} | handle={active_window.handle!r} | target_root={self._target_root_handle}"
            )
        elif action_kind == "long_press":
            self._emit_debug(
                f"长按已入队 | start=({gesture.start_screen_x},{gesture.start_screen_y}) | duration_ms={duration_ms} | button={button}"
            )
        elif action_kind == "swipe":
            self._emit_debug(
                f"拖动已入队 | start=({gesture.start_screen_x},{gesture.start_screen_y}) | end=({x},{y}) | duration_ms={duration_ms} | move={move_distance:.1f}"
            )
        elif action_kind == "back":
            self._emit_debug(f"返回已入队 | screen=({x},{y}) | button={button}")
        elif action_kind == "home":
            self._emit_debug(f"Home 已入队 | screen=({x},{y}) | button={button}")

        self._event_queue.put(
            _QueuedJob(
                kind=action_kind,
                payload=payload,
            )
        )

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if not self.is_recording:
            return
        active_window = get_active_window_info()
        if not self._is_target_window(active_window):
            self._emit_debug(
                f"滚轮已忽略 | screen=({x},{y}) | active_title={active_window.title!r} | process={active_window.process_name!r} | handle={active_window.handle!r}"
            )
            return
        client_rect = self._get_target_client_rect(active_window)
        if client_rect is None:
            self._emit_debug("滚轮已忽略，未拿到镜像客户区")
            return
        self._emit_debug(f"滚轮已入队 | screen=({x},{y}) | delta=({dx},{dy})")
        self._event_queue.put(
            _QueuedJob(
                kind="scroll",
                payload={
                    "screen_x": x,
                    "screen_y": y,
                    "dx": dx,
                    "dy": dy,
                    "timestamp": utc_now_iso(),
                    "window": active_window,
                    "client_rect": client_rect,
                },
            )
        )

    def _on_key_press(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if not self.is_recording:
            return
        window_info = get_active_window_info()
        if not self._is_target_window(window_info):
            self._emit_debug(
                f"按键已忽略 | key={key!s} | active_title={window_info.title!r} | process={window_info.process_name!r} | handle={window_info.handle!r}"
            )
            return

        key_name = self._normalize_key(key)
        key_char = getattr(key, "char", "") if hasattr(key, "char") else ""
        if not key_name:
            return
        if self._is_modifier_key(key_name):
            return

        self._emit_debug(f"按键已入队 | key={key_name!r} | char={key_char!r}")

        self._event_queue.put(
            _QueuedJob(
                kind="key",
                payload={
                    "key_name": key_name,
                    "key_char": key_char,
                    "timestamp": utc_now_iso(),
                    "window": window_info,
                },
            )
        )

    def _process_jobs(self) -> None:
        while True:
            job = self._event_queue.get()
            if job is None:
                self._event_queue.task_done()
                break
            try:
                if job.kind == "click":
                    self._record_click(job.payload)
                elif job.kind == "long_press":
                    self._record_long_press(job.payload)
                elif job.kind == "swipe":
                    self._record_swipe(job.payload)
                elif job.kind == "back":
                    self._record_navigation(job.payload, action_name="back")
                elif job.kind == "home":
                    self._record_navigation(job.payload, action_name="home")
                elif job.kind == "scroll":
                    self._record_scroll(job.payload)
                elif job.kind == "key":
                    self._record_key(job.payload)
            except Exception as exc:
                self._emit_debug(f"事件处理失败 | kind={job.kind} | error={exc}")
                self.logger.exception("Failed to record Android operation | kind=%s", job.kind)
            finally:
                self._event_queue.task_done()

    def _record_click(self, payload: dict[str, Any]) -> None:
        self._emit_debug("开始处理点击事件，准备抓取 Android 截图和页面树")
        capture = self._resolve_pointer_capture(payload, debug_action_name="点击")
        mapped_point = self._map_to_device_point(
            screen_x=int(payload.get("end_screen_x", payload.get("screen_x", 0))),
            screen_y=int(payload.get("end_screen_y", payload.get("screen_y", 0))),
            client_rect=payload["client_rect"],
            image_size=capture.screenshot.size,
        )
        if mapped_point is None:
            self._emit_debug("点击处理失败：无法把屏幕坐标映射到设备坐标")
            return

        node = self._find_node_at_point(capture.hierarchy_xml, mapped_point[0], mapped_point[1])
        screenshot = self._prepare_event_image(capture.screenshot, node, points=[mapped_point])
        screenshot_path = self.store.save_image(screenshot, "step")
        self._emit_debug(
            f"点击事件已写入 | device_point=({mapped_point[0]},{mapped_point[1]}) | screenshot={screenshot_path!r} | target={node.resource_id if node else ''}"
        )
        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=str(payload["timestamp"]),
            event_type="controlOperation",
            action="tap",
            screenshot=screenshot_path,
            mouse={
                "x": mapped_point[0],
                "y": mapped_point[1],
                "button": str(payload.get("button", "Button.left")),
            },
            window=self._build_window_info(capture, payload.get("window")),
            ui_element=self._build_ui_element(node),
            additional_details=self._build_android_details(
                capture,
                node,
                capture_reason="tap",
                action_name="tap",
                desktop_window=payload.get("window"),
                client_rect=payload.get("client_rect"),
                device_point=mapped_point,
                duration_ms=int(payload.get("duration_ms", 0) or 0),
                button=str(payload.get("button", "Button.left") or "Button.left"),
            ),
        )
        self.store.append_event(event)

    def _record_long_press(self, payload: dict[str, Any]) -> None:
        self._emit_debug("开始处理长按事件，准备抓取 Android 截图和页面树")
        capture = self._resolve_pointer_capture(payload, debug_action_name="长按")
        mapped_point = self._map_to_device_point(
            screen_x=int(payload.get("start_screen_x", 0)),
            screen_y=int(payload.get("start_screen_y", 0)),
            client_rect=payload["client_rect"],
            image_size=capture.screenshot.size,
        )
        if mapped_point is None:
            self._emit_debug("长按处理失败：无法把屏幕坐标映射到设备坐标")
            return

        node = self._find_node_at_point(capture.hierarchy_xml, mapped_point[0], mapped_point[1])
        screenshot = self._prepare_event_image(capture.screenshot, node, points=[mapped_point])
        screenshot_path = self.store.save_image(screenshot, "step")
        duration_ms = int(payload.get("duration_ms", 0) or 0)
        self._emit_debug(
            f"长按事件已写入 | device_point=({mapped_point[0]},{mapped_point[1]}) | duration_ms={duration_ms} | screenshot={screenshot_path!r}"
        )
        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=str(payload["timestamp"]),
            event_type="controlOperation",
            action="long_press",
            screenshot=screenshot_path,
            mouse={
                "x": mapped_point[0],
                "y": mapped_point[1],
                "button": str(payload.get("button", "Button.left")),
                "duration_ms": duration_ms,
            },
            window=self._build_window_info(capture, payload.get("window")),
            ui_element=self._build_ui_element(node),
            additional_details=self._build_android_details(
                capture,
                node,
                capture_reason="long_press",
                action_name="long_press",
                desktop_window=payload.get("window"),
                client_rect=payload.get("client_rect"),
                device_point=mapped_point,
                duration_ms=duration_ms,
                button=str(payload.get("button", "Button.left") or "Button.left"),
            ),
        )
        self.store.append_event(event)

    def _record_swipe(self, payload: dict[str, Any]) -> None:
        self._emit_debug("开始处理拖动事件，准备抓取 Android 截图和页面树")
        capture = self.backend.capture_device_state(self.serial)
        start_point = self._map_to_device_point(
            screen_x=int(payload.get("start_screen_x", 0)),
            screen_y=int(payload.get("start_screen_y", 0)),
            client_rect=payload["client_rect"],
            image_size=capture.screenshot.size,
        )
        end_point = self._map_to_device_point(
            screen_x=int(payload.get("end_screen_x", 0)),
            screen_y=int(payload.get("end_screen_y", 0)),
            client_rect=payload["client_rect"],
            image_size=capture.screenshot.size,
        )
        if start_point is None or end_point is None:
            self._emit_debug("拖动处理失败：无法把屏幕坐标映射到设备坐标")
            return

        start_node = self._find_node_at_point(capture.hierarchy_xml, start_point[0], start_point[1])
        end_node = self._find_node_at_point(capture.hierarchy_xml, end_point[0], end_point[1])
        screenshot = self._prepare_event_image(capture.screenshot, start_node, secondary_node=end_node, points=[start_point, end_point])
        screenshot_path = self.store.save_image(screenshot, "step")
        duration_ms = int(payload.get("duration_ms", 0) or 0)
        self._emit_debug(
            f"拖动事件已写入 | start=({start_point[0]},{start_point[1]}) | end=({end_point[0]},{end_point[1]}) | duration_ms={duration_ms} | screenshot={screenshot_path!r}"
        )
        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=str(payload["timestamp"]),
            event_type="mouseAction",
            action="swipe",
            screenshot=screenshot_path,
            mouse={
                "x": start_point[0],
                "y": start_point[1],
                "button": str(payload.get("button", "Button.left")),
                "end_x": end_point[0],
                "end_y": end_point[1],
                "duration_ms": duration_ms,
            },
            window=self._build_window_info(capture, payload.get("release_window") or payload.get("window")),
            ui_element=self._build_ui_element(start_node),
            additional_details=self._build_android_details(
                capture,
                start_node,
                capture_reason="swipe",
                action_name="swipe",
                desktop_window=payload.get("release_window") or payload.get("window"),
                client_rect=payload.get("client_rect"),
                start_point=start_point,
                end_point=end_point,
                secondary_node=end_node,
                duration_ms=duration_ms,
                button=str(payload.get("button", "Button.left") or "Button.left"),
            ),
        )
        self.store.append_event(event)

    def _resolve_pointer_capture(self, payload: dict[str, Any], *, debug_action_name: str) -> AndroidDeviceCapture:
        gesture_state = payload.get("gesture_state")
        if isinstance(gesture_state, _PointerGestureState):
            pre_capture_thread = gesture_state.pre_capture_thread
            if isinstance(pre_capture_thread, threading.Thread) and pre_capture_thread.is_alive():
                pre_capture_thread.join(timeout=0.05)
            if gesture_state.pre_capture is not None:
                self._emit_debug(f"{debug_action_name}事件使用按下瞬间的预抓图")
                return gesture_state.pre_capture
            if gesture_state.pre_capture_error:
                self._emit_debug(f"{debug_action_name}预抓图失败，回退为实时抓取 | error={gesture_state.pre_capture_error}")
        return self.backend.capture_device_state(self.serial)

    def _record_scroll(self, payload: dict[str, Any]) -> None:
        self._emit_debug("开始处理滚轮事件，准备抓取 Android 截图和页面树")
        capture = self.backend.capture_device_state(self.serial)
        mapped_point = self._map_to_device_point(
            screen_x=int(payload["screen_x"]),
            screen_y=int(payload["screen_y"]),
            client_rect=payload["client_rect"],
            image_size=capture.screenshot.size,
        )
        if mapped_point is None:
            self._emit_debug("滚轮处理失败：无法把屏幕坐标映射到设备坐标")
            return

        node = self._find_node_at_point(capture.hierarchy_xml, mapped_point[0], mapped_point[1])
        screenshot = self._prepare_event_image(capture.screenshot, node, points=[mapped_point])
        screenshot_path = self.store.save_image(screenshot, "step")
        self._emit_debug(
            f"滚轮事件已写入 | device_point=({mapped_point[0]},{mapped_point[1]}) | screenshot={screenshot_path!r}"
        )
        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=str(payload["timestamp"]),
            event_type="mouseAction",
            action="scroll",
            screenshot=screenshot_path,
            mouse={"x": mapped_point[0], "y": mapped_point[1]},
            scroll={"dx": int(payload.get("dx", 0) or 0), "dy": int(payload.get("dy", 0) or 0)},
            window=self._build_window_info(capture, payload.get("window")),
            ui_element=self._build_ui_element(node),
            additional_details=self._build_android_details(
                capture,
                node,
                capture_reason="scroll",
                action_name="scroll",
                desktop_window=payload.get("window"),
                client_rect=payload.get("client_rect"),
                device_point=mapped_point,
                scroll_delta=(int(payload.get("dx", 0) or 0), int(payload.get("dy", 0) or 0)),
            ),
        )
        self.store.append_event(event)

    def _record_navigation(self, payload: dict[str, Any], *, action_name: str) -> None:
        self._emit_debug(f"开始处理 {action_name} 事件，准备抓取 Android 截图和页面树")
        capture = self._resolve_pointer_capture(payload, debug_action_name=action_name)
        mapped_point = self._map_to_device_point(
            screen_x=int(payload.get("end_screen_x", payload.get("start_screen_x", 0))),
            screen_y=int(payload.get("end_screen_y", payload.get("start_screen_y", 0))),
            client_rect=payload["client_rect"],
            image_size=capture.screenshot.size,
        )
        node = None
        points: list[tuple[int, int]] = []
        if mapped_point is not None:
            node = self._find_node_at_point(capture.hierarchy_xml, mapped_point[0], mapped_point[1])
            points = [mapped_point]
        screenshot = self._prepare_event_image(capture.screenshot, node, points=points)
        screenshot_path = self.store.save_image(screenshot, "step")
        self._emit_debug(
            f"{action_name} 事件已写入 | screenshot={screenshot_path!r} | device_point={mapped_point!r}"
        )
        mouse_payload: dict[str, Any] = {"button": str(payload.get("button", ""))}
        if mapped_point is not None:
            mouse_payload["x"] = mapped_point[0]
            mouse_payload["y"] = mapped_point[1]
        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=str(payload["timestamp"]),
            event_type="controlOperation",
            action=action_name,
            screenshot=screenshot_path,
            mouse=mouse_payload,
            window=self._build_window_info(capture, payload.get("release_window") or payload.get("window")),
            ui_element=self._build_ui_element(node),
            additional_details=self._build_android_details(
                capture,
                node,
                capture_reason=action_name,
                action_name=action_name,
                desktop_window=payload.get("release_window") or payload.get("window"),
                client_rect=payload.get("client_rect"),
                device_point=mapped_point,
                button=str(payload.get("button", "") or ""),
            ),
        )
        self.store.append_event(event)

    def _record_key(self, payload: dict[str, Any]) -> None:
        self._emit_debug("开始处理按键事件，准备抓取 Android 截图和页面树")
        capture = self.backend.capture_device_state(self.serial)
        node = self._find_focused_node(capture.hierarchy_xml)
        screenshot = self._prepare_event_image(capture.screenshot, node)
        screenshot_path = self.store.save_image(screenshot, "step")
        key_name = str(payload.get("key_name", ""))
        key_char = payload.get("key_char", "")
        text_value = key_char if isinstance(key_char, str) and key_char.isprintable() and key_char else ""
        action_name = "text_input" if text_value else "key_press"
        self._emit_debug(f"按键事件已写入 | action={action_name!r} | key={key_name!r} | screenshot={screenshot_path!r}")
        event = RecordedEvent(
            event_id=self.store.next_event_id(),
            timestamp=str(payload["timestamp"]),
            event_type="input",
            action=action_name,
            screenshot=screenshot_path,
            keyboard={
                "key_name": key_name,
                "char": key_char,
                "text": text_value,
            },
            window=self._build_window_info(capture, payload.get("window")),
            ui_element=self._build_ui_element(node),
            additional_details=self._build_android_details(
                capture,
                node,
                capture_reason="key_press",
                action_name=action_name,
                desktop_window=payload.get("window"),
                key_name=key_name,
                text_value=text_value,
            ),
        )
        self.store.append_event(event)

    def _build_android_details(
        self,
        capture: AndroidDeviceCapture,
        node: _HierarchyNode | None,
        *,
        capture_reason: str,
        action_name: str = "",
        desktop_window: object | None = None,
        client_rect: dict[str, int] | None = None,
        device_point: tuple[int, int] | None = None,
        start_point: tuple[int, int] | None = None,
        end_point: tuple[int, int] | None = None,
        secondary_node: _HierarchyNode | None = None,
        key_name: str = "",
        text_value: str = "",
        duration_ms: int = 0,
        button: str = "",
        scroll_delta: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        window = desktop_window if isinstance(desktop_window, WindowInfo) else WindowInfo()
        payload: dict[str, Any] = {
            "platform": "android",
            "android_serial": self.serial,
            "capture_reason": capture_reason,
            "android_package": capture.package_name,
            "android_activity": capture.activity,
            "scrcpy_window_title": window.title,
            "scrcpy_window_process": window.process_name,
            "device_image_size": {"width": capture.screenshot.width, "height": capture.screenshot.height},
        }
        if action_name:
            payload["android_action"] = action_name
        if client_rect:
            payload["scrcpy_client_rect"] = dict(client_rect)
        if device_point:
            payload["device_point"] = {"x": device_point[0], "y": device_point[1]}
        if start_point:
            payload["start_point"] = {"x": start_point[0], "y": start_point[1]}
        if end_point:
            payload["end_point"] = {"x": end_point[0], "y": end_point[1]}
        if node is not None:
            payload["android_xpath"] = node.xpath
            payload["android_bounds"] = node.bounds
            payload["target_resource_id"] = node.resource_id
            payload["target_content_desc"] = node.content_desc
            payload["target_rectangle"] = dict(node.rectangle)
        if secondary_node is not None:
            payload["android_end_xpath"] = secondary_node.xpath
            payload["android_end_bounds"] = secondary_node.bounds
            payload["target_end_resource_id"] = secondary_node.resource_id
            payload["target_end_content_desc"] = secondary_node.content_desc
            payload["target_end_rectangle"] = dict(secondary_node.rectangle)
        if key_name:
            payload["key_name"] = key_name
        if text_value:
            payload["text_value"] = text_value
        if duration_ms > 0:
            payload["duration_ms"] = duration_ms
        if button:
            payload["pointer_button"] = button
        if scroll_delta is not None:
            payload["scroll_delta"] = {"dx": scroll_delta[0], "dy": scroll_delta[1]}
        u2_hint = self._build_u2_replay_hint(
            action_name=action_name,
            node=node,
            secondary_node=secondary_node,
            device_point=device_point,
            start_point=start_point,
            end_point=end_point,
            duration_ms=duration_ms,
            key_name=key_name,
            text_value=text_value,
            scroll_delta=scroll_delta,
        )
        if u2_hint:
            payload["uiautomator2_hint"] = u2_hint
        return payload

    def _start_pre_capture(self, gesture: _PointerGestureState) -> None:
        def worker() -> None:
            try:
                gesture.pre_capture = self.backend.capture_device_state(self.serial)
            except Exception as exc:
                gesture.pre_capture_error = str(exc) or exc.__class__.__name__

        gesture.pre_capture_thread = threading.Thread(target=worker, daemon=True)
        gesture.pre_capture_thread.start()

    def _build_window_info(self, capture: AndroidDeviceCapture, desktop_window: object | None) -> WindowInfo:
        source_window = desktop_window if isinstance(desktop_window, WindowInfo) else WindowInfo()
        title = " / ".join(item for item in [capture.package_name, capture.activity] if item)
        return WindowInfo(
            title=title or source_window.title,
            class_name="AndroidWindow",
            handle=self.serial,
            process_id=None,
            process_name=capture.package_name or source_window.process_name,
        )

    @staticmethod
    def _build_ui_element(node: _HierarchyNode | None) -> UIElementInfo:
        if node is None:
            return UIElementInfo()
        fallback_names = [value for value in [node.text, node.content_desc, node.resource_id] if value]
        name = next((value for value in fallback_names if value), node.class_name)
        return UIElementInfo(
            name=name,
            control_type=node.class_name,
            automation_id=node.resource_id,
            class_name=node.class_name,
            help_text=node.content_desc,
            rectangle=dict(node.rectangle),
            name_fallbacks=fallback_names,
        )

    @staticmethod
    def _prepare_event_image(
        image: Image.Image,
        node: _HierarchyNode | None,
        secondary_node: _HierarchyNode | None = None,
        points: list[tuple[int, int]] | None = None,
    ) -> Image.Image:
        prepared = image.copy()
        draw = ImageDraw.Draw(prepared)
        AndroidOperationRecorder._draw_node_rectangle(draw, node, outline="#ff2b2b")
        if secondary_node is not None and secondary_node is not node:
            AndroidOperationRecorder._draw_node_rectangle(draw, secondary_node, outline="#ff9800")
        if points:
            if len(points) >= 2:
                draw.line(points, fill="#00bcd4", width=6)
            for index, point in enumerate(points):
                color = "#00e676" if index == 0 else "#ffd54f"
                radius = 14 if index == 0 else 10
                draw.ellipse(
                    (
                        point[0] - radius,
                        point[1] - radius,
                        point[0] + radius,
                        point[1] + radius,
                    ),
                    outline=color,
                    width=4,
                )
        return prepared

    @staticmethod
    def _draw_node_rectangle(draw: ImageDraw.ImageDraw, node: _HierarchyNode | None, *, outline: str) -> None:
        if node is None or not node.rectangle:
            return
        rect = node.rectangle
        padding = 4
        for offset in range(4):
            draw.rectangle(
                (
                    rect["left"] - padding - offset,
                    rect["top"] - padding - offset,
                    rect["right"] + padding + offset,
                    rect["bottom"] + padding + offset,
                ),
                outline=outline,
                width=2,
            )

    @staticmethod
    def _distance_between_points(x1: int, y1: int, x2: int, y2: int) -> float:
        return math.hypot(x2 - x1, y2 - y1)

    @staticmethod
    def _classify_pointer_action(button: mouse.Button, duration_ms: int, move_distance: float) -> str:
        if button == mouse.Button.right:
            return "back"
        if button == mouse.Button.middle:
            return "home"
        if move_distance >= _SWIPE_MOVE_THRESHOLD_PX:
            return "swipe"
        if duration_ms >= _LONG_PRESS_THRESHOLD_MS:
            return "long_press"
        return "click"

    @staticmethod
    def _build_u2_replay_hint(
        *,
        action_name: str,
        node: _HierarchyNode | None,
        secondary_node: _HierarchyNode | None,
        device_point: tuple[int, int] | None,
        start_point: tuple[int, int] | None,
        end_point: tuple[int, int] | None,
        duration_ms: int,
        key_name: str,
        text_value: str,
        scroll_delta: tuple[int, int] | None,
    ) -> dict[str, Any]:
        if action_name == "tap":
            if node is not None and node.xpath:
                return {"method": "xpath.click", "xpath": node.xpath}
            if device_point is not None:
                return {"method": "click", "x": device_point[0], "y": device_point[1]}
        if action_name == "long_press":
            if node is not None and node.xpath:
                return {"method": "xpath.long_click", "xpath": node.xpath, "duration_s": max(duration_ms / 1000.0, 0.5)}
            if device_point is not None:
                return {
                    "method": "long_click",
                    "x": device_point[0],
                    "y": device_point[1],
                    "duration_s": max(duration_ms / 1000.0, 0.5),
                }
        if action_name == "swipe" and start_point is not None and end_point is not None:
            hint: dict[str, Any] = {
                "method": "swipe",
                "start_x": start_point[0],
                "start_y": start_point[1],
                "end_x": end_point[0],
                "end_y": end_point[1],
                "duration_s": max(duration_ms / 1000.0, 0.1),
            }
            if node is not None and node.xpath:
                hint["start_xpath"] = node.xpath
            if secondary_node is not None and secondary_node.xpath:
                hint["end_xpath"] = secondary_node.xpath
            return hint
        if action_name == "scroll":
            hint = {"method": "scroll"}
            if device_point is not None:
                hint["x"] = device_point[0]
                hint["y"] = device_point[1]
            if scroll_delta is not None:
                hint["dx"] = scroll_delta[0]
                hint["dy"] = scroll_delta[1]
            return hint
        if action_name == "back":
            return {"method": "press", "key": "back"}
        if action_name == "home":
            return {"method": "press", "key": "home"}
        if action_name == "text_input" and text_value:
            return {"method": "send_keys", "text": text_value}
        if action_name == "key_press" and key_name:
            return {"method": "press", "key": key_name}
        return {}

    def _is_target_window(self, window: WindowInfo | None) -> bool:
        if window is None:
            return False
        title = (window.title or "").lower()
        process_name = (window.process_name or "").lower()
        if self._target_root_handle is None and self.serial:
            self.refresh_target_window_binding(self.serial)

        if self._looks_like_scrcpy_window(title, process_name):
            return True

        handle_value = self._parse_handle(window.handle)
        if handle_value and self._target_root_handle:
            root_handle = self._resolve_root_handle(handle_value)
            if root_handle == self._target_root_handle:
                return True

        active_window = get_active_window_info()
        active_title = (active_window.title or "").lower()
        active_process = (active_window.process_name or "").lower()
        return self._looks_like_scrcpy_window(active_title, active_process)

    def _get_target_client_rect(self, window: WindowInfo | None) -> dict[str, int] | None:
        if isinstance(window, WindowInfo) and self._looks_like_scrcpy_window(window.title, window.process_name):
            active_rect = self._get_client_rect(window)
            if active_rect is not None:
                handle_value = self._parse_handle(window.handle)
                if handle_value:
                    self._target_root_handle = handle_value
                return active_rect

        if self._target_root_handle is None and self.serial:
            self.refresh_target_window_binding(self.serial)

        if self._target_root_handle:
            return self._get_client_rect_from_handle(self._target_root_handle)

        if isinstance(window, WindowInfo):
            return self._get_client_rect(window)
        return None

    @staticmethod
    def _normalize_key(key: keyboard.Key | keyboard.KeyCode) -> str:
        if hasattr(key, "char") and key.char:
            return str(key.char)
        return str(key).replace("Key.", "")

    @staticmethod
    def _is_modifier_key(key_name: str) -> bool:
        return key_name.lower() in {"ctrl", "ctrl_l", "ctrl_r", "shift", "shift_l", "shift_r", "alt", "alt_l", "alt_r", "cmd", "cmd_l", "cmd_r"}

    @staticmethod
    def _get_client_rect(window: WindowInfo) -> dict[str, int] | None:
        if win32gui is None or not window.handle:
            return None
        try:
            handle = AndroidOperationRecorder._parse_handle(str(window.handle))
            if not handle:
                return None
            return AndroidOperationRecorder._get_client_rect_from_handle(handle)
        except Exception:
            return None

    @staticmethod
    def _get_client_rect_from_handle(handle: int) -> dict[str, int] | None:
        if win32gui is None or not handle:
            return None
        try:
            left_top = win32gui.ClientToScreen(handle, (0, 0))
            client_rect = win32gui.GetClientRect(handle)
            width = int(client_rect[2] - client_rect[0])
            height = int(client_rect[3] - client_rect[1])
            if width <= 0 or height <= 0:
                return None
            return {
                "left": int(left_top[0]),
                "top": int(left_top[1]),
                "right": int(left_top[0] + width),
                "bottom": int(left_top[1] + height),
                "width": width,
                "height": height,
            }
        except Exception:
            return None

    @staticmethod
    def _map_to_device_point(
        *,
        screen_x: int,
        screen_y: int,
        client_rect: dict[str, int],
        image_size: tuple[int, int],
    ) -> tuple[int, int] | None:
        client_width = int(client_rect.get("width", 0) or 0)
        client_height = int(client_rect.get("height", 0) or 0)
        if client_width <= 0 or client_height <= 0:
            return None

        image_width, image_height = image_size
        if image_width <= 0 or image_height <= 0:
            return None

        scale = min(client_width / image_width, client_height / image_height)
        if scale <= 0:
            return None

        rendered_width = image_width * scale
        rendered_height = image_height * scale
        offset_x = (client_width - rendered_width) / 2.0
        offset_y = (client_height - rendered_height) / 2.0
        client_x = screen_x - int(client_rect.get("left", 0) or 0)
        client_y = screen_y - int(client_rect.get("top", 0) or 0)
        if client_x < offset_x or client_y < offset_y:
            return None
        if client_x > offset_x + rendered_width or client_y > offset_y + rendered_height:
            return None

        device_x = int(round((client_x - offset_x) / scale))
        device_y = int(round((client_y - offset_y) / scale))
        device_x = max(0, min(image_width - 1, device_x))
        device_y = max(0, min(image_height - 1, device_y))
        return device_x, device_y

    @classmethod
    def _find_node_at_point(cls, hierarchy_xml: str, x: int, y: int) -> _HierarchyNode | None:
        try:
            root = ET.fromstring(hierarchy_xml)
        except Exception:
            return None

        matches: list[tuple[int, int, _HierarchyNode]] = []

        def visit(element: ET.Element, depth: int, path: str) -> None:
            children = [child for child in list(element) if child.tag == "node"]
            for index, child in enumerate(children, start=1):
                class_name = str(child.attrib.get("class", "") or "node")
                child_path = f"{path}/{class_name}[{index}]"
                rectangle = cls._parse_bounds(str(child.attrib.get("bounds", "") or ""))
                if rectangle and cls._point_in_rect(x, y, rectangle):
                    area = max(1, (rectangle["right"] - rectangle["left"]) * (rectangle["bottom"] - rectangle["top"]))
                    matches.append((depth, area, cls._build_node(child, rectangle, child_path)))
                visit(child, depth + 1, child_path)

        visit(root, 0, "")
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        return matches[0][2]

    @classmethod
    def _find_focused_node(cls, hierarchy_xml: str) -> _HierarchyNode | None:
        try:
            root = ET.fromstring(hierarchy_xml)
        except Exception:
            return None

        focused_matches: list[tuple[int, _HierarchyNode]] = []

        def visit(element: ET.Element, depth: int, path: str) -> None:
            children = [child for child in list(element) if child.tag == "node"]
            for index, child in enumerate(children, start=1):
                class_name = str(child.attrib.get("class", "") or "node")
                child_path = f"{path}/{class_name}[{index}]"
                rectangle = cls._parse_bounds(str(child.attrib.get("bounds", "") or ""))
                if str(child.attrib.get("focused", "false")).lower() == "true":
                    focused_matches.append((depth, cls._build_node(child, rectangle, child_path)))
                visit(child, depth + 1, child_path)

        visit(root, 0, "")
        if not focused_matches:
            return None
        focused_matches.sort(key=lambda item: item[0], reverse=True)
        return focused_matches[0][1]

    @staticmethod
    def _build_node(element: ET.Element, rectangle: dict[str, int] | None, xpath: str) -> _HierarchyNode:
        return _HierarchyNode(
            text=str(element.attrib.get("text", "") or ""),
            resource_id=str(element.attrib.get("resource-id", "") or ""),
            content_desc=str(element.attrib.get("content-desc", "") or ""),
            class_name=str(element.attrib.get("class", "") or ""),
            bounds=str(element.attrib.get("bounds", "") or ""),
            rectangle=rectangle or {},
            xpath=xpath or "/",
            focused=str(element.attrib.get("focused", "false")).lower() == "true",
        )

    @staticmethod
    def _parse_bounds(raw_bounds: str) -> dict[str, int] | None:
        if not raw_bounds:
            return None
        try:
            left_part, right_part = raw_bounds.split("][")
            left_text = left_part.lstrip("[")
            right_text = right_part.rstrip("]")
            left, top = [int(value) for value in left_text.split(",", 1)]
            right, bottom = [int(value) for value in right_text.split(",", 1)]
        except Exception:
            return None
        if right <= left or bottom <= top:
            return None
        return {"left": left, "top": top, "right": right, "bottom": bottom}

    @staticmethod
    def _point_in_rect(x: int, y: int, rectangle: dict[str, int]) -> bool:
        return rectangle["left"] <= x <= rectangle["right"] and rectangle["top"] <= y <= rectangle["bottom"]

    def _resolve_scrcpy_root_handle(self, serial: str, allow_generic_fallback: bool = True) -> int | None:
        if win32gui is None:
            return None

        exact_title = f"Recorder Android Mirror [{serial}]".lower()
        generic_titles = {"scrcpy", f"scrcpy - {serial.lower()}"}
        matches: list[int] = []
        fallback_matches: list[tuple[int, str]] = []

        def callback(handle: int, _lparam: int) -> bool:
            try:
                if not win32gui.IsWindowVisible(handle):
                    return True
                title = (win32gui.GetWindowText(handle) or "").strip().lower()
            except Exception:
                return True
            process_name = ""
            try:
                import psutil  # local import to keep module import light
                import win32process  # type: ignore
                _, process_id = win32process.GetWindowThreadProcessId(handle)
                if process_id:
                    process_name = psutil.Process(process_id).name().lower()
            except Exception:
                process_name = ""
            if title == exact_title or (serial.lower() in title and "recorder android mirror" in title):
                matches.append(handle)
            elif allow_generic_fallback and (title in generic_titles or title.startswith("scrcpy")):
                fallback_matches.append((handle, process_name))
            return True

        try:
            win32gui.EnumWindows(callback, 0)
        except Exception:
            return None
        if matches:
            return matches[0]
        if not fallback_matches:
            return None

        scrcpy_process_matches = [handle for handle, process_name in fallback_matches if "scrcpy" in process_name]
        if scrcpy_process_matches:
            return scrcpy_process_matches[0]
        return fallback_matches[0][0]

    @staticmethod
    def _resolve_root_handle(handle: int) -> int:
        if win32gui is None:
            return handle
        current = handle
        visited: set[int] = set()
        while current and current not in visited:
            visited.add(current)
            try:
                parent = win32gui.GetParent(current)
            except Exception:
                break
            if not parent:
                break
            current = parent
        return current

    @staticmethod
    def _parse_handle(value: object) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.lower().startswith("0x"):
                return int(text, 16)
            return int(text)
        except Exception:
            return None

    def _looks_like_scrcpy_window(self, title: str, process_name: str) -> bool:
        normalized_title = (title or "").strip().lower()
        normalized_process = (process_name or "").strip().lower()
        if normalized_title == f"recorder android mirror [{self.serial.lower()}]":
            return True
        if normalized_title == "scrcpy" or normalized_title.startswith("scrcpy"):
            return True
        if self.serial and self.serial.lower() in normalized_title and "recorder android mirror" in normalized_title:
            return True
        if "scrcpy" in normalized_process:
            return True
        return False

    def _emit_debug(self, message: str) -> None:
        self.status_callback(f"[AndroidDebug] {message}")