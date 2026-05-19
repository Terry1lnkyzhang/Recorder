from __future__ import annotations

import json
import re
import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import yaml

from src.common.app_logging import configure_app_logging, get_logger, install_global_exception_logging
from src.common.runtime_paths import RecordingsDirResolution, get_local_recording_staging_dir, get_settings_path, resolve_recordings_dir
from src.common.session_lock import SESSION_LOCK_FILE_NAME, SessionLockHandle, acquire_session_lock
from src.common.session_summary import write_session_summary_from_session_payload
from src.android_recorder.dialog import AndroidRecorderDialog
from .dialogs import (
    AICheckpointDialog,
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
from src.viewer.window import open_viewer_window, pick_session_from_recordings


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

        recordings_dir = resolve_recordings_dir()
        output_dir = recordings_dir.path
        recording_work_dir = get_local_recording_staging_dir() if recordings_dir.using_network_share else output_dir
        self.recordings_target_root = output_dir
        self.recording_work_root = recording_work_dir
        self.sync_recordings_to_target = recordings_dir.using_network_share and recording_work_dir != output_dir
        self.settings_store = SettingsStore(get_settings_path())
        self.engine = RecorderEngine(
            output_dir=recording_work_dir,
            status_callback=self._set_status,
            settings_store=self.settings_store,
            ai_checkpoint_request_callback=self._request_ai_checkpoint_from_shortcut,
            ai_checkpoint_video_request_callback=self._request_ai_checkpoint_video_from_shortcut,
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
        self._shortcut_video_dialog: AICheckpointDialog | None = None
        self._manual_screenshot_in_progress = False
        self.android_recorder_dialog: AndroidRecorderDialog | None = None
        self.current_settings = self.settings_store.load()
        self.status_var = tk.StringVar(value=self._t("就绪", "Ready"))
        self.session_var = tk.StringVar(value=self._t("未开始录制", "Not recording"))
        self.design_steps_overlay = DesignStepsOverlay(self.root, self.current_settings)
        self._session_candidate_cache: dict[str, dict[str, object]] = {}
        self._sync_lock = threading.Lock()
        self._active_sync_jobs: dict[str, dict[str, object]] = {}
        self._continued_session_targets: dict[str, dict[str, object]] = {}

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_root_close)
        self._confirm_recordings_output_dir(recordings_dir)
        if self.sync_recordings_to_target:
            self._set_status(self._t(f"录制将先写入本地暂存目录，停止后同步到共享目录: {output_dir}", f"Recordings will be written locally first and synced to the shared folder on stop: {output_dir}"))
        self.logger.info("Recorder UI initialized | output_dir=%s | recording_work_dir=%s | sync_to_target=%s", output_dir, recording_work_dir, self.sync_recordings_to_target)

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

        self.android_button = ttk.Button(secondary_actions, text="", command=self.open_android_recorder)
        self.android_button.pack(side=tk.LEFT, padx=(10, 0))

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

        metadata_draft = open_session_metadata_dialog(
            self.root,
            self.session_metadata_draft,
            self.settings_store,
            recordings_root=self.recordings_target_root,
        )
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

    def _confirm_recordings_output_dir(self, recordings_dir: RecordingsDirResolution) -> None:
        if recordings_dir.using_network_share:
            return

        warning_message = self._t(
            "当前未连接到默认录屏目录：\n"
            "\\130.147.129.203\\AutomaticShared\\Recordings\n\n"
            f"将使用本地录屏路径：\n{recordings_dir.path}\n\n"
            "录屏成果需要手动拷贝到：\n"
            "\\130.147.129.203\\AutomaticShared\\Recordings\n\n"
            "是否确认继续？\n\n"
            "选择“否”将关闭程序，请连接共享目录后重新开启。",
            "The default recordings share is currently unavailable:\n"
            "\\130.147.129.203\\AutomaticShared\\Recordings\n\n"
            f"The app will use this local recordings path instead:\n{recordings_dir.path}\n\n"
            "You must manually copy the recording results to:\n"
            "\\130.147.129.203\\AutomaticShared\\Recordings\n\n"
            "Do you want to continue?\n\n"
            "Choosing No will close the app. Reconnect to the share and reopen it.",
        )
        self.logger.warning(
            "Network recordings share unavailable; falling back to local directory | local_path=%s | reason=%s",
            recordings_dir.path,
            recordings_dir.fallback_reason or "unknown",
        )
        self.root.bell()
        self.root.lift()
        self.root.focus_force()
        if not messagebox.askyesno(
            self._t("共享录屏目录未连接", "Shared recordings path unavailable"),
            warning_message,
            parent=self.root,
            icon="warning",
            default="no",
        ):
            self._set_status(self._t("未确认本地录屏路径，程序即将关闭", "Local recordings path not confirmed; closing the app"))
            self.root.after(0, self.root.destroy)
            return
        self._set_status(
            self._t(
                "当前未连接共享录屏目录，已切换到本地 recording 目录，请在录制后手动拷贝成果。",
                "The shared recordings path is unavailable. The app is using the local recording directory; copy the results manually after recording.",
            )
        )

    def import_and_continue_recording(self) -> None:
        if self.engine.is_recording or self.stop_in_progress or self.save_in_progress or self.import_in_progress:
            self.logger.info("Import-and-continue ignored because recorder is busy")
            return

        session_dir = pick_session_from_recordings(
            self.root,
            recordings_root=Path(self.output_var.get()),
            ui_language=self.current_settings.ui_language,
            session_candidate_cache=self._session_candidate_cache,
            title=self._t("选择要继续录制的 Session", "Choose a session to continue"),
            intro_text=self._t("从 recordings 中选择一个 session。", "Select a session from recordings."),
            confirm_button_text=self._t("继续录制所选 Session", "Continue selected session"),
            empty_result_message=self._t(
                f"未在以下目录找到可继续录制的 session:\n{Path(self.output_var.get())}",
                f"No resumable session was found under:\n{Path(self.output_var.get())}",
            ),
        )
        if session_dir is None:
            self.logger.info("Import-and-continue cancelled before session selection")
            return

        self.logger.info("Import-and-continue requested | session_dir=%s", session_dir)
        self.import_in_progress = True
        self._refresh_controls()
        self.session_var.set(self._t(f"导入中: {session_dir.name}", f"Importing: {session_dir.name}"))
        self._set_status(self._t("正在导入已有录制内容，请稍候...", "Importing an existing recording. Please wait..."))

        def worker() -> None:
            resume_session_dir = session_dir
            target_session_dir: Path | None = None
            target_lock_handle: SessionLockHandle | None = None
            try:
                resume_session_dir, target_session_dir, target_lock_handle = self._prepare_session_for_continue(session_dir)
                message = self.engine.continue_recording(resume_session_dir)
                if target_session_dir is not None and target_lock_handle is not None:
                    self._register_continued_session_target(resume_session_dir, target_session_dir, target_lock_handle)
                    target_lock_handle = None
            except Exception as exc:
                if target_lock_handle is not None:
                    target_lock_handle.release()
                self.logger.exception("Import-and-continue failed | session_dir=%s", session_dir)
                self.root.after(0, lambda: self._on_import_failed(str(exc)))
                return
            self.root.after(0, lambda: self._on_import_success(resume_session_dir, message, target_session_dir=target_session_dir))

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
                if self.android_recorder_dialog is not None and self.android_recorder_dialog.is_open() and self.android_recorder_dialog.operation_recorder.is_recording:
                    self.android_recorder_dialog.stop_capture_for_main()
                local_session_dir, local_suggestions_path = self.engine.stop()
            except RuntimeError as exc:
                self.logger.exception("Stop recording failed")
                self.root.after(0, lambda: self._on_stop_failed(str(exc)))
                return

            if self._should_sync_session_to_target(local_session_dir):
                self.root.after(0, lambda: self._on_local_stop_success(local_session_dir, local_suggestions_path))
                try:
                    session_dir = self._sync_session_to_target(local_session_dir)
                    suggestions_path = session_dir / local_suggestions_path.name
                except Exception as exc:
                    message = str(exc)
                    self.logger.exception("Failed to sync recording to target | local_session_dir=%s | target_root=%s", local_session_dir, self.recordings_target_root)
                    self.root.after(0, lambda: self._on_sync_failed(local_session_dir, message))
                    return
                self.root.after(0, lambda: self._on_sync_success(session_dir, suggestions_path, local_session_dir))
                return

            self.root.after(0, lambda: self._on_stop_success(local_session_dir, local_suggestions_path, local_session_dir=local_session_dir))

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

    def _add_video_checkpoint_from_shortcut(self) -> None:
        if not self.engine.is_recording:
            self.logger.info("AI checkpoint video shortcut ignored because recorder is not running")
            return
        active_dialog = self._get_active_shortcut_video_dialog()
        if active_dialog is not None:
            if active_dialog.is_video_stop_in_progress():
                self.logger.info("AI checkpoint video shortcut ignored because video stop is still in progress")
                active_dialog.restore_after_shortcut_recording()
                self._set_status(self._t("AI Checkpoint 视频仍在保存中，请稍候。", "AI checkpoint video is still being saved. Please wait."))
                return
            if active_dialog.is_video_recording_active():
                self.logger.info("AI checkpoint video shortcut stopping active shortcut recording")
                self.engine.suspend()
                active_dialog.restore_after_shortcut_recording()
                active_dialog.stop_video_async()
                self._set_status(self._t("AI Checkpoint 视频录制已停止，正在保存，请继续填写。", "AI checkpoint video recording stopped and is being saved. Continue filling in the checkpoint."))
                return
            self.logger.info("AI checkpoint video shortcut restoring existing dialog")
            self.engine.suspend()
            active_dialog.restore_after_shortcut_recording()
            return
        if self._checkpoint_dialog_open:
            self.logger.info("AI checkpoint video shortcut ignored because dialog is already open")
            return
        if self.ai_checkpoint_draft.image_selections or self.ai_checkpoint_draft.video_path is not None:
            self.logger.info(
                "AI checkpoint video shortcut falls back to dialog open | image_count=%s | has_video=%s",
                len(self.ai_checkpoint_draft.image_selections),
                self.ai_checkpoint_draft.video_path is not None,
            )
            self.add_checkpoint()
            return

        self.logger.info("AI checkpoint video shortcut capture opened")
        self._checkpoint_dialog_open = True
        dialog_created = False
        self.engine.suspend()
        try:
            selection = select_region(self.root, "选择 AI Checkpoint 视频区域")
            if not selection:
                self._checkpoint_dialog_open = False
                self._set_status(self._t("已取消 AI Checkpoint 快捷视频录制", "AI checkpoint quick video capture canceled"))
                return

            self.ai_checkpoint_draft.clear()
            dialog = AICheckpointDialog(
                self.root,
                self.engine,
                self.settings_store,
                self.ai_checkpoint_draft,
                auto_start_video_selection=selection,
                on_close=self._handle_shortcut_video_dialog_closed,
                start_hidden=True,
            )
            self._shortcut_video_dialog = dialog
            dialog_created = True
            self._set_status(self._t("AI Checkpoint 视频录制已开始，再按 Ctrl+F6 可停止录制并打开窗口。", "AI checkpoint video recording started. Press Ctrl+F6 again to stop and open the dialog."))
        finally:
            self.engine.resume()
            if not dialog_created:
                self._checkpoint_dialog_open = False
            self.logger.info("AI checkpoint video shortcut capture initialized")

    def _get_active_shortcut_video_dialog(self) -> AICheckpointDialog | None:
        dialog = self._shortcut_video_dialog
        if dialog is None:
            return None
        try:
            if dialog.window.winfo_exists():
                return dialog
        except tk.TclError:
            pass
        self._shortcut_video_dialog = None
        self._checkpoint_dialog_open = False
        return None

    def _handle_shortcut_video_dialog_closed(self, dialog: AICheckpointDialog) -> None:
        if self._shortcut_video_dialog is not dialog:
            return
        self._shortcut_video_dialog = None
        self._checkpoint_dialog_open = False
        self.engine.resume()
        self.logger.info("AI checkpoint shortcut video dialog closed")

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

    def _request_ai_checkpoint_video_from_shortcut(self) -> None:
        self.root.after(0, self._add_video_checkpoint_from_shortcut)

    def _request_manual_screenshot_from_shortcut(self) -> None:
        self.root.after(0, self.capture_manual_screenshot)

    def _set_status(self, message: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self.status_var.set(message)
            return
        self.root.after(0, lambda: self.status_var.set(message))

    def open_viewer(self) -> None:
        initial_dir = self.last_session_dir or self.recordings_target_root
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

    def _on_stop_success(self, session_dir: Path, suggestions_path: Path, *, local_session_dir: Path | None = None, sync_error: str = "") -> None:
        self.stop_in_progress = False
        self._hide_design_steps_overlay()
        self.session_var.set(self._t(f"已停止: {session_dir.name}", f"Stopped: {session_dir.name}"))
        self.last_session_dir = session_dir
        self._refresh_controls()
        if sync_error:
            local_hint = f"\n\n本地录制结果仍保留在:\n{local_session_dir}" if local_session_dir else ""
            self._set_status(self._t(f"本地录制已完成，但同步共享目录失败: {sync_error}", f"Local recording completed, but syncing to the shared folder failed: {sync_error}"))
            self.logger.error("Stop recording completed with sync failure | session_dir=%s | suggestions=%s | local_session_dir=%s | sync_error=%s", session_dir, suggestions_path, local_session_dir, sync_error)
            messagebox.showwarning(
                self._t("录制完成但同步失败", "Recording complete but sync failed"),
                self._t(
                    f"录制结果已先保存到本地，但同步到共享目录失败。\n\n错误:\n{sync_error}{local_hint}",
                    f"The recording was saved locally, but syncing to the shared folder failed.\n\nError:\n{sync_error}{local_hint}",
                ),
                parent=self.root,
            )
            return

        status_prefix = "已同步到共享目录" if local_session_dir and local_session_dir != session_dir else "已输出"
        self._set_status(self._t(f"{status_prefix}: {session_dir} | 建议文件: {suggestions_path.name}", f"Output saved: {session_dir} | Suggestions: {suggestions_path.name}"))
        self.logger.info("Stop recording completed | session_dir=%s | suggestions=%s | local_session_dir=%s", session_dir, suggestions_path, local_session_dir)
        local_hint = f"\n\n本地暂存目录:\n{local_session_dir}" if local_session_dir and local_session_dir != session_dir else ""
        messagebox.showinfo(
            self._t("录制完成", "Recording complete"),
            self._t(
                f"录制结果已保存到:\n{session_dir}\n\n复用建议文件:\n{suggestions_path}{local_hint}",
                f"Recording output saved to:\n{session_dir}\n\nSuggestions file:\n{suggestions_path}{local_hint}",
            ),
            parent=self.root,
        )

    def _on_local_stop_success(self, local_session_dir: Path, local_suggestions_path: Path) -> None:
        self.stop_in_progress = False
        self._hide_design_steps_overlay()
        self.session_var.set(self._t(f"已停止: {local_session_dir.name}", f"Stopped: {local_session_dir.name}"))
        self.last_session_dir = local_session_dir
        self._refresh_controls()
        self._set_status(
            self._t(
                f"本地录制已完成，正在后台同步到共享目录: {local_session_dir.name}。可以开始下一条录制。",
                f"Local recording completed and is syncing to the shared folder in the background: {local_session_dir.name}. You can start the next recording.",
            )
        )
        self.logger.info("Local stop completed; background sync started | local_session_dir=%s | suggestions=%s", local_session_dir, local_suggestions_path)

    def _on_sync_success(self, session_dir: Path, suggestions_path: Path, local_session_dir: Path) -> None:
        self._finish_sync_job(local_session_dir)
        self._release_continued_session_target(local_session_dir)
        if self.last_session_dir == local_session_dir:
            self.last_session_dir = session_dir
        self._set_status(self._t(f"同步完成: {session_dir} | 建议文件: {suggestions_path.name}", f"Sync completed: {session_dir} | Suggestions: {suggestions_path.name}"))
        self.logger.info("Background sync completed | session_dir=%s | suggestions=%s | local_session_dir=%s", session_dir, suggestions_path, local_session_dir)

    def _on_sync_failed(self, local_session_dir: Path, message: str) -> None:
        self._finish_sync_job(local_session_dir)
        self._release_continued_session_target(local_session_dir)
        self.last_session_dir = local_session_dir
        self._set_status(self._t(f"同步共享目录失败，本地结果仍保留: {local_session_dir}", f"Sync to shared folder failed. Local output is still available: {local_session_dir}"))
        messagebox.showwarning(
            self._t("同步失败", "Sync failed"),
            self._t(
                f"录制结果已保存到本地，但同步到共享目录失败。\n\n本地目录:\n{local_session_dir}\n\n错误:\n{message}",
                f"The recording was saved locally, but syncing to the shared folder failed.\n\nLocal folder:\n{local_session_dir}\n\nError:\n{message}",
            ),
            parent=self.root,
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

    def _on_import_success(self, session_dir: Path, message: str, *, target_session_dir: Path | None = None) -> None:
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
                converter_person=metadata.converter_person,
                design_steps=metadata.design_steps,
                scope=metadata.scope,
            )
            self._show_design_steps_overlay(metadata.design_steps)
        self._set_active_session_text(self._t("续录中", "Continuing"))
        self._refresh_controls()
        if target_session_dir is not None and target_session_dir != session_dir:
            self._set_status(self._t(
                f"已复制到本地暂存目录并开始续录，停止后将同步回: {target_session_dir}",
                f"Copied to local staging and continuing. It will sync back on stop: {target_session_dir}",
            ))
        else:
            self._set_status(message)
        self.logger.info("Import-and-continue completed | session_dir=%s | target_session_dir=%s", session_dir, target_session_dir)

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
        active_sync_count = self._get_active_sync_job_count()
        if active_sync_count > 0:
            if not messagebox.askyesno(
                self._t("同步仍在进行", "Sync still in progress"),
                self._t(
                    f"当前还有 {active_sync_count} 个录制结果正在同步到共享目录。\n\n关闭应用会中断正在进行的网络复制；本地暂存目录会保留，可稍后手动处理。\n\n确定要关闭吗？",
                    f"There are {active_sync_count} recording sync job(s) still running.\n\nClosing the app will interrupt the network copy. The local staging folder will remain available for manual recovery.\n\nClose anyway?",
                ),
                parent=self.root,
                icon="warning",
                default="no",
            ):
                return
        self.design_steps_overlay.destroy()
        self.root.destroy()

    def _set_active_session_text(self, prefix: str) -> None:
        session_dir = self.engine.store.session_dir
        if session_dir is None:
            self.session_var.set(prefix)
            return
        self.session_var.set(f"{prefix}: {session_dir.name}")

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
        self.android_button.configure(state=tk.NORMAL)

    def open_android_recorder(self) -> None:
        if self.android_recorder_dialog is not None and self.android_recorder_dialog.is_open():
            self.android_recorder_dialog.focus()
            return
        self.android_recorder_dialog = AndroidRecorderDialog(
            self.root,
            output_dir=self.recordings_target_root,
            ui_language=self.current_settings.ui_language,
            settings_store=self.settings_store,
            is_main_recording_active=lambda: self.engine.is_recording,
            get_main_session_metadata_draft=lambda: self.session_metadata_draft,
            get_main_session_store=lambda: self.engine.store if self.engine.is_recording and self.engine.store.session_dir is not None and self.engine.store.data is not None else None,
        )

    def _should_sync_session_to_target(self, local_session_dir: Path) -> bool:
        if not self.sync_recordings_to_target:
            return False
        try:
            local_session_dir.resolve().relative_to(self.recording_work_root.resolve())
            return True
        except Exception:
            return False

    def _prepare_session_for_continue(self, session_dir: Path) -> tuple[Path, Path | None, SessionLockHandle | None]:
        target_session_dir = Path(session_dir)
        if not self._should_stage_session_for_continue(target_session_dir):
            return target_session_dir, None, None

        target_lock_handle = acquire_session_lock(target_session_dir, owner_kind="recorder", owner_label="Recorder Import/Continue")
        try:
            relative_session_path = target_session_dir.resolve().relative_to(self.recordings_target_root.resolve())
            local_session_dir = self.recording_work_root / relative_session_path
            self._copy_session_to_local_staging(target_session_dir, local_session_dir)
            self._rewrite_session_paths(local_session_dir)
            self.logger.info(
                "Imported session staged locally | target_session_dir=%s | local_session_dir=%s",
                target_session_dir,
                local_session_dir,
            )
            return local_session_dir, target_session_dir, target_lock_handle
        except Exception:
            target_lock_handle.release()
            raise

    def _should_stage_session_for_continue(self, session_dir: Path) -> bool:
        if not self.sync_recordings_to_target:
            return False
        try:
            session_dir.resolve().relative_to(self.recordings_target_root.resolve())
        except Exception:
            return False
        try:
            session_dir.resolve().relative_to(self.recording_work_root.resolve())
            return False
        except Exception:
            return True

    def _copy_session_to_local_staging(self, source_session_dir: Path, local_session_dir: Path) -> None:
        local_session_dir.parent.mkdir(parents=True, exist_ok=True)
        temp_session_dir = local_session_dir.with_name(f"{local_session_dir.name}.importing")
        if temp_session_dir.exists():
            shutil.rmtree(temp_session_dir)
        if local_session_dir.exists():
            shutil.rmtree(local_session_dir)

        files = self._collect_sync_files(source_session_dir)
        total_files = len(files)
        total_bytes = sum(size for _path, size in files)
        temp_session_dir.mkdir(parents=True, exist_ok=True)
        for directory in source_session_dir.rglob("*"):
            if directory.is_dir():
                (temp_session_dir / directory.relative_to(source_session_dir)).mkdir(parents=True, exist_ok=True)

        copied_files = 0
        copied_bytes = 0
        for source_path, file_size in files:
            relative_path = source_path.relative_to(source_session_dir)
            target_path = temp_session_dir / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            copied_files += 1
            copied_bytes += int(file_size or 0)
            if copied_files == total_files or copied_files % 5 == 0:
                percent = 100.0 if total_files <= 0 else min(100.0, max(0.0, copied_files / total_files * 100.0))
                self._set_status(self._t(
                    f"正在复制到本地暂存目录: {source_session_dir.name} {percent:.1f}% ({copied_files}/{total_files} 个文件，{self._format_bytes(copied_bytes)}/{self._format_bytes(total_bytes)})",
                    f"Copying to local staging: {source_session_dir.name} {percent:.1f}% ({copied_files}/{total_files} files, {self._format_bytes(copied_bytes)}/{self._format_bytes(total_bytes)})",
                ))

        temp_session_dir.rename(local_session_dir)

    def _register_continued_session_target(self, local_session_dir: Path, target_session_dir: Path, lock_handle: SessionLockHandle) -> None:
        with self._sync_lock:
            self._continued_session_targets[str(local_session_dir)] = {
                "target_session_dir": str(target_session_dir),
                "lock_handle": lock_handle,
            }

    def _release_continued_session_target(self, local_session_dir: Path) -> None:
        with self._sync_lock:
            entry = self._continued_session_targets.pop(str(local_session_dir), None)
        if not isinstance(entry, dict):
            return
        lock_handle = entry.get("lock_handle")
        if isinstance(lock_handle, SessionLockHandle):
            lock_handle.release()

    def _sync_session_to_target(self, local_session_dir: Path) -> Path:
        relative_session_path = local_session_dir.resolve().relative_to(self.recording_work_root.resolve())
        target_session_dir = self.recordings_target_root / relative_session_path
        target_session_dir.parent.mkdir(parents=True, exist_ok=True)

        self._register_sync_job(local_session_dir, target_session_dir)
        files = self._collect_sync_files(local_session_dir)
        total_files = len(files)
        total_bytes = sum(size for _path, size in files)
        self._update_sync_progress(local_session_dir, 0, total_files, 0, total_bytes)

        if target_session_dir.exists():
            copy_target_dir = target_session_dir
            temp_session_dir = None
        else:
            temp_session_dir = target_session_dir.with_name(f"{target_session_dir.name}.syncing")
            if temp_session_dir.exists():
                shutil.rmtree(temp_session_dir)
            copy_target_dir = temp_session_dir

        self._copy_session_tree_with_progress(local_session_dir, copy_target_dir, files, total_bytes)
        if temp_session_dir is not None:
            temp_session_dir.rename(target_session_dir)
        self._rewrite_session_paths(target_session_dir)
        self.logger.info("Recording synced to target | local_session_dir=%s | target_session_dir=%s", local_session_dir, target_session_dir)
        return target_session_dir

    def _register_sync_job(self, local_session_dir: Path, target_session_dir: Path) -> None:
        key = str(local_session_dir)
        with self._sync_lock:
            self._active_sync_jobs[key] = {
                "local_session_dir": str(local_session_dir),
                "target_session_dir": str(target_session_dir),
                "files_copied": 0,
                "total_files": 0,
                "bytes_copied": 0,
                "total_bytes": 0,
            }

    def _update_sync_progress(
        self,
        local_session_dir: Path,
        files_copied: int,
        total_files: int,
        bytes_copied: int,
        total_bytes: int,
    ) -> None:
        key = str(local_session_dir)
        with self._sync_lock:
            job = self._active_sync_jobs.get(key)
            if job is not None:
                job.update(
                    {
                        "files_copied": files_copied,
                        "total_files": total_files,
                        "bytes_copied": bytes_copied,
                        "total_bytes": total_bytes,
                    }
                )
                active_count = len(self._active_sync_jobs)
            else:
                active_count = len(self._active_sync_jobs)

        percent = 100.0 if total_files <= 0 else min(100.0, max(0.0, files_copied / total_files * 100.0))
        active_suffix = f" | 后台同步任务 {active_count}" if active_count > 1 else ""
        self._set_status(
            self._t(
                f"正在同步到共享目录: {local_session_dir.name} {percent:.1f}% ({files_copied}/{total_files} 个文件，{self._format_bytes(bytes_copied)}/{self._format_bytes(total_bytes)}){active_suffix}；可继续录制下一条。",
                f"Syncing to shared folder: {local_session_dir.name} {percent:.1f}% ({files_copied}/{total_files} files, {self._format_bytes(bytes_copied)}/{self._format_bytes(total_bytes)}){active_suffix}; you can start the next recording.",
            )
        )

    def _finish_sync_job(self, local_session_dir: Path) -> None:
        with self._sync_lock:
            self._active_sync_jobs.pop(str(local_session_dir), None)

    def _get_active_sync_job_count(self) -> int:
        with self._sync_lock:
            return len(self._active_sync_jobs)

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, int(value or 0)))
        for unit in ("B", "KB", "MB", "GB"):
            if amount < 1024.0 or unit == "GB":
                if unit == "B":
                    return f"{int(amount)}{unit}"
                return f"{amount:.1f}{unit}"
            amount /= 1024.0
        return f"{amount:.1f}GB"

    @staticmethod
    def _collect_sync_files(source_dir: Path) -> list[tuple[Path, int]]:
        files: list[tuple[Path, int]] = []
        for path in source_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name == SESSION_LOCK_FILE_NAME:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append((path, int(size)))
        return files

    def _copy_session_tree_with_progress(self, source_dir: Path, target_dir: Path, files: list[tuple[Path, int]], total_bytes: int) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        for directory in source_dir.rglob("*"):
            if directory.is_dir():
                (target_dir / directory.relative_to(source_dir)).mkdir(parents=True, exist_ok=True)

        copied_files = 0
        copied_bytes = 0
        for source_path, file_size in files:
            relative_path = source_path.relative_to(source_dir)
            target_path = target_dir / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            copied_files += 1
            copied_bytes += int(file_size or 0)
            self._update_sync_progress(source_dir, copied_files, len(files), copied_bytes, total_bytes)

    def _rewrite_session_paths(self, session_dir: Path) -> None:
        session_path = session_dir / "session.json"
        if not session_path.exists():
            return
        payload = json.loads(session_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload["output_dir"] = str(session_dir)
        payload["screenshots_dir"] = str(session_dir / "screenshots")
        payload["media_dir"] = str(session_dir / "media")
        session_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        (session_dir / "session.yaml").write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        write_session_summary_from_session_payload(session_dir, payload)

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
        self.android_button.configure(text=self._t("Android 操作录制", "Android Operation Recorder"))
        self.settings_button.configure(text=self._t("设置", "Settings"))
        self.notes_frame.configure(text=self._t("说明", "Notes"))
        self.notes_label.configure(text=self._t(
            "1. 点击开始录制后，会先填写本次录制的 Session 元数据，再开始监听键盘、鼠标点击和滚轮事件。\n"
            "2. Comment 通过鼠标拖拽选择截图区域，再填写大文本说明。\n"
            "3. 等待事件支持框选等待区域并自动保存截图，当前第一版用于记录等待图片出现的步骤。\n"
            "4. 记录截图支持手动选区并保存到当前 Session 的 screenshots，可通过 Ctrl+F4 快捷键快速触发。\n"
            "5. AI Checkpoint 支持截图、区域视频录制、Query 调模型并保存返回内容；Ctrl+F5 可快速截图打开，Ctrl+F6 可选区后立即开始视频录制。\n"
            "6. 可手动点击保存，立即将当前 session 快照和 suggestions 落盘。\n"
            "7. 支持暂停/继续录制，以及导入已有 session 后继续录制。\n"
            "8. 停止录制会在后台收尾，不再阻塞整个窗口。\n"
            "9. Session 元数据在录制完成后也可以在 Session Viewer 中继续修改。\n"
            "10. Android 操作录制入口是独立面板，要求通过 scrcpy 操作手机，并结合 uiautomator2 抓取截图和控件信息，不会混入当前 Windows 录制链。",
            "1. When you start recording, the app first collects session metadata, then listens for keyboard, mouse-click, and wheel events.\n"
            "2. Comment lets you drag-select a screenshot region and enter a detailed note.\n"
            "3. Wait events let you select a wait region and save a screenshot for image-appearance wait steps.\n"
            "4. Capture Screenshot saves a manual region into the current session screenshots folder and can be triggered with Ctrl+F4.\n"
            "5. AI Checkpoint supports screenshots, region video capture, model queries, and saving the response; Ctrl+F5 quickly captures a screenshot and opens it, while Ctrl+F6 selects a region and starts video recording immediately.\n"
            "6. Save writes the current session snapshot and suggestions immediately.\n"
            "7. Recording can be paused/resumed, and you can continue from an existing session.\n"
            "8. Stopping recording completes background cleanup without blocking the window.\n"
            "9. Session metadata can still be edited later in Session Viewer.\n"
            "10. Android operation recording lives in a separate panel, requires operating the phone through scrcpy, and combines screenshots plus uiautomator2 element data instead of the current Windows recording pipeline."
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
    try:
        root.mainloop()
    except KeyboardInterrupt:
        logger.info("Application interrupted from terminal/debugger")
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass