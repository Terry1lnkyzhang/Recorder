from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import imageio
import mss
import numpy as np
from PIL import Image, ImageGrab, ImageTk


@dataclass(slots=True)
class RegionSelection:
    left: int
    top: int
    right: int
    bottom: int
    image: Image.Image

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def to_region_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
        }


def get_virtual_screen_bounds() -> tuple[int, int, int, int]:
    user32 = __import__("ctypes").windll.user32
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    return left, top, width, height


class RegionSelector:
    def __init__(self, parent: tk.Misc, title: str) -> None:
        self.parent = parent
        self.title = title
        self.result: RegionSelection | None = None
        self._start_x = 0
        self._start_y = 0
        self._rect_id: int | None = None

        left, top, width, height = get_virtual_screen_bounds()
        self._bounds = (left, top, width, height)
        self._full_image = ImageGrab.grab(all_screens=True, bbox=(left, top, left + width, top + height))

        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(f"{width}x{height}{left:+d}{top:+d}")
        self.window.configure(bg="black")

        self.canvas = tk.Canvas(self.window, cursor="crosshair", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._photo = ImageTk.PhotoImage(self._full_image)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self.canvas.create_text(
            20,
            20,
            anchor=tk.NW,
            text=f"{title} | 鼠标拖拽选择区域 | Esc 取消",
            fill="#ffffff",
            font=("Segoe UI", 14, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.window.bind("<Escape>", self._on_cancel)
        self.window.focus_force()
        self.window.grab_set()

    def wait(self) -> RegionSelection | None:
        self.parent.wait_window(self.window)
        return self.result

    def _on_press(self, event: tk.Event) -> None:
        self._start_x = event.x
        self._start_y = event.y
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#00e5ff",
            width=3,
        )

    def _on_drag(self, event: tk.Event) -> None:
        if not self._rect_id:
            return
        self.canvas.coords(self._rect_id, self._start_x, self._start_y, event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        if not self._rect_id:
            return

        left = min(self._start_x, event.x)
        top = min(self._start_y, event.y)
        right = max(self._start_x, event.x)
        bottom = max(self._start_y, event.y)
        if right - left < 5 or bottom - top < 5:
            return

        screen_left, screen_top, _, _ = self._bounds
        absolute_bbox = (screen_left + left, screen_top + top, screen_left + right, screen_top + bottom)
        cropped = self._full_image.crop((left, top, right, bottom))
        self.result = RegionSelection(
            left=absolute_bbox[0],
            top=absolute_bbox[1],
            right=absolute_bbox[2],
            bottom=absolute_bbox[3],
            image=cropped,
        )
        self.window.grab_release()
        self.window.destroy()

    def _on_cancel(self, _event: tk.Event) -> None:
        self.window.grab_release()
        self.window.destroy()


def select_region(parent: tk.Misc, title: str) -> RegionSelection | None:
    selector = RegionSelector(parent, title)
    return selector.wait()


def load_video_preview_frame(video_path: Path) -> Image.Image | None:
    try:
        reader = imageio.get_reader(video_path)
        try:
            frame = reader.get_data(0)
            return Image.fromarray(frame)
        finally:
            reader.close()
    except Exception:
        return None


class RegionVideoRecorder:
    def __init__(
        self,
        output_path: Path,
        region: dict[str, int],
        fps: int = 5,
        preview_callback=None,
    ) -> None:
        self.output_path = output_path
        self.region = region
        self.fps = max(1, fps)
        self.preview_callback = preview_callback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_recording = False
        self.frame_count = 0
        self.duration_seconds = 0.0

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def start(self) -> None:
        if self._is_recording:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()
        self._is_recording = True

    def stop(self) -> Path:
        if not self._is_recording:
            return self.output_path
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._is_recording = False
        return self.output_path

    def _record_loop(self) -> None:
        started_at = time.perf_counter()
        monitor = {
            "left": self.region["left"],
            "top": self.region["top"],
            "width": self.region["width"],
            "height": self.region["height"],
        }
        frame_interval = 1.0 / self.fps

        with mss.mss() as sct, imageio.get_writer(self.output_path, fps=self.fps, codec="libx264") as writer:
            while not self._stop_event.is_set():
                frame_start = time.perf_counter()
                shot = sct.grab(monitor)
                frame = np.asarray(shot)[:, :, :3][:, :, ::-1]
                writer.append_data(frame)
                self.frame_count += 1
                if self.preview_callback and self.frame_count % 2 == 0:
                    self.preview_callback(Image.fromarray(frame))
                elapsed = time.perf_counter() - frame_start
                if elapsed < frame_interval:
                    time.sleep(frame_interval - elapsed)

        self.duration_seconds = time.perf_counter() - started_at