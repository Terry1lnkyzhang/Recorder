from __future__ import annotations

import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from src.common.app_logging import configure_app_logging, get_logger, install_global_exception_logging
from src.common.runtime_paths import get_recordings_dir, get_settings_path
from src.common.session_discovery import scan_session_candidates
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
from .capture import select_region
from .i18n import pick_text
from .recorder import RecorderEngine
from .settings import Settings, SettingsStore
from src.viewer.window import open_viewer_window


class DesignStepsOverlay:
    def __init__(self, parent: tk.Misc, settings: Settings) -> None:
        self.parent = parent
        self._expanded_size = (520, 220)
        self._all_steps_size = (520, 360)
        self._collapsed_size = (520, 44)
        self._collapsed = False
        self._show_all_steps = False
        self._enabled = True
        self._manual_position: tuple[int, int] | None = None
        self._drag_offset = (0, 0)
        self._steps: list[str] = []
        self._current_step_index = 0
        self._base_bg = "#d7caa3"
        self._header_bg = "#efe4bd"
        self._body_bg = "#fffaf0"
        self._text_fg = "#2f2a1f"
        self._muted_fg = "#7a6c4d"
        self._ui_language = settings.ui_language
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.window.configure(bg=self._base_bg)

        outer = tk.Frame(self.window, bg=self._base_bg, bd=1, relief=tk.SOLID)
        outer.pack(fill=tk.BOTH, expand=True)
        self.outer = outer

        header = tk.Frame(outer, bg=self._header_bg, height=36)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        self.header = header

        title = tk.Label(
            header,
            text="Design Steps",
            bg=self._header_bg,
            fg=self._text_fg,
            font=("Segoe UI", 11, "bold"),
            anchor=tk.W,
            padx=12,
        )
        title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.title_label = title

        self.close_button = tk.Button(
            header,
            text="x",
            command=self.hide,
            bg=self._header_bg,
            fg=self._text_fg,
            activebackground=self._header_bg,
            activeforeground=self._text_fg,
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", 10, "bold"),
            width=3,
            cursor="hand2",
        )
        self.close_button.pack(side=tk.RIGHT, padx=(0, 4), pady=4)

        self.toggle_button = tk.Button(
            header,
            text="－",
            command=self.toggle_collapsed,
            bg=self._header_bg,
            fg=self._text_fg,
            activebackground=self._header_bg,
            activeforeground=self._text_fg,
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
            bg=self._header_bg,
            fg=self._text_fg,
            activebackground=self._header_bg,
            activeforeground=self._text_fg,
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

        body = tk.Frame(outer, bg=self._body_bg)
        body.pack(fill=tk.BOTH, expand=True)
        self.body = body
        body.columnconfigure(1, weight=1)

        self.previous_button = tk.Button(
            body,
            text="◀",
            command=self.show_previous_step,
            bg=self._body_bg,
            fg=self._text_fg,
            activebackground=self._body_bg,
            activeforeground=self._text_fg,
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", 16, "bold"),
            width=3,
            cursor="hand2",
        )
        self.previous_button.grid(row=0, column=0, sticky="ns", padx=(8, 0), pady=8)

        content = tk.Frame(body, bg=self._body_bg)
        content.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        self.content_frame = content

        self.step_index_var = tk.StringVar(value="1 / 1")
        self.step_text_var = tk.StringVar(value=self._empty_steps_text())

        self.step_index_label = tk.Label(
            content,
            textvariable=self.step_index_var,
            bg=self._body_bg,
            fg=self._muted_fg,
            font=("Segoe UI", 9, "bold"),
            anchor=tk.CENTER,
        )
        self.step_index_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.step_message = tk.Message(
            content,
            textvariable=self.step_text_var,
            bg=self._body_bg,
            fg=self._text_fg,
            font=("Segoe UI", 11),
            width=360,
            justify=tk.LEFT,
            anchor=tk.NW,
            padx=6,
            pady=6,
        )
        self.step_message.grid(row=1, column=0, sticky="nsew")

        self.all_steps_frame = tk.Frame(content, bg=self._body_bg)
        self.all_steps_frame.grid(row=1, column=0, sticky="nsew")
        self.all_steps_frame.columnconfigure(0, weight=1)
        self.all_steps_frame.rowconfigure(0, weight=1)

        self.all_steps_text = tk.Text(
            self.all_steps_frame,
            wrap=tk.WORD,
            font=("Segoe UI", 11),
            bg=self._body_bg,
            fg=self._text_fg,
            relief=tk.FLAT,
            borderwidth=0,
            padx=6,
            pady=6,
            highlightthickness=0,
        )
        self.all_steps_text.grid(row=0, column=0, sticky="nsew")
        self.all_steps_text.configure(state=tk.DISABLED)

        self.all_steps_scrollbar = ttk.Scrollbar(self.all_steps_frame, orient=tk.VERTICAL, command=self.all_steps_text.yview)
        self.all_steps_scrollbar.grid(row=0, column=1, sticky="ns")
        self.all_steps_text.configure(yscrollcommand=self.all_steps_scrollbar.set)
        self.all_steps_frame.grid_remove()

        self.next_button = tk.Button(
            body,
            text="▶",
            command=self.show_next_step,
            bg=self._body_bg,
            fg=self._text_fg,
            activebackground=self._body_bg,
            activeforeground=self._text_fg,
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", 16, "bold"),
            width=3,
            cursor="hand2",
        )
        self.next_button.grid(row=0, column=2, sticky="ns", padx=(0, 8), pady=8)
        self.apply_settings(settings)

    def apply_settings(self, settings: Settings) -> None:
        self._ui_language = settings.ui_language
        self._enabled = bool(settings.show_design_steps_overlay)
        width = max(320, int(settings.design_steps_overlay_width or 520))
        height = max(160, int(settings.design_steps_overlay_height or 220))
        self._expanded_size = (width, height)
        self._all_steps_size = (width, max(height + 140, int(height * 1.6)))
        self._collapsed_size = (width, 44)
        self.step_message.configure(width=max(220, width - 160))

        self._base_bg = settings.design_steps_overlay_bg_color or "#d7caa3"
        self._header_bg = self._adjust_color(self._base_bg, 0.12)
        self._body_bg = self._adjust_color(self._base_bg, 0.28)
        self._text_fg = "#2f2a1f"
        self._muted_fg = "#7a6c4d"

        try:
            self.window.attributes("-alpha", max(0.1, min(1.0, float(settings.design_steps_overlay_opacity))))
        except tk.TclError:
            pass

        self.window.configure(bg=self._base_bg)
        self.outer.configure(bg=self._base_bg)
        self.header.configure(bg=self._header_bg)
        self.title_label.configure(bg=self._header_bg, fg=self._text_fg)
        self.close_button.configure(bg=self._header_bg, fg=self._text_fg, activebackground=self._header_bg, activeforeground=self._text_fg)
        self.toggle_button.configure(bg=self._header_bg, fg=self._text_fg, activebackground=self._header_bg, activeforeground=self._text_fg)
        self.mode_button.configure(bg=self._header_bg, fg=self._text_fg, activebackground=self._header_bg, activeforeground=self._text_fg)
        self.body.configure(bg=self._body_bg)
        self.content_frame.configure(bg=self._body_bg)
        self.step_index_label.configure(bg=self._body_bg, fg=self._muted_fg)
        self.step_message.configure(bg=self._body_bg, fg=self._text_fg)
        self.all_steps_frame.configure(bg=self._body_bg)
        self.all_steps_text.configure(bg=self._body_bg, fg=self._text_fg, insertbackground=self._text_fg)
        self.previous_button.configure(bg=self._body_bg, fg=self._text_fg, activebackground=self._body_bg, activeforeground=self._text_fg)
        self.next_button.configure(bg=self._body_bg, fg=self._text_fg, activebackground=self._body_bg, activeforeground=self._text_fg)
        self.title_label.configure(text=self._t("Design Steps", "Design Steps"))
        if not self._show_all_steps:
            self.mode_button.configure(text=self._t("All Steps", "All Steps"))
        else:
            self.mode_button.configure(text=self._t("单步", "Single Step"))
        if not self._steps:
            self.step_text_var.set(self._empty_steps_text())

        if not self._enabled:
            self.hide()
        elif self.window.winfo_viewable():
            self._position_window()

    def show(self, design_steps: str) -> None:
        if not self._enabled:
            return
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
            self._steps = [self._empty_steps_text()]
            self._current_step_index = 0

        total = len(self._steps)
        self._current_step_index = min(max(self._current_step_index, 0), total - 1)
        if self._show_all_steps:
            self.step_message.grid_remove()
            self.all_steps_frame.grid()
            self._set_all_steps_text("\n\n".join(self._steps))
            self.step_index_var.set(self._t(f"全部步骤 · {total} 条", f"All Steps · {total}"))
            self.previous_button.configure(state=tk.DISABLED)
            self.next_button.configure(state=tk.DISABLED)
            self.mode_button.configure(text=self._t("单步", "Single Step"))
            return

        self.all_steps_frame.grid_remove()
        self.step_message.grid()
        current_text = self._steps[self._current_step_index]
        self.step_text_var.set(current_text)
        self.step_index_var.set(f"{self._current_step_index + 1} / {total}")
        self.previous_button.configure(state=tk.NORMAL if self._current_step_index > 0 else tk.DISABLED)
        self.next_button.configure(state=tk.NORMAL if self._current_step_index < total - 1 else tk.DISABLED)
        self.mode_button.configure(text=self._t("全部步骤", "All Steps"))
        self.step_message.configure(width=360)

    def _set_all_steps_text(self, text: str) -> None:
        self.all_steps_text.configure(state=tk.NORMAL)
        self.all_steps_text.delete("1.0", tk.END)
        self.all_steps_text.insert("1.0", text)
        self.all_steps_text.configure(state=tk.DISABLED)
        self.all_steps_text.yview_moveto(0)

    def _split_design_steps(self, design_steps: str) -> list[str]:
        normalized = (design_steps or "").replace("\r\n", "\n").strip()
        if not normalized:
            return [self._empty_steps_text()]

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

    def _empty_steps_text(self) -> str:
        return self._t("当前 Session 未填写 Design Steps。", "No design steps were provided for the current session.")

    def _t(self, zh_text: str, en_text: str) -> str:
        return pick_text(self._ui_language, zh_text, en_text)

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

    @staticmethod
    def _adjust_color(color: str, amount: float) -> str:
        color = color.strip().lstrip("#")
        if len(color) != 6:
            return "#d7caa3"
        channels = [int(color[index:index + 2], 16) for index in range(0, 6, 2)]
        adjusted = [min(255, max(0, int(channel + (255 - channel) * amount))) for channel in channels]
        return f"#{adjusted[0]:02x}{adjusted[1]:02x}{adjusted[2]:02x}"


class RecorderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
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

        self.output_var = tk.StringVar(value=str(output_dir))
        self.last_session_dir: Path | None = None
        self.stop_in_progress = False
        self.save_in_progress = False
        self.import_in_progress = False
        self.ai_checkpoint_draft = AICheckpointDraft()
        self.session_metadata_draft = SessionMetadataDraft()
        self._checkpoint_dialog_open = False
        self._manual_screenshot_in_progress = False
        self.current_settings = self.settings_store.load()
        self.status_var = tk.StringVar(value=self._t("就绪", "Ready"))
        self.session_var = tk.StringVar(value=self._t("未开始录制", "Not recording"))
        self.design_steps_overlay = DesignStepsOverlay(self.root, self.current_settings)
        self._session_picker_scan_token = 0
        self._session_candidate_cache: dict[str, dict[str, object]] = {}

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_root_close)
        self.logger.info("Recorder UI initialized | output_dir=%s", output_dir)

    def _build_ui(self) -> None:
        wrapper = ttk.Frame(self.root, padding=20)
        wrapper.pack(fill=tk.BOTH, expand=True)

        self.title_label = ttk.Label(wrapper, text="", font=("Segoe UI", 18, "bold"))
        self.title_label.pack(anchor=tk.W)

        self.desc_label = ttk.Label(
            wrapper,
            text="",
            wraplength=560,
        )
        self.desc_label.pack(anchor=tk.W, pady=(8, 16))

        self.info_frame = ttk.LabelFrame(wrapper, text="")
        self.info_frame.pack(fill=tk.X)
        self.session_status_label = ttk.Label(self.info_frame, text="")
        self.session_status_label.grid(row=0, column=0, sticky=tk.W, padx=12, pady=8)
        ttk.Label(self.info_frame, textvariable=self.session_var).grid(row=0, column=1, sticky=tk.W, padx=8, pady=8)
        self.output_dir_label = ttk.Label(self.info_frame, text="")
        self.output_dir_label.grid(row=1, column=0, sticky=tk.W, padx=12, pady=8)
        ttk.Label(self.info_frame, textvariable=self.output_var, wraplength=420).grid(row=1, column=1, sticky=tk.W, padx=8, pady=8)

        self.button_frame = ttk.LabelFrame(wrapper, text="")
        self.button_frame.pack(fill=tk.X, pady=20)

        primary_actions = ttk.Frame(self.button_frame, padding=(12, 10, 12, 6))
        primary_actions.pack(fill=tk.X)

        secondary_actions = ttk.Frame(self.button_frame, padding=(12, 0, 12, 10))
        secondary_actions.pack(fill=tk.X)

        self.start_button = ttk.Button(primary_actions, text="", command=self.start_recording)
        self.start_button.pack(side=tk.LEFT)

        self.import_button = ttk.Button(primary_actions, text="", command=self.import_and_continue_recording)
        self.import_button.pack(side=tk.LEFT, padx=(10, 0))

        self.stop_button = ttk.Button(primary_actions, text="", command=self.stop_recording, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))

        self.save_button = ttk.Button(primary_actions, text="", command=self.save_recording, state=tk.DISABLED)
        self.save_button.pack(side=tk.LEFT, padx=(10, 0))

        self.pause_resume_button = ttk.Button(primary_actions, text="", command=self.toggle_pause_resume, state=tk.DISABLED)
        self.pause_resume_button.pack(side=tk.LEFT, padx=(10, 0))

        self.comment_button = ttk.Button(secondary_actions, text="", command=self.add_comment, state=tk.DISABLED)
        self.comment_button.pack(side=tk.LEFT)

        self.wait_button = ttk.Button(secondary_actions, text="", command=self.add_wait_for_image, state=tk.DISABLED)
        self.wait_button.pack(side=tk.LEFT, padx=(10, 0))

        self.screenshot_button = ttk.Button(secondary_actions, text="", command=self.capture_manual_screenshot, state=tk.DISABLED)
        self.screenshot_button.pack(side=tk.LEFT, padx=(10, 0))

        self.checkpoint_button = ttk.Button(
            secondary_actions,
            text="",
            command=self.add_checkpoint,
            state=tk.DISABLED,
        )
        self.checkpoint_button.pack(side=tk.LEFT, padx=(10, 0))

        self.viewer_button = ttk.Button(secondary_actions, text="", command=self.open_viewer)
        self.viewer_button.pack(side=tk.LEFT, padx=(10, 0))

        self.settings_button = ttk.Button(secondary_actions, text="", command=self.open_settings)
        self.settings_button.pack(side=tk.LEFT, padx=(10, 0))

        self.notes_frame = ttk.LabelFrame(wrapper, text="")
        self.notes_frame.pack(fill=tk.BOTH, expand=True)
        self.notes_label = ttk.Label(self.notes_frame, text="", justify=tk.LEFT, wraplength=760)
        self.notes_label.pack(anchor=tk.W, padx=12, pady=12)

        self.status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._apply_ui_language()

    def start_recording(self) -> None:
        if self.stop_in_progress or self.save_in_progress or self.import_in_progress:
            self.logger.info("Start recording ignored because another operation is in progress")
            return

        metadata_draft = open_session_metadata_dialog(self.root, self.session_metadata_draft, self.settings_store)
        if metadata_draft is None:
            self.logger.info("Start recording cancelled in session metadata dialog")
            self._set_status(self._t("已取消开始录制", "Start recording canceled"))
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
        self._set_active_session_text(self._t("录制中", "Recording"))
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
        self.session_var.set(self._t(f"导入中: {session_dir.name}", f"Importing: {session_dir.name}"))
        self._set_status(self._t("正在导入已有录制内容，请稍候...", "Importing an existing recording. Please wait..."))

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
        self._set_status(self._t("正在停止录制并等待后台任务落盘...", "Stopping recording and waiting for background tasks to finish..."))
        self.session_var.set(self._t("停止中...", "Stopping..."))

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
        self._set_status(self._t("正在保存当前录制快照...", "Saving the current recording snapshot..."))

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
                self._set_active_session_text(self._t("录制中", "Recording"))
            else:
                self.logger.info("Pause recording requested")
                message = self.engine.pause_recording()
                self._set_active_session_text(self._t("已暂停", "Paused"))
        except RuntimeError as exc:
            self.logger.exception("Pause/resume failed")
            messagebox.showerror(self._t("操作失败", "Operation failed"), str(exc), parent=self.root)
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

    def _add_checkpoint_from_shortcut(self) -> None:
        if not self.engine.is_recording:
            self.logger.info("AI checkpoint shortcut ignored because recorder is not running")
            return
        if self._checkpoint_dialog_open:
            self.logger.info("AI checkpoint shortcut ignored because dialog is already open")
            return

        next_slot_index = len(self.ai_checkpoint_draft.image_selections)
        if self.ai_checkpoint_draft.video_path is not None or next_slot_index >= 5:
            self.logger.info("AI checkpoint shortcut falls back to dialog open | slot_index=%s | has_video=%s", next_slot_index, self.ai_checkpoint_draft.video_path is not None)
            self.add_checkpoint()
            return
        self.logger.info("AI checkpoint shortcut capture opened | slot_index=%s", next_slot_index)
        self._checkpoint_dialog_open = True
        self.engine.suspend()
        try:
            selection = select_region(self.root, f"选择 AI Checkpoint 截图区域 {next_slot_index + 1}")
            if not selection:
                self._set_status(self._t("已取消 AI Checkpoint 快捷截图", "AI checkpoint quick capture canceled"))
                return

            relative_path = self.engine.save_manual_image(selection.image, "checkpoint")
            if not relative_path:
                messagebox.showerror(self._t("保存失败", "Save failed"), self._t("AI Checkpoint 截图保存失败。", "Failed to save the AI checkpoint screenshot."), parent=self.root)
                return

            session_dir = self.engine.store.session_dir
            if session_dir is None:
                messagebox.showerror(self._t("保存失败", "Save failed"), self._t("当前没有可用的 session 目录。", "No active session directory is available."), parent=self.root)
                return

            self.ai_checkpoint_draft.video_path = None
            self.ai_checkpoint_draft.video_region = None
            self.ai_checkpoint_draft.video_status = "未录制视频"
            captured_item = ((session_dir / relative_path).resolve(), selection.to_region_dict())
            if next_slot_index < len(self.ai_checkpoint_draft.image_selections):
                self.ai_checkpoint_draft.image_selections[next_slot_index] = captured_item
            else:
                self.ai_checkpoint_draft.image_selections.append(captured_item)

            open_ai_checkpoint_dialog(self.root, self.engine, self.settings_store, self.ai_checkpoint_draft)
        finally:
            self.engine.resume()
            self._checkpoint_dialog_open = False
            self.logger.info("AI checkpoint shortcut capture closed")

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
                self._set_status(self._t(f"已保存截图: {relative_path}", f"Screenshot saved: {relative_path}"))
            else:
                self._set_status(self._t("已取消记录截图", "Screenshot capture canceled"))
        finally:
            self.engine.resume()
            self._manual_screenshot_in_progress = False
            self.logger.info("Manual screenshot capture closed")

    def _request_ai_checkpoint_from_shortcut(self) -> None:
        self.root.after(0, self._add_checkpoint_from_shortcut)

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
        self.current_settings = self.settings_store.load()
        self._apply_ui_language()
        self.design_steps_overlay.apply_settings(self.current_settings)
        self.engine.reload_capture_filters()
        if self.engine.is_recording:
            metadata = self.engine.store.data.metadata if self.engine.store.data else None
            if self.current_settings.show_design_steps_overlay and metadata is not None:
                self._show_design_steps_overlay(metadata.design_steps)
            elif not self.current_settings.show_design_steps_overlay:
                self._hide_design_steps_overlay()
            self._set_status(self._t("已更新录制排除规则", "Recording exclusion rules updated"))
        self.logger.info("Settings dialog closed")

    def _on_stop_success(self, session_dir: Path, suggestions_path: Path) -> None:
        self.stop_in_progress = False
        self._hide_design_steps_overlay()
        self.session_var.set(self._t(f"已停止: {session_dir.name}", f"Stopped: {session_dir.name}"))
        self.last_session_dir = session_dir
        self._refresh_controls()
        self._set_status(self._t(f"已输出: {session_dir} | 建议文件: {suggestions_path.name}", f"Output saved: {session_dir} | Suggestions: {suggestions_path.name}"))
        self.logger.info("Stop recording completed | session_dir=%s | suggestions=%s", session_dir, suggestions_path)
        messagebox.showinfo(
            self._t("录制完成", "Recording complete"),
            self._t(
                f"录制结果已保存到:\n{session_dir}\n\n复用建议文件:\n{suggestions_path}",
                f"Recording output saved to:\n{session_dir}\n\nSuggestions file:\n{suggestions_path}",
            ),
        )

    def _on_stop_failed(self, message: str) -> None:
        self.stop_in_progress = False
        self._refresh_controls()
        self.logger.error("Stop recording failed | message=%s", message)
        messagebox.showerror(self._t("停止失败", "Stop failed"), message)

    def _on_save_success(self, session_dir: Path, suggestions_path: Path) -> None:
        self.save_in_progress = False
        self.last_session_dir = session_dir
        self._set_active_session_text(self._t("已暂停", "Paused") if self.engine.is_paused else self._t("录制中", "Recording"))
        self._refresh_controls()
        self._set_status(self._t(f"已保存: {session_dir} | 建议文件: {suggestions_path.name}", f"Saved: {session_dir} | Suggestions: {suggestions_path.name}"))
        self.logger.info("Save snapshot completed | session_dir=%s | suggestions=%s", session_dir, suggestions_path)

    def _on_save_failed(self, message: str) -> None:
        self.save_in_progress = False
        self._refresh_controls()
        self.logger.error("Save snapshot failed | message=%s", message)
        messagebox.showerror(self._t("保存失败", "Save failed"), message, parent=self.root)

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
        self._set_active_session_text(self._t("续录中", "Continuing"))
        self._refresh_controls()
        self._set_status(message)
        self.logger.info("Import-and-continue completed | session_dir=%s", session_dir)

    def _on_import_failed(self, message: str) -> None:
        self.import_in_progress = False
        self._hide_design_steps_overlay()
        self.session_var.set(self._t("未开始录制", "Not recording"))
        self._refresh_controls()
        self.logger.error("Import-and-continue failed | message=%s", message)
        messagebox.showerror(self._t("导入失败", "Import failed"), message, parent=self.root)

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
        dialog = tk.Toplevel(self.root)
        dialog.title(self._t("选择要继续录制的 Session", "Choose a session to continue"))
        dialog.geometry("760x520")
        dialog.minsize(680, 420)
        dialog.transient(self.root)
        dialog.grab_set()

        selected_path: Path | None = None

        ttk.Label(dialog, text=self._t("请选择一个已有 session 继续录制。", "Select an existing session to continue recording."), padding=(16, 12, 16, 4)).pack(anchor=tk.W)
        ttk.Label(dialog, text=str(recordings_root), padding=(16, 0, 16, 8)).pack(anchor=tk.W)

        columns = ("name", "modified", "events")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        tree.heading("name", text=self._t("Session 目录", "Session folder"))
        tree.heading("modified", text=self._t("最后修改时间", "Last modified"))
        tree.heading("events", text=self._t("事件数", "Events"))
        tree.column("name", width=360, anchor=tk.W)
        tree.column("modified", width=200, anchor=tk.W, stretch=False)
        tree.column("events", width=80, anchor=tk.CENTER, stretch=False)
        tree.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        status_var = tk.StringVar(value=self._t("正在扫描 Session...", "Scanning sessions..."))
        ttk.Label(dialog, textvariable=status_var, padding=(16, 0, 16, 8)).pack(anchor=tk.W)

        button_bar = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        button_bar.pack(fill=tk.X)

        sessions: list[dict[str, object]] = []

        def populate(force_refresh: bool = False) -> None:
            self._session_picker_scan_token += 1
            token = self._session_picker_scan_token
            status_var.set(self._t("正在扫描 Session...", "Scanning sessions..."))
            for item_id in tree.get_children():
                tree.delete(item_id)

            def worker() -> None:
                try:
                    items = self._find_session_candidates(recordings_root, force_refresh=force_refresh)
                except Exception as exc:
                    self.root.after(0, lambda: status_var.set(self._t(f"扫描 Session 失败: {exc}", f"Failed to scan sessions: {exc}")))
                    return

                def apply_results() -> None:
                    if not dialog.winfo_exists() or token != self._session_picker_scan_token:
                        return
                    sessions.clear()
                    sessions.extend(items)
                    status_var.set(self._t(f"共找到 {len(sessions)} 个 Session", f"Found {len(sessions)} sessions"))
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

                self.root.after(0, apply_results)

            threading.Thread(target=worker, daemon=True).start()

        def confirm() -> None:
            nonlocal selected_path
            selection = tree.selection()
            if not selection:
                messagebox.showinfo(self._t("提示", "Notice"), self._t("请选择一个 session。", "Select a session."), parent=dialog)
                return
            selected_path = Path(str(sessions[int(selection[0])]["path"]))
            dialog.destroy()

        ttk.Button(button_bar, text=self._t("刷新", "Refresh"), command=lambda: populate(force_refresh=True)).pack(side=tk.LEFT)
        ttk.Button(button_bar, text=self._t("取消", "Cancel"), command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(button_bar, text=self._t("继续录制所选 Session", "Continue selected session"), command=confirm).pack(side=tk.RIGHT, padx=(0, 8))

        tree.bind("<Double-1>", lambda _event: confirm())
        populate()
        dialog.lift()
        dialog.focus_force()
        self.root.wait_window(dialog)
        if selected_path is None and not sessions:
            messagebox.showinfo(self._t("提示", "Notice"), self._t(f"未在以下目录找到可继续录制的 session:\n{recordings_root}", f"No resumable session was found under:\n{recordings_root}"), parent=self.root)
        return selected_path

    def _find_session_candidates(self, base_dir: Path, force_refresh: bool = False) -> list[dict[str, object]]:
        return scan_session_candidates(
            base_dir,
            cache=self._session_candidate_cache,
            force_refresh=force_refresh,
        )

    def _refresh_controls(self) -> None:
        is_recording = self.engine.is_recording
        can_operate = is_recording and not self.stop_in_progress and not self.save_in_progress

        self.start_button.configure(state=tk.DISABLED if is_recording or self.stop_in_progress or self.save_in_progress or self.import_in_progress else tk.NORMAL)
        self.import_button.configure(state=tk.DISABLED if is_recording or self.stop_in_progress or self.save_in_progress or self.import_in_progress else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if is_recording and not self.stop_in_progress else tk.DISABLED)
        self.save_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.pause_resume_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.pause_resume_button.configure(text=self._t("继续录制", "Resume") if self.engine.is_paused else self._t("暂停录制", "Pause"))
        self.comment_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.wait_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.screenshot_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)
        self.checkpoint_button.configure(state=tk.NORMAL if can_operate else tk.DISABLED)

    def _apply_ui_language(self) -> None:
        self.root.title(self._t("Automation Recorder", "Automation Recorder"))
        self.title_label.configure(text=self._t("Automation Recorder", "Automation Recorder"))
        self.desc_label.configure(text=self._t("录制人工操作、截图和附加上下文，为后续自动化脚本 YAML 转换做准备。", "Record manual operations, screenshots, and context for later YAML automation conversion."))
        self.info_frame.configure(text=self._t("Session", "Session"))
        self.session_status_label.configure(text=self._t("状态:", "Status:"))
        self.output_dir_label.configure(text=self._t("输出目录:", "Output folder:"))
        self.button_frame.configure(text=self._t("操作", "Actions"))
        self.start_button.configure(text=self._t("开始录制", "Start Recording"))
        self.import_button.configure(text=self._t("导入并续录", "Import and Continue"))
        self.stop_button.configure(text=self._t("停止录制", "Stop Recording"))
        self.save_button.configure(text=self._t("保存", "Save"))
        self.comment_button.configure(text=self._t("添加 Comment", "Add Comment"))
        self.wait_button.configure(text=self._t("添加等待事件", "Add Wait Event"))
        self.screenshot_button.configure(text=self._t("记录截图", "Capture Screenshot"))
        self.checkpoint_button.configure(text=self._t("添加 AI Checkpoint", "Add AI Checkpoint"))
        self.viewer_button.configure(text=self._t("查看录制内容", "Open Viewer"))
        self.settings_button.configure(text=self._t("设置", "Settings"))
        self.notes_frame.configure(text=self._t("说明", "Notes"))
        self.notes_label.configure(text=self._t(
            "1. 点击开始录制后，会先填写本次录制的 Session 元数据，再开始监听键盘、鼠标点击和滚轮事件。\n"
            "2. Comment 通过鼠标拖拽选择截图区域，再填写大文本说明。\n"
            "3. 等待事件支持框选等待区域并自动保存截图，当前第一版用于记录等待图片出现的步骤。\n"
            "4. 记录截图支持手动选区并保存到当前 Session 的 screenshots，可通过 Ctrl+F4 快捷键快速触发。\n"
            "5. AI Checkpoint 支持两张截图、区域视频录制、Query 调模型并保存返回内容，也支持 Ctrl+F5 快捷键快速打开。\n"
            "6. 可手动点击保存，立即将当前 session 快照和 suggestions 落盘。\n"
            "7. 支持暂停/继续录制，以及导入已有 session 后继续录制。\n"
            "8. 停止录制会在后台收尾，不再阻塞整个窗口。\n"
            "9. Session 元数据在录制完成后也可以在 Session Viewer 中继续修改。",
            "1. When you start recording, the app first collects session metadata, then listens for keyboard, mouse-click, and wheel events.\n"
            "2. Comment lets you drag-select a screenshot region and enter a detailed note.\n"
            "3. Wait events let you select a wait region and save a screenshot for image-appearance wait steps.\n"
            "4. Capture Screenshot saves a manual region into the current session screenshots folder and can be triggered with Ctrl+F4.\n"
            "5. AI Checkpoint supports screenshots, region video capture, model queries, and saving the response; Ctrl+F5 also opens it quickly.\n"
            "6. Save writes the current session snapshot and suggestions immediately.\n"
            "7. Recording can be paused/resumed, and you can continue from an existing session.\n"
            "8. Stopping recording completes background cleanup without blocking the window.\n"
            "9. Session metadata can still be edited later in Session Viewer."
        ))
        self._refresh_controls()

    def _t(self, zh_text: str, en_text: str) -> str:
        return pick_text(self.current_settings.ui_language, zh_text, en_text)


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