from __future__ import annotations

import json
import re
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from src.common.app_logging import configure_app_logging, get_logger, install_global_exception_logging
from src.common.runtime_paths import get_recordings_dir, get_settings_path
from .dialogs import (
    AICheckpointDraft,
    SessionMetadataDraft,
    capture_manual_screenshot,
    open_ai_checkpoint_dialog,
    open_comment_dialog,
    open_session_metadata_dialog,
    open_settings_dialog,
    open_wait_for_image_dialog,
)
from .recorder import RecorderEngine
from .settings import SettingsStore
from src.viewer.window import open_viewer_window


class DesignStepsOverlay:
    def __init__(self, parent: tk.Misc) -> None:
        self.parent = parent
        self._expanded_size = (520, 220)
        self._all_steps_size = (520, 360)
        self._collapsed_size = (420, 44)
        self._collapsed = False
        self._show_all_steps = False
        self._manual_position: tuple[int, int] | None = None
        self._drag_offset = (0, 0)
        self._steps: list[str] = []
        self._current_step_index = 0
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-alpha", 0.88)
        except tk.TclError:
            pass
        try:
            self.window.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.window.configure(bg="#d7caa3")

        outer = tk.Frame(self.window, bg="#d7caa3", bd=1, relief=tk.SOLID)
        outer.pack(fill=tk.BOTH, expand=True)
        self.outer = outer

        header = tk.Frame(outer, bg="#efe4bd", height=36)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        self.header = header

        title = tk.Label(
            header,
            text="Design Steps",
            bg="#efe4bd",
            fg="#2f2a1f",
            font=("Segoe UI", 11, "bold"),
            anchor=tk.W,
            padx=12,
        )
        title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.title_label = title

        self.toggle_button = tk.Button(
            header,
            text="－",
            command=self.toggle_collapsed,
            bg="#efe4bd",
            fg="#2f2a1f",
            activebackground="#e5d8aa",
            activeforeground="#2f2a1f",
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", 11, "bold"),
            width=3,
            cursor="hand2",
        )
        self.toggle_button.pack(side=tk.RIGHT, padx=(0, 4), pady=4)

        self.mode_button = tk.Button(
            header,
            text="All Steps",
            command=self.toggle_steps_mode,
            bg="#efe4bd",
            fg="#2f2a1f",
            activebackground="#e5d8aa",
            activeforeground="#2f2a1f",
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", 9, "bold"),
            padx=8,
            cursor="hand2",
        )
        self.mode_button.pack(side=tk.RIGHT, padx=(0, 4), pady=4)

        for widget in (header, title):
            widget.bind("<ButtonPress-1>", self._start_drag, add="+")
            widget.bind("<B1-Motion>", self._drag_window, add="+")

        body = tk.Frame(outer, bg="#fffaf0")
        body.pack(fill=tk.BOTH, expand=True)
        self.body = body
        body.columnconfigure(1, weight=1)

        self.previous_button = tk.Button(
            body,
            text="◀",
            command=self.show_previous_step,
            bg="#fffaf0",
            fg="#2f2a1f",
            activebackground="#f2ead8",
            activeforeground="#2f2a1f",
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", 16, "bold"),
            width=3,
            cursor="hand2",
        )
        self.previous_button.grid(row=0, column=0, sticky="ns", padx=(8, 0), pady=8)

        content = tk.Frame(body, bg="#fffaf0")
        content.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        self.content_frame = content

        self.step_index_var = tk.StringVar(value="1 / 1")
        self.step_text_var = tk.StringVar(value="当前 Session 未填写 Design Steps。")

        self.step_index_label = tk.Label(
            content,
            textvariable=self.step_index_var,
            bg="#fffaf0",
            fg="#7a6c4d",
            font=("Segoe UI", 9, "bold"),
            anchor=tk.CENTER,
        )
        self.step_index_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.step_message = tk.Message(
            content,
            textvariable=self.step_text_var,
            bg="#fffaf0",
            fg="#2f2a1f",
            font=("Segoe UI", 11),
            width=360,
            justify=tk.LEFT,
            anchor=tk.NW,
            padx=6,
            pady=6,
        )
        self.step_message.grid(row=1, column=0, sticky="nsew")

        self.next_button = tk.Button(
            body,
            text="▶",
            command=self.show_next_step,
            bg="#fffaf0",
            fg="#2f2a1f",
            activebackground="#f2ead8",
            activeforeground="#2f2a1f",
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", 16, "bold"),
            width=3,
            cursor="hand2",
        )
        self.next_button.grid(row=0, column=2, sticky="ns", padx=(0, 8), pady=8)

    def show(self, design_steps: str) -> None:
        self._steps = self._split_design_steps(design_steps)
        self._current_step_index = 0
        self._render_current_step()
        self._position_window()
        self.window.deiconify()
        self.window.lift()

    def hide(self) -> None:
        if self.window.winfo_exists():
            self.window.withdraw()

    def destroy(self) -> None:
        if self.window.winfo_exists():
            self.window.destroy()

    def toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.body.pack_forget()
            self.toggle_button.configure(text="＋")
        else:
            self.body.pack(fill=tk.BOTH, expand=True)
            self.toggle_button.configure(text="－")
        self._position_window()

    def toggle_steps_mode(self) -> None:
        self._show_all_steps = not self._show_all_steps
        self._render_current_step()
        self._position_window()

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root, event.y_root)

    def _drag_window(self, event: tk.Event) -> None:
        current_x = self.window.winfo_x()
        current_y = self.window.winfo_y()
        delta_x = event.x_root - self._drag_offset[0]
        delta_y = event.y_root - self._drag_offset[1]
        new_x = max(0, current_x + delta_x)
        new_y = max(0, current_y + delta_y)
        self.window.geometry(f"+{new_x}+{new_y}")
        self._manual_position = (new_x, new_y)
        self._drag_offset = (event.x_root, event.y_root)

    def show_previous_step(self) -> None:
        if self._current_step_index <= 0:
            return
        self._current_step_index -= 1
        self._render_current_step()

    def show_next_step(self) -> None:
        if self._current_step_index >= len(self._steps) - 1:
            return
        self._current_step_index += 1
        self._render_current_step()

    def _render_current_step(self) -> None:
        if not self._steps:
            self._steps = ["当前 Session 未填写 Design Steps。"]
            self._current_step_index = 0

        total = len(self._steps)
        self._current_step_index = min(max(self._current_step_index, 0), total - 1)
        if self._show_all_steps:
            self.step_text_var.set("\n\n".join(self._steps))
            self.step_index_var.set(f"All Steps · {total} 条")
            self.previous_button.configure(state=tk.DISABLED)
            self.next_button.configure(state=tk.DISABLED)
            self.mode_button.configure(text="Single Step")
            self.step_message.configure(width=420)
            return

        current_text = self._steps[self._current_step_index]
        self.step_text_var.set(current_text)
        self.step_index_var.set(f"{self._current_step_index + 1} / {total}")
        self.previous_button.configure(state=tk.NORMAL if self._current_step_index > 0 else tk.DISABLED)
        self.next_button.configure(state=tk.NORMAL if self._current_step_index < total - 1 else tk.DISABLED)
        self.mode_button.configure(text="All Steps")
        self.step_message.configure(width=360)

    def _split_design_steps(self, design_steps: str) -> list[str]:
        normalized = (design_steps or "").replace("\r\n", "\n").strip()
        if not normalized:
            return ["当前 Session 未填写 Design Steps。"]

        numbered_steps = self._split_by_number_prefix(normalized)
        if numbered_steps:
            return numbered_steps

        line_steps = [line.strip() for line in re.split(r"\n+", normalized) if line.strip()]
        if len(line_steps) > 1:
            return line_steps

        return [normalized]

    @staticmethod
    def _split_by_number_prefix(text: str) -> list[str]:
        line_matches = [
            match.group(1).strip()
            for match in re.finditer(r"(?ms)(?:^|\n)\s*(\d+\.\s*.*?)(?=(?:\n\s*\d+\.)|\Z)", text)
        ]
        if len(line_matches) > 1:
            return line_matches

        inline_text = re.sub(r"\s+", " ", text).strip()
        inline_matches = [
            match.group(1).strip()
            for match in re.finditer(r"(?s)(\d+\.\s*.*?)(?=(?:\s+\d+\.)|\Z)", inline_text)
        ]
        if len(inline_matches) > 1:
            return inline_matches

        return []

    def _position_window(self) -> None:
        if self._collapsed:
            width, height = self._collapsed_size
        elif self._show_all_steps:
            width, height = self._all_steps_size
        else:
            width, height = self._expanded_size
        margin_x = 24
        margin_y = 24
        if self._manual_position is not None:
            x, y = self._manual_position
        else:
            screen_width = self.window.winfo_screenwidth()
            x = max(0, screen_width - width - margin_x)
            y = margin_y
        self.window.geometry(f"{width}x{height}+{x}+{y}")


class RecorderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Automation Recorder MVP")
        self.root.geometry("840x470")
        self.root.minsize(760, 420)
        self.logger = get_logger("app")

        output_dir = get_recordings_dir()
        self.settings_store = SettingsStore(get_settings_path())
        self.engine = RecorderEngine(
            output_dir=output_dir,
            status_callback=self._set_status,
            settings_store=self.settings_store,
            ai_checkpoint_request_callback=self._request_ai_checkpoint_from_shortcut,
            manual_screenshot_request_callback=self._request_manual_screenshot_from_shortcut,
        )

        self.status_var = tk.StringVar(value="Ready")
        self.session_var = tk.StringVar(value="未开始录制")
        self.output_var = tk.StringVar(value=str(output_dir))
        self.last_session_dir: Path | None = None
        self.stop_in_progress = False
        self.save_in_progress = False
        self.import_in_progress = False
        self.ai_checkpoint_draft = AICheckpointDraft()
        self.session_metadata_draft = SessionMetadataDraft()
        self._checkpoint_dialog_open = False
        self._manual_screenshot_in_progress = False
        self.design_steps_overlay = DesignStepsOverlay(self.root)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_root_close)
        self.logger.info("Recorder UI initialized | output_dir=%s", output_dir)

    def _build_ui(self) -> None:
        wrapper = ttk.Frame(self.root, padding=20)
        wrapper.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(wrapper, text="Automation Recorder", font=("Segoe UI", 18, "bold"))
        title.pack(anchor=tk.W)

        desc = ttk.Label(
            wrapper,
            text="录制人工操作、截图和附加上下文，为后续自动化脚本 YAML 转换做准备。",
            wraplength=560,
        )
        desc.pack(anchor=tk.W, pady=(8, 16))

        info_frame = ttk.LabelFrame(wrapper, text="Session")
        info_frame.pack(fill=tk.X)
        ttk.Label(info_frame, text="状态:").grid(row=0, column=0, sticky=tk.W, padx=12, pady=8)
        ttk.Label(info_frame, textvariable=self.session_var).grid(row=0, column=1, sticky=tk.W, padx=8, pady=8)
        ttk.Label(info_frame, text="输出目录:").grid(row=1, column=0, sticky=tk.W, padx=12, pady=8)
        ttk.Label(info_frame, textvariable=self.output_var, wraplength=420).grid(row=1, column=1, sticky=tk.W, padx=8, pady=8)

        button_frame = ttk.LabelFrame(wrapper, text="操作")
        button_frame.pack(fill=tk.X, pady=20)

        primary_actions = ttk.Frame(button_frame, padding=(12, 10, 12, 6))
        primary_actions.pack(fill=tk.X)

        secondary_actions = ttk.Frame(button_frame, padding=(12, 0, 12, 10))
        secondary_actions.pack(fill=tk.X)

        self.start_button = ttk.Button(primary_actions, text="开始录制", command=self.start_recording)
        self.start_button.pack(side=tk.LEFT)

        self.import_button = ttk.Button(primary_actions, text="导入并续录", command=self.import_and_continue_recording)
        self.import_button.pack(side=tk.LEFT, padx=(10, 0))

        self.stop_button = ttk.Button(primary_actions, text="停止录制", command=self.stop_recording, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))

        self.save_button = ttk.Button(primary_actions, text="保存", command=self.save_recording, state=tk.DISABLED)
        self.save_button.pack(side=tk.LEFT, padx=(10, 0))

        self.pause_resume_button = ttk.Button(primary_actions, text="暂停录制", command=self.toggle_pause_resume, state=tk.DISABLED)
        self.pause_resume_button.pack(side=tk.LEFT, padx=(10, 0))

        self.comment_button = ttk.Button(secondary_actions, text="添加 Comment", command=self.add_comment, state=tk.DISABLED)
        self.comment_button.pack(side=tk.LEFT)

        self.wait_button = ttk.Button(secondary_actions, text="添加等待事件", command=self.add_wait_for_image, state=tk.DISABLED)
        self.wait_button.pack(side=tk.LEFT, padx=(10, 0))

        self.screenshot_button = ttk.Button(secondary_actions, text="记录截图", command=self.capture_manual_screenshot, state=tk.DISABLED)
        self.screenshot_button.pack(side=tk.LEFT, padx=(10, 0))

        self.checkpoint_button = ttk.Button(
            secondary_actions,
            text="添加 AI Checkpoint",
            command=self.add_checkpoint,
            state=tk.DISABLED,
        )
        self.checkpoint_button.pack(side=tk.LEFT, padx=(10, 0))

        self.viewer_button = ttk.Button(secondary_actions, text="查看录制内容", command=self.open_viewer)
        self.viewer_button.pack(side=tk.LEFT, padx=(10, 0))

        self.settings_button = ttk.Button(secondary_actions, text="Settings", command=self.open_settings)
        self.settings_button.pack(side=tk.LEFT, padx=(10, 0))

        notes_frame = ttk.LabelFrame(wrapper, text="说明")
        notes_frame.pack(fill=tk.BOTH, expand=True)
        notes_text = (
            "1. 点击开始录制后，会先填写本次录制的 Session 元数据，再开始监听键盘、鼠标点击和滚轮事件。\n"
            "2. Comment 通过鼠标拖拽选择截图区域，再填写大文本说明。\n"
            "3. 等待事件支持框选等待区域并自动保存截图，当前第一版用于记录等待图片出现的步骤。\n"
            "4. 记录截图支持手动选区并保存到当前 Session 的 screenshots，可通过 Ctrl+F4 快捷键快速触发。\n"
            "5. AI Checkpoint 支持两张截图、区域视频录制、Query 调模型并保存返回内容，也支持 Ctrl+F5 快捷键快速打开。\n"
            "6. 可手动点击保存，立即将当前 session 快照和 suggestions 落盘。\n"
            "7. 支持暂停/继续录制，以及导入已有 session 后继续录制。\n"
            "8. 停止录制会在后台收尾，不再阻塞整个窗口。\n"
            "9. Session 元数据在录制完成后也可以在 Session Viewer 中继续修改。"
        )
        ttk.Label(notes_frame, text=notes_text, justify=tk.LEFT, wraplength=760).pack(anchor=tk.W, padx=12, pady=12)

        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def start_recording(self) -> None:
        if self.stop_in_progress or self.save_in_progress or self.import_in_progress:
            self.logger.info("Start recording ignored because another operation is in progress")
            return

        metadata_draft = open_session_metadata_dialog(self.root, self.session_metadata_draft, self.settings_store)
        if metadata_draft is None:
            self.logger.info("Start recording cancelled in session metadata dialog")
            self._set_status("已取消开始录制")
            return

        self.session_metadata_draft = metadata_draft
        self.logger.info(
            "Start recording requested | prs=%s | testcase_id=%s | name=%s | scope=%s",
            metadata_draft.is_prs_recording,
            metadata_draft.testcase_id,
            metadata_draft.name,
            metadata_draft.scope,
        )
        message = self.engine.start(metadata=metadata_draft.to_dict())
        self.last_session_dir = self.engine.store.session_dir
        self._show_design_steps_overlay(metadata_draft.design_steps)
        self._set_active_session_text("录制中")
        self._refresh_controls()
        self._set_status(message)

    def import_and_continue_recording(self) -> None:
        if self.engine.is_recording or self.stop_in_progress or self.save_in_progress or self.import_in_progress:
            self.logger.info("Import-and-continue ignored because recorder is busy")
            return

        session_dir = self._prompt_session_to_continue()
        if session_dir is None:
            self.logger.info("Import-and-continue cancelled before session selection")
            return

        self.logger.info("Import-and-continue requested | session_dir=%s", session_dir)
        self.import_in_progress = True
        self._refresh_controls()
        self.session_var.set(f"导入中: {session_dir.name}")
        self._set_status("正在导入已有录制内容，请稍候...")

        def worker() -> None:
            try:
                message = self.engine.continue_recording(session_dir)
            except Exception as exc:
                self.logger.exception("Import-and-continue failed | session_dir=%s", session_dir)
                self.root.after(0, lambda: self._on_import_failed(str(exc)))
                return
            self.root.after(0, lambda: self._on_import_success(session_dir, message))

        threading.Thread(target=worker, daemon=True).start()

    def stop_recording(self) -> None:
        if self.stop_in_progress:
            self.logger.info("Stop recording ignored because stop is already in progress")
            return

        self.logger.info("Stop recording requested")
        self.stop_in_progress = True
        self._refresh_controls()
        self._set_status("正在停止录制并等待后台任务落盘...")
        self.session_var.set("停止中...")

        def worker() -> None:
            try:
                session_dir, suggestions_path = self.engine.stop()
            except RuntimeError as exc:
                self.logger.exception("Stop recording failed")
                self.root.after(0, lambda: self._on_stop_failed(str(exc)))
                return
            self.root.after(0, lambda: self._on_stop_success(session_dir, suggestions_path))

        threading.Thread(target=worker, daemon=True).start()

    def save_recording(self) -> None:
        if not self.engine.is_recording or self.stop_in_progress or self.save_in_progress:
            self.logger.info("Save snapshot ignored because recorder is not ready")
            return

        self.logger.info("Save snapshot requested")
        self.save_in_progress = True
        self._refresh_controls()
        self._set_status("正在保存当前录制快照...")

        def worker() -> None:
            try:
                session_dir, suggestions_path = self.engine.save_snapshot()
            except RuntimeError as exc:
                self.logger.exception("Save snapshot failed")
                self.root.after(0, lambda: self._on_save_failed(str(exc)))
                return
            self.root.after(0, lambda: self._on_save_success(session_dir, suggestions_path))

        threading.Thread(target=worker, daemon=True).start()

    def toggle_pause_resume(self) -> None:
        if not self.engine.is_recording or self.stop_in_progress or self.save_in_progress:
            self.logger.info("Pause/resume ignored because recorder is not ready")
            return

        try:
            if self.engine.is_paused:
                self.logger.info("Resume recording requested")
                message = self.engine.resume_recording()
                self._set_active_session_text("录制中")
            else:
                self.logger.info("Pause recording requested")
                message = self.engine.pause_recording()
                self._set_active_session_text("已暂停")
        except RuntimeError as exc:
            self.logger.exception("Pause/resume failed")
            messagebox.showerror("操作失败", str(exc), parent=self.root)
            return

        self._refresh_controls()
        self._set_status(message)

    def add_comment(self) -> None:
        if not self.engine.is_recording:
            self.logger.info("Add comment ignored because recorder is not running")
            return
        self.logger.info("Add comment dialog opened")
        self.engine.suspend()
        try:
            open_comment_dialog(self.root, self.engine)
        finally:
            self.engine.resume()
            self.logger.info("Add comment dialog closed")

    def add_wait_for_image(self) -> None:
        if not self.engine.is_recording:
            self.logger.info("Add wait-for-image ignored because recorder is not running")
            return
        self.logger.info("Add wait-for-image dialog opened")
        self.engine.suspend()
        try:
            open_wait_for_image_dialog(self.root, self.engine)
        finally:
            self.engine.resume()
            self.logger.info("Add wait-for-image dialog closed")

    def add_checkpoint(self) -> None:
        if not self.engine.is_recording:
            self.logger.info("Add checkpoint ignored because recorder is not running")
            return
        if self._checkpoint_dialog_open:
            self.logger.info("Add checkpoint ignored because AI checkpoint dialog is already open")
            return
        self.logger.info("Add AI checkpoint dialog opened")
        self._checkpoint_dialog_open = True
        self.engine.suspend()
        try:
            open_ai_checkpoint_dialog(self.root, self.engine, self.settings_store, self.ai_checkpoint_draft)
        finally:
            self.engine.resume()
            self._checkpoint_dialog_open = False
            self.logger.info("Add AI checkpoint dialog closed")

    def capture_manual_screenshot(self) -> None:
        if not self.engine.is_recording:
            self.logger.info("Manual screenshot ignored because recorder is not running")
            return
        if self._manual_screenshot_in_progress:
            self.logger.info("Manual screenshot ignored because capture is already in progress")
            return
        self.logger.info("Manual screenshot capture opened")
        self._manual_screenshot_in_progress = True
        self.engine.suspend()
        try:
            self.root.iconify()
            relative_path = capture_manual_screenshot(self.root, self.engine, "选择要保存到历史截图的区域")
            if relative_path:
                self._set_status(f"已保存截图: {relative_path}")
            else:
                self._set_status("已取消记录截图")
        finally:
            self.engine.resume()
            self._manual_screenshot_in_progress = False
            self.logger.info("Manual screenshot capture closed")

    def _request_ai_checkpoint_from_shortcut(self) -> None:
        self.root.after(0, self.add_checkpoint)

    def _request_manual_screenshot_from_shortcut(self) -> None:
        self.root.after(0, self.capture_manual_screenshot)

    def _set_status(self, message: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self.status_var.set(message)
            return
        self.root.after(0, lambda: self.status_var.set(message))

    def open_viewer(self) -> None:
        initial_dir = self.last_session_dir or Path(self.output_var.get())
        self.logger.info("Open viewer requested | initial_dir=%s", initial_dir)
        open_viewer_window(self.root, initial_dir)

    def open_settings(self) -> None:
        self.logger.info("Open settings requested")
        open_settings_dialog(self.root, self.settings_store)
        self.engine.reload_capture_filters()
        if self.engine.is_recording:
            self._set_status("已更新录制排除规则")
        self.logger.info("Settings dialog closed")

    def _on_stop_success(self, session_dir: Path, suggestions_path: Path) -> None:
        self.stop_in_progress = False
        self._hide_design_steps_overlay()
        self.session_var.set(f"已停止: {session_dir.name}")
        self.last_session_dir = session_dir
        self._refresh_controls()
        self._set_status(f"已输出: {session_dir} | 建议文件: {suggestions_path.name}")
        self.logger.info("Stop recording completed | session_dir=%s | suggestions=%s", session_dir, suggestions_path)
        messagebox.showinfo(
            "录制完成",
            f"录制结果已保存到:\n{session_dir}\n\n复用建议文件:\n{suggestions_path}",
        )

    def _on_stop_failed(self, message: str) -> None:
        self.stop_in_progress = False
        self._refresh_controls()
        self.logger.error("Stop recording failed | message=%s", message)
        messagebox.showerror("Stop failed", message)

    def _on_save_success(self, session_dir: Path, suggestions_path: Path) -> None:
        self.save_in_progress = False
        self.last_session_dir = session_dir
        self._set_active_session_text("已暂停" if self.engine.is_paused else "录制中")
        self._refresh_controls()
        self._set_status(f"已保存: {session_dir} | 建议文件: {suggestions_path.name}")
        self.logger.info("Save snapshot completed | session_dir=%s | suggestions=%s", session_dir, suggestions_path)

    def _on_save_failed(self, message: str) -> None:
        self.save_in_progress = False
        self._refresh_controls()
        self.logger.error("Save snapshot failed | message=%s", message)
        messagebox.showerror("保存失败", message, parent=self.root)

    def _on_import_success(self, session_dir: Path, message: str) -> None:
        self.import_in_progress = False
        self.last_session_dir = session_dir
        metadata = self.engine.store.data.metadata if self.engine.store.data else None
        if metadata is not None:
            self.session_metadata_draft = SessionMetadataDraft(
                is_prs_recording=metadata.is_prs_recording,
                testcase_id=metadata.testcase_id,
                version_number=metadata.version_number,
                project=metadata.project,
                baseline_name=metadata.baseline_name,
                name=metadata.name,
                recorder_person=metadata.recorder_person,
                design_steps=metadata.design_steps,
                scope=metadata.scope,
            )
            self._show_design_steps_overlay(metadata.design_steps)
        self._set_active_session_text("续录中")
        self._refresh_controls()
        self._set_status(message)
        self.logger.info("Import-and-continue completed | session_dir=%s", session_dir)

    def _on_import_failed(self, message: str) -> None:
        self.import_in_progress = False
        self._hide_design_steps_overlay()
        self.session_var.set("未开始录制")
        self._refresh_controls()
        self.logger.error("Import-and-continue failed | message=%s", message)
        messagebox.showerror("导入失败", message, parent=self.root)

    def _show_design_steps_overlay(self, design_steps: str) -> None:
        self.design_steps_overlay.show(design_steps)

    def _hide_design_steps_overlay(self) -> None:
        self.design_steps_overlay.hide()

    def _handle_root_close(self) -> None:
        self.design_steps_overlay.destroy()
        self.root.destroy()

    def _set_active_session_text(self, prefix: str) -> None:
        session_dir = self.engine.store.session_dir
        if session_dir is None:
            self.session_var.set(prefix)
            return
        self.session_var.set(f"{prefix}: {session_dir.name}")

    def _prompt_session_to_continue(self) -> Path | None:
        recordings_root = Path(self.output_var.get())
        sessions = self._find_session_candidates(recordings_root)
        if not sessions:
            messagebox.showinfo("提示", f"未在以下目录找到可继续录制的 session:\n{recordings_root}", parent=self.root)
            return None

        dialog = tk.Toplevel(self.root)
        dialog.title("选择要继续录制的 Session")
        dialog.geometry("760x520")
        dialog.minsize(680, 420)
        dialog.transient(self.root)
        dialog.grab_set()

        selected_path: Path | None = None

        ttk.Label(dialog, text="请选择一个已有 session 继续录制。", padding=(16, 12, 16, 4)).pack(anchor=tk.W)
        ttk.Label(dialog, text=str(recordings_root), padding=(16, 0, 16, 8)).pack(anchor=tk.W)

        columns = ("name", "modified", "events")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        tree.heading("name", text="Session 目录")
        tree.heading("modified", text="最后修改时间")
        tree.heading("events", text="事件数")
        tree.column("name", width=360, anchor=tk.W)
        tree.column("modified", width=200, anchor=tk.W, stretch=False)
        tree.column("events", width=80, anchor=tk.CENTER, stretch=False)
        tree.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        button_bar = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        button_bar.pack(fill=tk.X)

        for index, item in enumerate(sessions):
            tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(item["name"], item["modified"], item["events"]),
            )

        if sessions:
            tree.selection_set("0")
            tree.focus("0")

        def confirm() -> None:
            nonlocal selected_path
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("提示", "请选择一个 session。", parent=dialog)
                return
            selected_path = Path(str(sessions[int(selection[0])]["path"]))
            dialog.destroy()

        ttk.Button(button_bar, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(button_bar, text="继续录制所选 Session", command=confirm).pack(side=tk.RIGHT, padx=(0, 8))

        tree.bind("<Double-1>", lambda _event: confirm())
        dialog.lift()
        dialog.focus_force()
        self.root.wait_window(dialog)
        return selected_path

    def _find_session_candidates(self, base_dir: Path) -> list[dict[str, object]]:
        if not base_dir.exists():
            return []

        candidates: list[dict[str, object]] = []
        session_dirs: list[Path] = []
        try:
            testcase_dirs = [item for item in base_dir.iterdir() if item.is_dir()]
        except Exception:
            testcase_dirs = []

        for testcase_dir in testcase_dirs:
            try:
                child_dirs = [item for item in testcase_dir.iterdir() if item.is_dir()]
            except Exception:
                continue

            for child_dir in child_dirs:
                session_json = child_dir / "session.json"
                events_log = child_dir / "events.jsonl"
                if session_json.exists() or events_log.exists():
                    session_dirs.append(child_dir)
                    continue

                try:
                    project_session_dirs = [item for item in child_dir.iterdir() if item.is_dir()]
                except Exception:
                    continue
                session_dirs.extend(project_session_dirs)

        session_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for item in session_dirs:
            if not item.is_dir():
                continue
            session_json = item / "session.json"
            events_log = item / "events.jsonl"
            if not session_json.exists() and not events_log.exists():
                continue

            event_count: str | int = ""
            try:
                if session_json.exists():
                    payload = json.loads(session_json.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        events = payload.get("events", [])
                        if isinstance(events, list):
                            event_count = len(events)
                elif events_log.exists():
                    with events_log.open("r", encoding="utf-8") as handle:
                        event_count = sum(1 for line in handle if line.strip())
            except Exception:
                event_count = "?"

            candidates.append(
                {
                    "name": self._format_session_candidate_name(base_dir, item),
                    "modified": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "events": event_count,
                    "path": str(item),
                }
            )
        return candidates

    @staticmethod
    def _format_session_candidate_name(base_dir: Path, item: Path) -> str:
        try:
            return item.relative_to(base_dir).as_posix()
        except ValueError:
            return item.name

    def _refresh_controls(self) -> None:
        is_recording = self.engine.is_recording
        can_operate = is_recording and not self.stop_in_progress and not self.save_in_progress

        self.start_button.configure(state=tk.DISABLED if is_recording or self.stop_in_progress or self.save_in_progress or self.import_in_progress else tk.NORMAL)
        self.import_button.configure(state=tk.DISABLED if is_recording or self.stop_in_progress or self.save_in_progress or self.import_in_progress else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if is_recording and not self.stop_in_progress else tk.DISABLED)
        self.save_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.pause_resume_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.pause_resume_button.configure(text="继续录制" if self.engine.is_paused else "暂停录制")
        self.comment_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.wait_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.screenshot_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.checkpoint_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)


def launch_app() -> None:
    log_path = configure_app_logging()
    install_global_exception_logging()
    root = tk.Tk()
    logger = get_logger("app")
    root.report_callback_exception = lambda exc, val, tb: logger.exception(
        "Unhandled Tk callback exception",
        exc_info=(exc, val, tb),
    )
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = RecorderApp(root)
    logger.info("Application started | log_path=%s", log_path)
    root.mainloop()