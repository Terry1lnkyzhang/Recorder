from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import tkinter as tk

import requests
import yaml
from PIL import Image

from src.ai import AISuggestionService
from src.ai.client import OpenAICompatibleAIClient
from src.ai.method_mapping import resolve_method_name_for_event
from src.ai.remote_service_client import RemoteAIServiceClient
from src.ai.session_analyzer import SessionWorkflowAnalyzer
from src.common.display_utils import prepare_image_path_for_ai
from src.common.image_widgets import ZoomableImageView
from src.common.media_utils import load_video_preview_frame
from src.common.runtime_paths import get_recordings_dir, get_resource_root, get_settings_path
from src.common.session_discovery import find_latest_session_dir, scan_session_candidates
from src.converter.compiler import build_atframework_yaml_dict, export_suggestions_to_atframework_yaml
from src.recorder.models import format_recorded_action, normalize_event_type, normalize_keyboard_key_name
from src.recorder.dialogs import (
    AICheckpointDraft,
    capture_manual_screenshot,
    open_ai_checkpoint_dialog,
    open_ai_checkpoint_editor_dialog,
    open_comment_dialog,
    open_wait_for_image_dialog,
)
from src.recorder.recorder import RecorderEngine
from src.recorder.settings import SettingsStore
from src.recorder.system_info import safe_relpath
from src.recorder.session_metadata_ai import build_missing_summary, format_keyword_terms, should_prompt_ai_analysis, analyze_session_metadata, merge_keyword_text
from .cleaning import CleaningSuggestion, apply_cleaning_suggestions, build_cleaning_suggestions


class RecorderViewerWindow:
    def __init__(self, master: tk.Misc, initial_path: Path | None = None) -> None:
        self.window = tk.Toplevel(master)
        self.window.title("Recorder Session Viewer")
        self.window.geometry("1440x900")
        self.window.minsize(1180, 760)

        self.session_dir: Path | None = None
        self.session_data: dict[str, object] | None = None
        self.event_rows: list[dict[str, object]] = []
        self.media_views: list[ZoomableImageView] = []
        self.media_tab_pool: list[dict[str, object]] = []
        self.media_cache: dict[str, Image.Image | None] = {}
        self.current_media_token = 0
        self._top_pane_ratio_initialized = False
        self._content_pane_ratio_initialized = False
        self._tree_reload_after_id: str | None = None
        self._pending_tree_rows: list[tuple[int, dict[str, object]]] = []
        self._tree_batch_size = 200
        self._pending_tree_selection: list[int] = []
        self._pending_tree_focus: int | None = None
        self.event_list_window: tk.Toplevel | None = None
        self.event_list_tree: ttk.Treeview | None = None
        self.event_list_status_var: tk.StringVar | None = None
        self.popup_process_filter_combo: ttk.Combobox | None = None
        self._event_list_reload_after_id: str | None = None
        self._pending_event_list_rows: list[tuple[int, dict[str, object]]] = []
        self._event_list_batch_size = 300
        self._session_picker_scan_token = 0
        self._session_load_token = 0
        self._session_candidate_cache: dict[str, dict[str, object]] = {}
        self._synchronizing_tree_selection = False
        self.cleaning_suggestions: list[CleaningSuggestion] = []
        self.ai_analysis: dict[str, object] | None = None
        self.ai_step_tags: dict[int, str] = {}
        self.ai_step_texts: dict[int, str] = {}
        self.ai_process_summary_texts: dict[int, str] = {}
        self.suggestion_service = AISuggestionService()
        self.suggestion_result = None
        self.step_method_suggestions: dict[int, str] = {}
        self.step_module_suggestions: dict[int, str] = {}
        self.step_parameter_summaries: dict[int, str] = {}
        self.parameter_prompt_by_step: dict[int, str] = {}
        self.parameter_response_by_step: dict[int, str] = {}
        self.current_analyzer: SessionWorkflowAnalyzer | OpenAICompatibleAIClient | None = None
        self.close_callback = None
        self.analysis_running = False
        self.analysis_cancel_event = threading.Event()
        self.coverage_query_running = False
        self.parameter_recommendation_running = False
        self.suggestion_generation_running = False
        self.export_yaml_running = False
        self.debug_run_running = False
        self.analysis_started_at = 0.0
        self.analysis_status_base = "未执行 AI 分析"
        self.analysis_status_token = 0
        project_root = get_resource_root()
        self.project_root = project_root
        self.recordings_root = get_recordings_dir()
        self.settings_store = SettingsStore(get_settings_path())

        self.path_var = tk.StringVar(value="未加载 session")
        self.summary_var = tk.StringVar(value="请选择录制目录")
        self.load_status_var = tk.StringVar(value="")
        self.cleaning_var = tk.StringVar(value="未分析清洗建议")
        self.ai_var = tk.StringVar(value="未执行 AI 分析")
        self.suggestion_var = tk.StringVar(value="未生成调用建议")
        self.parameter_progress_var = tk.StringVar(value="参数推荐批处理未执行")
        self.coverage_status_var = tk.StringVar(value="请先执行 AI 分析，再进行覆盖判断")
        self.parameter_status_var = tk.StringVar(value="请选择左侧步骤并先生成调用建议。")
        self.preview_single_monitor_var = tk.BooleanVar(value=False)
        self.media_summary_var = tk.StringVar(value="当前事件无媒体")
        self.process_filter_var = tk.StringVar(value="全部进程")
        self.event_type_filter_var = tk.StringVar(value="全部类型")
        self.action_filter_var = tk.StringVar(value="全部动作")
        self.process_filter_values: tuple[str, ...] = ("全部进程",)
        self.event_type_filter_values: tuple[str, ...] = ("全部类型",)
        self.action_filter_values: tuple[str, ...] = ("全部动作",)
        self.session_testcase_id_var = tk.StringVar()
        self.session_version_number_var = tk.StringVar()
        self.session_name_var = tk.StringVar()
        self.session_recorder_person_var = tk.StringVar()
        self.session_is_prs_recording_var = tk.StringVar(value="是")
        self.session_scope_var = tk.StringVar(value="All")
        self.session_metadata_ai_running = False

        self._build_ui()
        self.window.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.window.bind("<Left>", lambda event: self._handle_navigation_key(event, -1))
        self.window.bind("<Right>", lambda event: self._handle_navigation_key(event, 1))
        self.window.bind("<Control-a>", self._handle_select_all_shortcut)
        if initial_path:
            self._try_load_initial_path(initial_path)

    def set_close_callback(self, callback) -> None:
        self.close_callback = callback

    def _handle_close(self) -> None:
        if self.analysis_running:
            self.cancel_ai_analysis()
        self._close_event_list_window()
        if self.close_callback:
            self.close_callback()
            return
        self.window.destroy()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.window, padding=(16, 12))
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="选择 Session 目录", command=self.select_session_dir).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="打开当前 Session 目录", command=self.open_current_session_dir).pack(side=tk.LEFT, padx=(8, 0))
        self.ai_button = ttk.Button(toolbar, text="AI分析", command=self.run_ai_analysis)
        self.ai_button.pack(side=tk.LEFT, padx=(8, 0))
        self.selected_ai_button = ttk.Button(toolbar, text="AI分析选中行", command=self.run_selected_ai_analysis)
        self.selected_ai_button.pack(side=tk.LEFT, padx=(8, 0))
        self.ai_process_summary_button = ttk.Button(toolbar, text="AI总结", command=self.run_ai_process_summary)
        self.ai_process_summary_button.pack(side=tk.LEFT, padx=(8, 0))
        self.load_ai_button = ttk.Button(toolbar, text="加载历史AI结果", command=self.load_historical_ai_analysis, state=tk.DISABLED)
        self.load_ai_button.pack(side=tk.LEFT, padx=(8, 0))
        self.cancel_ai_button = ttk.Button(toolbar, text="终止AI分析", command=self.cancel_ai_analysis, state=tk.DISABLED)
        self.cancel_ai_button.pack(side=tk.LEFT, padx=(8, 0))
        self.generate_suggestion_button = ttk.Button(toolbar, text="为当前步骤生成方法建议", command=self.run_method_suggestion_generation)
        self.generate_suggestion_button.pack(side=tk.LEFT, padx=(8, 0))
        self.parameter_recommend_button = ttk.Button(toolbar, text="为当前步骤生成参数推荐", command=self.run_parameter_recommendation)
        self.parameter_recommend_button.pack(side=tk.LEFT, padx=(8, 0))
        self.export_yaml_button = ttk.Button(toolbar, text="转成ATFramework YAML", command=self.export_atframework_yaml)
        self.export_yaml_button.pack(side=tk.LEFT, padx=(8, 0))
        self.debug_run_button = ttk.Button(toolbar, text="调试", command=self.debug_atframework_steps)
        self.debug_run_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="全选步骤", command=self.select_all_events).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="应用AI删除建议", command=self.apply_ai_deletions).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="数据清洗", command=self.preview_cleaning).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="应用清洗", command=self.apply_cleaning,).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="清除高亮", command=self.clear_cleaning_highlight).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="弹出事件列表", command=self.open_event_list_window).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(toolbar, text="进程筛选").pack(side=tk.LEFT, padx=(12, 0))
        self.process_filter_combo = ttk.Combobox(toolbar, textvariable=self.process_filter_var, state="readonly", width=24)
        self.process_filter_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.process_filter_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_filter_changed())
        ttk.Label(toolbar, textvariable=self.path_var).pack(side=tk.LEFT, padx=12)

        summary = ttk.Label(self.window, textvariable=self.summary_var, anchor=tk.W, padding=(16, 0))
        summary.pack(fill=tk.X)
        ai_summary = ttk.Label(self.window, textvariable=self.ai_var, anchor=tk.W, padding=(16, 0))
        ai_summary.pack(fill=tk.X)
        suggestion_summary = ttk.Label(self.window, textvariable=self.suggestion_var, anchor=tk.W, padding=(16, 0))
        suggestion_summary.pack(fill=tk.X)
        parameter_progress = ttk.Label(self.window, textvariable=self.parameter_progress_var, anchor=tk.W, padding=(16, 0))
        parameter_progress.pack(fill=tk.X)
        load_status = ttk.Label(self.window, textvariable=self.load_status_var, anchor=tk.W, padding=(16, 0))
        load_status.pack(fill=tk.X)
        cleaning = ttk.Label(self.window, textvariable=self.cleaning_var, anchor=tk.W, padding=(16, 0))
        cleaning.pack(fill=tk.X)

        content = ttk.Panedwindow(self.window, orient=tk.VERTICAL)
        content.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        top = ttk.Panedwindow(content, orient=tk.HORIZONTAL)
        table_frame = ttk.LabelFrame(content, text="事件列表")
        content.add(top, weight=6)
        content.add(table_frame, weight=4)
        content.bind("<Configure>", lambda _event: self._set_vertical_paned_ratio(content, 0.56), add="+")

        image_frame = ttk.LabelFrame(top, text="媒体预览")
        right = ttk.LabelFrame(top, text="事件明细")
        top.add(image_frame, weight=7)
        top.add(right, weight=3)
        top.bind("<Configure>", lambda _event: self._set_paned_ratio(top, 0.75), add="+")

        image_toolbar = ttk.Frame(image_frame)
        image_toolbar.pack(fill=tk.X, padx=12, pady=(8, 0))
        ttk.Checkbutton(
            image_toolbar,
            text="仅预览有操作的单屏",
            variable=self.preview_single_monitor_var,
            command=self._on_preview_mode_changed,
        ).pack(side=tk.LEFT)
        ttk.Label(image_toolbar, textvariable=self.media_summary_var).pack(side=tk.LEFT, padx=(12, 0))

        left = ttk.Frame(table_frame)
        left.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            left,
            columns=("idx", "event_type", "action", "time", "process_name", "method_suggestion", "parameter_suggestion", "comment", "ai_note", "ai_summary", "module_suggestion"),
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("event_type", text=self._build_filter_heading_text("type"))
        self.tree.heading("action", text=self._build_filter_heading_text("action"))
        self.tree.heading("time", text="时间")
        self.tree.heading("process_name", text=self._build_filter_heading_text("process"))
        self.tree.heading("method_suggestion", text="方法建议")
        self.tree.heading("parameter_suggestion", text="参数建议")
        self.tree.heading("comment", text="Comment")
        self.tree.heading("ai_note", text="AI看图")
        self.tree.heading("ai_summary", text="AI总结")
        self.tree.heading("module_suggestion", text="模块建议")
        self.tree.column("idx", width=42, minwidth=36, anchor=tk.CENTER, stretch=False)
        self.tree.column("event_type", width=110, minwidth=92, anchor=tk.W, stretch=False)
        self.tree.column("action", width=92, minwidth=76, anchor=tk.W, stretch=False)
        self.tree.column("time", width=132, minwidth=118, anchor=tk.W, stretch=False)
        self.tree.column("process_name", width=150, minwidth=120, anchor=tk.W, stretch=False)
        self.tree.column("method_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)
        self.tree.column("parameter_suggestion", width=320, minwidth=180, anchor=tk.W, stretch=False)
        self.tree.column("comment", width=220, minwidth=140, anchor=tk.W, stretch=True)
        self.tree.column("ai_note", width=420, minwidth=240, anchor=tk.W, stretch=False)
        self.tree.column("ai_summary", width=420, minwidth=240, anchor=tk.W, stretch=False)
        self.tree.column("module_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_event)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self._on_event_tree_context_menu, add="+")
        self.tree.bind("<Button-1>", self._on_event_tree_mouse_down, add="+")
        self.tree.tag_configure("clean-delete", background="#5c1f1f", foreground="#ffe7e7")
        self.tree.tag_configure("clean-merge", background="#4e3f12", foreground="#fff6d7")
        self.tree.tag_configure("clean-review", background="#17354d", foreground="#d9f0ff")
        self.tree.tag_configure("ai-delete", background="#3d184f", foreground="#f2dcff")
        self.tree.tag_configure("ai-review", background="#113f2d", foreground="#ddffef")

        tree_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        tree_x_scroll = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=self.tree.xview)
        tree_x_scroll.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=tree_x_scroll.set)

        self.media_notebook = ttk.Notebook(image_frame)
        self.media_notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self._ensure_media_tabs(1)

        self.details_notebook = ttk.Notebook(right)
        self.details_notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        event_tab = ttk.Frame(self.details_notebook)
        metadata_tab = ttk.Frame(self.details_notebook)
        self.ai_tab = ttk.Frame(self.details_notebook)
        self.ai_chat_tab = ttk.Frame(self.details_notebook)
        coverage_tab = ttk.Frame(self.details_notebook)
        self.details_notebook.add(event_tab, text="事件明细")
        self.details_notebook.add(metadata_tab, text="Session 元数据")
        self.details_notebook.add(self.ai_tab, text="AI看图")
        self.details_notebook.add(self.ai_chat_tab, text="AI Chat")
        self.details_notebook.add(coverage_tab, text="AI总结/覆盖")

        self.details_text = tk.Text(event_tab, wrap=tk.WORD, font=("Consolas", 10))
        self.details_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        details_scroll = ttk.Scrollbar(event_tab, orient=tk.VERTICAL, command=self.details_text.yview)
        details_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.details_text.configure(yscrollcommand=details_scroll.set, state=tk.DISABLED)

        self._build_session_metadata_tab(metadata_tab)

        self._build_ai_panel(self.ai_tab)
        self._build_ai_chat_panel(self.ai_chat_tab)
        self._build_coverage_panel(coverage_tab)

    def _build_session_metadata_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(4, weight=1)
        parent.rowconfigure(6, weight=1)
        parent.rowconfigure(7, weight=1)
        parent.rowconfigure(8, weight=1)

        ttk.Label(parent, text="是否PRS用例录制").grid(row=0, column=0, sticky=tk.W, padx=8, pady=(12, 6))
        self.session_prs_combo = ttk.Combobox(parent, textvariable=self.session_is_prs_recording_var, state="readonly", values=("是", "否"), width=12)
        self.session_prs_combo.grid(row=0, column=1, sticky=tk.W, padx=8, pady=(12, 6))
        self.session_prs_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_session_metadata_mode())

        self.session_testcase_id_label = ttk.Label(parent, text="Testcase ID")
        self.session_testcase_id_label.grid(row=1, column=0, sticky=tk.W, padx=8, pady=6)
        self.session_testcase_id_entry = ttk.Entry(parent, textvariable=self.session_testcase_id_var)
        self.session_testcase_id_entry.grid(row=1, column=1, sticky=tk.EW, padx=8, pady=6)

        self.session_version_number_label = ttk.Label(parent, text="Version Number")
        self.session_version_number_label.grid(row=2, column=0, sticky=tk.W, padx=8, pady=6)
        self.session_version_number_entry = ttk.Entry(parent, textvariable=self.session_version_number_var)
        self.session_version_number_entry.grid(row=2, column=1, sticky=tk.EW, padx=8, pady=6)

        self.session_name_label = ttk.Label(parent, text="Name")
        self.session_name_entry = ttk.Entry(parent, textvariable=self.session_name_var)

        ttk.Label(parent, text="录制人员").grid(row=3, column=0, sticky=tk.W, padx=8, pady=6)
        ttk.Entry(parent, textvariable=self.session_recorder_person_var).grid(row=3, column=1, sticky=tk.EW, padx=8, pady=6)

        ttk.Label(parent, text="Design Steps").grid(row=4, column=0, sticky=tk.NW, padx=8, pady=6)
        self.session_design_steps_text = tk.Text(parent, height=10, wrap=tk.WORD, font=("Consolas", 10))
        self.session_design_steps_text.grid(row=4, column=1, sticky="nsew", padx=8, pady=6)

        ttk.Label(parent, text="前置条件").grid(row=6, column=0, sticky=tk.NW, padx=8, pady=6)
        self.session_preconditions_text = tk.Text(parent, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.session_preconditions_text.grid(row=6, column=1, sticky="nsew", padx=8, pady=6)

        ttk.Label(parent, text="配置要求").grid(row=7, column=0, sticky=tk.NW, padx=8, pady=6)
        self.session_configuration_requirements_text = tk.Text(parent, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.session_configuration_requirements_text.grid(row=7, column=1, sticky="nsew", padx=8, pady=6)

        ttk.Label(parent, text="额外设备").grid(row=8, column=0, sticky=tk.NW, padx=8, pady=6)
        self.session_extra_devices_text = tk.Text(parent, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.session_extra_devices_text.grid(row=8, column=1, sticky="nsew", padx=8, pady=6)

        ttk.Label(parent, text="Scope").grid(row=9, column=0, sticky=tk.W, padx=8, pady=6)
        self.session_scope_combo = ttk.Combobox(parent, textvariable=self.session_scope_var, state="readonly", values=("All", "Sub"), width=12)
        self.session_scope_combo.grid(row=9, column=1, sticky=tk.W, padx=8, pady=6)

        ttk.Label(parent, text="以上 3 项建议按词语/短语填写，每行一个。", justify=tk.LEFT).grid(row=10, column=1, sticky=tk.W, padx=8, pady=(0, 4))

        actions = ttk.Frame(parent)
        actions.grid(row=11, column=0, columnspan=2, sticky=tk.EW, padx=8, pady=(6, 12))
        self.session_metadata_ai_button = ttk.Button(actions, text="AI分析", command=self.run_session_metadata_ai_analysis)
        self.session_metadata_ai_button.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(actions, text="保存 Session 元数据", command=self.save_session_metadata).pack(side=tk.LEFT)
        self.session_metadata_status_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.session_metadata_status_var).pack(side=tk.LEFT, padx=(12, 0))
        self._refresh_session_metadata_mode()

    def _is_session_prs_recording_selected(self) -> bool:
        return self.session_is_prs_recording_var.get().strip() != "否"

    def _refresh_session_metadata_mode(self) -> None:
        is_prs = self._is_session_prs_recording_selected()
        if is_prs:
            self.session_name_var.set("")
            self.session_name_label.grid_remove()
            self.session_name_entry.grid_remove()
            self.session_testcase_id_label.grid(row=1, column=0, sticky=tk.W, padx=8, pady=6)
            self.session_testcase_id_entry.grid(row=1, column=1, sticky=tk.EW, padx=8, pady=6)
            self.session_version_number_label.grid(row=2, column=0, sticky=tk.W, padx=8, pady=6)
            self.session_version_number_entry.grid(row=2, column=1, sticky=tk.EW, padx=8, pady=6)
            return

        self.session_testcase_id_var.set("")
        self.session_version_number_var.set("")
        self.session_testcase_id_label.grid_remove()
        self.session_testcase_id_entry.grid_remove()
        self.session_version_number_label.grid_remove()
        self.session_version_number_entry.grid_remove()
        self.session_name_label.grid(row=1, column=0, sticky=tk.W, padx=8, pady=6)
        self.session_name_entry.grid(row=1, column=1, sticky=tk.EW, padx=8, pady=6)

    def _set_paned_ratio(self, paned: ttk.Panedwindow, left_ratio: float) -> None:
        if self._top_pane_ratio_initialized:
            return
        try:
            total_width = paned.winfo_width()
            if total_width > 1:
                paned.sashpos(0, int(total_width * left_ratio))
                self._top_pane_ratio_initialized = True
        except Exception:
            return

    def _set_vertical_paned_ratio(self, paned: ttk.Panedwindow, top_ratio: float) -> None:
        if self._content_pane_ratio_initialized:
            return
        try:
            total_height = paned.winfo_height()
            if total_height > 1:
                paned.sashpos(0, int(total_height * top_ratio))
                self._content_pane_ratio_initialized = True
        except Exception:
            return

    def select_session_dir(self) -> None:
        self._show_session_picker()

    def open_current_session_dir(self) -> None:
        if not self.session_dir:
            messagebox.showinfo("提示", "当前还没有加载 Session，请先选择 Session 目录。", parent=self.window)
            return
        if not self.session_dir.exists():
            messagebox.showerror("打开失败", f"目录不存在:\n{self.session_dir}", parent=self.window)
            return
        self._open_path(self.session_dir)

    def _try_load_initial_path(self, initial_path: Path) -> None:
        candidate = initial_path
        if candidate.is_file():
            candidate = candidate.parent
        if candidate.name == "recordings":
            latest = self._find_latest_session(candidate)
            if latest:
                self.load_session(latest)
            return
        if (candidate / "session.json").exists():
            self.load_session(candidate)

    def _show_session_picker(self) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title("选择 Session")
        dialog.geometry("760x520")
        dialog.transient(self.window)
        dialog.grab_set()

        ttk.Label(dialog, text="从 recordings 中选择一个 session。", padding=(16, 12, 16, 4)).pack(anchor=tk.W)
        path_var = tk.StringVar(value=str(self.recordings_root))
        ttk.Label(dialog, textvariable=path_var, padding=(16, 0, 16, 8)).pack(anchor=tk.W)

        columns = ("name", "modified", "events")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        tree.heading("name", text="Session 目录")
        tree.heading("modified", text="最后修改时间")
        tree.heading("events", text="事件数")
        tree.column("name", width=330, anchor=tk.W)
        tree.column("modified", width=180, anchor=tk.W, stretch=False)
        tree.column("events", width=80, anchor=tk.CENTER, stretch=False)
        tree.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        status_var = tk.StringVar(value="正在扫描 Session...")
        ttk.Label(dialog, textvariable=status_var, padding=(16, 0, 16, 8)).pack(anchor=tk.W)

        button_bar = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        button_bar.pack(fill=tk.X)

        sessions: list[dict[str, object]] = []

        def populate(force_refresh: bool = False) -> None:
            self._session_picker_scan_token += 1
            token = self._session_picker_scan_token
            status_var.set("正在扫描 Session...")
            for item_id in tree.get_children():
                tree.delete(item_id)

            def worker() -> None:
                try:
                    items = self._find_session_candidates(self.recordings_root, force_refresh=force_refresh)
                except Exception as exc:
                    self.window.after(0, lambda: status_var.set(f"扫描 Session 失败: {exc}"))
                    return

                def apply_results() -> None:
                    if not dialog.winfo_exists() or token != self._session_picker_scan_token:
                        return
                    sessions.clear()
                    sessions.extend(items)
                    path_var.set(str(self.recordings_root))
                    status_var.set(f"共找到 {len(sessions)} 个 Session")
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

                self.window.after(0, apply_results)

            threading.Thread(target=worker, daemon=True).start()

        def confirm() -> None:
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("提示", "请选择一个 session。", parent=dialog)
                return
            session_dir = Path(str(sessions[int(selection[0])]["path"]))
            dialog.destroy()
            self.load_session(session_dir)

        ttk.Button(button_bar, text="刷新", command=lambda: populate(force_refresh=True)).pack(side=tk.LEFT)
        ttk.Button(button_bar, text="打开 recordings 目录", command=lambda: self._open_path(self.recordings_root)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(button_bar, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(button_bar, text="加载所选 Session", command=confirm).pack(side=tk.RIGHT, padx=(0, 8))

        tree.bind("<Double-1>", lambda _event: confirm())
        populate()
        dialog.lift()
        dialog.focus_force()

    def _prompt_session_to_import(self) -> Path | None:
        sessions = self._find_session_candidates(self.recordings_root)
        if not sessions:
            messagebox.showinfo("提示", f"未在以下目录找到可导入的 session:\n{self.recordings_root}", parent=self.window)
            return None

        dialog = tk.Toplevel(self.window)
        dialog.title("选择要导入的 Session")
        dialog.geometry("760x520")
        dialog.minsize(680, 420)
        dialog.transient(self.window)
        dialog.grab_set()

        selected_path: Path | None = None

        ttk.Label(dialog, text="请选择一个已有 session 导入到当前插入位置。", padding=(16, 12, 16, 4)).pack(anchor=tk.W)
        ttk.Label(dialog, text=str(self.recordings_root), padding=(16, 0, 16, 8)).pack(anchor=tk.W)

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
            candidate = Path(str(sessions[int(selection[0])]["path"])).resolve()
            if self.session_dir and candidate == self.session_dir.resolve():
                messagebox.showinfo("提示", "不能导入当前正在查看的 session。", parent=dialog)
                return
            selected_path = candidate
            dialog.destroy()

        ttk.Button(button_bar, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(button_bar, text="导入所选 Session", command=confirm).pack(side=tk.RIGHT, padx=(0, 8))

        tree.bind("<Double-1>", lambda _event: confirm())
        dialog.lift()
        dialog.focus_force()
        self.window.wait_window(dialog)
        return selected_path

    def open_event_list_window(self) -> None:
        if self.event_list_window and self.event_list_window.winfo_exists():
            self.event_list_window.deiconify()
            self.event_list_window.lift()
            self.event_list_window.focus_force()
            self._reload_event_list_popup()
            return

        popup = tk.Toplevel(self.window)
        popup.title("事件列表")
        popup.geometry("1320x720")
        popup.minsize(960, 520)
        self.event_list_window = popup

        toolbar = ttk.Frame(popup, padding=(12, 12, 12, 0))
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="进程筛选").pack(side=tk.LEFT)
        popup_filter = ttk.Combobox(toolbar, textvariable=self.process_filter_var, state="readonly", width=24)
        popup_filter.pack(side=tk.LEFT, padx=(6, 0))
        popup_filter.bind("<<ComboboxSelected>>", lambda _event: self._on_filter_changed())
        self.event_list_status_var = tk.StringVar(value="准备加载事件列表")
        ttk.Label(toolbar, textvariable=self.event_list_status_var).pack(side=tk.LEFT, padx=(12, 0))
        self.popup_process_filter_combo = popup_filter
        self._sync_filter_combo_values()

        wrapper = ttk.Frame(popup, padding=12)
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(0, weight=1)

        tree = ttk.Treeview(
            wrapper,
            columns=("idx", "event_type", "action", "time", "process_name", "method_suggestion", "parameter_suggestion", "comment", "ai_note", "ai_summary", "module_suggestion"),
            show="headings",
            selectmode="extended",
        )
        self._configure_event_tree_columns(tree)
        tree.grid(row=0, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", self._on_popup_tree_select)
        tree.bind("<Double-1>", self.on_double_click)
        tree.bind("<Button-3>", self._on_event_tree_context_menu, add="+")
        tree.bind("<Button-1>", self._on_event_tree_mouse_down, add="+")

        y_scroll = ttk.Scrollbar(wrapper, orient=tk.VERTICAL, command=tree.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(wrapper, orient=tk.HORIZONTAL, command=tree.xview)
        x_scroll.grid(row=1, column=0, columnspan=2, sticky="ew")
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.tag_configure("clean-delete", background="#5c1f1f", foreground="#ffe7e7")
        tree.tag_configure("clean-merge", background="#4e3f12", foreground="#fff6d7")
        tree.tag_configure("clean-review", background="#17354d", foreground="#d9f0ff")
        tree.tag_configure("ai-delete", background="#3d184f", foreground="#f2dcff")
        tree.tag_configure("ai-review", background="#113f2d", foreground="#ddffef")

        self.event_list_tree = tree
        popup.protocol("WM_DELETE_WINDOW", self._close_event_list_window)
        self._reload_event_list_popup()

    def _configure_event_tree_columns(self, tree: ttk.Treeview) -> None:
        tree.heading("idx", text="#")
        tree.heading("event_type", text=self._build_filter_heading_text("type"))
        tree.heading("action", text=self._build_filter_heading_text("action"))
        tree.heading("time", text="时间")
        tree.heading("process_name", text=self._build_filter_heading_text("process"))
        tree.heading("method_suggestion", text="方法建议")
        tree.heading("parameter_suggestion", text="参数建议")
        tree.heading("comment", text="Comment")
        tree.heading("ai_note", text="AI看图")
        tree.heading("ai_summary", text="AI总结")
        tree.heading("module_suggestion", text="模块建议")
        tree.column("idx", width=42, minwidth=36, anchor=tk.CENTER, stretch=False)
        tree.column("event_type", width=110, minwidth=92, anchor=tk.W, stretch=False)
        tree.column("action", width=92, minwidth=76, anchor=tk.W, stretch=False)
        tree.column("time", width=132, minwidth=118, anchor=tk.W, stretch=False)
        tree.column("process_name", width=150, minwidth=120, anchor=tk.W, stretch=False)
        tree.column("method_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)
        tree.column("parameter_suggestion", width=320, minwidth=180, anchor=tk.W, stretch=False)
        tree.column("comment", width=220, minwidth=140, anchor=tk.W, stretch=True)
        tree.column("ai_note", width=420, minwidth=240, anchor=tk.W, stretch=False)
        tree.column("ai_summary", width=420, minwidth=240, anchor=tk.W, stretch=False)
        tree.column("module_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)

    def _reload_event_list_popup(self) -> None:
        if not self.event_list_tree or not self.event_list_tree.winfo_exists():
            return
        self._cancel_event_list_reload()
        self._clear_tree(self.event_list_tree)
        self._pending_event_list_rows = [(row_index, self.event_rows[row_index]) for row_index in self._visible_row_indexes()]
        self._sync_filter_combo_values()
        total = len(self._pending_event_list_rows)
        if self.event_list_status_var is not None:
            self.event_list_status_var.set(f"正在加载事件 0/{total}")
        if not total:
            return
        self._load_next_event_list_batch(0)

    def _cancel_event_list_reload(self) -> None:
        if self._event_list_reload_after_id:
            try:
                self.window.after_cancel(self._event_list_reload_after_id)
            except Exception:
                pass
            self._event_list_reload_after_id = None
        self._pending_event_list_rows = []

    def _load_next_event_list_batch(self, inserted_count: int) -> None:
        if not self.event_list_tree or not self.event_list_tree.winfo_exists():
            self._event_list_reload_after_id = None
            self._pending_event_list_rows = []
            return

        batch = self._pending_event_list_rows[: self._event_list_batch_size]
        self._pending_event_list_rows = self._pending_event_list_rows[self._event_list_batch_size :]
        for row_index, event in batch:
            self.event_list_tree.insert(
                "",
                tk.END,
                iid=str(row_index),
                tags=self._build_row_tags(row_index),
                values=self._build_event_row_values(row_index, event),
            )

        inserted_count += len(batch)
        total = inserted_count + len(self._pending_event_list_rows)
        if self.event_list_status_var is not None:
            self.event_list_status_var.set(f"正在加载事件 {inserted_count}/{total}")

        if inserted_count >= total:
            self._event_list_reload_after_id = None
            if self.event_list_status_var is not None:
                self.event_list_status_var.set(f"已加载 {total} 条事件")
            row_index = self._get_primary_selected_row_index()
            if row_index is not None and self.event_list_tree.exists(str(row_index)):
                self.event_list_tree.selection_set(str(row_index))
                self.event_list_tree.focus(str(row_index))
                self.event_list_tree.see(str(row_index))
            return

        self._event_list_reload_after_id = self.window.after(1, lambda: self._load_next_event_list_batch(inserted_count))

    def _on_popup_tree_select(self, _event: object) -> None:
        if self._synchronizing_tree_selection or not self.event_list_tree:
            return
        selection = self.event_list_tree.selection()
        if not selection:
            return
        try:
            row_index = int(selection[0])
        except ValueError:
            return
        self._select_row_index(row_index, source_tree=self.event_list_tree)

    def _close_event_list_window(self) -> None:
        self._cancel_event_list_reload()
        if self.event_list_window and self.event_list_window.winfo_exists():
            self.event_list_window.destroy()
        self.event_list_window = None
        self.event_list_tree = None
        self.event_list_status_var = None
        self.popup_process_filter_combo = None

    def _find_session_candidates(self, base_dir: Path, force_refresh: bool = False) -> list[dict[str, object]]:
        return scan_session_candidates(
            base_dir,
            cache=self._session_candidate_cache,
            force_refresh=force_refresh,
        )

    def _find_latest_session(self, base_dir: Path) -> Path | None:
        return find_latest_session_dir(base_dir)

    def load_session(self, session_dir: Path) -> None:
        session_path = session_dir / "session.json"
        if not session_path.exists():
            messagebox.showerror("加载失败", f"未找到 session.json:\n{session_path}", parent=self.window)
            return

        self._session_load_token += 1
        token = self._session_load_token
        self.path_var.set(str(session_dir))
        self.load_status_var.set(f"正在加载 Session: {session_dir.name}")

        def worker() -> None:
            try:
                payload = json.loads(session_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("session.json 格式无效")
            except Exception as exc:
                self.window.after(0, lambda: self._on_load_session_failed(token, session_path, str(exc)))
                return

            self.window.after(0, lambda: self._apply_loaded_session(token, session_dir, payload))

        threading.Thread(target=worker, daemon=True).start()

    def _on_load_session_failed(self, token: int, session_path: Path, message: str) -> None:
        if token != self._session_load_token:
            return
        self.load_status_var.set("")
        messagebox.showerror("加载失败", f"无法读取 session.json:\n{session_path}\n\n{message}", parent=self.window)

    def _apply_loaded_session(self, token: int, session_dir: Path, payload: dict[str, object]) -> None:
        if token != self._session_load_token:
            return

        self.session_dir = session_dir
        self.session_data = payload
        self.event_rows = list(self.session_data.get("events", []))
        self.path_var.set(str(session_dir))
        self.cleaning_suggestions = []
        self.ai_analysis = None
        self.ai_step_tags = {}
        self.ai_step_texts = {}
        self.suggestion_result = None
        self.step_method_suggestions = {}
        self.step_module_suggestions = {}
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        self.cleaning_var.set("未分析清洗建议")
        self.ai_var.set(self._build_initial_ai_status_text(session_dir))
        self.suggestion_var.set(self._build_initial_suggestion_status_text(session_dir))
        self.parameter_progress_var.set("参数推荐批处理未执行")
        self.parameter_status_var.set("请选择左侧步骤并先生成调用建议。")
        self._set_text_widget(self.parameter_result_text, "")
        self._update_historical_ai_button_state()
        self._restore_historical_analysis_outputs()
        self._refresh_coverage_summary()
        self._refresh_filter_options()
        self._load_session_metadata_editor()
        self.summary_var.set(self._build_session_summary_text())
        self._reload_tree()
        self._reload_event_list_popup()

    def _build_session_summary_text(self) -> str:
        if not self.session_data:
            return "请选择录制目录"
        metadata = self._get_session_metadata()
        is_prs_recording = metadata.get("is_prs_recording", True)
        testcase_id = metadata.get("testcase_id", "")
        version_number = metadata.get("version_number", "")
        project = metadata.get("project", "")
        baseline_name = metadata.get("baseline_name", "")
        name = metadata.get("name", "")
        recorder_person = metadata.get("recorder_person", "")
        metadata_bits: list[str] = []
        if is_prs_recording and testcase_id:
            metadata_bits.append(f"TestcaseID={testcase_id}")
        if is_prs_recording and version_number:
            metadata_bits.append(f"Version={version_number}")
        if project:
            metadata_bits.append(f"Project={project}")
        if baseline_name:
            metadata_bits.append(f"Baseline={baseline_name}")
        if not is_prs_recording and name:
            metadata_bits.append(f"Name={name}")
        if recorder_person:
            metadata_bits.append(f"录制人员={recorder_person}")
        metadata_text = f" | {' | '.join(metadata_bits)}" if metadata_bits else ""
        return (
            f"session_id={self.session_data.get('session_id', '')} | 事件数={len(self.event_rows)} | 评论数={len(self.session_data.get('comments', []))} "
            f"| checkpoint数={len(self.session_data.get('checkpoints', []))}{metadata_text}"
        )

    def _get_session_metadata(self) -> dict[str, object]:
        if not self.session_data:
            return {
                "is_prs_recording": True,
                "testcase_id": "",
                "version_number": "",
                "project": "Taichi",
                "baseline_name": "",
                "name": "",
                "recorder_person": "",
                "design_steps": "",
                    "preconditions": "",
                    "configuration_requirements": "",
                    "extra_devices": "",
                "scope": "All",
            }
        metadata = self.session_data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        raw_prs_flag = metadata.get("is_prs_recording")
        if isinstance(raw_prs_flag, bool):
            is_prs_recording = raw_prs_flag
        else:
            is_prs_recording = not bool(str(metadata.get("name", "")).strip())
        scope = str(metadata.get("scope", "All")).strip() or "All"
        if scope not in {"All", "Sub"}:
            scope = "All"
        normalized = {
            "is_prs_recording": is_prs_recording,
            "testcase_id": str(metadata.get("testcase_id", "")) if is_prs_recording else "",
            "version_number": str(metadata.get("version_number", "")) if is_prs_recording else "",
            "project": str(metadata.get("project", "Taichi") or "Taichi"),
            "baseline_name": str(metadata.get("baseline_name", "")),
            "name": "" if is_prs_recording else str(metadata.get("name", "")),
            "recorder_person": str(metadata.get("recorder_person", "")),
            "design_steps": str(metadata.get("design_steps", "")),
            "preconditions": str(metadata.get("preconditions", "")),
            "configuration_requirements": str(metadata.get("configuration_requirements", "")),
            "extra_devices": str(metadata.get("extra_devices", "")),
            "scope": scope,
        }
        self.session_data["metadata"] = normalized
        return normalized

    def _load_session_metadata_editor(self) -> None:
        metadata = self._get_session_metadata()
        self.session_is_prs_recording_var.set("是" if bool(metadata["is_prs_recording"]) else "否")
        self.session_testcase_id_var.set(metadata["testcase_id"])
        self.session_version_number_var.set(metadata["version_number"])
        self.session_name_var.set(str(metadata["name"]))
        self.session_recorder_person_var.set(metadata["recorder_person"])
        self.session_scope_var.set(metadata["scope"])
        self.session_design_steps_text.delete("1.0", tk.END)
        self.session_design_steps_text.insert("1.0", metadata["design_steps"])
        self.session_preconditions_text.delete("1.0", tk.END)
        self.session_preconditions_text.insert("1.0", str(metadata.get("preconditions", "")))
        self.session_configuration_requirements_text.delete("1.0", tk.END)
        self.session_configuration_requirements_text.insert("1.0", str(metadata.get("configuration_requirements", "")))
        self.session_extra_devices_text.delete("1.0", tk.END)
        self.session_extra_devices_text.insert("1.0", str(metadata.get("extra_devices", "")))
        self.session_metadata_status_var.set("")
        self._refresh_session_metadata_mode()

    def _validate_session_metadata_payload(self, payload: dict[str, object]) -> str | None:
        is_prs_recording = bool(payload.get("is_prs_recording", True))
        if is_prs_recording:
            if not str(payload.get("testcase_id", "")).strip():
                return "请输入 Testcase ID。"
            if not str(payload.get("version_number", "")).strip():
                return "请输入 Version Number。"
        else:
            if not str(payload.get("name", "")).strip():
                return "请输入 Name。"
        if not str(payload.get("recorder_person", "")).strip():
            return "请输入录制人员。"
        if not str(payload.get("design_steps", "")).strip():
            return "请输入 Design Steps。"
        if str(payload.get("scope", "")).strip() not in {"All", "Sub"}:
            return "请选择 Scope。"
        return None

    def run_session_metadata_ai_analysis(self) -> None:
        if not self.session_data or self.session_metadata_ai_running:
            return

        metadata_payload = self._collect_session_metadata_payload()
        error_message = self._validate_session_metadata_payload(metadata_payload)
        if error_message:
            messagebox.showerror("元数据未填写完整", error_message, parent=self.window)
            return

        self.session_metadata_ai_running = True
        self.session_metadata_status_var.set("AI分析中...")
        self.session_metadata_ai_button.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = analyze_session_metadata(self.settings_store, metadata_payload)
            except Exception as exc:
                self.window.after(0, lambda: self._on_session_metadata_ai_failed(str(exc)))
                return
            self.window.after(0, lambda: self._on_session_metadata_ai_success(result, save_after=False, metadata_payload=metadata_payload))

        threading.Thread(target=worker, daemon=True).start()

    def save_session_metadata(self) -> None:
        if not self.session_data or not self.session_dir:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return

        metadata_payload = self._collect_session_metadata_payload()
        error_message = self._validate_session_metadata_payload(metadata_payload)
        if error_message:
            messagebox.showerror("元数据未填写完整", error_message, parent=self.window)
            return
        if should_prompt_ai_analysis(metadata_payload):
            if messagebox.askyesno("AI分析", "前置条件、配置要求、额外设备当前都为空，是否先让 AI 根据 Design Steps 生成建议？", parent=self.window):
                self.session_metadata_ai_running = True
                self.session_metadata_status_var.set("AI分析中...")
                self.session_metadata_ai_button.configure(state=tk.DISABLED)

                def worker() -> None:
                    try:
                        result = analyze_session_metadata(self.settings_store, metadata_payload)
                    except Exception as exc:
                        self.window.after(0, lambda: self._on_session_metadata_ai_failed(str(exc)))
                        return
                    self.window.after(0, lambda: self._on_session_metadata_ai_success(result, save_after=True, metadata_payload=metadata_payload))

                threading.Thread(target=worker, daemon=True).start()
                return

        self._validate_session_metadata_with_ai(metadata_payload)

    def _collect_session_metadata_payload(self) -> dict[str, object]:
        existing_metadata = self._get_session_metadata()
        return {
            "is_prs_recording": self._is_session_prs_recording_selected(),
            "testcase_id": self.session_testcase_id_var.get().strip(),
            "version_number": self.session_version_number_var.get().strip(),
            "project": str(existing_metadata.get("project", "")).strip(),
            "baseline_name": str(existing_metadata.get("baseline_name", "")).strip(),
            "name": self.session_name_var.get().strip(),
            "recorder_person": self.session_recorder_person_var.get().strip(),
            "design_steps": self.session_design_steps_text.get("1.0", tk.END).strip(),
            "preconditions": format_keyword_terms(self.session_preconditions_text.get("1.0", tk.END).strip().splitlines()),
            "configuration_requirements": format_keyword_terms(self.session_configuration_requirements_text.get("1.0", tk.END).strip().splitlines()),
            "extra_devices": format_keyword_terms(self.session_extra_devices_text.get("1.0", tk.END).strip().splitlines()),
            "scope": self.session_scope_var.get().strip() if self.session_scope_var.get().strip() in {"All", "Sub"} else "All",
        }

    def _validate_session_metadata_with_ai(self, metadata_payload: dict[str, object]) -> None:
        self.session_metadata_ai_running = True
        self.session_metadata_status_var.set("AI校验中...")
        self.session_metadata_ai_button.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = analyze_session_metadata(self.settings_store, metadata_payload)
            except Exception as exc:
                self.window.after(0, lambda: self._on_session_metadata_validation_failed(metadata_payload, str(exc)))
                return
            self.window.after(0, lambda: self._finalize_session_metadata_save(metadata_payload, result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_session_metadata_ai_success(self, result, save_after: bool, metadata_payload: dict[str, object]) -> None:
        self.session_metadata_ai_running = False
        self.session_metadata_ai_button.configure(state=tk.NORMAL)
        self.session_metadata_status_var.set("AI分析完成")
        self._set_text_widget(self.session_preconditions_text, merge_keyword_text(self.session_preconditions_text.get("1.0", tk.END), result.preconditions))
        self._set_text_widget(
            self.session_configuration_requirements_text,
            merge_keyword_text(self.session_configuration_requirements_text.get("1.0", tk.END), result.configuration_requirements),
        )
        self._set_text_widget(self.session_extra_devices_text, merge_keyword_text(self.session_extra_devices_text.get("1.0", tk.END), result.extra_devices))
        if save_after:
            refreshed_payload = self._collect_session_metadata_payload()
            self._finalize_session_metadata_save(refreshed_payload, result)
            return
        summary = build_missing_summary(result)
        if summary:
            messagebox.showinfo("AI分析建议", summary, parent=self.window)

    def _on_session_metadata_ai_failed(self, message: str) -> None:
        self.session_metadata_ai_running = False
        self.session_metadata_ai_button.configure(state=tk.NORMAL)
        self.session_metadata_status_var.set("AI分析失败")
        messagebox.showerror("AI分析失败", message, parent=self.window)

    def _on_session_metadata_validation_failed(self, metadata_payload: dict[str, object], message: str) -> None:
        self.session_metadata_ai_running = False
        self.session_metadata_ai_button.configure(state=tk.NORMAL)
        self.session_metadata_status_var.set("AI校验失败")
        if messagebox.askyesno("AI校验失败", f"AI 校验失败:\n{message}\n\n是否忽略并继续保存？", parent=self.window):
            self.session_data["metadata"] = metadata_payload
            self.summary_var.set(self._build_session_summary_text())
            self._persist_session()
            self.session_metadata_status_var.set("已保存")

    def _finalize_session_metadata_save(self, metadata_payload: dict[str, object], result) -> None:
        self.session_metadata_ai_running = False
        self.session_metadata_ai_button.configure(state=tk.NORMAL)
        summary = build_missing_summary(result)
        if summary:
            should_continue = messagebox.askyesno(
                "AI校验建议",
                "AI 认为当前内容可能还有遗漏:\n\n" + summary + "\n\n是否仍然继续保存？",
                parent=self.window,
            )
            if not should_continue:
                self.session_metadata_status_var.set("请根据建议调整后再保存")
                return
        self.session_data["metadata"] = metadata_payload
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        self.session_metadata_status_var.set("已保存")

    def _sync_filter_combo_values(self) -> None:
        process_values = self.process_filter_combo.cget("values")
        if self.popup_process_filter_combo and self.popup_process_filter_combo.winfo_exists():
            self.popup_process_filter_combo.configure(values=process_values)

    def _refresh_filter_options(self) -> None:
        process_names = sorted(
            {
                self._extract_process_name(event)
                for event in self.event_rows
                if self._extract_process_name(event) and self._matches_filter_selection(event, ignore_filter="process")
            }
        )
        process_values = ["全部进程", *process_names]
        self.process_filter_values = tuple(process_values)
        self.process_filter_combo.configure(values=process_values)
        current_process = self.process_filter_var.get().strip()
        if current_process not in process_values:
            self.process_filter_var.set("全部进程")

        event_types = sorted(
            {
                self._extract_event_type(event)
                for event in self.event_rows
                if self._extract_event_type(event) and self._matches_filter_selection(event, ignore_filter="type")
            }
        )
        type_values = ["全部类型", *event_types]
        self.event_type_filter_values = tuple(type_values)
        current_type = self.event_type_filter_var.get().strip()
        if current_type not in type_values:
            self.event_type_filter_var.set("全部类型")

        actions = sorted(
            {
                self._extract_event_action(event)
                for event in self.event_rows
                if self._extract_event_action(event) and self._matches_filter_selection(event, ignore_filter="action")
            }
        )
        action_values = ["全部动作", *actions]
        self.action_filter_values = tuple(action_values)
        current_action = self.action_filter_var.get().strip()
        if current_action not in action_values:
            self.action_filter_var.set("全部动作")

        self._sync_filter_combo_values()
        self._update_filter_headings()

    def _on_filter_changed(self) -> None:
        self._refresh_filter_options()
        self._reload_tree()

    def _build_filter_heading_text(self, column_name: str) -> str:
        if column_name == "process":
            selected_value = self.process_filter_var.get().strip()
            base_text = "进程"
            default_value = "全部进程"
        elif column_name == "type":
            selected_value = self.event_type_filter_var.get().strip()
            base_text = "类型"
            default_value = "全部类型"
        elif column_name == "action":
            selected_value = self.action_filter_var.get().strip()
            base_text = "动作"
            default_value = "全部动作"
        else:
            return column_name
        suffix = " ▼*" if selected_value and selected_value != default_value else " ▼"
        return f"{base_text}{suffix}"

    def _update_filter_headings(self) -> None:
        if hasattr(self, "tree") and self.tree.winfo_exists():
            self.tree.heading("event_type", text=self._build_filter_heading_text("type"))
            self.tree.heading("process_name", text=self._build_filter_heading_text("process"))
            self.tree.heading("action", text=self._build_filter_heading_text("action"))
        if self.event_list_tree and self.event_list_tree.winfo_exists():
            self.event_list_tree.heading("event_type", text=self._build_filter_heading_text("type"))
            self.event_list_tree.heading("process_name", text=self._build_filter_heading_text("process"))
            self.event_list_tree.heading("action", text=self._build_filter_heading_text("action"))

    def _on_event_tree_mouse_down(self, event: tk.Event) -> str | None:
        tree = event.widget if isinstance(event.widget, ttk.Treeview) else None
        if tree is None:
            return None
        region = tree.identify_region(event.x, event.y)
        if region == "separator":
            return None
        if region != "heading":
            return None
        column_id = tree.identify_column(event.x)
        column_map = {"#2": "type", "#3": "action", "#5": "process"}
        column_name = column_map.get(column_id)
        if not column_name:
            return None
        self._show_tree_column_filter_menu(column_name, event.x_root, event.y_root + 4)
        return "break"

    def _show_tree_column_filter_menu(self, column_name: str, x_root: int, y_root: int) -> None:
        menu = tk.Menu(self.window, tearoff=False)
        if column_name == "process":
            title = "进程筛选"
            values = self.process_filter_values
            selected_var = self.process_filter_var
        elif column_name == "type":
            title = "类型筛选"
            values = self.event_type_filter_values
            selected_var = self.event_type_filter_var
        else:
            title = "动作筛选"
            values = self.action_filter_values
            selected_var = self.action_filter_var

        menu.add_command(label=title, state=tk.DISABLED)
        menu.add_separator()
        for value in values:
            menu.add_radiobutton(
                label=value,
                value=value,
                variable=selected_var,
                command=self._on_filter_changed,
            )
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _on_event_tree_context_menu(self, event: tk.Event) -> str | None:
        tree = event.widget if isinstance(event.widget, ttk.Treeview) else None
        if tree is None:
            return None
        region = tree.identify_region(event.x, event.y)
        if region != "cell":
            return None
        row_id = tree.identify_row(event.y)
        if not row_id:
            return None
        try:
            row_index = int(row_id)
        except ValueError:
            return None

        current_selection = {item for item in tree.selection()}
        if row_id not in current_selection:
            self._select_row_index(row_index, source_tree=tree)
        else:
            tree.focus(row_id)

        selected_rows = self._get_selected_row_indexes()
        if not selected_rows:
            selected_rows = [row_index]

        menu = tk.Menu(self.window, tearoff=False)
        insert_steps_menu = tk.Menu(menu, tearoff=False)
        insert_steps_menu.add_command(label="录制", command=lambda idx=row_index: self.insert_recorded_steps_after_row(idx))
        insert_steps_menu.add_command(label="导入", command=lambda idx=row_index: self.insert_imported_steps_after_row(idx))
        menu.add_cascade(label="插入步骤", menu=insert_steps_menu)
        menu.add_command(label="插入 CheckPoint", command=lambda idx=row_index: self.insert_checkpoint_after_row(idx))
        menu.add_separator()
        delete_label = "删除选中行" if len(selected_rows) > 1 else "删除"
        menu.add_command(label=delete_label, command=self.delete_selected_events)

        if len(selected_rows) == 1:
            selected_event = self.event_rows[selected_rows[0]]
            if self._extract_event_type(selected_event) == "checkpoint":
                menu.add_command(label="修改", command=self.edit_selected_checkpoint)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _extract_event_type(self, event: dict[str, object]) -> str:
        return normalize_event_type(event.get("event_type", ""), event.get("action", ""))

    def _extract_event_action(self, event: dict[str, object]) -> str:
        return format_recorded_action(event.get("action", "")).strip()

    def _extract_combined_action(self, event: dict[str, object]) -> str:
        details = event.get("additional_details", {}) if isinstance(event, dict) else {}
        if isinstance(details, dict):
            combined_action = str(details.get("combined_action", "")).strip()
            if combined_action:
                return combined_action

        modifiers = self._extract_modifiers_from_event(event)
        if not modifiers:
            return ""

        event_type = self._extract_event_type(event)
        action = format_recorded_action(event.get("action", "")).strip()
        action_value = action.lower()
        readable_modifiers = [self._clean_key_name(str(item)) for item in modifiers if str(item).strip()]
        readable_modifiers = [item for item in readable_modifiers if item]
        if not readable_modifiers:
            return ""

        if event_type == "controlOperation":
            mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
            button = str(mouse.get("button", action)).strip() or action
            return f"{' + '.join(readable_modifiers)} + {button}"
        if event_type == "mouseAction" and action_value != "mouse_scroll":
            mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
            button = str(mouse.get("button", action)).strip() or action
            return f"{' + '.join(readable_modifiers)} + {button}"
        if event_type == "mouseAction" and action_value == "mouse_scroll":
            return f"{' + '.join(readable_modifiers)} + mouse_scroll"
        if event_type == "input" and action_value == "press":
            keyboard = event.get("keyboard", {}) if isinstance(event.get("keyboard", {}), dict) else {}
            key_name = self._clean_key_name(str(keyboard.get("key_name", "")))
            char = keyboard.get("char")
            key_label = str(char).upper() if isinstance(char, str) and len(char) == 1 and char.isprintable() else key_name
            if key_label:
                return f"{' + '.join(readable_modifiers)} + {key_label}"
        return ""

    def _extract_modifiers_from_event(self, event: dict[str, object]) -> list[str]:
        details = event.get("additional_details", {}) if isinstance(event.get("additional_details", {}), dict) else {}
        detail_modifiers = details.get("modifiers", []) if isinstance(details, dict) else []
        if isinstance(detail_modifiers, list) and detail_modifiers:
            return [str(item) for item in detail_modifiers if str(item).strip()]
        keyboard = event.get("keyboard", {}) if isinstance(event.get("keyboard", {}), dict) else {}
        keyboard_modifiers = keyboard.get("modifiers", []) if isinstance(keyboard, dict) else []
        if isinstance(keyboard_modifiers, list):
            return [str(item) for item in keyboard_modifiers if str(item).strip()]
        return []

    def _matches_filter_selection(self, event: dict[str, object], ignore_filter: str | None = None) -> bool:
        if ignore_filter != "process":
            selected_process = self.process_filter_var.get().strip()
            if selected_process and selected_process != "全部进程" and self._extract_process_name(event) != selected_process:
                return False
        if ignore_filter != "type":
            selected_type = self.event_type_filter_var.get().strip()
            if selected_type and selected_type != "全部类型" and self._extract_event_type(event) != selected_type:
                return False
        if ignore_filter != "action":
            selected_action = self.action_filter_var.get().strip()
            if selected_action and selected_action != "全部动作" and self._extract_event_action(event) != selected_action:
                return False
        return True

    def _is_event_visible(self, event: dict[str, object]) -> bool:
        return self._matches_filter_selection(event)

    def _visible_row_indexes(self) -> list[int]:
        return [index for index, event in enumerate(self.event_rows) if self._is_event_visible(event)]

    def load_historical_ai_analysis(self) -> None:
        if not self.session_dir:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        analysis = self._load_ai_analysis(self.session_dir)
        if not analysis:
            messagebox.showinfo("提示", "当前 Session 没有可加载的历史 AI 结果。", parent=self.window)
            self._update_historical_ai_button_state()
            return
        self.ai_analysis = analysis
        self.ai_step_tags = self._build_ai_step_tags(analysis)
        self.ai_step_texts = self._build_ai_step_texts(analysis)
        self.ai_process_summary_texts = self._build_ai_process_summary_texts(analysis)
        self.ai_var.set(f"已加载历史 AI 分析结果 | {self._build_ai_summary_text(analysis)}")
        self._try_load_historical_suggestions()
        self._refresh_ai_panels()
        self._refresh_selected_suggestion_panel()
        self._refresh_coverage_summary()
        self._reload_tree()

    def _restore_historical_analysis_outputs(self) -> None:
        if not self.session_dir:
            return
        analysis = self._load_ai_analysis(self.session_dir)
        if analysis:
            self.ai_analysis = analysis
            self.ai_step_tags = self._build_ai_step_tags(analysis)
            self.ai_step_texts = self._build_ai_step_texts(analysis)
            self.ai_process_summary_texts = self._build_ai_process_summary_texts(analysis)
            self.ai_var.set(f"已自动加载历史 AI 分析结果 | {self._build_ai_summary_text(analysis)}")
        else:
            self.ai_analysis = None
            self.ai_step_tags = {}
            self.ai_step_texts = {}
            self.ai_process_summary_texts = {}
        self._try_load_historical_suggestions()
        self._refresh_ai_panels()
        self._refresh_selected_suggestion_panel()

    def _update_historical_ai_button_state(self) -> None:
        has_history = bool(self.session_dir and (self.session_dir / "ai_analysis.json").exists())
        self.load_ai_button.configure(state=tk.NORMAL if has_history else tk.DISABLED)

    def _reload_tree(self) -> None:
        self._cancel_tree_reload()
        self._pending_tree_selection = self._get_selected_row_indexes()
        self._pending_tree_focus = self._get_primary_selected_row_index()
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        self._set_details({"message": "请选择左侧事件查看详细内容。"})
        self._show_media_items([])
        self._refresh_selected_suggestion_panel()
        self._refresh_ai_chat_panel()
        self._refresh_ai_panels()
        total = len(self.event_rows)
        visible_indexes = self._visible_row_indexes()
        visible_total = len(visible_indexes)
        if not visible_total:
            self.load_status_var.set("当前筛选条件下无匹配事件")
            self._reload_event_list_popup()
            return
        self._pending_tree_rows = [(index, self.event_rows[index]) for index in visible_indexes]
        if visible_total >= 2000:
            self.load_status_var.set(f"大 Session 模式: 正在加载事件 0/{visible_total}")
        else:
            self.load_status_var.set(f"正在加载事件 0/{visible_total}")
        self._load_next_tree_batch(0)
        self._reload_event_list_popup()

    def _cancel_tree_reload(self) -> None:
        if self._tree_reload_after_id:
            try:
                self.window.after_cancel(self._tree_reload_after_id)
            except Exception:
                pass
            self._tree_reload_after_id = None
        self._pending_tree_rows = []

    def _load_next_tree_batch(self, inserted_count: int) -> None:
        batch = self._pending_tree_rows[: self._tree_batch_size]
        self._pending_tree_rows = self._pending_tree_rows[self._tree_batch_size :]

        for row_index, event in batch:
            self.tree.insert(
                "",
                tk.END,
                iid=str(row_index),
                tags=self._build_row_tags(row_index),
                values=self._build_event_row_values(row_index, event),
            )

        inserted_count += len(batch)
        total = len(self._pending_tree_rows) + inserted_count
        mode_prefix = "大 Session 模式: " if total >= 2000 else ""
        self.load_status_var.set(f"{mode_prefix}正在加载事件 {inserted_count}/{total}")

        if inserted_count >= total:
            self._tree_reload_after_id = None
            if total >= 2000:
                self.load_status_var.set(f"大 Session 模式: 已加载 {total} 条事件")
            else:
                self.load_status_var.set("")
            preferred_selection = [row_index for row_index in self._pending_tree_selection if self.tree.exists(str(row_index))]
            if preferred_selection:
                preferred_ids = [str(row_index) for row_index in preferred_selection]
                self.tree.selection_set(preferred_ids)
                focus_index = self._pending_tree_focus if self._pending_tree_focus in preferred_selection else preferred_selection[0]
                self.tree.focus(str(focus_index))
                self.tree.see(str(focus_index))
                self.on_select_event(None)
            else:
                visible_indexes = self._visible_row_indexes()
                if visible_indexes and self.tree.exists(str(visible_indexes[0])):
                    self.tree.selection_set(str(visible_indexes[0]))
                    self.on_select_event(None)
            self._pending_tree_selection = []
            self._pending_tree_focus = None
            return

        self._tree_reload_after_id = self.window.after(1, lambda: self._load_next_tree_batch(inserted_count))

    def on_select_event(self, _event: object) -> None:
        row_index = self._get_primary_selected_row_index()
        if row_index is None:
            self._refresh_selected_suggestion_panel()
            return

        event = self.event_rows[row_index]
        self._set_details(event)
        self._show_event_media(event)
        self._refresh_selected_suggestion_panel()
        self._refresh_ai_chat_panel()

    def on_double_click(self, event: tk.Event) -> None:
        tree = event.widget if isinstance(event.widget, ttk.Treeview) else self.tree
        row_id = tree.identify_row(event.y)
        column_id = tree.identify_column(event.x)
        if not row_id:
            return

        self._select_row_index(int(row_id), source_tree=tree)

        if column_id == "#2":
            self._edit_event_type(int(row_id))
            return

        if column_id in {"#6", "#7"}:
            if column_id == "#6":
                self._edit_method_or_module_suggestion(int(row_id), "method_name")
            else:
                self._edit_parameter_suggestion(int(row_id))
            return

        if column_id == "#8":
            index = int(row_id)
            current = self._extract_comment(self.event_rows[index])
            new_comment = simpledialog.askstring("编辑 Comment", "请输入 comment:", initialvalue=current, parent=self.window)
            if new_comment is None:
                return

            self._update_event_comment(index, new_comment)
            self._persist_session()
            self._reload_tree()
            self._reload_event_list_popup()
            self._select_row_index(index)
            return

        if column_id == "#3":
            self._edit_event_action(int(row_id))
            return

        if column_id not in {"#9", "#10", "#11"}:
            return

        if column_id == "#9":
            self._edit_ai_note(int(row_id))
            return

        if column_id == "#10":
            self._edit_ai_process_summary(int(row_id))
            return

        if column_id == "#11":
            self._edit_method_or_module_suggestion(int(row_id), "script_name")
            return

    def _build_event_row_values(self, row_index: int, event: dict[str, object]) -> tuple[object, ...]:
        comment = self._extract_comment(event)
        method_suggestion = self._describe_method_suggestion_for_view(row_index)
        module_suggestion = self._describe_module_suggestion_for_view(row_index)
        parameter_suggestion = self._describe_parameter_suggestion_for_view(row_index)
        ai_note = self._describe_event_for_view(row_index, event)
        ai_summary = self._describe_process_summary_for_view(row_index)
        return (
            row_index + 1,
            self._extract_event_type(event),
            self._extract_event_action(event),
            self._format_timestamp(event.get("timestamp", "")),
            self._extract_process_name(event),
            method_suggestion,
            parameter_suggestion,
            comment,
            ai_note,
            ai_summary,
            module_suggestion,
        )

    def _extract_process_name(self, event: dict[str, object]) -> str:
        window = event.get("window", {})
        if isinstance(window, dict):
            return str(window.get("process_name", "")).strip()
        return ""

    def preview_cleaning(self) -> None:
        if not self.session_dir:
            return
        self.cleaning_suggestions = build_cleaning_suggestions(self.session_dir, self.event_rows)
        self.clear_cleaning_highlight()

        if not self.cleaning_suggestions:
            self.cleaning_var.set("未发现明显可清洗项")
            self._reload_event_list_popup()
            return

        for suggestion in self.cleaning_suggestions:
            for row_index in suggestion.row_indexes:
                if self.tree.exists(str(row_index)):
                    self.tree.item(str(row_index), tags=self._build_row_tags(row_index))

        delete_count = sum(1 for item in self.cleaning_suggestions if item.kind in {"drop_noop", "drop_noop_scroll"})
        merge_count = sum(1 for item in self.cleaning_suggestions if item.kind == "merge_keypress")
        review_count = sum(1 for item in self.cleaning_suggestions if item.kind == "review_revert_pair")
        self.cleaning_var.set(
            f"发现 {delete_count} 条可删除项，{merge_count} 组可合并 key_press，{review_count} 组 A→B→A 回退提示项。"
        )
        self._reload_event_list_popup()

    def apply_cleaning(self) -> None:
        if not self.cleaning_suggestions:
            self.preview_cleaning()
            if not self.cleaning_suggestions:
                return

        lines = []
        for suggestion in self.cleaning_suggestions[:8]:
            rows = ", ".join(str(item + 1) for item in suggestion.row_indexes)
            lines.append(f"行 {rows}: {suggestion.reason}")
        if len(self.cleaning_suggestions) > 8:
            lines.append("...")

        confirmed = messagebox.askyesno(
            "应用清洗",
            "将执行以下清洗:\n\n"
            + "\n".join(lines)
            + "\n\n注意：A→B→A 回退提示项默认不会自动删除。\n\n是否继续？",
            parent=self.window,
        )
        if not confirmed:
            return

        self.event_rows = apply_cleaning_suggestions(self.event_rows, self.cleaning_suggestions)
        if self.session_data is not None:
            self.session_data["events"] = self.event_rows
        self.ai_analysis = None
        self.ai_step_tags = {}
        self.ai_step_texts = {}
        self.ai_process_summary_texts = {}
        self.suggestion_result = None
        self.step_method_suggestions = {}
        self.step_module_suggestions = {}
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        self.ai_var.set("AI 分析结果已过期，请重新执行 AI 分析")
        self.suggestion_var.set("调用建议结果已过期，请点击“生成方法建议”，或重新应用清洗自动生成")
        self.parameter_progress_var.set("参数推荐批处理结果已过期，请重新生成")
        self.parameter_status_var.set("参数推荐结果已过期，请先重新生成方法建议；如需参数推荐也要重新执行 AI 分析")
        self._set_text_widget(self.parameter_result_text, "")
        self._refresh_coverage_summary()
        self._persist_session()
        self.cleaning_var.set(f"已应用 {len(self.cleaning_suggestions)} 条清洗建议")
        self.cleaning_suggestions = []
        self._reload_tree()
        self._generate_method_suggestions_async(status_message="数据清洗完成，正在生成方法建议...")

    def clear_cleaning_highlight(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.item(item_id, tags=self._build_row_tags(int(item_id), include_cleaning=False))
        if self.event_list_tree and self.event_list_tree.winfo_exists():
            for item_id in self.event_list_tree.get_children():
                self.event_list_tree.item(item_id, tags=self._build_row_tags(int(item_id), include_cleaning=False))

    def run_ai_analysis(self) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        settings = self.settings_store.load()
        target_rows = self._build_default_ai_analysis_row_indexes(settings)
        if not target_rows:
            messagebox.showinfo("提示", "当前没有可执行 AI 分析的步骤。", parent=self.window)
            return
        self._start_ai_analysis(target_rows)

    def run_ai_process_summary(self) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        if self.analysis_running:
            return
        selected_rows = self._get_selected_row_indexes()
        if selected_rows:
            should_use_selected_rows = messagebox.askyesno(
                "AI总结",
                f"检测到当前选中了 {len(selected_rows)} 行。\n\n是否仅对选中行执行总结？\n\n选择“是”：忽略按进程分段，把当前选中的步骤作为一个整体总结。\n选择“否”：继续按当前默认逻辑，对整次录制按进程分段总结。",
                parent=self.window,
            )
            if should_use_selected_rows:
                self._start_ai_process_summary(selected_rows=selected_rows)
                return
        self._start_ai_process_summary()

    def run_method_suggestion_generation(self) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        selected_rows = self._get_selected_row_indexes()
        if not selected_rows:
            messagebox.showinfo("提示", "请先选择至少一个步骤。", parent=self.window)
            return
        if len(selected_rows) == 1:
            status_message = f"正在为步骤 {selected_rows[0] + 1} 生成方法建议..."
        else:
            preview = ", ".join(str(row_index + 1) for row_index in selected_rows[:8])
            suffix = "..." if len(selected_rows) > 8 else ""
            status_message = f"正在为所选 {len(selected_rows)} 个步骤生成方法建议: {preview}{suffix}"
        self._generate_method_suggestions_async(
            selected_rows=selected_rows,
            status_message=status_message,
            interactive=True,
        )

    def run_selected_ai_analysis(self) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        selected_rows = self._get_selected_row_indexes()
        if not selected_rows:
            messagebox.showinfo("提示", "请先选择至少一个步骤。", parent=self.window)
            return
        self._start_ai_analysis(selected_rows)

    def _start_ai_analysis(self, selected_rows: list[int] | None = None) -> None:
        if not self.session_dir or not self.session_data:
            return
        if self.analysis_running:
            return

        partial_row_indexes = self._normalize_selected_analysis_rows(selected_rows)
        if not partial_row_indexes:
            messagebox.showinfo("提示", "所选行无可分析事件。", parent=self.window)
            return

        self.analysis_running = True
        self.analysis_cancel_event.clear()
        self.ai_button.configure(state=tk.DISABLED)
        self.selected_ai_button.configure(state=tk.DISABLED)
        self.ai_process_summary_button.configure(state=tk.DISABLED)
        self.cancel_ai_button.configure(state=tk.NORMAL)
        self.analysis_started_at = time.time()
        self.analysis_status_base = f"AI 分析预处理中（共 {len(partial_row_indexes)} 步）"
        self.analysis_status_token += 1
        self._refresh_analysis_status(self.analysis_status_token)

        def worker() -> None:
            try:
                settings = self.settings_store.load()
                client = OpenAICompatibleAIClient(settings)
                self.current_analyzer = client
                targets = self._build_ai_analysis_targets(partial_row_indexes, settings)
                if not targets:
                    raise ValueError("所选步骤在 AI 看图过滤范围外，或没有可发送给 AI 的图片。")
                target_batches = [[target] for target in targets]
                analyses: list[dict[str, object]] = []

                for batch_index, target_batch in enumerate(target_batches, start=1):
                    self._raise_if_analysis_cancel_requested()
                    self.window.after(
                        0,
                        lambda batch_index=batch_index, total_batches=len(target_batches), count=len(target_batch), start_step=target_batch[0]["step_id"], end_step=target_batch[-1]["step_id"]: self._on_ai_analysis_progress(
                            "batch_preprocess_done",
                            {
                                "current_batch": batch_index,
                                "total_batches": total_batches,
                                "start_step": start_step,
                                "end_step": end_step,
                                "image_count": count,
                                "cropped_monitor_count": 0,
                            },
                        ),
                    )

                    response = client.query(
                        user_prompt=self._build_single_pass_ai_analysis_prompt(target_batch),
                        image_paths=[item["image_path"] for item in target_batch],
                        system_prompt=(
                            "你是桌面自动化单步看图助手。"
                            "你会一次看到多张按步骤顺序排列的截图，以及每一步的事件元数据。"
                            "必须严格按给定 step_id 输出 JSON 结果。"
                        ),
                        cancel_callback=self._is_analysis_cancel_requested,
                        progress_callback=lambda stage, payload, batch_index=batch_index, total_batches=len(target_batches), start_step=target_batch[0]["step_id"], end_step=target_batch[-1]["step_id"]: self.window.after(
                            0,
                            lambda stage=stage, payload=payload, batch_index=batch_index, total_batches=total_batches, start_step=start_step, end_step=end_step: self._on_ai_analysis_progress(
                                stage,
                                {
                                    "current_batch": batch_index,
                                    "total_batches": total_batches,
                                    "start_step": start_step,
                                    "end_step": end_step,
                                    **payload,
                                },
                            ),
                        ),
                    )
                    analyses.append(self._build_single_pass_ai_analysis_result(target_batch, str(response.get("response_text", ""))))

                self._raise_if_analysis_cancel_requested()
                analysis = self._combine_single_pass_ai_analysis_results(analyses, 1)
            except Exception as exc:
                message = str(exc)
                self.window.after(0, lambda message=message: self._on_ai_analysis_failed(message))
                return

            self.window.after(
                0,
                lambda analysis=analysis, partial_row_indexes=partial_row_indexes: self._on_selected_ai_analysis_success(analysis, partial_row_indexes or []),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _build_default_ai_analysis_row_indexes(self, settings) -> list[int]:
        return [row_index for row_index, event in enumerate(self.event_rows) if self._is_event_supported_for_ai_analysis(event, settings)]

    def _is_event_supported_for_ai_analysis(self, event: dict[str, object], settings) -> bool:
        event_type = self._extract_event_type(event).strip().lower()
        action = format_recorded_action(self._extract_event_action(event)).strip().lower()
        if event_type in {"comment", "checkpoint", "getscreenshot"}:
            return False
        if event_type == "wait" or action in {"manual_comment", "ai_checkpoint", "getscreenshot", "manual_screenshot"}:
            return False
        if self._is_process_excluded_for_ai_analysis(event, settings):
            return False
        return self._resolve_event_primary_image_path(event) is not None

    def _is_process_excluded_for_ai_analysis(self, event: dict[str, object], settings) -> bool:
        process_name = self._normalize_process_name_for_ai_filter(self._extract_process_name(event))
        if not process_name:
            return False
        excluded_process_names = self._build_ai_observation_excluded_process_names(settings)
        return process_name in excluded_process_names

    def _build_ai_observation_excluded_process_names(self, settings) -> set[str]:
        normalized: set[str] = set()
        for item in SettingsStore.parse_pattern_list(getattr(settings, "ai_observation_excluded_process_names", "")):
            process_name = self._normalize_process_name_for_ai_filter(item)
            if not process_name:
                continue
            normalized.add(process_name)
            if process_name == "wordpad":
                normalized.add("write")
            elif process_name == "write":
                normalized.add("wordpad")
        return normalized

    @staticmethod
    def _normalize_process_name_for_ai_filter(process_name: object) -> str:
        value = str(process_name or "").strip().lower().replace("\\", "/")
        if not value:
            return ""
        name = value.rsplit("/", 1)[-1]
        if name.endswith(".exe"):
            name = name[:-4]
        return name

    def _resolve_event_primary_image_path(self, event: dict[str, object]) -> Path | None:
        if not self.session_dir:
            return None
        media_items = event.get("media", [])
        if isinstance(media_items, list):
            for item in media_items:
                if isinstance(item, dict) and item.get("type") == "image" and item.get("path"):
                    candidate = self.session_dir / str(item.get("path"))
                    if candidate.exists():
                        return candidate
        screenshot = event.get("screenshot")
        if screenshot:
            candidate = self.session_dir / str(screenshot)
            if candidate.exists():
                return candidate
        return None

    def _build_ai_analysis_targets(self, row_indexes: list[int], settings) -> list[dict[str, object]]:
        display_layout = self._get_session_display_layout()
        cache_dir = self.session_dir / "ai_preprocessed" / "viewer_single_pass"
        targets: list[dict[str, object]] = []
        for row_index in row_indexes:
            if not (0 <= row_index < len(self.event_rows)):
                continue
            event = self.event_rows[row_index]
            if not self._is_event_supported_for_ai_analysis(event, settings):
                continue
            image_path = self._resolve_event_primary_image_path(event)
            if image_path is None:
                continue
            prepared_path, _was_cropped = prepare_image_path_for_ai(
                image_path,
                event,
                display_layout,
                cache_dir,
                send_fullscreen=settings.send_fullscreen_screenshots,
                cache_key=f"viewer_single_pass_{row_index + 1:04d}",
            )
            optimized_path = self._optimize_ai_analysis_image(prepared_path, row_index + 1)
            targets.append(
                {
                    "row_index": row_index,
                    "step_id": row_index + 1,
                    "image_path": optimized_path,
                    "analysis_mode": self._determine_ai_analysis_mode(event),
                    "event_type": self._extract_event_type(event),
                    "action": self._extract_event_action(event),
                    "process_name": self._extract_process_name(event),
                    "ui_element": self._build_prompt_ui_element_for_viewer(event),
                }
            )
        return targets

    def _optimize_ai_analysis_image(self, image_path: Path, step_id: int) -> Path:
        if not self.session_dir:
            return image_path
        output_dir = self.session_dir / "ai_preprocessed" / "viewer_single_pass_resized"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"step_{step_id:04d}.jpg"
        try:
            with Image.open(image_path) as image:
                converted = image.convert("RGB")
                converted.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                converted.save(output_path, format="JPEG", quality=82, optimize=True)
            return output_path
        except Exception:
            return image_path

    def _determine_ai_analysis_mode(self, event: dict[str, object]) -> str:
        return "control_observation" if resolve_method_name_for_event(event) == "FindControlByName" else "image_summary"

    def _build_prompt_ui_element_for_viewer(self, event: dict[str, object]) -> dict[str, object]:
        ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
        payload: dict[str, object] = {}
        for key in ("name", "control_type", "help_text", "automation_id", "class_name"):
            value = str(ui_element.get(key, "")).strip()
            if value:
                payload[key] = value
        return payload

    def _build_single_pass_ai_analysis_prompt(self, targets: list[dict[str, object]]) -> str:
        instruction = {
            "task": "对给定步骤截图逐步输出 AI看图结果",
            "requirements": [
                "你会按顺序看到多张截图，每张截图对应 steps 数组里的同序步骤。",
                "必须输出 step_results，且顺序与 steps 一致，step_id 必须原样返回。",
                "当 analysis_mode=control_observation 时，沿用旧的 FindControlByName 看图规则，输出 control_type、label、relative_position、need_scroll、is_table、action，并尽量同时补 observation。",
                "当 analysis_mode=image_summary 时，请只基于当前单张截图，输出详细中文 observation，总结这一步界面上发生了什么、用户在做什么。此时其余字段可留空或省略。",
                "不要跨步骤合并，不要输出额外解释，只输出 JSON。",
            ],
            "json_schema_hint": {
                "step_results": [
                    {
                        "step_id": 11,
                        "analysis_mode": "control_observation",
                        "control_type": "button",
                        "label": "Save",
                        "relative_position": "self",
                        "need_scroll": False,
                        "is_table": False,
                        "action": "click",
                        "observation": "label=Save | direction=self | control_type=button | scroll=false | table=false | action=click",
                    },
                    {
                        "step_id": 12,
                        "analysis_mode": "image_summary",
                        "observation": "当前在资源管理器中浏览目标文件夹内容，并准备进行下一步操作。",
                    },
                ]
            },
            "steps": [
                {
                    "step_id": item["step_id"],
                    "analysis_mode": item["analysis_mode"],
                    "event_type": item["event_type"],
                    "action": item["action"],
                    "process_name": item["process_name"],
                    "ui_element": item["ui_element"],
                }
                for item in targets
            ],
        }
        return json.dumps(instruction, ensure_ascii=False, indent=2)

    def _build_single_pass_ai_analysis_result(self, targets: list[dict[str, object]], response_text: str) -> dict[str, object]:
        parsed = self._parse_viewer_ai_json(response_text)
        values = parsed.get("step_results", []) if isinstance(parsed, dict) else []
        results_by_step: dict[int, dict[str, object]] = {}
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, dict):
                    continue
                step_id = int(item.get("step_id", 0) or 0)
                if step_id > 0:
                    results_by_step[step_id] = item

        step_observations: list[dict[str, object]] = []
        step_insights: list[dict[str, object]] = []
        for target in targets:
            step_id = int(target["step_id"])
            item = results_by_step.get(step_id, {})
            observation = self._normalize_single_pass_observation_text(target, item)
            if not observation:
                continue
            observation_item: dict[str, object] = {"step_id": step_id, "observation": observation}
            if target["analysis_mode"] == "control_observation":
                control_type = str(item.get("control_type", "")).strip()
                label = str(item.get("label", "")).strip()
                relative_position = str(item.get("relative_position", "")).strip()
                if control_type:
                    observation_item["control_type"] = control_type
                if label:
                    observation_item["label"] = label
                if relative_position:
                    observation_item["relative_position"] = relative_position
                if isinstance(item.get("need_scroll"), bool):
                    observation_item["need_scroll"] = item.get("need_scroll")
                if isinstance(item.get("is_table"), bool):
                    observation_item["is_table"] = item.get("is_table")
                action = str(item.get("action", "")).strip()
                if action:
                    observation_item["action"] = action
            step_observations.append(observation_item)
            step_insights.append({"step_id": step_id, "description": observation})

        return {
            "session_id": str((self.session_data or {}).get("session_id", self.session_dir.name if self.session_dir else "")),
            "batch_size": len(targets),
            "status": "completed",
            "failure_message": "",
            "carry_memory": [],
            "batches": [
                {
                    "batch_id": f"viewer_single_pass_{targets[0]['step_id']:04d}_{targets[-1]['step_id']:04d}",
                    "start_step": targets[0]["step_id"],
                    "end_step": targets[-1]["step_id"],
                    "event_indexes": [item["step_id"] for item in targets],
                    "image_paths": [str(Path(item["image_path"]).relative_to(self.session_dir).as_posix()) for item in targets],
                    "prompt_preview": self._build_single_pass_ai_analysis_prompt(targets)[:2000],
                    "response_text": response_text,
                    "parsed_result": parsed,
                }
            ],
            "step_observations": step_observations,
            "step_insights": step_insights,
            "invalid_steps": [],
            "reusable_modules": [],
            "wait_suggestions": [],
            "analysis_notes": [],
            "workflow_report_markdown": "",
        }

    def _combine_single_pass_ai_analysis_results(self, analyses: list[dict[str, object]], batch_size: int) -> dict[str, object]:
        combined = {
            "session_id": str((self.session_data or {}).get("session_id", self.session_dir.name if self.session_dir else "")),
            "batch_size": max(1, batch_size),
            "status": "completed",
            "failure_message": "",
            "carry_memory": [],
            "batches": [],
            "step_observations": [],
            "step_insights": [],
            "invalid_steps": [],
            "reusable_modules": [],
            "wait_suggestions": [],
            "analysis_notes": [],
            "workflow_report_markdown": "",
        }
        for analysis in analyses:
            if not isinstance(analysis, dict):
                continue
            for key in ("batches", "step_observations", "step_insights", "invalid_steps", "reusable_modules", "wait_suggestions"):
                values = analysis.get(key, [])
                if isinstance(values, list):
                    combined[key].extend(copy.deepcopy(values))
            notes = analysis.get("analysis_notes", [])
            if isinstance(notes, list):
                combined["analysis_notes"].extend(str(item) for item in notes if isinstance(item, str))
        combined["batches"] = sorted(
            combined["batches"],
            key=lambda item: (
                int(item.get("start_step", 0) or 0),
                int(item.get("end_step", 0) or 0),
                str(item.get("batch_id", "")),
            ),
        )
        combined["step_observations"] = sorted(combined["step_observations"], key=lambda item: int(item.get("step_id", 0) or 0))
        combined["step_insights"] = sorted(combined["step_insights"], key=lambda item: int(item.get("step_id", 0) or 0))
        combined["wait_suggestions"] = sorted(combined["wait_suggestions"], key=lambda item: int(item.get("step_id", 0) or 0))
        combined["analysis_notes"] = self._deduplicate_texts(combined["analysis_notes"])
        return combined

    def _normalize_single_pass_observation_text(self, target: dict[str, object], item: dict[str, object]) -> str:
        direct_observation = self._clean_sentence(str(item.get("observation", item.get("description", ""))))
        if target["analysis_mode"] != "control_observation":
            return direct_observation
        parts: list[str] = []
        label = self._clean_sentence(str(item.get("label", "")))
        if label:
            parts.append(f"label={label}")
        relative_position = self._clean_sentence(str(item.get("relative_position", "")))
        if relative_position:
            parts.append(f"direction={relative_position}")
        control_type = self._clean_sentence(str(item.get("control_type", "")))
        if control_type:
            parts.append(f"control_type={control_type}")
        if isinstance(item.get("need_scroll"), bool):
            parts.append(f"scroll={str(item.get('need_scroll')).lower()}")
        if isinstance(item.get("is_table"), bool):
            parts.append(f"table={str(item.get('is_table')).lower()}")
        action = self._clean_sentence(str(item.get("action", "")))
        if action:
            parts.append(f"action={action}")
        if parts:
            return " | ".join(parts)
        return direct_observation

    def _parse_viewer_ai_json(self, response_text: str) -> dict[str, object]:
        text = str(response_text or "").strip()
        candidates = [text]
        if "```" in text:
            lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
            candidates.append("\n".join(lines).strip())
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except Exception:
                start = candidate.find("{")
                end = candidate.rfind("}")
                if start == -1 or end == -1 or end <= start:
                    continue
                try:
                    payload = json.loads(candidate[start : end + 1])
                except Exception:
                    continue
            if isinstance(payload, dict):
                return payload
        raise ValueError("AI 返回无法解析为 JSON。")

    def _normalize_selected_analysis_rows(self, selected_rows: list[int] | None) -> list[int] | None:
        if selected_rows is None:
            return None
        valid_rows = sorted({row_index for row_index in selected_rows if 0 <= row_index < len(self.event_rows)})
        return valid_rows

    def _build_analysis_session_data(self, selected_rows: list[int] | None) -> tuple[dict[str, object], dict[int, int]]:
        session_data = copy.deepcopy(self.session_data) if isinstance(self.session_data, dict) else {}
        events = session_data.get("events", []) if isinstance(session_data.get("events", []), list) else []
        if selected_rows is None:
            return session_data, {index + 1: index + 1 for index in range(len(events))}
        selected_events: list[dict[str, object]] = []
        for row_index in selected_rows:
            copied_event = copy.deepcopy(self.event_rows[row_index])
            copied_event["analysis_step_id"] = row_index + 1
            selected_events.append(copied_event)
        session_data["events"] = selected_events
        return session_data, {subset_index + 1: row_index + 1 for subset_index, row_index in enumerate(selected_rows)}

    def _build_suggestion_session_data(self, selected_rows: list[int] | None) -> tuple[dict[str, object], dict[int, int]]:
        session_data = copy.deepcopy(self.session_data) if isinstance(self.session_data, dict) else {}
        events = session_data.get("events", []) if isinstance(session_data.get("events", []), list) else []
        if selected_rows is None:
            return session_data, {index + 1: index + 1 for index in range(len(events))}
        selected_events = [copy.deepcopy(self.event_rows[row_index]) for row_index in selected_rows]
        session_data["events"] = selected_events
        return session_data, {subset_index + 1: row_index + 1 for subset_index, row_index in enumerate(selected_rows)}

    def _create_temp_analysis_session_dir(self, session_data: dict[str, object]) -> Path:
        temp_root = Path(tempfile.mkdtemp(prefix="viewer_partial_ai_"))
        session_name = self.session_dir.name if self.session_dir else "session"
        temp_session_dir = temp_root / session_name
        temp_session_dir.mkdir(parents=True, exist_ok=True)

        events = session_data.get("events", []) if isinstance(session_data.get("events", []), list) else []
        copied_paths: set[str] = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            candidate_paths: list[str] = []
            screenshot = event.get("screenshot")
            if screenshot:
                candidate_paths.append(str(screenshot))
            media = event.get("media", [])
            if isinstance(media, list):
                for item in media:
                    if isinstance(item, dict) and item.get("path"):
                        candidate_paths.append(str(item.get("path")))
            for relative_path in candidate_paths:
                normalized = relative_path.replace("\\", "/")
                if normalized in copied_paths or not self.session_dir:
                    continue
                source_path = self.session_dir / relative_path
                if not source_path.exists() or not source_path.is_file():
                    continue
                target_path = temp_session_dir / relative_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                copied_paths.add(normalized)
        return temp_session_dir

    def _remap_analysis_step_ids(self, analysis: dict[str, object], step_id_mapping: dict[int, int]) -> dict[str, object]:
        remapped = copy.deepcopy(analysis)
        remapped["step_observations"] = [
            self._remap_analysis_item_step_id(item, step_id_mapping)
            for item in remapped.get("step_observations", [])
            if isinstance(item, dict) and self._mapped_step_id(item.get("step_id"), step_id_mapping) is not None
        ]
        remapped["step_insights"] = [
            self._remap_analysis_item_step_id(item, step_id_mapping)
            for item in remapped.get("step_insights", [])
            if isinstance(item, dict) and self._mapped_step_id(item.get("step_id"), step_id_mapping) is not None
        ]
        remapped["invalid_steps"] = [
            self._remap_invalid_steps_item(item, step_id_mapping)
            for item in remapped.get("invalid_steps", [])
            if isinstance(item, dict)
        ]
        remapped["invalid_steps"] = [item for item in remapped["invalid_steps"] if item.get("step_ids")]
        remapped["wait_suggestions"] = [
            self._remap_analysis_item_step_id(item, step_id_mapping)
            for item in remapped.get("wait_suggestions", [])
            if isinstance(item, dict) and self._mapped_step_id(item.get("step_id"), step_id_mapping) is not None
        ]
        remapped["reusable_modules"] = []
        remapped["workflow_report_markdown"] = ""
        remapped["batches"] = [
            self._remap_analysis_batch_item(item, step_id_mapping)
            for item in remapped.get("batches", [])
            if isinstance(item, dict)
        ]
        remapped["batches"] = [item for item in remapped["batches"] if item.get("event_indexes") or item.get("batch_id") == "workflow_aggregation"]
        remapped["process_summaries"] = [
            self._remap_process_summary_item(item, step_id_mapping)
            for item in remapped.get("process_summaries", [])
            if isinstance(item, dict)
        ]
        remapped["process_summaries"] = [item for item in remapped["process_summaries"] if item.get("last_step_id")]
        overview = remapped.get("process_summary_overview", {}) if isinstance(remapped.get("process_summary_overview", {}), dict) else {}
        if overview:
            remapped["process_summary_overview"] = self._remap_process_summary_overview(overview, step_id_mapping)
        return remapped

    def _mapped_step_id(self, value: object, step_id_mapping: dict[int, int]) -> int | None:
        if not isinstance(value, int):
            return None
        return step_id_mapping.get(value)

    def _remap_analysis_item_step_id(self, item: dict[str, object], step_id_mapping: dict[int, int]) -> dict[str, object]:
        remapped = dict(item)
        mapped_step_id = self._mapped_step_id(item.get("step_id"), step_id_mapping)
        if mapped_step_id is not None:
            remapped["step_id"] = mapped_step_id
        return remapped

    def _remap_invalid_steps_item(self, item: dict[str, object], step_id_mapping: dict[int, int]) -> dict[str, object]:
        remapped = dict(item)
        step_ids = item.get("step_ids", []) if isinstance(item.get("step_ids", []), list) else []
        remapped["step_ids"] = [step_id_mapping[step_id] for step_id in step_ids if isinstance(step_id, int) and step_id in step_id_mapping]
        return remapped

    def _remap_analysis_batch_item(self, item: dict[str, object], step_id_mapping: dict[int, int]) -> dict[str, object]:
        remapped = dict(item)
        mapped_indexes = [step_id_mapping[step_id] for step_id in item.get("event_indexes", []) if isinstance(step_id, int) and step_id in step_id_mapping]
        remapped["event_indexes"] = mapped_indexes
        if mapped_indexes:
            remapped["start_step"] = min(mapped_indexes)
            remapped["end_step"] = max(mapped_indexes)
        return remapped

    def _remap_process_summary_item(self, item: dict[str, object], step_id_mapping: dict[int, int]) -> dict[str, object]:
        remapped = dict(item)
        step_ids = item.get("step_ids", []) if isinstance(item.get("step_ids", []), list) else []
        mapped_step_ids = [step_id_mapping[step_id] for step_id in step_ids if isinstance(step_id, int) and step_id in step_id_mapping]
        remapped["step_ids"] = mapped_step_ids
        if mapped_step_ids:
            remapped["start_step"] = min(mapped_step_ids)
            remapped["end_step"] = max(mapped_step_ids)
            remapped["last_step_id"] = max(mapped_step_ids)
        else:
            remapped["last_step_id"] = None
        return remapped

    def _remap_process_summary_overview(self, overview: dict[str, object], step_id_mapping: dict[int, int]) -> dict[str, object]:
        remapped = copy.deepcopy(overview)
        candidates = remapped.get("rollback_candidates", []) if isinstance(remapped.get("rollback_candidates", []), list) else []
        normalized_candidates: list[dict[str, object]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            step_ids = item.get("step_ids", []) if isinstance(item.get("step_ids", []), list) else []
            mapped_step_ids = [step_id_mapping[step_id] for step_id in step_ids if isinstance(step_id, int) and step_id in step_id_mapping]
            candidate = dict(item)
            candidate["step_ids"] = mapped_step_ids
            if mapped_step_ids:
                candidate["start_step"] = min(mapped_step_ids)
                candidate["end_step"] = max(mapped_step_ids)
                normalized_candidates.append(candidate)
        remapped["rollback_candidates"] = normalized_candidates
        return remapped

    def _on_selected_ai_analysis_success(self, analysis: dict[str, object], selected_rows: list[int]) -> None:
        selected_step_ids = {row_index + 1 for row_index in selected_rows}
        base_analysis = copy.deepcopy(self.ai_analysis) if isinstance(self.ai_analysis, dict) else self._load_ai_analysis(self.session_dir) or {}
        merged_analysis = self._merge_selected_ai_analysis(base_analysis, analysis, selected_step_ids)
        self._persist_ai_analysis(merged_analysis)

        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.ai_process_summary_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "选中行 AI 分析完成"
        self.analysis_status_token += 1
        self.ai_analysis = merged_analysis
        self.ai_step_tags = self._build_ai_step_tags(merged_analysis)
        self.ai_step_texts = self._build_ai_step_texts(merged_analysis)
        self.ai_process_summary_texts = self._build_ai_process_summary_texts(merged_analysis)
        self.ai_var.set(f"已完成选中 {len(selected_rows)} 行 AI 分析 | {self._build_ai_summary_text(merged_analysis)}")
        self._refresh_coverage_summary()
        self._refresh_ai_panels()
        self._refresh_selected_suggestion_panel()
        self._reload_tree()
        messagebox.showinfo("AI 分析完成", f"已更新所选 {len(selected_rows)} 行的 AI 建议。", parent=self.window)

    def _merge_selected_ai_analysis(
        self,
        base_analysis: dict[str, object],
        partial_analysis: dict[str, object],
        selected_step_ids: set[int],
    ) -> dict[str, object]:
        merged = {
            "session_id": str(partial_analysis.get("session_id") or base_analysis.get("session_id") or (self.session_data or {}).get("session_id", "")),
            "batch_size": int(partial_analysis.get("batch_size", base_analysis.get("batch_size", 1)) or 1),
            "status": str(partial_analysis.get("status", "completed") or "completed"),
            "failure_message": str(partial_analysis.get("failure_message", "")),
            "carry_memory": [
                item for item in (base_analysis.get("carry_memory", []) or partial_analysis.get("carry_memory", [])) if isinstance(item, dict)
            ],
            "batches": self._merge_selected_analysis_batches(
                [item for item in base_analysis.get("batches", []) if isinstance(item, dict)],
                [item for item in partial_analysis.get("batches", []) if isinstance(item, dict)],
                selected_step_ids,
            ),
            "step_observations": [],
            "step_insights": [],
            "invalid_steps": [],
            "reusable_modules": [],
            "wait_suggestions": [],
            "analysis_notes": [item for item in base_analysis.get("analysis_notes", []) if isinstance(item, str)],
            "workflow_report_markdown": "",
            "process_summaries": [item for item in base_analysis.get("process_summaries", []) if isinstance(item, dict)],
            "process_summary_overview": copy.deepcopy(base_analysis.get("process_summary_overview", {})) if isinstance(base_analysis.get("process_summary_overview", {}), dict) else {},
        }

        existing_step_observations = [
            item for item in base_analysis.get("step_observations", [])
            if isinstance(item, dict) and int(item.get("step_id", 0) or 0) not in selected_step_ids
        ]
        partial_step_observations = [item for item in partial_analysis.get("step_observations", []) if isinstance(item, dict)]
        merged["step_observations"] = sorted(
            [*existing_step_observations, *partial_step_observations],
            key=lambda item: int(item.get("step_id", 0) or 0),
        )

        existing_step_insights = [
            item for item in base_analysis.get("step_insights", [])
            if isinstance(item, dict) and int(item.get("step_id", 0) or 0) not in selected_step_ids
        ]
        partial_step_insights = [item for item in partial_analysis.get("step_insights", []) if isinstance(item, dict)]
        merged["step_insights"] = sorted(
            [*existing_step_insights, *partial_step_insights],
            key=lambda item: int(item.get("step_id", 0) or 0),
        )

        merged["invalid_steps"] = [
            item for item in base_analysis.get("invalid_steps", [])
            if isinstance(item, dict)
            and not any(isinstance(step_id, int) and step_id in selected_step_ids for step_id in item.get("step_ids", []))
        ]
        merged["invalid_steps"].extend(item for item in partial_analysis.get("invalid_steps", []) if isinstance(item, dict))

        merged["wait_suggestions"] = [
            item for item in base_analysis.get("wait_suggestions", [])
            if isinstance(item, dict) and int(item.get("step_id", 0) or 0) not in selected_step_ids
        ]
        merged["wait_suggestions"].extend(item for item in partial_analysis.get("wait_suggestions", []) if isinstance(item, dict))
        merged["wait_suggestions"] = sorted(
            merged["wait_suggestions"],
            key=lambda item: int(item.get("step_id", 0) or 0),
        )
        merged["analysis_notes"] = self._deduplicate_texts(merged["analysis_notes"])
        return merged

    def _merge_selected_analysis_batches(
        self,
        base_batches: list[dict[str, object]],
        partial_batches: list[dict[str, object]],
        selected_step_ids: set[int],
    ) -> list[dict[str, object]]:
        merged_batches: list[dict[str, object]] = []

        for item in base_batches:
            if self._analysis_batch_overlaps_selected(item, selected_step_ids):
                continue
            if str(item.get("batch_id", "")) == "workflow_aggregation":
                continue
            merged_batches.append(dict(item))

        for item in partial_batches:
            if str(item.get("batch_id", "")) == "workflow_aggregation":
                continue
            merged_batches.append(dict(item))

        return sorted(
            merged_batches,
            key=lambda item: (
                int(item.get("start_step", 0) or 0),
                int(item.get("end_step", 0) or 0),
                str(item.get("batch_id", "")),
            ),
        )

    def _analysis_batch_overlaps_selected(self, item: dict[str, object], selected_step_ids: set[int]) -> bool:
        event_indexes = item.get("event_indexes", []) if isinstance(item.get("event_indexes", []), list) else []
        for step_id in event_indexes:
            if isinstance(step_id, int) and step_id in selected_step_ids:
                return True
        start_step = int(item.get("start_step", 0) or 0)
        end_step = int(item.get("end_step", 0) or 0)
        if start_step > 0 and end_step >= start_step:
            return any(start_step <= step_id <= end_step for step_id in selected_step_ids)
        return False

    def _persist_ai_analysis(self, analysis: dict[str, object]) -> None:
        if not self.session_dir:
            return
        analysis_path = self.session_dir / "ai_analysis.json"
        yaml_path = self.session_dir / "ai_analysis.yaml"
        analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
        yaml_path.write_text(yaml.safe_dump(analysis, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _invalidate_suggestion_outputs(self, message: str) -> None:
        self.suggestion_result = None
        self.step_method_suggestions = {}
        self.step_module_suggestions = {}
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        if self.session_dir:
            suggestion_path = self.session_dir / "conversion_suggestions.json"
            if suggestion_path.exists():
                try:
                    suggestion_path.unlink()
                except Exception:
                    pass
        self.suggestion_var.set(message)
        self.parameter_progress_var.set("参数推荐批处理未执行")
        self.parameter_status_var.set("请重新生成调用建议后再执行参数推荐。")
        self._set_text_widget(self.parameter_result_text, "")

    def _start_ai_process_summary(self, selected_rows: list[int] | None = None) -> None:
        if not self.session_dir or not self.session_data:
            return

        normalized_selected_rows = self._normalize_selected_analysis_rows(selected_rows)
        use_selected_rows = bool(normalized_selected_rows)
        segments = self._build_selected_process_summary_segments(normalized_selected_rows or []) if use_selected_rows else self._build_process_summary_segments()
        if not segments:
            messagebox.showinfo("提示", "当前没有可按进程总结的步骤。", parent=self.window)
            return

        self.analysis_running = True
        self.analysis_cancel_event.clear()
        self.ai_button.configure(state=tk.DISABLED)
        self.selected_ai_button.configure(state=tk.DISABLED)
        self.ai_process_summary_button.configure(state=tk.DISABLED)
        self.cancel_ai_button.configure(state=tk.NORMAL)
        self.analysis_started_at = time.time()
        self.analysis_status_base = f"AI总结预处理中（已选 {len(normalized_selected_rows or [])} 步）" if use_selected_rows else "AI总结预处理中"
        self.analysis_status_token += 1
        self._refresh_analysis_status(self.analysis_status_token)

        def worker() -> None:
            try:
                settings = self.settings_store.load()
                client = OpenAICompatibleAIClient(settings)
                self.current_analyzer = client
                summaries: list[dict[str, object]] = []
                total_segments = len(segments)
                for batch_index, segment in enumerate(segments, start=1):
                    self._raise_if_analysis_cancel_requested()
                    self.window.after(
                        0,
                        lambda batch_index=batch_index, total_segments=total_segments, segment=segment: self._on_ai_analysis_progress(
                            "process_summary_segment_start",
                            {
                                "current_batch": batch_index,
                                "total_batches": total_segments,
                                "start_step": segment["start_step"],
                                "end_step": segment["end_step"],
                                "process_name": segment["process_name"],
                            },
                        ),
                    )
                    window_batches = self._build_process_summary_image_batches(segment, settings)
                    window_summaries: list[dict[str, object]] = []
                    if not window_batches:
                        response = client.query(
                            user_prompt=self._build_process_summary_text_only_prompt(segment),
                            system_prompt=(
                                "你是桌面自动化按进程总结助手。"
                                "你基于连续同进程步骤的事件元数据与已有 AI 看图描述，输出详细中文总结。"
                                "不要输出 markdown，不要输出列表前缀，不要输出 JSON，只输出总结正文。"
                            ),
                            cancel_callback=self._is_analysis_cancel_requested,
                            progress_callback=(
                                lambda stage, payload, batch_index=batch_index, total_segments=total_segments, segment=segment: self.window.after(
                                    0,
                                    lambda stage=stage, payload=payload, batch_index=batch_index, total_segments=total_segments, segment=segment: self._on_ai_analysis_progress(
                                        stage,
                                        {
                                            "current_batch": batch_index,
                                            "total_batches": total_segments,
                                            "start_step": segment["start_step"],
                                            "end_step": segment["end_step"],
                                            "process_name": segment["process_name"],
                                            "analysis_phase": "process_summary",
                                            **payload,
                                        },
                                    ),
                                )
                            ),
                        )
                        summary_text = self._clean_ai_process_summary_response(str(response.get("response_text", "")))
                    else:
                        for window_index, batch in enumerate(window_batches, start=1):
                            self._raise_if_analysis_cancel_requested()
                            response = client.query(
                                user_prompt=self._build_process_summary_prompt(segment, batch),
                                image_paths=batch["image_paths"],
                                system_prompt=(
                                    "你是桌面自动化按进程总结助手。"
                                    "你基于当前批次截图、连续同进程步骤事件元数据与已有 AI 看图描述，输出详细中文总结。"
                                    "不要输出 markdown，不要输出列表前缀，不要输出 JSON，只输出总结正文。"
                                ),
                                cancel_callback=self._is_analysis_cancel_requested,
                                progress_callback=(
                                    lambda stage, payload, batch_index=batch_index, total_segments=total_segments, segment=segment, window_index=window_index, total_windows=len(window_batches): self.window.after(
                                        0,
                                        lambda stage=stage, payload=payload, batch_index=batch_index, total_segments=total_segments, segment=segment, window_index=window_index, total_windows=total_windows: self._on_ai_analysis_progress(
                                            stage,
                                            {
                                                "current_batch": batch_index,
                                                "total_batches": total_segments,
                                                "start_step": segment["start_step"],
                                                "end_step": segment["end_step"],
                                                "process_name": segment["process_name"],
                                                "analysis_phase": "process_summary",
                                                "window_index": window_index,
                                                "total_windows": total_windows,
                                                **payload,
                                            },
                                        ),
                                    )
                                ),
                            )
                            window_summaries.append(
                                {
                                    "window_index": window_index,
                                    "step_ids": batch["step_ids"],
                                    "summary": self._clean_ai_process_summary_response(str(response.get("response_text", ""))),
                                }
                            )
                        if len(window_summaries) == 1:
                            summary_text = window_summaries[0]["summary"]
                        else:
                            self._raise_if_analysis_cancel_requested()
                            merge_response = client.query(
                                user_prompt=self._build_process_summary_merge_prompt(segment, window_summaries),
                                system_prompt=(
                                    "你是桌面自动化按进程总结助手。"
                                    "你基于同一进程多个图像窗口的分段总结，输出这一整段连续操作的最终详细中文总结。"
                                    "不要输出 markdown，不要输出 JSON，只输出总结正文。"
                                ),
                                cancel_callback=self._is_analysis_cancel_requested,
                            )
                            summary_text = self._clean_ai_process_summary_response(str(merge_response.get("response_text", "")))
                    if not summary_text:
                        continue
                    summaries.append(
                        {
                            "process_name": segment["process_name"],
                            "start_step": segment["start_step"],
                            "end_step": segment["end_step"],
                            "last_step_id": segment["last_step_id"],
                            "step_ids": list(segment["step_ids"]),
                            "summary": summary_text,
                        }
                    )
                    self.window.after(
                        0,
                        lambda batch_index=batch_index, total_segments=total_segments, segment=segment: self._on_ai_analysis_progress(
                            "process_summary_segment_done",
                            {
                                "current_batch": batch_index,
                                "total_batches": total_segments,
                                "start_step": segment["start_step"],
                                "end_step": segment["end_step"],
                                "process_name": segment["process_name"],
                            },
                        ),
                    )
                self._raise_if_analysis_cancel_requested()
                process_summary_overview = None if use_selected_rows else self._build_process_summary_overview(client, segments, summaries)
            except Exception as exc:
                self.window.after(0, lambda message=str(exc): self._on_ai_process_summary_failed(message))
                return

            self.window.after(
                0,
                lambda summaries=summaries, process_summary_overview=process_summary_overview, normalized_selected_rows=normalized_selected_rows: self._on_ai_process_summary_success(
                    summaries,
                    process_summary_overview,
                    normalized_selected_rows,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _build_selected_process_summary_segments(self, row_indexes: list[int]) -> list[dict[str, object]]:
        valid_rows = [row_index for row_index in row_indexes if 0 <= row_index < len(self.event_rows)]
        if not valid_rows:
            return []
        items = [(row_index, self.event_rows[row_index]) for row_index in valid_rows if isinstance(self.event_rows[row_index], dict)]
        if not items:
            return []
        return [self._finalize_selected_process_summary_segment(items)]

    def _finalize_selected_process_summary_segment(self, items: list[tuple[int, dict[str, object]]]) -> dict[str, object]:
        step_ids = [row_index + 1 for row_index, _event in items]
        return {
            "process_name": "已选步骤",
            "start_step": min(step_ids),
            "end_step": max(step_ids),
            "last_step_id": max(step_ids),
            "step_ids": step_ids,
            "summary_scope": "selected_rows",
            "events": [self._build_process_summary_event_payload(row_index, event) for row_index, event in items],
        }

    def _build_process_summary_segments(self) -> list[dict[str, object]]:
        segments: list[dict[str, object]] = []
        current_items: list[tuple[int, dict[str, object]]] = []
        current_process_name = ""
        for row_index, event in enumerate(self.event_rows):
            process_name = self._extract_process_name(event) or "(未知进程)"
            if current_items and process_name != current_process_name:
                segments.append(self._finalize_process_summary_segment(current_process_name, current_items))
                current_items = []
            current_process_name = process_name
            current_items.append((row_index, event))
        if current_items:
            segments.append(self._finalize_process_summary_segment(current_process_name, current_items))
        return [segment for segment in segments if segment.get("step_ids")]

    def _finalize_process_summary_segment(
        self,
        process_name: str,
        items: list[tuple[int, dict[str, object]]],
    ) -> dict[str, object]:
        step_ids = [row_index + 1 for row_index, _event in items]
        return {
            "process_name": process_name,
            "start_step": step_ids[0],
            "end_step": step_ids[-1],
            "last_step_id": step_ids[-1],
            "step_ids": step_ids,
            "events": [self._build_process_summary_event_payload(row_index, event) for row_index, event in items],
        }

    def _build_process_summary_event_payload(self, row_index: int, event: dict[str, object]) -> dict[str, object]:
        payload: dict[str, object] = {
            "step_id": row_index + 1,
            "event_type": self._extract_event_type(event),
            "action": self._extract_event_action(event),
        }
        process_name = self._extract_process_name(event)
        if process_name:
            payload["process_name"] = process_name
        ui_element = event.get("ui_element", {}) if isinstance(event.get("ui_element", {}), dict) else {}
        if ui_element:
            filtered_ui_element = {
                key: value
                for key, value in ui_element.items()
                if key in {"name", "control_type", "help_text", "automation_id", "class_name"} and str(value).strip()
            }
            if filtered_ui_element:
                payload["ui_element"] = filtered_ui_element
        comment = self._extract_comment(event)
        if comment:
            payload["comment"] = comment
        ai_note = self.ai_step_texts.get(row_index, "")
        if ai_note:
            payload["ai_note"] = ai_note
        return payload

    def _build_process_summary_text_only_prompt(self, segment: dict[str, object]) -> str:
        is_selected_rows_summary = str(segment.get("summary_scope", "")).strip() == "selected_rows"
        instruction = {
            "task": "总结当前选中步骤整体完成了什么操作" if is_selected_rows_summary else "总结连续同进程步骤完成了什么操作",
            "process_name": segment.get("process_name", ""),
            "step_range": [segment.get("start_step", 0), segment.get("end_step", 0)],
            "requirements": [
                "这些步骤是用户当前选中的步骤，请把它们作为一个整体来总结。" if is_selected_rows_summary else "这些步骤都属于同一个连续进程段，请总结这一段操作整体在做什么。",
                "请结合每一步的 event_type、action、ui_element、comment，以及已有 ai_note 做总结。",
                "总结要详细，但要是自然语言，不要逐条机械复述原字段名。",
                "如果某些步骤只是过渡、定位、切换或确认动作，也要体现在整体流程里。",
                "不要输出 JSON，不要输出 markdown，不要输出标题，只输出一段中文总结。",
            ],
            "events": segment.get("events", []),
        }
        return json.dumps(instruction, ensure_ascii=False, indent=2)

    def _build_process_summary_prompt(self, segment: dict[str, object], batch: dict[str, object]) -> str:
        is_selected_rows_summary = str(segment.get("summary_scope", "")).strip() == "selected_rows"
        instruction = {
            "task": "总结当前选中步骤中的当前图像批次操作" if is_selected_rows_summary else "总结同一进程连续步骤中的当前图像批次操作",
            "process_name": segment.get("process_name", ""),
            "segment_step_range": [segment.get("start_step", 0), segment.get("end_step", 0)],
            "window_step_ids": batch.get("step_ids", []),
            "requirements": [
                "当前会提供这一批选中步骤中的一批截图，最多 5 张，按步骤顺序排列。" if is_selected_rows_summary else "当前会提供这一进程段中的一批截图，最多 5 张，按步骤顺序排列。",
                "请结合当前批次截图、整段事件明细，以及已有 AI 看图描述，总结这批图像对应的操作过程。",
                "没有截图的步骤也已经包含在 events 中，请在整体总结时一并考虑。",
                "只输出一段详细中文总结，不要输出 JSON，不要输出 markdown。",
            ],
            "events": segment.get("events", []),
        }
        return json.dumps(instruction, ensure_ascii=False, indent=2)

    def _build_process_summary_merge_prompt(self, segment: dict[str, object], window_summaries: list[dict[str, object]]) -> str:
        is_selected_rows_summary = str(segment.get("summary_scope", "")).strip() == "selected_rows"
        instruction = {
            "task": "汇总当前选中步骤的多个图像批次总结" if is_selected_rows_summary else "汇总同一进程连续步骤的多个图像批次总结",
            "process_name": segment.get("process_name", ""),
            "segment_step_range": [segment.get("start_step", 0), segment.get("end_step", 0)],
            "requirements": [
                "这些 window_summaries 来自当前选中的步骤，只是因为图片较多被拆成多个窗口。" if is_selected_rows_summary else "这些 window_summaries 来自同一进程的连续步骤，只是因为图片较多被拆成多个窗口。",
                "请去重并合并这些窗口总结，得到整段连续操作的最终中文总结。",
                "同时要参考整段 events，确保没有图片的步骤也被纳入整体流程理解。",
                "只输出一段详细中文总结，不要输出 JSON，不要输出 markdown。",
            ],
            "events": segment.get("events", []),
            "window_summaries": window_summaries,
        }
        return json.dumps(instruction, ensure_ascii=False, indent=2)

    def _build_process_summary_overview(self, client: OpenAICompatibleAIClient, segments: list[dict[str, object]], summaries: list[dict[str, object]]) -> dict[str, object]:
        if not summaries:
            return {"summary": "", "rollback_candidates": [], "notes": []}
        response = client.query(
            user_prompt=self._build_process_summary_overview_prompt(segments, summaries),
            system_prompt=(
                "你是桌面自动化流程归纳助手。"
                "你需要从多段进程总结中提炼出用户真正完成的目标操作。"
                "中途走错、回退、重复尝试、不满意后重做的步骤，不应混进最终主流程总结。"
                "同时你还需要单独指出这些可能无效或后续被回退覆盖的步骤。"
                "必须返回 JSON，不要输出 markdown。"
            ),
            cancel_callback=self._is_analysis_cancel_requested,
        )
        return self._parse_process_summary_overview_response(str(response.get("response_text", "")))

    def _build_process_summary_overview_prompt(self, segments: list[dict[str, object]], summaries: list[dict[str, object]]) -> str:
        instruction = {
            "task": "基于整次录制的分段总结，生成统一总结并识别可能无效或被回退的步骤",
            "requirements": [
                "final_summary 只保留用户真正要完成的操作主线，不要把明显走错、回退、重复尝试、修正性中间动作写进主线总结。",
                "rollback_candidates 需要列出可能无效、后来被回退、被重做覆盖、或最终不属于主线目标的步骤范围。",
                "rollback_candidates 中每一项都要给出 step_ids 和 reason。step_ids 必须使用整数数组。",
                "如果没有明显无效操作，rollback_candidates 返回空数组。",
                "必须严格返回 JSON 对象，字段为 final_summary、rollback_candidates、notes。不要输出额外说明。",
            ],
            "json_schema_hint": {
                "final_summary": "用户先打开设置页面，定位到目标配置项并完成修改，随后保存配置并返回主界面确认结果。",
                "rollback_candidates": [
                    {"step_ids": [12, 13, 14], "reason": "先进入了错误菜单，随后返回并改走正确入口。"},
                    {"step_ids": [21], "reason": "一次不满意的点击尝试，后续又重新执行了正确操作。"}
                ],
                "notes": ["如果某段只是重复确认且没有改变结果，可视为非主线。"],
            },
            "segment_summaries": summaries,
            "segments": [
                {
                    "process_name": segment.get("process_name", ""),
                    "start_step": segment.get("start_step", 0),
                    "end_step": segment.get("end_step", 0),
                    "events": segment.get("events", []),
                }
                for segment in segments
            ],
        }
        return json.dumps(instruction, ensure_ascii=False, indent=2)

    def _parse_process_summary_overview_response(self, response_text: str) -> dict[str, object]:
        payload = self._parse_viewer_ai_json(response_text)
        final_summary = self._clean_sentence(str(payload.get("final_summary", payload.get("summary", ""))))
        notes = payload.get("notes", []) if isinstance(payload.get("notes", []), list) else []
        cleaned_notes = self._deduplicate_texts([self._clean_sentence(str(item)) for item in notes if self._clean_sentence(str(item))])
        rollback_candidates: list[dict[str, object]] = []
        values = payload.get("rollback_candidates", []) if isinstance(payload.get("rollback_candidates", []), list) else []
        for item in values:
            if not isinstance(item, dict):
                continue
            raw_step_ids = item.get("step_ids", []) if isinstance(item.get("step_ids", []), list) else []
            step_ids = sorted({int(step_id) for step_id in raw_step_ids if isinstance(step_id, int) and step_id > 0})
            reason = self._clean_sentence(str(item.get("reason", "")))
            if not step_ids and not reason:
                continue
            rollback_candidates.append(
                {
                    "step_ids": step_ids,
                    "start_step": min(step_ids) if step_ids else None,
                    "end_step": max(step_ids) if step_ids else None,
                    "reason": reason,
                }
            )
        return {
            "summary": final_summary,
            "rollback_candidates": rollback_candidates,
            "notes": cleaned_notes,
            "raw": str(response_text or "").strip(),
        }

    def _build_process_summary_image_batches(self, segment: dict[str, object], settings) -> list[dict[str, object]]:
        display_layout = self._get_session_display_layout()
        cache_dir = self.session_dir / "ai_preprocessed" / "process_summary"
        image_entries: list[dict[str, object]] = []
        for event_item in segment.get("events", []):
            if not isinstance(event_item, dict):
                continue
            step_id = int(event_item.get("step_id", 0) or 0)
            if step_id < 1 or step_id > len(self.event_rows):
                continue
            event = self.event_rows[step_id - 1]
            image_path = self._resolve_event_primary_image_path(event)
            if image_path is None:
                continue
            prepared_path, _was_cropped = prepare_image_path_for_ai(
                image_path,
                event,
                display_layout,
                cache_dir,
                send_fullscreen=settings.send_fullscreen_screenshots,
                cache_key=f"process_summary_{step_id:04d}",
            )
            optimized_path = self._optimize_image_for_ai_batch(prepared_path, step_id)
            image_entries.append({"step_id": step_id, "image_path": optimized_path})

        batches: list[dict[str, object]] = []
        for start in range(0, len(image_entries), 5):
            batch_entries = image_entries[start : start + 5]
            if not batch_entries:
                continue
            batches.append(
                {
                    "step_ids": [item["step_id"] for item in batch_entries],
                    "image_paths": [item["image_path"] for item in batch_entries],
                }
            )
        return batches

    def _get_session_display_layout(self) -> dict[str, object] | None:
        environment = self.session_data.get("environment", {}) if isinstance(self.session_data, dict) and isinstance(self.session_data.get("environment", {}), dict) else {}
        return environment.get("display_layout") if isinstance(environment, dict) else None

    def _optimize_image_for_ai_batch(self, image_path: Path, step_id: int) -> Path:
        if not self.session_dir:
            return image_path
        output_dir = self.session_dir / "ai_preprocessed" / "process_summary_resized"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"step_{step_id:04d}.jpg"
        try:
            with Image.open(image_path) as image:
                converted = image.convert("RGB")
                converted.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                converted.save(output_path, format="JPEG", quality=82, optimize=True)
            return output_path
        except Exception:
            return image_path

    def _clean_ai_process_summary_response(self, value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("```"):
            lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
            text = "\n".join(lines).strip()
        return self._clean_sentence(text)

    def _on_ai_process_summary_success(self, summaries: list[dict[str, object]], process_summary_overview: dict[str, object] | None, selected_rows: list[int] | None = None) -> None:
        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.ai_process_summary_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "AI总结完成"
        self.analysis_status_token += 1
        base_analysis = copy.deepcopy(self.ai_analysis) if isinstance(self.ai_analysis, dict) else self._load_ai_analysis(self.session_dir) or {}
        if selected_rows:
            analysis = self._merge_selected_process_summaries_into_analysis(base_analysis, summaries, selected_rows)
        else:
            analysis = self._merge_process_summaries_into_analysis(base_analysis, summaries, process_summary_overview)
        self.ai_analysis = analysis
        self.ai_step_tags = self._build_ai_step_tags(analysis)
        self.ai_step_texts = self._build_ai_step_texts(analysis)
        self.ai_process_summary_texts = self._build_ai_process_summary_texts(analysis)
        self._persist_ai_analysis(analysis)
        if selected_rows:
            self.ai_var.set(f"AI总结完成: 已更新所选 {len(selected_rows)} 行的总结")
        else:
            self.ai_var.set(f"AI总结完成: 已生成 {len(summaries)} 段进程总结，并更新统一总结")
        self._refresh_ai_panels()
        self._refresh_coverage_summary()
        self._reload_tree()
        if selected_rows:
            messagebox.showinfo("AI总结完成", f"已将所选 {len(selected_rows)} 行作为一个整体完成总结。", parent=self.window)
        else:
            messagebox.showinfo("AI总结完成", f"已生成 {len(summaries)} 段进程总结，并更新统一总结与回退判断。", parent=self.window)

    def _on_ai_process_summary_failed(self, message: str) -> None:
        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.ai_process_summary_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "AI总结失败"
        self.analysis_status_token += 1
        self.ai_var.set(f"AI总结失败: {message}")
        messagebox.showerror("AI总结失败", message, parent=self.window)

    def _merge_process_summaries_into_analysis(
        self,
        base_analysis: dict[str, object],
        process_summaries: list[dict[str, object]],
        process_summary_overview: dict[str, object] | None = None,
    ) -> dict[str, object]:
        analysis = copy.deepcopy(base_analysis) if isinstance(base_analysis, dict) else {}
        analysis.setdefault("session_id", str((self.session_data or {}).get("session_id", self.session_dir.name if self.session_dir else "")))
        analysis.setdefault("batch_size", 1)
        analysis.setdefault("status", "completed")
        analysis.setdefault("failure_message", "")
        analysis.setdefault("carry_memory", [])
        analysis.setdefault("batches", [])
        analysis.setdefault("step_observations", [])
        analysis.setdefault("step_insights", [])
        analysis.setdefault("invalid_steps", [])
        analysis.setdefault("reusable_modules", [])
        analysis.setdefault("wait_suggestions", [])
        analysis.setdefault("analysis_notes", [])
        analysis.setdefault("workflow_report_markdown", "")
        analysis["process_summaries"] = process_summaries
        analysis["process_summary_overview"] = copy.deepcopy(process_summary_overview) if isinstance(process_summary_overview, dict) else {}
        return analysis

    def _merge_selected_process_summaries_into_analysis(
        self,
        base_analysis: dict[str, object],
        process_summaries: list[dict[str, object]],
        selected_rows: list[int],
    ) -> dict[str, object]:
        analysis = copy.deepcopy(base_analysis) if isinstance(base_analysis, dict) else {}
        analysis.setdefault("session_id", str((self.session_data or {}).get("session_id", self.session_dir.name if self.session_dir else "")))
        analysis.setdefault("batch_size", 1)
        analysis.setdefault("status", "completed")
        analysis.setdefault("failure_message", "")
        analysis.setdefault("carry_memory", [])
        analysis.setdefault("batches", [])
        analysis.setdefault("step_observations", [])
        analysis.setdefault("step_insights", [])
        analysis.setdefault("invalid_steps", [])
        analysis.setdefault("reusable_modules", [])
        analysis.setdefault("wait_suggestions", [])
        analysis.setdefault("analysis_notes", [])
        analysis.setdefault("workflow_report_markdown", "")
        selected_step_ids = {row_index + 1 for row_index in selected_rows if row_index >= 0}
        existing_summaries = analysis.get("process_summaries", []) if isinstance(analysis.get("process_summaries", []), list) else []
        preserved_summaries: list[dict[str, object]] = []
        for item in existing_summaries:
            if not isinstance(item, dict):
                continue
            step_ids = item.get("step_ids", []) if isinstance(item.get("step_ids", []), list) else []
            item_step_ids = {step_id for step_id in step_ids if isinstance(step_id, int) and step_id > 0}
            if item_step_ids & selected_step_ids:
                continue
            preserved_summaries.append(copy.deepcopy(item))
        merged_summaries = [*preserved_summaries, *[copy.deepcopy(item) for item in process_summaries if isinstance(item, dict)]]
        analysis["process_summaries"] = sorted(
            merged_summaries,
            key=lambda item: int(item.get("last_step_id", item.get("end_step", 0)) or 0),
        )
        return analysis

    def _invalidate_suggestion_outputs_for_rows(self, row_indexes: list[int], message: str) -> None:
        target_step_ids = {row_index + 1 for row_index in row_indexes if row_index >= 0}
        if not target_step_ids:
            return

        if self.suggestion_result is None and self.session_dir:
            suggestion_path = self.session_dir / "conversion_suggestions.json"
            if suggestion_path.exists():
                try:
                    self.suggestion_result = self.suggestion_service.load_result_file(suggestion_path)
                except Exception:
                    self.suggestion_result = None

        if self.suggestion_result is None:
            for row_index in row_indexes:
                self.parameter_prompt_by_step.pop(row_index, None)
                self.parameter_response_by_step.pop(row_index, None)
            self.suggestion_var.set(message)
            self.parameter_progress_var.set("参数推荐批处理未执行")
            self.parameter_status_var.set("请为当前步骤重新生成调用建议后再执行参数推荐。")
            self._refresh_ai_chat_panel()
            return

        self.suggestion_result.suggestions = [
            item for item in self.suggestion_result.suggestions
            if int(getattr(item, "step_id", 0) or 0) not in target_step_ids
        ]

        for row_index in row_indexes:
            self.parameter_prompt_by_step.pop(row_index, None)
            self.parameter_response_by_step.pop(row_index, None)

        if self.suggestion_result.suggestions:
            self._persist_suggestion_result()
            self.suggestion_var.set(message)
        else:
            self.suggestion_result = None
            self.step_method_suggestions = {}
            self.step_module_suggestions = {}
            self.step_parameter_summaries = {}
            if self.session_dir:
                suggestion_path = self.session_dir / "conversion_suggestions.json"
                if suggestion_path.exists():
                    try:
                        suggestion_path.unlink()
                    except Exception:
                        pass
            self.suggestion_var.set(message)

        self.parameter_progress_var.set("参数推荐批处理未执行")
        self.parameter_status_var.set("请仅对当前受影响步骤重新生成调用建议后再执行参数推荐。")
        self._refresh_ai_chat_panel()

    def cancel_ai_analysis(self) -> None:
        if not self.analysis_running:
            return
        self.analysis_status_base = "正在请求取消 AI 分析"
        self._refresh_analysis_status(self.analysis_status_token)
        self.analysis_cancel_event.set()
        analyzer = self.current_analyzer
        if analyzer is not None:
            analyzer.cancel()
        elif self.settings_store.load().use_remote_ai_service:
            self.analysis_status_base = "远端共享服务当前不支持取消，等待本次请求结束"
            self._refresh_analysis_status(self.analysis_status_token)

    def _is_analysis_cancel_requested(self) -> bool:
        return self.analysis_cancel_event.is_set()

    def _raise_if_analysis_cancel_requested(self) -> None:
        if self._is_analysis_cancel_requested():
            raise RuntimeError("AI 分析已取消。")

    def _set_details(self, payload: dict[str, object]) -> None:
        self.details_text.configure(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert(tk.END, json.dumps(payload, indent=2, ensure_ascii=False))
        self.details_text.configure(state=tk.DISABLED)

    def _set_text_widget(self, widget: tk.Text, text: str, disabled: bool = True) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.configure(state=tk.DISABLED if disabled else tk.NORMAL)

    def _show_text_dialog(self, title: str, text: str) -> None:
        dialog = tk.Toplevel(self.window)
        dialog.title(title)
        dialog.geometry("900x600")
        dialog.transient(self.window)

        container = ttk.Frame(dialog, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        text_widget = tk.Text(container, wrap=tk.WORD, font=("Consolas", 10))
        text_widget.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=text_widget.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scrollbar.set)
        self._set_text_widget(text_widget, text, disabled=False)

        button_bar = ttk.Frame(container)
        button_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(button_bar, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def _show_edit_text_dialog(self, title: str, text: str) -> str | None:
        dialog = tk.Toplevel(self.window)
        dialog.title(title)
        dialog.geometry("900x600")
        dialog.transient(self.window)
        dialog.grab_set()

        result: dict[str, str | None] = {"value": None}

        container = ttk.Frame(dialog, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        text_widget = tk.Text(container, wrap=tk.WORD, font=("Consolas", 10))
        text_widget.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=text_widget.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scrollbar.set)
        self._set_text_widget(text_widget, text, disabled=False)

        def save() -> None:
            result["value"] = text_widget.get("1.0", tk.END).rstrip("\n")
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        button_bar = ttk.Frame(container)
        button_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(button_bar, text="取消", command=cancel).pack(side=tk.RIGHT)
        ttk.Button(button_bar, text="保存", command=save).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        text_widget.focus_set()
        dialog.wait_window()
        return result["value"]

    def _edit_method_or_module_suggestion(self, row_index: int, field_name: str) -> None:
        suggestion = self._find_suggestion_by_row_index(row_index)
        if suggestion is None or not self.session_dir or self.suggestion_result is None:
            messagebox.showinfo("提示", f"步骤 {row_index + 1} 暂无可编辑的建议。", parent=self.window)
            return
        title = "方法建议" if field_name == "method_name" else "模块建议"
        current_value = str(getattr(suggestion, field_name, "") or "")
        edited_value = self._show_edit_text_dialog(f"编辑步骤 {row_index + 1} {title}", current_value)
        if edited_value is None:
            return
        setattr(suggestion, field_name, edited_value.strip())
        self._persist_suggestion_result()
        self._after_edit_row_content(row_index)

    def _edit_parameter_suggestion(self, row_index: int) -> None:
        suggestion = self._find_suggestion_by_row_index(row_index)
        if suggestion is None or not self.session_dir or self.suggestion_result is None:
            messagebox.showinfo("提示", f"步骤 {row_index + 1} 暂无可编辑的参数建议。", parent=self.window)
            return
        current_value = self._describe_parameter_suggestion_for_view(row_index) or ""
        edited_value = self._show_edit_text_dialog(f"编辑步骤 {row_index + 1} 参数建议", current_value)
        if edited_value is None:
            return
        suggestion.candidate_payload["viewer_parameter_summary_override"] = edited_value.strip()
        self._persist_suggestion_result()
        self._after_edit_row_content(row_index)

    def _edit_ai_note(self, row_index: int) -> None:
        current_value = self._describe_event_for_view(row_index, self.event_rows[row_index])
        edited_value = self._show_edit_text_dialog(f"编辑步骤 {row_index + 1} AI看图", current_value)
        if edited_value is None:
            return
        if not self._update_ai_analysis_text(row_index, edited_value.strip()):
            messagebox.showinfo("提示", f"步骤 {row_index + 1} 当前没有可编辑的 AI 结果。", parent=self.window)
            return
        self._after_edit_row_content(row_index)

    def _edit_ai_process_summary(self, row_index: int) -> None:
        current_value = self._describe_process_summary_for_view(row_index)
        edited_value = self._show_edit_text_dialog(f"编辑步骤 {row_index + 1} AI总结", current_value)
        if edited_value is None:
            return
        if not self._update_ai_process_summary_text(row_index, edited_value.strip()):
            messagebox.showinfo("提示", f"步骤 {row_index + 1} 当前没有可编辑的 AI总结结果。", parent=self.window)
            return
        self._after_edit_row_content(row_index)

    def _after_edit_row_content(self, row_index: int) -> None:
        self._reload_tree()
        self._select_row_index(row_index)

    def _edit_event_action(self, row_index: int) -> None:
        event = self.event_rows[row_index]
        current_value = self._extract_event_action(event)
        edited_value = self._show_edit_text_dialog(f"编辑步骤 {row_index + 1} 动作", current_value)
        if edited_value is None:
            return
        self._update_event_action(row_index, edited_value.strip())
        self._persist_session()
        self._after_edit_row_content(row_index)

    def _edit_event_type(self, row_index: int) -> None:
        event = self.event_rows[row_index]
        current_value = self._extract_event_type(event)
        edited_value = self._show_edit_text_dialog(f"编辑步骤 {row_index + 1} 类型", current_value)
        if edited_value is None:
            return
        self._update_event_type(row_index, edited_value.strip())
        self._persist_session()
        self._refresh_filter_options()
        self._after_edit_row_content(row_index)

    def _persist_suggestion_result(self) -> None:
        if not self.session_dir or self.suggestion_result is None:
            return
        self.suggestion_service.write_result_file(self.session_dir / "conversion_suggestions.json", self.suggestion_result)
        self.step_method_suggestions = self._build_method_suggestion_map(self.suggestion_result)
        self.step_module_suggestions = self._build_module_suggestion_map(self.suggestion_result)
        self.step_parameter_summaries = self._build_parameter_suggestion_map(self.suggestion_result)
        self.suggestion_var.set(self._build_suggestion_summary_text(self.suggestion_result))
        self._refresh_selected_suggestion_panel()

    def _update_ai_analysis_text(self, row_index: int, text: str) -> bool:
        analysis = self.ai_analysis
        if not isinstance(analysis, dict):
            if not self.session_dir:
                return False
            analysis = self._load_ai_analysis(self.session_dir)
            if not isinstance(analysis, dict):
                return False

        step_id = row_index + 1
        updated = False
        explicit_observations = analysis.get("step_observations", [])
        if isinstance(explicit_observations, list):
            for item in explicit_observations:
                if isinstance(item, dict) and item.get("step_id") == step_id:
                    item["observation"] = text
                    updated = True

        for batch in analysis.get("batches", []):
            if not isinstance(batch, dict):
                continue
            parsed_result = batch.get("parsed_result", {}) if isinstance(batch.get("parsed_result", {}), dict) else {}
            observation_round = parsed_result.get("observation_round", {}) if isinstance(parsed_result.get("observation_round", {}), dict) else {}
            values = observation_round.get("step_observations", [])
            event_indexes = batch.get("event_indexes", []) if isinstance(batch.get("event_indexes", []), list) else []
            if not isinstance(values, list):
                continue
            for offset, item in enumerate(values):
                if not isinstance(item, dict):
                    continue
                item_step_id = item.get("step_id")
                if not isinstance(item_step_id, int) and offset < len(event_indexes) and isinstance(event_indexes[offset], int):
                    item_step_id = event_indexes[offset]
                if item_step_id == step_id:
                    item["observation"] = text
                    updated = True

        step_insights = analysis.get("step_insights", [])
        if isinstance(step_insights, list):
            for item in step_insights:
                if isinstance(item, dict) and item.get("step_id") == step_id:
                    item["description"] = text
                    updated = True

        if not updated:
            return False

        self.ai_analysis = analysis
        self.ai_step_tags = self._build_ai_step_tags(analysis)
        self.ai_step_texts = self._build_ai_step_texts(analysis)
        self.ai_var.set(f"已更新 AI 分析结果 | {self._build_ai_summary_text(analysis)}")
        self._persist_ai_analysis(analysis)
        self._refresh_ai_panels()
        self._refresh_selected_suggestion_panel()
        self._refresh_coverage_summary()
        return True

    def _update_ai_process_summary_text(self, row_index: int, text: str) -> bool:
        analysis = self.ai_analysis
        if not isinstance(analysis, dict):
            if not self.session_dir:
                return False
            analysis = self._load_ai_analysis(self.session_dir)
            if not isinstance(analysis, dict):
                return False

        step_id = row_index + 1
        process_summaries = analysis.get("process_summaries", [])
        if not isinstance(process_summaries, list):
            return False

        updated = False
        for item in process_summaries:
            if not isinstance(item, dict):
                continue
            item_step_id = item.get("last_step_id", item.get("end_step"))
            if item_step_id == step_id:
                item["summary"] = text
                updated = True

        if not updated:
            return False

        self.ai_analysis = analysis
        self.ai_step_tags = self._build_ai_step_tags(analysis)
        self.ai_step_texts = self._build_ai_step_texts(analysis)
        self.ai_process_summary_texts = self._build_ai_process_summary_texts(analysis)
        self.ai_var.set(f"已更新 AI 总结结果 | {self._build_ai_summary_text(analysis)}")
        self._persist_ai_analysis(analysis)
        self._refresh_ai_panels()
        self._refresh_selected_suggestion_panel()
        self._refresh_coverage_summary()
        return True

    def _update_event_action(self, index: int, action: str) -> None:
        event = self.event_rows[index]
        event["action"] = action

    def _update_event_type(self, index: int, event_type: str) -> None:
        event = self.event_rows[index]
        event["event_type"] = event_type

    def _get_selected_row_indexes(self) -> list[int]:
        selection = self.tree.selection()
        if not selection:
            return []
        row_indexes = []
        for item_id in selection:
            try:
                row_indexes.append(int(item_id))
            except ValueError:
                continue
        return sorted(set(row_indexes))

    def _get_primary_selected_row_index(self) -> int | None:
        row_indexes = self._get_selected_row_indexes()
        if not row_indexes:
            return None
        focus_item = self.tree.focus()
        if focus_item:
            try:
                focus_index = int(focus_item)
            except ValueError:
                focus_index = None
            if focus_index is not None and focus_index in row_indexes:
                return focus_index
        return row_indexes[0]

    def _select_row_index(self, row_index: int, source_tree: ttk.Treeview | None = None) -> None:
        row_id = str(row_index)
        self._synchronizing_tree_selection = True
        try:
            if self.tree.exists(row_id):
                self.tree.selection_set(row_id)
                self.tree.focus(row_id)
                self.tree.see(row_id)
            if self.event_list_tree and self.event_list_tree.winfo_exists() and self.event_list_tree.exists(row_id):
                self.event_list_tree.selection_set(row_id)
                self.event_list_tree.focus(row_id)
                self.event_list_tree.see(row_id)
        finally:
            self._synchronizing_tree_selection = False
        if source_tree is self.event_list_tree or source_tree is None:
            self.on_select_event(None)

    def select_all_events(self) -> None:
        row_ids = [str(index) for index in self._visible_row_indexes() if self.tree.exists(str(index))]
        if not row_ids:
            return
        self.tree.selection_set(row_ids)
        self.tree.focus(row_ids[0])
        self.tree.see(row_ids[0])
        self.on_select_event(None)

    def delete_selected_events(self) -> None:
        if not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        row_indexes = self._get_selected_row_indexes()
        if not row_indexes:
            messagebox.showinfo("提示", "请先选择至少一行事件。", parent=self.window)
            return
        if len(row_indexes) == 1:
            prompt = f"确认删除步骤 {row_indexes[0] + 1} 吗？"
        else:
            prompt = f"确认删除所选 {len(row_indexes)} 行事件吗？"
        if not messagebox.askyesno("确认删除", prompt, parent=self.window):
            return
        self._delete_event_rows(row_indexes)

    def edit_selected_checkpoint(self) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        row_indexes = self._get_selected_row_indexes()
        if len(row_indexes) != 1:
            messagebox.showinfo("提示", "请只选择一条 checkpoint 事件进行修改。", parent=self.window)
            return
        row_index = row_indexes[0]
        if not (0 <= row_index < len(self.event_rows)):
            return
        event = self.event_rows[row_index]
        if self._extract_event_type(event) != "checkpoint":
            messagebox.showinfo("提示", "只有 checkpoint 事件支持修改。", parent=self.window)
            return

        engine = self._create_session_edit_engine()
        if engine is None:
            return
        draft = self._build_ai_checkpoint_draft_from_event(event)
        payload = open_ai_checkpoint_editor_dialog(self.window, engine, self.settings_store, draft)
        if not payload:
            return

        updated_event = self._build_checkpoint_event_from_payload(event, payload)
        self.event_rows[row_index] = updated_event
        self.session_data["events"] = self.event_rows
        self._sync_checkpoint_collection_entry(event, updated_event)
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        self._invalidate_derived_outputs()
        self.media_cache.clear()
        self._reload_tree()
        self._select_row_index(row_index)

    def insert_checkpoint_after_row(self, row_index: int) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        if not (0 <= row_index < len(self.event_rows)):
            return

        engine = self._create_session_edit_engine()
        if engine is None:
            return
        draft = AICheckpointDraft()
        payload = open_ai_checkpoint_editor_dialog(self.window, engine, self.settings_store, draft)
        if not payload:
            return

        reference_event = self.event_rows[row_index]
        inserted_event = self._build_new_checkpoint_event(reference_event, payload, engine)
        insert_at = row_index + 1
        original_event_count = len(self.event_rows)
        self.event_rows.insert(insert_at, inserted_event)
        self.session_data["events"] = self.event_rows
        self._sync_checkpoint_collection_entry({}, inserted_event)
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        step_id_mapping = self._build_step_id_mapping_after_row_insertion(original_event_count, insert_at, 1)
        self._remap_outputs_after_row_insertion(step_id_mapping)
        self.media_cache.clear()
        self._reload_tree()
        self._select_row_index(insert_at)

    def insert_recorded_steps_after_row(self, row_index: int) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        if not (0 <= row_index < len(self.event_rows)):
            return
        should_start = messagebox.askyesno(
            "插入步骤 - 录制",
            "将开始一段临时录制。\n\n完成操作后，请在弹出的控制窗口中点击“停止并插入”。\n临时录制过程中也支持添加 Comment / 等待事件 / 记录截图 / AI Checkpoint，它们会一起插入事件列表。\n\n是否继续？",
            parent=self.window,
        )
        if not should_start:
            return

        temp_root = Path(tempfile.mkdtemp(prefix="viewer_insert_recording_"))
        temp_engine = RecorderEngine(temp_root, settings_store=self.settings_store)
        temp_metadata = {
            "is_prs_recording": True,
            "testcase_id": "VIEWER_TEMP",
            "version_number": "TEMP",
            "recorder_person": "viewer_insert",
            "design_steps": "Viewer 临时插入录制",
            "scope": "All",
        }
        popup_was_visible = bool(self.event_list_window and self.event_list_window.winfo_exists())
        owner_window = self.window.master if isinstance(self.window.master, tk.Misc) else None
        owner_window_state = owner_window.wm_state() if owner_window and owner_window.winfo_exists() else "withdrawn"
        try:
            temp_engine.start(metadata=temp_metadata)
        except Exception as exc:
            shutil.rmtree(temp_root, ignore_errors=True)
            messagebox.showerror("启动录制失败", str(exc), parent=self.window)
            return

        if owner_window and owner_window is not self.window and owner_window_state not in {"withdrawn", "iconic"}:
            owner_window.iconify()
        self.window.withdraw()
        if popup_was_visible and self.event_list_window:
            self.event_list_window.withdraw()

        outcome: tuple[str, Path | None] = self._run_temporary_recording_controller(temp_engine)

        if popup_was_visible and self.event_list_window and self.event_list_window.winfo_exists():
            self.event_list_window.deiconify()
            self.event_list_window.lift()
        if owner_window and owner_window is not self.window and owner_window.winfo_exists() and owner_window_state not in {"withdrawn", "iconic"}:
            owner_window.deiconify()
            owner_window.lift()
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

        try:
            status, session_dir = outcome
            if status != "saved" or session_dir is None:
                return
            recorded_events = self._load_insertable_recorded_events(session_dir)
            if not recorded_events:
                messagebox.showinfo("提示", "临时录制未产生可插入的普通步骤。", parent=self.window)
                return
            self._insert_recorded_events_after_row(row_index, recorded_events, session_dir)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def insert_imported_steps_after_row(self, row_index: int) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        if not (0 <= row_index < len(self.event_rows)):
            return
        source_session_dir = self._prompt_session_to_import()
        if source_session_dir is None:
            return
        recorded_events = self._load_insertable_recorded_events(source_session_dir)
        if not recorded_events:
            messagebox.showinfo("提示", "所选 session 未包含可导入的步骤。", parent=self.window)
            return
        self._insert_recorded_events_after_row(row_index, recorded_events, source_session_dir)

    def _delete_event_rows(self, row_indexes: list[int]) -> None:
        if not self.session_data:
            return
        valid_indexes = sorted({index for index in row_indexes if 0 <= index < len(self.event_rows)}, reverse=True)
        if not valid_indexes:
            return

        original_event_count = len(self.event_rows)
        step_id_mapping = self._build_step_id_mapping_after_row_deletion(original_event_count, valid_indexes)

        removed_events: list[dict[str, object]] = []
        for row_index in valid_indexes:
            removed_events.append(self.event_rows[row_index])
            del self.event_rows[row_index]

        self.session_data["events"] = self.event_rows
        self._remove_checkpoint_collection_entries(removed_events)
        self._remove_comment_collection_entries(removed_events)
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        self._remap_outputs_after_row_deletion(step_id_mapping)
        self.media_cache.clear()
        self._reload_tree()

        if self.event_rows:
            next_index = min(valid_indexes[-1], len(self.event_rows) - 1)
            self._select_row_index(next_index)

    def _build_step_id_mapping_after_row_deletion(self, original_event_count: int, removed_row_indexes: list[int]) -> dict[int, int]:
        removed_step_ids = {row_index + 1 for row_index in removed_row_indexes}
        next_step_id = 1
        step_id_mapping: dict[int, int] = {}
        for original_step_id in range(1, original_event_count + 1):
            if original_step_id in removed_step_ids:
                continue
            step_id_mapping[original_step_id] = next_step_id
            next_step_id += 1
        return step_id_mapping

    def _build_step_id_mapping_after_row_insertion(self, original_event_count: int, insert_at: int, inserted_count: int) -> dict[int, int]:
        step_id_mapping: dict[int, int] = {}
        for original_step_id in range(1, original_event_count + 1):
            if original_step_id <= insert_at:
                step_id_mapping[original_step_id] = original_step_id
            else:
                step_id_mapping[original_step_id] = original_step_id + inserted_count
        return step_id_mapping

    def _remap_outputs_after_row_deletion(self, step_id_mapping: dict[int, int]) -> None:
        self._remap_ai_analysis_after_row_deletion(step_id_mapping)
        self._remap_suggestions_after_row_deletion(step_id_mapping)
        self._remap_parameter_chat_history_after_row_deletion(step_id_mapping)
        self._refresh_coverage_summary()
        self._update_historical_ai_button_state()

    def _remap_outputs_after_row_insertion(self, step_id_mapping: dict[int, int]) -> None:
        self._remap_ai_analysis_after_row_deletion(step_id_mapping)
        self._remap_suggestions_after_row_deletion(step_id_mapping)
        self._remap_parameter_chat_history_after_row_deletion(step_id_mapping)
        self._refresh_coverage_summary()
        self._update_historical_ai_button_state()

    def _remap_ai_analysis_after_row_deletion(self, step_id_mapping: dict[int, int]) -> None:
        analysis = copy.deepcopy(self.ai_analysis) if isinstance(self.ai_analysis, dict) else None
        if analysis is None and self.session_dir:
            analysis = self._load_ai_analysis(self.session_dir)
        if not isinstance(analysis, dict):
            return

        remapped_analysis = self._remap_analysis_step_ids(analysis, step_id_mapping)
        self.ai_analysis = remapped_analysis
        self.ai_step_tags = self._build_ai_step_tags(remapped_analysis)
        self.ai_step_texts = self._build_ai_step_texts(remapped_analysis)
        self.ai_process_summary_texts = self._build_ai_process_summary_texts(remapped_analysis)
        self._persist_ai_analysis(remapped_analysis)
        self.ai_var.set(f"已更新历史 AI 分析结果 | {self._build_ai_summary_text(remapped_analysis)}")
        self._refresh_ai_panels()

    def _remap_suggestions_after_row_deletion(self, step_id_mapping: dict[int, int]) -> None:
        result = copy.deepcopy(self.suggestion_result) if self.suggestion_result is not None else None
        if result is None and self.session_dir:
            suggestion_path = self.session_dir / "conversion_suggestions.json"
            if suggestion_path.exists():
                try:
                    result = self.suggestion_service.load_result_file(suggestion_path)
                except Exception:
                    result = None
        if result is None:
            return

        remapped_suggestions = []
        for item in result.suggestions:
            step_id = int(getattr(item, "step_id", 0) or 0)
            mapped_step_id = step_id_mapping.get(step_id)
            if mapped_step_id is None:
                continue
            copied_item = copy.deepcopy(item)
            copied_item.step_id = mapped_step_id
            remapped_suggestions.append(copied_item)
        result.suggestions = sorted(remapped_suggestions, key=lambda item: int(getattr(item, "step_id", 0) or 0))
        self.suggestion_result = result
        self._persist_suggestion_result()

    def _remap_parameter_chat_history_after_row_deletion(self, step_id_mapping: dict[int, int]) -> None:
        remapped_prompt_history: dict[int, str] = {}
        for row_index, prompt in self.parameter_prompt_by_step.items():
            mapped_step_id = step_id_mapping.get(row_index + 1)
            if mapped_step_id is None:
                continue
            remapped_prompt_history[mapped_step_id - 1] = prompt
        remapped_response_history: dict[int, str] = {}
        for row_index, response in self.parameter_response_by_step.items():
            mapped_step_id = step_id_mapping.get(row_index + 1)
            if mapped_step_id is None:
                continue
            remapped_response_history[mapped_step_id - 1] = response
        self.parameter_prompt_by_step = remapped_prompt_history
        self.parameter_response_by_step = remapped_response_history

    def _create_session_edit_engine(self) -> RecorderEngine | None:
        if not self.session_dir:
            return None
        try:
            engine = RecorderEngine(self.recordings_root, settings_store=self.settings_store)
            engine.store.resume(self.session_dir)
        except Exception as exc:
            messagebox.showerror("打开编辑器失败", str(exc), parent=self.window)
            return None
        return engine

    def _build_ai_checkpoint_draft_from_event(self, event: dict[str, object]) -> AICheckpointDraft:
        checkpoint = event.get("checkpoint", {}) if isinstance(event.get("checkpoint"), dict) else {}
        ai_result = event.get("ai_result", {}) if isinstance(event.get("ai_result"), dict) else {}
        image_selections: list[tuple[Path, dict[str, int]]] = []
        video_path: Path | None = None
        video_region: dict[str, int] | None = None

        raw_media = event.get("media", [])
        if isinstance(raw_media, list):
            for item in raw_media:
                if not isinstance(item, dict):
                    continue
                relative_path = str(item.get("path", "")).strip()
                if not relative_path or not self.session_dir:
                    continue
                absolute_path = self.session_dir / relative_path
                region = item.get("region", {}) if isinstance(item.get("region"), dict) else {}
                media_type = str(item.get("type", "image"))
                if media_type == "video":
                    video_path = absolute_path
                    video_region = region
                else:
                    image_selections.append((absolute_path, region))

        if not image_selections and event.get("screenshot") and self.session_dir:
            image_selections.append((self.session_dir / str(event.get("screenshot")), {}))

        return AICheckpointDraft(
            title=str(checkpoint.get("title") or event.get("note", "") or "AI Checkpoint"),
            prompt=str(checkpoint.get("prompt", "")),
            query_text=str(checkpoint.get("query", "")),
            design_steps=str(checkpoint.get("design_steps", ai_result.get("design_steps", ""))),
            step_comment=str(
                checkpoint.get(
                    "step_description",
                    checkpoint.get("step_comment", ai_result.get("step_description", ai_result.get("step_comment", ""))),
                )
            ),
            prompt_template_key=str(checkpoint.get("prompt_template_key", "ct_validation") or "ct_validation"),
            response_text=str(checkpoint.get("response", "") or ai_result.get("display_text", "") or ai_result.get("response", "")),
            query_status="已加载历史 Checkpoint",
            image_selections=image_selections,
            video_path=video_path,
            video_region=video_region,
            video_status=f"已加载视频: {video_path.name}" if video_path else "未录制视频",
            query_result=ai_result or None,
        )

    def _build_checkpoint_event_from_payload(self, original_event: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
        existing_checkpoint = original_event.get("checkpoint", {}) if isinstance(original_event.get("checkpoint"), dict) else {}
        media = payload.get("media", []) if isinstance(payload.get("media"), list) else []
        primary_screenshot = next((str(item.get("path", "")) for item in media if isinstance(item, dict) and str(item.get("type", "image")) == "image" and item.get("path")), None)
        design_steps = str(payload.get("design_steps", ""))
        step_description = str(payload.get("step_description", payload.get("step_comment", "")))
        step_comment = str(payload.get("step_comment", step_description))
        query_payload = payload.get("query_payload", {}) if isinstance(payload.get("query_payload"), dict) else {"response": str(payload.get("response_text", ""))}
        query_payload = {
            **query_payload,
            "design_steps": design_steps,
            "step_description": step_description,
            "step_comment": step_comment,
        }
        checkpoint_payload = {
            "title": str(payload.get("title", "AI Checkpoint")),
            "query": str(payload.get("query", "")),
            "prompt": str(payload.get("prompt", "")),
            "response": str(payload.get("response_text", "")),
            "prompt_template_key": str(payload.get("prompt_template_key", "ct_validation") or "ct_validation"),
            "design_steps": design_steps,
            "step_description": step_description,
            "step_comment": step_comment,
            "media_count": len(media),
            "created_at": str(existing_checkpoint.get("created_at", "") or original_event.get("timestamp", "") or datetime.now().isoformat(timespec="seconds")),
        }

        updated_event = dict(original_event)
        updated_event.update(
            {
                "event_type": "checkpoint",
                "action": "ai_checkpoint",
                "screenshot": primary_screenshot,
                "note": checkpoint_payload["title"],
                "checkpoint": checkpoint_payload,
                "media": media,
                "ai_result": query_payload,
            }
        )
        return updated_event

    def _build_new_checkpoint_event(
        self,
        reference_event: dict[str, object],
        payload: dict[str, object],
        engine: RecorderEngine,
    ) -> dict[str, object]:
        timestamp = datetime.now().astimezone().isoformat()
        base_event = {
            "event_id": engine.store.next_event_id("checkpoint"),
            "timestamp": timestamp,
            "event_type": "checkpoint",
            "action": "ai_checkpoint",
            "window": dict(reference_event.get("window", {})) if isinstance(reference_event.get("window"), dict) else {},
            "ui_element": dict(reference_event.get("ui_element", {})) if isinstance(reference_event.get("ui_element"), dict) else {},
            "mouse": {},
            "keyboard": {},
            "scroll": {},
            "note": "",
            "checkpoint": {},
            "media": [],
            "ai_result": {},
            "additional_details": {
                "source": "viewer_insert",
                "analysis_ready": True,
            },
        }
        return self._build_checkpoint_event_from_payload(base_event, payload)

    def _run_temporary_recording_controller(self, temp_engine: RecorderEngine) -> tuple[str, Path | None]:
        result: dict[str, object] = {"status": "cancelled", "session_dir": None}
        dialog_parent = self.window.master if self.window.state() == "withdrawn" else self.window
        dialog = tk.Toplevel(dialog_parent)
        dialog.title("插入录制步骤")
        dialog.geometry("520x230")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        dialog.protocol("WM_DELETE_WINDOW", lambda: on_cancel())

        container = ttk.Frame(dialog, padding=16)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="临时录制已开始。\n可直接执行真实操作，也可以在这里补充 Comment / 等待事件 / 记录截图 / AI Checkpoint。\n完成后点击“停止并插入”；若放弃本次录制，点击“取消”。",
            justify=tk.LEFT,
            wraplength=480,
        ).pack(anchor=tk.W)
        status_var = tk.StringVar(value="录制中...")
        ttk.Label(container, textvariable=status_var).pack(anchor=tk.W, pady=(12, 0))

        action_bar = ttk.Frame(container)
        action_bar.pack(fill=tk.X, pady=(14, 0))

        def with_engine_suspended(callback) -> None:
            temp_engine.suspend()
            current_grab = dialog.grab_current()
            try:
                if current_grab is not None:
                    try:
                        current_grab.grab_release()
                    except Exception:
                        current_grab = None
                callback()
            finally:
                if current_grab is not None and current_grab.winfo_exists():
                    try:
                        current_grab.grab_set()
                    except Exception:
                        pass
                temp_engine.resume()
                dialog.lift()
                dialog.focus_force()

        ttk.Button(
            action_bar,
            text="添加 Comment",
            command=lambda: with_engine_suspended(lambda: open_comment_dialog(dialog, temp_engine)),
        ).pack(side=tk.LEFT)
        ttk.Button(
            action_bar,
            text="添加等待事件",
            command=lambda: with_engine_suspended(lambda: open_wait_for_image_dialog(dialog, temp_engine)),
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            action_bar,
            text="记录截图",
            command=lambda: with_engine_suspended(
                lambda: capture_manual_screenshot(dialog, temp_engine, "选择要插入到临时录制中的截图区域")
            ),
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            action_bar,
            text="添加 AI Checkpoint",
            command=lambda: with_engine_suspended(
                lambda: open_ai_checkpoint_dialog(
                    dialog,
                    temp_engine,
                    self.settings_store,
                    AICheckpointDraft(),
                    historical_screenshots_dir=(self.session_dir / "screenshots") if self.session_dir else None,
                )
            ),
        ).pack(side=tk.LEFT, padx=(8, 0))

        button_bar = ttk.Frame(container)
        button_bar.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))

        def finalize(status: str, session_dir: Path | None = None) -> None:
            result["status"] = status
            result["session_dir"] = session_dir
            if dialog.winfo_exists():
                dialog.destroy()

        def on_stop() -> None:
            try:
                status_var.set("正在停止并整理录制结果...")
                dialog.update_idletasks()
                session_dir, _ = temp_engine.stop()
            except Exception as exc:
                messagebox.showerror("停止录制失败", str(exc), parent=dialog)
                return
            finalize("saved", session_dir)

        def on_cancel() -> None:
            try:
                if temp_engine.is_recording:
                    temp_engine.stop()
            except Exception:
                pass
            finalize("cancelled", None)

        ttk.Button(button_bar, text="取消", command=on_cancel).pack(side=tk.RIGHT)
        ttk.Button(button_bar, text="停止并插入", command=on_stop).pack(side=tk.RIGHT, padx=(0, 8))

        if dialog_parent is self.window and self.window.state() != "withdrawn":
            dialog.transient(self.window)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        self.window.wait_window(dialog)
        return str(result.get("status", "cancelled")), result.get("session_dir") if isinstance(result.get("session_dir"), Path) else None

    def _load_insertable_recorded_events(self, session_dir: Path) -> list[dict[str, object]]:
        session_path = session_dir / "session.json"
        if not session_path.exists():
            return []
        try:
            payload = json.loads(session_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        raw_events = payload.get("events", []) if isinstance(payload, dict) else []
        if not isinstance(raw_events, list):
            return []
        insertable_events: list[dict[str, object]] = []
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            insertable_events.append(event)
        return insertable_events

    def _insert_recorded_events_after_row(self, row_index: int, recorded_events: list[dict[str, object]], source_session_dir: Path) -> None:
        engine = self._create_session_edit_engine()
        if engine is None or engine.store.session_dir is None or not self.session_data:
            return
        inserted_events = [self._clone_recorded_event_for_current_session(event, source_session_dir, engine) for event in recorded_events]
        insert_at = row_index + 1
        original_event_count = len(self.event_rows)
        self.event_rows[insert_at:insert_at] = inserted_events
        self.session_data["events"] = self.event_rows
        self._sync_auxiliary_collections_for_inserted_events(inserted_events)
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        step_id_mapping = self._build_step_id_mapping_after_row_insertion(original_event_count, insert_at, len(inserted_events))
        self._remap_outputs_after_row_insertion(step_id_mapping)
        self.media_cache.clear()
        self._reload_tree()
        self._select_row_index(insert_at)

    def _clone_recorded_event_for_current_session(
        self,
        event: dict[str, object],
        source_session_dir: Path,
        engine: RecorderEngine,
    ) -> dict[str, object]:
        cloned_event = copy.deepcopy(event)
        prefix = self._derive_event_id_prefix(
            str(event.get("event_id", "")),
            self._extract_event_type(event),
        )
        cloned_event["event_id"] = engine.store.next_event_id(prefix)
        screenshot = cloned_event.get("screenshot")
        if screenshot:
            cloned_event["screenshot"] = self._copy_session_artifact(str(screenshot), source_session_dir, engine, preferred_folder="screenshots")
        raw_media = cloned_event.get("media", [])
        if isinstance(raw_media, list):
            normalized_media: list[dict[str, object]] = []
            for item in raw_media:
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                copied_item = dict(item)
                preferred_folder = self._detect_media_folder(str(item.get("path", "")), str(item.get("type", "")))
                copied_item["path"] = self._copy_session_artifact(str(item.get("path", "")), source_session_dir, engine, preferred_folder=preferred_folder)
                normalized_media.append(copied_item)
            cloned_event["media"] = normalized_media
        return cloned_event

    def _copy_session_artifact(
        self,
        relative_path: str,
        source_session_dir: Path,
        engine: RecorderEngine,
        preferred_folder: str,
    ) -> str:
        source_path = source_session_dir / relative_path
        if not source_path.exists() or engine.store.session_dir is None:
            return relative_path
        extension = source_path.suffix or ".png"
        target_path = engine.store.allocate_media_path(source_path.stem, extension, folder_name=preferred_folder)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        return safe_relpath(target_path, engine.store.session_dir)

    @staticmethod
    def _derive_event_id_prefix(event_id: str, event_type: str) -> str:
        event_id = str(event_id or "").strip()
        if "_" in event_id:
            prefix = event_id.split("_", 1)[0]
            if prefix:
                return prefix
        if event_type == "checkpoint":
            return "checkpoint"
        return "evt"

    @staticmethod
    def _detect_media_folder(relative_path: str, media_type: str) -> str:
        parts = Path(relative_path).parts
        if parts:
            if parts[0] == "screenshots":
                return "screenshots"
            if parts[0] == "media":
                return "media"
        if str(media_type).lower() == "video":
            return "media"
        return "screenshots"

    def _sync_checkpoint_collection_entry(self, original_event: dict[str, object], updated_event: dict[str, object]) -> None:
        if not self.session_data:
            return
        checkpoints = self.session_data.get("checkpoints")
        if not isinstance(checkpoints, list):
            checkpoints = []
            self.session_data["checkpoints"] = checkpoints
        for index, item in enumerate(checkpoints):
            if isinstance(item, dict) and self._event_identity_matches(item, original_event):
                checkpoints[index] = dict(updated_event)
                return
        checkpoints.append(dict(updated_event))

    def _sync_comment_collection_entry(self, original_event: dict[str, object], updated_event: dict[str, object]) -> None:
        if not self.session_data:
            return
        comments = self.session_data.get("comments")
        if not isinstance(comments, list):
            comments = []
            self.session_data["comments"] = comments
        for index, item in enumerate(comments):
            if isinstance(item, dict) and self._event_identity_matches(item, original_event):
                comments[index] = dict(updated_event)
                return
        comments.append(dict(updated_event))

    def _sync_auxiliary_collections_for_inserted_events(self, inserted_events: list[dict[str, object]]) -> None:
        for event in inserted_events:
            event_type = self._extract_event_type(event)
            if event_type == "checkpoint":
                self._sync_checkpoint_collection_entry({}, event)
            elif event_type == "comment":
                self._sync_comment_collection_entry({}, event)

    def _remove_checkpoint_collection_entries(self, removed_events: list[dict[str, object]]) -> None:
        if not self.session_data:
            return
        checkpoints = self.session_data.get("checkpoints")
        if not isinstance(checkpoints, list) or not checkpoints:
            return
        self.session_data["checkpoints"] = [
            item
            for item in checkpoints
            if not isinstance(item, dict) or not any(self._event_identity_matches(item, removed_event) for removed_event in removed_events)
        ]

    def _remove_comment_collection_entries(self, removed_events: list[dict[str, object]]) -> None:
        if not self.session_data:
            return
        comments = self.session_data.get("comments")
        if not isinstance(comments, list) or not comments:
            return
        self.session_data["comments"] = [
            item
            for item in comments
            if not isinstance(item, dict) or not any(self._event_identity_matches(item, removed_event) for removed_event in removed_events)
        ]

    def _event_identity_matches(self, left: dict[str, object], right: dict[str, object]) -> bool:
        left_event_id = str(left.get("event_id", "")).strip()
        right_event_id = str(right.get("event_id", "")).strip()
        if left_event_id and right_event_id:
            return left_event_id == right_event_id
        return (
            str(left.get("timestamp", "")) == str(right.get("timestamp", ""))
            and self._extract_event_type(left) == self._extract_event_type(right)
            and format_recorded_action(left.get("action", "")) == format_recorded_action(right.get("action", ""))
            and str(left.get("note", "")) == str(right.get("note", ""))
        )

    def _invalidate_derived_outputs(self) -> None:
        self.ai_analysis = None
        self.ai_step_tags = {}
        self.ai_step_texts = {}
        self.ai_process_summary_texts = {}
        self.suggestion_result = None
        self.step_method_suggestions = {}
        self.step_module_suggestions = {}
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        if self.session_dir:
            for file_name in ("ai_analysis.json", "conversion_suggestions.json"):
                target = self.session_dir / file_name
                if target.exists():
                    try:
                        target.unlink()
                    except Exception:
                        pass
            self.ai_var.set(self._build_initial_ai_status_text(self.session_dir))
            self.suggestion_var.set(self._build_initial_suggestion_status_text(self.session_dir))
        else:
            self.ai_var.set("未执行 AI 分析")
            self.suggestion_var.set("未生成调用建议")
        self.parameter_progress_var.set("参数推荐批处理未执行")
        self.parameter_status_var.set("请选择左侧步骤并先生成调用建议。")
        self._set_text_widget(self.parameter_result_text, "")
        self._refresh_coverage_summary()
        self._update_historical_ai_button_state()

    def _handle_select_all_shortcut(self, _event: tk.Event) -> str | None:
        focus_widget = self.window.focus_get()
        if isinstance(focus_widget, tk.Text):
            return None
        self.select_all_events()
        return "break"

    def _build_coverage_panel(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)

        summary_tab = ttk.Frame(notebook)
        coverage_tab = ttk.Frame(notebook)
        notebook.add(summary_tab, text="AI总结")
        notebook.add(coverage_tab, text="覆盖判断")

        summary_frame = ttk.LabelFrame(summary_tab, text="统一操作总结")
        summary_frame.pack(fill=tk.BOTH, expand=True)
        self.coverage_summary_text = tk.Text(summary_frame, height=8, wrap=tk.WORD, font=("Segoe UI", 10))
        self.coverage_summary_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=8, pady=8)
        summary_scroll = ttk.Scrollbar(summary_frame, orient=tk.VERTICAL, command=self.coverage_summary_text.yview)
        summary_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        self.coverage_summary_text.configure(yscrollcommand=summary_scroll.set, state=tk.DISABLED)

        review_frame = ttk.LabelFrame(summary_tab, text="可能无效 / 回退 / 被覆盖的步骤")
        review_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.coverage_review_text = tk.Text(review_frame, height=10, wrap=tk.WORD, font=("Segoe UI", 10))
        self.coverage_review_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=8, pady=8)
        review_scroll = ttk.Scrollbar(review_frame, orient=tk.VERTICAL, command=self.coverage_review_text.yview)
        review_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        self.coverage_review_text.configure(yscrollcommand=review_scroll.set, state=tk.DISABLED)

        input_frame = ttk.LabelFrame(coverage_tab, text="覆盖目标判断")
        input_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(input_frame, textvariable=self.coverage_status_var).pack(anchor=tk.W, padx=8, pady=(8, 4))
        ttk.Label(input_frame, text="请输入想验证是否已覆盖的需求或场景说明:").pack(anchor=tk.W, padx=8)
        self.coverage_input_text = tk.Text(input_frame, height=4, wrap=tk.WORD, font=("Segoe UI", 10))
        self.coverage_input_text.pack(fill=tk.X, padx=8, pady=(4, 8))

        button_bar = ttk.Frame(input_frame)
        button_bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.coverage_button = ttk.Button(button_bar, text="判断是否覆盖", command=self.run_coverage_check)
        self.coverage_button.pack(side=tk.RIGHT)

        self.coverage_result_text = tk.Text(input_frame, height=8, wrap=tk.WORD, font=("Consolas", 10))
        self.coverage_result_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.coverage_result_text.configure(state=tk.DISABLED)
        self._set_text_widget(self.coverage_summary_text, "请先执行 AI 总结。")
        self._set_text_widget(self.coverage_review_text, "请先执行 AI 总结。")
        self._set_text_widget(self.coverage_result_text, "")

    def _build_ai_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        workflow_frame = ttk.LabelFrame(parent, text="流程聚合 AI 总结")
        workflow_frame.pack(fill=tk.BOTH, expand=True)
        self.ai_summary_text = tk.Text(workflow_frame, height=18, wrap=tk.WORD, font=("Consolas", 10))
        self.ai_summary_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=8, pady=8)
        ai_summary_scroll = ttk.Scrollbar(workflow_frame, orient=tk.VERTICAL, command=self.ai_summary_text.yview)
        ai_summary_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        self.ai_summary_text.configure(yscrollcommand=ai_summary_scroll.set, state=tk.DISABLED)
        self._set_text_widget(self.ai_summary_text, "请先执行 AI 分析。")

        suggestion_frame = ttk.LabelFrame(parent, text="方法建议 / 参数推荐")
        suggestion_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        ttk.Label(suggestion_frame, textvariable=self.parameter_status_var).pack(anchor=tk.W, padx=8, pady=(8, 4))
        self.parameter_result_text = tk.Text(suggestion_frame, height=10, wrap=tk.WORD, font=("Consolas", 10))
        self.parameter_result_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.parameter_result_text.configure(state=tk.DISABLED)
        self._set_text_widget(self.parameter_result_text, "")

    def _build_ai_chat_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        chat_frame = ttk.LabelFrame(parent, text="AI 原始交互")
        chat_frame.grid(row=0, column=0, sticky="nsew")
        chat_frame.columnconfigure(0, weight=1)
        chat_frame.rowconfigure(0, weight=1)

        self.ai_chat_text = tk.Text(chat_frame, wrap=tk.WORD, font=("Consolas", 10))
        self.ai_chat_text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        chat_scroll = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self.ai_chat_text.yview)
        chat_scroll.grid(row=0, column=1, sticky="ns", pady=8)
        self.ai_chat_text.configure(yscrollcommand=chat_scroll.set, state=tk.DISABLED)
        self._set_text_widget(self.ai_chat_text, "请选择一个步骤以查看 AI 分析与参数推荐交互内容。")

    def _clear_parameter_chat_history(self) -> None:
        self.parameter_prompt_by_step = {}
        self.parameter_response_by_step = {}
        if hasattr(self, "ai_chat_text"):
            self._set_text_widget(self.ai_chat_text, "请选择一个步骤以查看 AI 分析与参数推荐交互内容。")

    def _find_step_analysis_batch(self, step_id: int) -> dict[str, object] | None:
        if not self.ai_analysis:
            return None
        batches = self.ai_analysis.get("batches", [])
        if not isinstance(batches, list):
            return None
        for item in reversed(batches):
            if not isinstance(item, dict):
                continue
            batch_id = str(item.get("batch_id", ""))
            if batch_id == "workflow_aggregation":
                continue
            start_step = int(item.get("start_step", 0) or 0)
            end_step = int(item.get("end_step", 0) or 0)
            if start_step <= step_id <= end_step:
                return item
        return None

    def _find_workflow_aggregation_batch(self) -> dict[str, object] | None:
        if not self.ai_analysis:
            return None
        for item in self.ai_analysis.get("batches", []):
            if isinstance(item, dict) and str(item.get("batch_id", "")) == "workflow_aggregation":
                return item
        return None

    def _format_ai_chat_text(self, row_index: int) -> str:
        if not self.ai_analysis:
            return "请先执行 AI 分析，之后这里会显示当前步骤相关的 AI prompt / response。"
        step_id = row_index + 1
        sections = [f"步骤 {step_id} AI Chat"]

        step_batch = self._find_step_analysis_batch(step_id)
        if step_batch:
            sections.extend(
                [
                    "",
                    f"[步骤分析 Batch] {step_batch.get('batch_id', '')}",
                    "",
                    "[Prompt Preview]",
                    str(step_batch.get("prompt_preview", "")) or "(无)",
                    "",
                    "[Response]",
                    str(step_batch.get("response_text", "")) or "(无)",
                ]
            )

        workflow_batch = self._find_workflow_aggregation_batch()
        if workflow_batch:
            sections.extend(
                [
                    "",
                    "[流程聚合 Prompt Preview]",
                    str(workflow_batch.get("prompt_preview", "")) or "(无)",
                    "",
                    "[流程聚合 Response]",
                    str(workflow_batch.get("response_text", "")) or "(无)",
                ]
            )

        if row_index in self.parameter_prompt_by_step or row_index in self.parameter_response_by_step:
            sections.extend(
                [
                    "",
                    "[参数推荐 Prompt]",
                    self.parameter_prompt_by_step.get(row_index, "(无)"),
                    "",
                    "[参数推荐 Response]",
                    self.parameter_response_by_step.get(row_index, "(无)"),
                ]
            )

        if len(sections) == 1:
            sections.append("")
            sections.append("当前步骤尚无可显示的 AI 原始交互内容。")
        return "\n".join(sections)

    def _refresh_ai_chat_panel(self) -> None:
        if not hasattr(self, "ai_chat_text"):
            return
        row_index = self._get_primary_selected_row_index()
        if row_index is None:
            self._set_text_widget(self.ai_chat_text, "请选择一个步骤以查看 AI 分析与参数推荐交互内容。")
            return
        self._set_text_widget(self.ai_chat_text, self._format_ai_chat_text(row_index))

    def _show_event_media(self, event: dict[str, object]) -> None:
        if not self.session_dir:
            return

        media_items: list[dict[str, object]] = []
        raw_media = event.get("media", [])
        if isinstance(raw_media, list):
            for item in raw_media:
                if isinstance(item, dict) and item.get("path"):
                    media_items.append(item)

        if not media_items and event.get("screenshot"):
            media_items.append({"type": "image", "path": event.get("screenshot"), "label": "主截图"})

        self._show_media_items(media_items, event)
        row_index = self._get_primary_selected_row_index()
        if row_index is not None:
            self._prefetch_neighbor_media(row_index)

    def _on_preview_mode_changed(self) -> None:
        row_index = self._get_primary_selected_row_index()
        if row_index is None or not (0 <= row_index < len(self.event_rows)):
            self._show_media_items([])
            return
        self._show_event_media(self.event_rows[row_index])

    def _navigate_event(self, offset: int) -> None:
        if not self.event_rows:
            return
        current_index = self._get_primary_selected_row_index() or 0
        next_index = max(0, min(len(self.event_rows) - 1, current_index + offset))
        row_id = str(next_index)
        if not self.tree.exists(row_id):
            return
        self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        self.tree.see(row_id)
        self.on_select_event(None)

    def _handle_navigation_key(self, event: tk.Event, offset: int) -> str | None:
        focus_widget = self.window.focus_get()
        if isinstance(focus_widget, tk.Text):
            return None
        if isinstance(focus_widget, ttk.Entry):
            return None
        self._navigate_event(offset)
        return "break"

    def _on_coverage_mousewheel(self, event: tk.Event) -> str:
        delta = 0
        if hasattr(event, "delta") and event.delta:
            delta = -1 if event.delta < 0 else 1
        elif getattr(event, "num", None) in {4, 5}:
            delta = -1 if event.num == 5 else 1
        if delta:
            self.coverage_canvas.yview_scroll(-delta, "units")
        return "break"

    def _show_media_items(self, media_items: list[dict[str, object]], event: dict[str, object] | None = None) -> None:
        self.current_media_token += 1
        token = self.current_media_token
        normalized_items: list[dict[str, object]] = []
        image_index = 0
        video_index = 0
        total_images = sum(1 for item in media_items if str(item.get("type", "image")) == "image" and item.get("path"))
        total_videos = sum(1 for item in media_items if str(item.get("type", "image")) == "video" and item.get("path"))
        for item in media_items:
            media_type = str(item.get("type", "image"))
            path = item.get("path")
            label = str(item.get("label", ""))
            if not path:
                continue
            if media_type == "image":
                image_index += 1
                default_label = f"截图 {image_index}/{max(total_images, 1)}"
                tab_text = f"{label} ({image_index}/{max(total_images, 1)})" if label and total_images > 1 else (label or default_label)
            elif media_type == "video":
                video_index += 1
                default_label = f"视频 {video_index}/{max(total_videos, 1)}"
                tab_text = f"{label} ({video_index}/{max(total_videos, 1)})" if label and total_videos > 1 else (label or default_label)
            else:
                tab_text = label or media_type
            normalized_items.append({"type": media_type, "path": str(path), "tab_text": tab_text})

        if not normalized_items:
            normalized_items = [{"type": "empty", "path": "", "tab_text": "无媒体"}]
            self.media_summary_var.set("当前事件无媒体")
        else:
            summary_parts: list[str] = []
            if total_images:
                summary_parts.append(f"{total_images} 张截图")
            if total_videos:
                summary_parts.append(f"{total_videos} 段视频")
            self.media_summary_var.set("当前事件媒体: " + " / ".join(summary_parts))

        self._ensure_media_tabs(len(normalized_items))
        self.media_views = []

        active_tabs = set(self.media_notebook.tabs())
        for index, item in enumerate(normalized_items):
            tab = self.media_tab_pool[index]
            frame = tab["frame"]
            view = tab["view"]
            path_var = tab["path_var"]
            if str(frame) not in active_tabs:
                self.media_notebook.add(frame, text=str(item["tab_text"]))
            else:
                self.media_notebook.tab(frame, text=str(item["tab_text"]))
            self.media_views.append(view)

            if item["type"] == "empty":
                path_var.set("")
                view.clear("该事件没有媒体")
                continue

            media_path = self.session_dir / str(item["path"])
            path_var.set(str(item["path"]))
            if not media_path.exists():
                view.clear(f"媒体不存在: {media_path.name}")
                continue
            self._display_media_in_view(view, media_path, str(item["type"]), token, event)

        for index in range(len(normalized_items), len(self.media_tab_pool)):
            frame = self.media_tab_pool[index]["frame"]
            if str(frame) in self.media_notebook.tabs():
                self.media_notebook.forget(frame)

        self.media_notebook.select(self.media_tab_pool[0]["frame"])

    def _ensure_media_tabs(self, count: int) -> None:
        while len(self.media_tab_pool) < count:
            frame = ttk.Frame(self.media_notebook)
            header = ttk.Frame(frame)
            header.pack(fill=tk.X, padx=8, pady=(8, 0))
            path_var = tk.StringVar(value="")
            ttk.Label(header, textvariable=path_var).pack(anchor=tk.W)
            view = ZoomableImageView(frame, empty_text="无法加载媒体")
            view.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
            self.media_tab_pool.append({"frame": frame, "view": view, "path_var": path_var})

    def _display_media_in_view(
        self,
        view: ZoomableImageView,
        media_path: Path,
        media_type: str,
        token: int,
        event: dict[str, object] | None = None,
    ) -> None:
        prepared_path = media_path
        if media_type == "image" and self.preview_single_monitor_var.get():
            prepared_path = self._prepare_preview_image_path(media_path, event)

        cache_key = f"{media_type}:{prepared_path.resolve()}:{'single' if self.preview_single_monitor_var.get() else 'full'}"
        if cache_key in self.media_cache:
            cached = self.media_cache[cache_key]
            if cached is None:
                view.clear(f"无法加载媒体: {prepared_path.name}")
            else:
                view.set_image(cached)
            return

        if view.original_image is None:
            view.set_status(f"正在加载: {prepared_path.name}")

        def worker() -> None:
            loaded: Image.Image | None = None
            try:
                if media_type == "video":
                    loaded = load_video_preview_frame(prepared_path)
                else:
                    with Image.open(prepared_path) as image:
                        loaded = image.copy()
            except Exception:
                loaded = None
            self.media_cache[cache_key] = loaded

            def apply() -> None:
                if token != self.current_media_token or not view.winfo_exists():
                    return
                if loaded is None:
                    view.clear(f"无法加载媒体: {prepared_path.name}")
                    return
                view.set_image(loaded)

            self.window.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _prepare_preview_image_path(self, media_path: Path, event: dict[str, object] | None) -> Path:
        if not self.session_dir or not isinstance(event, dict):
            return media_path
        display_layout: dict[str, object] | None = None
        if isinstance(self.session_data, dict):
            environment = self.session_data.get("environment", {})
            if isinstance(environment, dict):
                raw_layout = environment.get("display_layout")
                if isinstance(raw_layout, dict):
                    display_layout = raw_layout
        prepared_path, _ = prepare_image_path_for_ai(
            media_path,
            event,
            display_layout,
            self.session_dir / "viewer_preprocessed" / "monitors",
            send_fullscreen=False,
            cache_key=f"preview_{media_path.stem}",
        )
        return prepared_path

    def _prefetch_neighbor_media(self, index: int) -> None:
        if not self.session_dir:
            return
        neighbor_indexes = [item for item in (index - 1, index + 1) if 0 <= item < len(self.event_rows)]
        for neighbor_index in neighbor_indexes:
            event = self.event_rows[neighbor_index]
            raw_media = event.get("media", [])
            media_items: list[dict[str, object]] = []
            if isinstance(raw_media, list):
                media_items.extend(item for item in raw_media if isinstance(item, dict) and item.get("path"))
            if not media_items and event.get("screenshot"):
                media_items.append({"type": "image", "path": event.get("screenshot")})
            for item in media_items:
                media_type = str(item.get("type", "image"))
                media_path = self.session_dir / str(item.get("path"))
                if not media_path.exists():
                    continue
                prepared_path = media_path
                if media_type == "image" and self.preview_single_monitor_var.get():
                    prepared_path = self._prepare_preview_image_path(media_path, event)
                cache_key = f"{media_type}:{prepared_path.resolve()}:{'single' if self.preview_single_monitor_var.get() else 'full'}"
                if cache_key in self.media_cache:
                    continue

                def worker(path: Path = prepared_path, kind: str = media_type, key: str = cache_key) -> None:
                    loaded: Image.Image | None = None
                    try:
                        if kind == "video":
                            loaded = load_video_preview_frame(path)
                        else:
                            with Image.open(path) as image:
                                loaded = image.copy()
                    except Exception:
                        loaded = None
                    self.media_cache[key] = loaded

                threading.Thread(target=worker, daemon=True).start()

    def _persist_session(self) -> None:
        if not self.session_dir or not self.session_data:
            return
        self.session_data["events"] = self.event_rows
        session_path = self.session_dir / "session.json"
        yaml_path = self.session_dir / "session.yaml"
        events_log_path = self.session_dir / "events.jsonl"
        session_path.write_text(json.dumps(self.session_data, indent=2, ensure_ascii=False), encoding="utf-8")
        yaml_path.write_text(yaml.safe_dump(self.session_data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        event_lines = [json.dumps(event, ensure_ascii=False) for event in self.event_rows if isinstance(event, dict)]
        events_log_path.write_text("\n".join(event_lines) + ("\n" if event_lines else ""), encoding="utf-8")

    def apply_ai_deletions(self) -> None:
        if not self.ai_analysis:
            messagebox.showinfo("提示", "请先执行 AI 分析。", parent=self.window)
            return

        deletions: list[int] = []
        lines: list[str] = []
        for item in self.ai_analysis.get("invalid_steps", []):
            if not isinstance(item, dict) or str(item.get("decision", "")) != "delete":
                continue
            step_ids = item.get("step_ids", [])
            if not isinstance(step_ids, list):
                continue
            valid_steps = [step_id for step_id in step_ids if isinstance(step_id, int) and step_id >= 1]
            if not valid_steps:
                continue
            deletions.extend(step_id - 1 for step_id in valid_steps)
            lines.append(f"步骤 {', '.join(str(step) for step in valid_steps)}: {item.get('reason', '')}")

        if not deletions:
            messagebox.showinfo("提示", "当前没有 AI 判定为 delete 的步骤。", parent=self.window)
            return

        confirmed = messagebox.askyesno(
            "应用 AI 删除建议",
            "将删除以下 AI 判定为 delete 的步骤:\n\n" + "\n".join(lines[:12]) + ("\n..." if len(lines) > 12 else ""),
            parent=self.window,
        )
        if not confirmed:
            return

        delete_set = set(deletions)
        self.event_rows = [event for index, event in enumerate(self.event_rows) if index not in delete_set]
        if self.session_data is not None:
            self.session_data["events"] = self.event_rows
        self.ai_analysis = None
        self.ai_step_tags = {}
        self.ai_step_texts = {}
        self.suggestion_result = None
        self.step_method_suggestions = {}
        self.step_module_suggestions = {}
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        self.ai_var.set("AI 分析结果已过期，请重新执行 AI 分析")
        self.suggestion_var.set("调用建议结果已过期，请点击“生成方法建议”，或重新应用清洗自动生成")
        self.parameter_progress_var.set("参数推荐批处理结果已过期，请重新生成")
        self.parameter_status_var.set("参数推荐结果已过期，请先重新生成方法建议；如需参数推荐也要重新执行 AI 分析")
        self._set_text_widget(self.parameter_result_text, "")
        suggestion_path = self.session_dir / "conversion_suggestions.json" if self.session_dir else None
        if suggestion_path and suggestion_path.exists():
            try:
                suggestion_path.unlink()
            except Exception:
                pass
        self._refresh_coverage_summary()
        self._persist_session()
        self._reload_tree()
        messagebox.showinfo("完成", f"已删除 {len(delete_set)} 个 AI 建议删除的步骤。", parent=self.window)

    def _build_row_tags(self, row_index: int, include_cleaning: bool = True) -> tuple[str, ...]:
        tags: list[str] = []
        ai_tag = self.ai_step_tags.get(row_index)
        if ai_tag:
            tags.append(ai_tag)
        if include_cleaning:
            for suggestion in self.cleaning_suggestions:
                if row_index not in suggestion.row_indexes:
                    continue
                if suggestion.kind in {"drop_noop", "drop_noop_scroll"}:
                    tags.append("clean-delete")
                elif suggestion.kind == "merge_keypress":
                    tags.append("clean-merge")
                else:
                    tags.append("clean-review")
                break
        return tuple(tags)

    def _load_ai_analysis(self, session_dir: Path) -> dict[str, object] | None:
        analysis_path = session_dir / "ai_analysis.json"
        if not analysis_path.exists():
            return None
        try:
            payload = json.loads(analysis_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _build_ai_step_tags(self, analysis: dict[str, object] | None) -> dict[int, str]:
        tags: dict[int, str] = {}
        if not analysis:
            return tags
        for item in analysis.get("invalid_steps", []):
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision", "review"))
            step_ids = item.get("step_ids", [])
            if not isinstance(step_ids, list):
                continue
            tag = "ai-delete" if decision == "delete" else "ai-review"
            for step_id in step_ids:
                if isinstance(step_id, int) and step_id >= 1:
                    tags[step_id - 1] = tag
        return tags

    def _build_ai_step_texts(self, analysis: dict[str, object] | None) -> dict[int, str]:
        texts: dict[int, str] = {}
        if not analysis:
            return texts

        for item in self._extract_analysis_step_observations(analysis):
            if not isinstance(item, dict):
                continue
            step_id = item.get("step_id")
            if not isinstance(step_id, int) or step_id < 1:
                continue
            observation = self._clean_sentence(str(item.get("observation", "")))
            if observation:
                texts[step_id - 1] = observation

        step_insights = analysis.get("step_insights", [])
        if isinstance(step_insights, list):
            for item in step_insights:
                if not isinstance(item, dict):
                    continue
                step_id = item.get("step_id")
                if not isinstance(step_id, int) or step_id < 1:
                    continue
                if step_id - 1 in texts:
                    continue
                description = self._clean_sentence(str(item.get("description", "")))
                conclusion = self._clean_sentence(str(item.get("conclusion", "")))
                display_text = description
                if description and conclusion:
                    display_text = f"{description}；{conclusion}"
                elif not display_text:
                    display_text = conclusion
                if display_text:
                    texts[step_id - 1] = display_text
        return texts

    def _build_ai_process_summary_texts(self, analysis: dict[str, object] | None) -> dict[int, str]:
        texts: dict[int, str] = {}
        if not analysis:
            return texts
        values = analysis.get("process_summaries", [])
        if not isinstance(values, list):
            return texts
        for item in values:
            if not isinstance(item, dict):
                continue
            step_id = item.get("last_step_id", item.get("end_step"))
            if not isinstance(step_id, int) or step_id < 1:
                continue
            summary = self._clean_sentence(str(item.get("summary", "")))
            if summary:
                texts[step_id - 1] = summary
        return texts

    def _extract_analysis_step_observations(self, analysis: dict[str, object]) -> list[dict[str, object]]:
        explicit_values = analysis.get("step_observations", [])
        if isinstance(explicit_values, list) and explicit_values:
            return [item for item in explicit_values if isinstance(item, dict)]

        extracted: list[dict[str, object]] = []
        for batch in analysis.get("batches", []):
            if not isinstance(batch, dict):
                continue
            parsed_result = batch.get("parsed_result", {}) if isinstance(batch.get("parsed_result", {}), dict) else {}
            observation_round = parsed_result.get("observation_round", {}) if isinstance(parsed_result.get("observation_round", {}), dict) else {}
            values = observation_round.get("step_observations", [])
            event_indexes = batch.get("event_indexes", []) if isinstance(batch.get("event_indexes", []), list) else []
            if not isinstance(values, list):
                continue
            for offset, item in enumerate(values):
                if not isinstance(item, dict):
                    continue
                step_id = item.get("step_id")
                if not isinstance(step_id, int) and offset < len(event_indexes) and isinstance(event_indexes[offset], int):
                    step_id = event_indexes[offset]
                if not isinstance(step_id, int) or step_id < 1:
                    continue
                observation = str(item.get("observation", item.get("description", ""))).strip()
                if not observation:
                    control_type = str(item.get("control_type", "")).strip()
                    label = str(item.get("label", "")).strip()
                    label_kind = str(item.get("label_kind", "label")).strip().lower() or "label"
                    relative_position = str(item.get("relative_position", "")).strip()
                    need_scroll = item.get("need_scroll")
                    is_table = item.get("is_table")
                    parts: list[str] = []
                    if label:
                        key = "helptext" if label_kind == "helptext" else "label"
                        parts.append(f"{key}={label}")
                    if relative_position:
                        parts.append(f"direction={relative_position}")
                    if control_type:
                        parts.append(f"control_type={control_type}")
                    if isinstance(need_scroll, bool):
                        parts.append(f"scroll={str(need_scroll).lower()}")
                    if isinstance(is_table, bool):
                        parts.append(f"table={str(is_table).lower()}")
                    observation = " | ".join(parts)
                if not observation:
                    continue
                extracted.append({"step_id": step_id, "observation": observation})

        extracted.sort(key=lambda item: int(item.get("step_id", 0) or 0))
        return extracted

    def _build_ai_summary_text(self, analysis: dict[str, object] | None) -> str:
        if not analysis:
            return "未执行 AI 分析"
        status = str(analysis.get("status", "completed"))
        step_count = len(analysis.get("step_insights", [])) if isinstance(analysis.get("step_insights", []), list) else 0
        invalid_count = len(analysis.get("invalid_steps", [])) if isinstance(analysis.get("invalid_steps", []), list) else 0
        module_count = len(analysis.get("reusable_modules", [])) if isinstance(analysis.get("reusable_modules", []), list) else 0
        wait_count = len(analysis.get("wait_suggestions", [])) if isinstance(analysis.get("wait_suggestions", []), list) else 0
        batch_count = len(analysis.get("batches", [])) if isinstance(analysis.get("batches", []), list) else 0
        if status == "partial_failed":
            return f"AI 分析部分完成: 已生成步骤建议 {step_count} 条 | 批次 {batch_count} | 无效步骤候选 {invalid_count} | 模块建议 {module_count} | 等待建议 {wait_count}"
        return f"AI 分析结果: 批次 {batch_count} | 无效步骤候选 {invalid_count} | 模块建议 {module_count} | 等待建议 {wait_count}"

    def _build_initial_ai_status_text(self, session_dir: Path) -> str:
        analysis_path = session_dir / "ai_analysis.json"
        if analysis_path.exists():
            return "检测到历史 AI 分析结果，但当前未加载。可点击“加载历史AI结果”，或点击“AI分析”重新生成。"
        return "未执行 AI 分析"

    def _build_initial_suggestion_status_text(self, session_dir: Path) -> str:
        suggestion_path = session_dir / "conversion_suggestions.json"
        if suggestion_path.exists():
            return "检测到历史调用建议结果，可直接使用，也可点击“生成方法建议”重新生成。"
        return "未生成调用建议"

    def _on_ai_analysis_success(self, analysis: dict[str, object]) -> None:
        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.ai_process_summary_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "AI 分析完成"
        self.analysis_status_token += 1
        existing_analysis = copy.deepcopy(self.ai_analysis) if isinstance(self.ai_analysis, dict) else self._load_ai_analysis(self.session_dir) or {}
        analysis = self._merge_analysis_extras(existing_analysis, analysis)
        self.ai_analysis = analysis
        self.ai_step_tags = self._build_ai_step_tags(analysis)
        self.ai_step_texts = self._build_ai_step_texts(analysis)
        self.ai_process_summary_texts = self._build_ai_process_summary_texts(analysis)
        self._persist_ai_analysis(analysis)
        self.ai_var.set(self._build_ai_summary_text(analysis))
        self._refresh_coverage_summary()
        self._refresh_selected_suggestion_panel()
        self._reload_tree()
        messagebox.showinfo("AI 分析完成", "已输出 ai_analysis.json 和 ai_analysis.yaml，并更新 viewer 高亮。", parent=self.window)

    def _on_ai_analysis_failed(self, message: str) -> None:
        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.ai_process_summary_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "AI 分析失败"
        self.analysis_status_token += 1
        partial_analysis = self._load_ai_analysis(self.session_dir) if self.session_dir else None
        has_partial = self._has_partial_ai_analysis(partial_analysis)
        if message == "AI 分析已取消。":
            self.analysis_status_base = "AI 分析已取消"
            if has_partial and partial_analysis is not None:
                self.ai_analysis = partial_analysis
                self.ai_step_tags = self._build_ai_step_tags(partial_analysis)
                self.ai_step_texts = self._build_ai_step_texts(partial_analysis)
                self.ai_process_summary_texts = self._build_ai_process_summary_texts(partial_analysis)
                self.ai_var.set(f"AI 分析已取消，但已加载部分结果 | {self._build_ai_summary_text(partial_analysis)}")
                self.suggestion_var.set("AI 分析已取消，已保留现有调用建议与已完成的 AI 看图结果")
                self._refresh_coverage_summary()
                self._refresh_selected_suggestion_panel()
                self._reload_tree()
                return
            self.ai_var.set("AI 分析已取消")
            self._refresh_selected_suggestion_panel()
            return
        if has_partial and partial_analysis is not None:
            self.ai_analysis = partial_analysis
            self.ai_step_tags = self._build_ai_step_tags(partial_analysis)
            self.ai_step_texts = self._build_ai_step_texts(partial_analysis)
            self.ai_process_summary_texts = self._build_ai_process_summary_texts(partial_analysis)
            self.ai_var.set(f"AI 分析部分成功: 已加载成功步骤 | {self._build_ai_summary_text(partial_analysis)}")
            self.suggestion_var.set("AI 分析部分成功，已保留现有调用建议")
            self._refresh_coverage_summary()
            self._refresh_selected_suggestion_panel()
            self._reload_tree()
        elif not has_partial:
            self.ai_var.set(f"AI 分析失败: {message}")
            self._refresh_selected_suggestion_panel()
        parse_error_path = self._get_last_parse_error_response_path()
        if parse_error_path:
            prompt = f"{message}\n\n是否打开原始返回文件？\n{parse_error_path}"
            if has_partial:
                prompt = f"{message}\n\n已将成功解析的 AI 建议写入事件列表。\n\n是否打开原始返回文件？\n{parse_error_path}"
            should_open = messagebox.askyesno(
                "AI 分析失败",
                prompt,
                parent=self.window,
            )
            if should_open:
                self._open_path(parse_error_path)
            return
        if has_partial:
            messagebox.showwarning("AI 分析部分成功", f"{message}\n\n已将成功解析的 AI 建议写入事件列表。", parent=self.window)
            return
        messagebox.showerror("AI 分析失败", message, parent=self.window)

    def _has_partial_ai_analysis(self, analysis: dict[str, object] | None) -> bool:
        if not isinstance(analysis, dict):
            return False
        for field_name in ("step_insights", "step_observations", "batches"):
            values = analysis.get(field_name, [])
            if isinstance(values, list) and values:
                return True
        return False

    def _get_last_parse_error_response_path(self) -> Path | None:
        if not self.session_dir:
            return None
        response_path = self.session_dir / "ai_parse_error_last_response.txt"
        return response_path if response_path.exists() else None

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法打开文件:\n{path}\n\n{exc}", parent=self.window)

    def _on_ai_analysis_progress(self, stage: str, payload: dict[str, object]) -> None:
        self.analysis_status_base = self._format_analysis_progress(stage, payload)
        self._refresh_analysis_status(self.analysis_status_token)

    def _format_analysis_progress(self, stage: str, payload: dict[str, object]) -> str:
        current_batch = payload.get("current_batch")
        total_batches = payload.get("total_batches")
        batch_prefix = ""
        if isinstance(current_batch, int) and isinstance(total_batches, int) and total_batches > 0:
            batch_prefix = f"第 {current_batch}/{total_batches} 批 | "
        analysis_phase = str(payload.get("analysis_phase", "")).strip()
        segment_index = payload.get("segment_index")
        total_segments = payload.get("total_segments")
        window_index = payload.get("window_index")
        total_windows = payload.get("total_windows")
        group_prefix = ""
        if analysis_phase == "group_summary_window":
            segment_text = ""
            if isinstance(segment_index, int) and isinstance(total_segments, int) and total_segments > 0:
                segment_text = f"区间 {segment_index}/{total_segments} | "
            window_text = ""
            if isinstance(window_index, int) and isinstance(total_windows, int) and total_windows > 0:
                window_text = f"窗口 {window_index}/{total_windows} | "
            elif batch_prefix:
                window_text = batch_prefix
            group_prefix = f"{segment_text}{window_text}"
        elif analysis_phase == "group_summary_merge":
            if isinstance(segment_index, int) and isinstance(total_segments, int) and total_segments > 0:
                group_prefix = f"区间 {segment_index}/{total_segments} | 汇总 | "
            else:
                group_prefix = "汇总 | "
        prefix = group_prefix or batch_prefix

        if stage == "start":
            event_count = payload.get("event_count", 0)
            batch_size = payload.get("batch_size", 0)
            total_batches = payload.get("total_batches", 0)
            return f"AI 分析启动: 共 {event_count} 步，batch_size={batch_size}，预计 {total_batches} 批"
        if stage == "process_summary_segment_start":
            start_step = payload.get("start_step", "?")
            end_step = payload.get("end_step", "?")
            process_name = payload.get("process_name", "")
            return f"{batch_prefix}AI总结: 正在总结进程 {process_name} 的连续步骤 {start_step}-{end_step}"
        if stage == "process_summary_segment_done":
            start_step = payload.get("start_step", "?")
            end_step = payload.get("end_step", "?")
            process_name = payload.get("process_name", "")
            return f"{batch_prefix}AI总结完成: 进程 {process_name} 的步骤 {start_step}-{end_step} 已生成总结"
        if stage == "group_summary_segment_start":
            start_step = payload.get("start_step", "?")
            end_step = payload.get("end_step", "?")
            total_windows = payload.get("total_windows", 0)
            segment_text = ""
            if isinstance(segment_index, int) and isinstance(total_segments, int) and total_segments > 0:
                segment_text = f"区间 {segment_index}/{total_segments} | "
            return f"{segment_text}连续步骤总结: 步骤 {start_step}-{end_step}，按每批最多 5 张、重叠 1 张发送，共 {total_windows} 个窗口"
        if stage == "batch_preprocess_start":
            start_step = payload.get("start_step", "?")
            end_step = payload.get("end_step", "?")
            return f"{prefix}预处理中: 步骤 {start_step}-{end_step}，正在整理事件与定位发送图片"
        if stage == "batch_preprocess_done":
            start_step = payload.get("start_step", "?")
            end_step = payload.get("end_step", "?")
            image_count = payload.get("image_count", 0)
            cropped_monitor_count = payload.get("cropped_monitor_count", 0)
            return f"{prefix}预处理完成: 步骤 {start_step}-{end_step}，发送图片 {image_count} 张，其中单屏裁切 {cropped_monitor_count} 张"
        if stage == "prepare_media":
            image_count = payload.get("image_count", 0)
            inline_image_count = payload.get("inline_image_count", 0)
            has_video = payload.get("has_video", False)
            if analysis_phase == "group_summary_window":
                start_step = payload.get("window_start_step", payload.get("start_step", "?"))
                end_step = payload.get("window_end_step", payload.get("end_step", "?"))
                return f"{prefix}准备请求媒体: 当前窗口步骤 {start_step}-{end_step}，图片 {image_count} 张，临时图片 {inline_image_count} 张，视频 {'有' if has_video else '无'}"
            return f"{prefix}准备请求媒体: 文件图片 {image_count} 张，临时图片 {inline_image_count} 张，视频 {'有' if has_video else '无'}"
        if stage == "send_request":
            timeout_seconds = payload.get("timeout_seconds", 0)
            if analysis_phase == "group_summary_window":
                start_step = payload.get("window_start_step", payload.get("start_step", "?"))
                end_step = payload.get("window_end_step", payload.get("end_step", "?"))
                return f"{prefix}已发送当前窗口到模型: 步骤 {start_step}-{end_step}，等待响应中，单批超时 {timeout_seconds} 秒"
            if analysis_phase == "group_summary_merge":
                start_step = payload.get("start_step", "?")
                end_step = payload.get("end_step", "?")
                return f"{prefix}已发送区间汇总请求: 步骤 {start_step}-{end_step}，等待响应中，单批超时 {timeout_seconds} 秒"
            return f"{prefix}已发送到模型，等待响应中，单批超时 {timeout_seconds} 秒"
        if stage == "response_received":
            status_code = payload.get("status_code", "?")
            return f"{prefix}模型已返回 HTTP {status_code}，正在读取响应"
        if stage == "parse_response":
            return f"{prefix}正在解析模型返回 JSON"
        if stage == "batch_parse":
            return f"{prefix}正在解析当前步骤分析结果"
        if stage == "batch_done":
            step_insight_count = payload.get("step_insight_count", 0)
            return f"{prefix}当前步骤分析完成，累计生成步骤总结 {step_insight_count} 条"
        if stage == "workflow_aggregate_start":
            step_count = payload.get("step_count", 0)
            return f"正在基于 {step_count} 条步骤总结做二次流程聚合分析"
        if stage == "workflow_aggregate_parse":
            return "正在解析流程聚合分析结果"
        if stage == "workflow_aggregate_done":
            invalid_count = payload.get("invalid_count", 0)
            module_count = payload.get("module_count", 0)
            wait_count = payload.get("wait_count", 0)
            return f"流程聚合完成: 无效步骤 {invalid_count}，模块 {module_count}，等待建议 {wait_count}"
        if stage == "write_result":
            return "正在写出 ai_analysis.json / ai_analysis.yaml / ai_batch_memory.json"
        if stage == "done":
            invalid_count = payload.get("invalid_count", 0)
            module_count = payload.get("module_count", 0)
            wait_count = payload.get("wait_count", 0)
            return f"AI 分析完成: 无效步骤 {invalid_count}，模块建议 {module_count}，等待建议 {wait_count}"
        return "AI 分析中..."

    def _refresh_analysis_status(self, token: int) -> None:
        if token != self.analysis_status_token:
            return
        if self.analysis_running and self.analysis_started_at:
            elapsed = max(0, int(time.time() - self.analysis_started_at))
            self.ai_var.set(f"{self.analysis_status_base} | 已耗时 {elapsed} 秒")
            self.window.after(1000, lambda: self._refresh_analysis_status(token))
            return
        self.ai_var.set(self.analysis_status_base)

    def _extract_comment(self, event: dict[str, object]) -> str:
        event_type = self._extract_event_type(event)
        if event_type in {"comment", "wait"}:
            return str(event.get("note", ""))
        details = event.get("additional_details", {})
        if isinstance(details, dict):
            return str(details.get("viewer_comment", ""))
        return ""

    def _update_event_comment(self, index: int, comment: str) -> None:
        event = self.event_rows[index]
        if self._extract_event_type(event) == "comment":
            event["note"] = comment
            return
        details = dict(event.get("additional_details", {}))
        details["viewer_comment"] = comment
        event["additional_details"] = details

    def _format_timestamp(self, timestamp: object) -> str:
        if not isinstance(timestamp, str) or not timestamp:
            return ""
        try:
            formatted = datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M:%S")
            return formatted
        except ValueError:
            return timestamp[:19].replace("T", " ")

    def _describe_event_for_view(self, row_index: int, event: dict[str, object]) -> str:
        return self.ai_step_texts.get(row_index, "")

    def _describe_process_summary_for_view(self, row_index: int) -> str:
        return self.ai_process_summary_texts.get(row_index, "")

    def _refresh_coverage_summary(self) -> None:
        summary = self._build_workflow_summary_text()
        self._set_text_widget(self.coverage_summary_text, summary)
        if hasattr(self, "coverage_review_text"):
            self._set_text_widget(self.coverage_review_text, self._build_process_summary_review_text())
        if self.ai_analysis:
            self.coverage_status_var.set("可输入目标，让 AI 判断当前录制是否已覆盖。")
        else:
            self.coverage_status_var.set("请先执行 AI 分析，再进行覆盖判断")
        self._set_text_widget(self.coverage_result_text, "")

    def _build_process_summary_review_text(self) -> str:
        if not self.ai_analysis:
            return "请先执行 AI 总结。"
        overview = self.ai_analysis.get("process_summary_overview", {}) if isinstance(self.ai_analysis.get("process_summary_overview", {}), dict) else {}
        candidates = overview.get("rollback_candidates", []) if isinstance(overview.get("rollback_candidates", []), list) else []
        notes = overview.get("notes", []) if isinstance(overview.get("notes", []), list) else []
        lines: list[str] = []
        if candidates:
            lines.append("以下步骤可能属于无效尝试、回退路径，或后续被正确操作覆盖:")
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                step_ids = item.get("step_ids", []) if isinstance(item.get("step_ids", []), list) else []
                reason = self._clean_sentence(str(item.get("reason", "")))
                label = self._format_step_id_list(step_ids)
                if label and reason:
                    lines.append(f"- 步骤 {label}: {reason}")
                elif label:
                    lines.append(f"- 步骤 {label}")
                elif reason:
                    lines.append(f"- {reason}")
        if notes:
            if lines:
                lines.append("")
            lines.append("补充判断:")
            for note in notes:
                cleaned = self._clean_sentence(str(note))
                if cleaned:
                    lines.append(f"- {cleaned}")
        return "\n".join(lines) if lines else "当前没有识别到明显的无效操作或回退步骤。"

    def _format_step_id_list(self, step_ids: list[int]) -> str:
        normalized = sorted({int(step_id) for step_id in step_ids if isinstance(step_id, int) and step_id > 0})
        if not normalized:
            return ""
        ranges: list[str] = []
        start = normalized[0]
        end = normalized[0]
        for step_id in normalized[1:]:
            if step_id == end + 1:
                end = step_id
                continue
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = step_id
        ranges.append(f"{start}-{end}" if start != end else str(start))
        return ", ".join(ranges)

    def _extract_coverage_text(self, value: object) -> str:
        if not isinstance(value, str):
            return ""
        return self._clean_sentence(value)

    def _parse_coverage_response(self, response_text: str) -> dict[str, object]:
        cleaned = response_text.strip()
        result = {
            "conclusion": "",
            "covered": [],
            "partial": [],
            "gaps": [],
            "suggestions": [],
            "reason": "",
            "evidence": "",
            "raw": cleaned,
        }
        if not cleaned:
            return result

        normalized = cleaned
        if normalized.startswith("```"):
            lines = normalized.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            normalized = "\n".join(lines).strip()

        try:
            payload = json.loads(normalized)
        except Exception:
            payload = None

        if isinstance(payload, dict):
            result["conclusion"] = self._extract_coverage_text(payload.get("conclusion"))
            result["reason"] = self._extract_coverage_text(payload.get("reason"))
            result["evidence"] = self._extract_coverage_text(payload.get("evidence"))
            for key, target in [
                ("covered", "covered"),
                ("covered_points", "covered"),
                ("partial", "partial"),
                ("partial_points", "partial"),
                ("gaps", "gaps"),
                ("missing", "gaps"),
                ("suggestions", "suggestions"),
                ("recommended_steps", "suggestions"),
            ]:
                value = payload.get(key)
                if isinstance(value, list):
                    result[target].extend(self._extract_coverage_text(item) for item in value)
                elif isinstance(value, str):
                    result[target].append(self._extract_coverage_text(value))
            for key in ("covered", "partial", "gaps", "suggestions"):
                result[key] = self._deduplicate_texts([item for item in result[key] if isinstance(item, str)])
            return result

        section_map = {
            "结论": "conclusion",
            "原因": "reason",
            "依据": "evidence",
            "已覆盖": "covered",
            "覆盖点": "covered",
            "部分覆盖": "partial",
            "缺口": "gaps",
            "未覆盖": "gaps",
            "建议": "suggestions",
            "补充建议": "suggestions",
        }
        current_section = ""
        for raw_line in cleaned.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            normalized_line = line.lstrip("-•*0123456789. )（").strip()
            matched = False
            for label, target in section_map.items():
                prefix = f"{label}:"
                cn_prefix = f"{label}："
                if normalized_line.startswith(prefix) or normalized_line.startswith(cn_prefix):
                    value = normalized_line.split(":", 1)[1] if ":" in normalized_line else normalized_line.split("：", 1)[1]
                    text = self._extract_coverage_text(value)
                    current_section = target
                    if target in {"covered", "partial", "gaps", "suggestions"}:
                        if text:
                            result[target].append(text)
                    else:
                        result[target] = text
                    matched = True
                    break
            if matched:
                continue
            text = self._extract_coverage_text(normalized_line)
            if not text:
                continue
            if current_section in {"covered", "partial", "gaps", "suggestions"}:
                result[current_section].append(text)
            elif current_section in {"reason", "evidence"}:
                existing = str(result[current_section]).strip()
                result[current_section] = f"{existing}；{text}" if existing else text

        for key in ("covered", "partial", "gaps", "suggestions"):
            result[key] = self._deduplicate_texts([item for item in result[key] if isinstance(item, str)])
        return result

    def _format_coverage_result(self, payload: dict[str, object]) -> str:
        conclusion = self._extract_coverage_text(payload.get("conclusion")) or "未明确"
        reason = self._extract_coverage_text(payload.get("reason"))
        evidence = self._extract_coverage_text(payload.get("evidence"))
        covered = payload.get("covered", []) if isinstance(payload.get("covered", []), list) else []
        partial = payload.get("partial", []) if isinstance(payload.get("partial", []), list) else []
        gaps = payload.get("gaps", []) if isinstance(payload.get("gaps", []), list) else []
        suggestions = payload.get("suggestions", []) if isinstance(payload.get("suggestions", []), list) else []

        lines = [f"结论: {conclusion}"]
        if reason:
            lines.append(f"原因: {reason}")
        if evidence:
            lines.append(f"依据: {evidence}")

        sections = [
            ("已覆盖", covered, "- 暂无明确已覆盖点"),
            ("部分覆盖", partial, "- 暂无部分覆盖点"),
            ("缺口", gaps, "- 暂无明确缺口"),
            ("建议补录步骤", suggestions, "- 暂无补录建议"),
        ]
        for title, items, empty_text in sections:
            lines.append("")
            lines.append(f"{title}:")
            if items:
                for item in items:
                    text = self._extract_coverage_text(item)
                    if text:
                        lines.append(f"- {text}")
            else:
                lines.append(empty_text)

        raw = str(payload.get("raw", "")).strip()
        if raw and not any([covered, partial, gaps, suggestions, reason, evidence]):
            lines.append("")
            lines.append("原始返回:")
            lines.append(raw)
        return "\n".join(lines)

    def _build_workflow_summary_text(self) -> str:
        if not self.ai_analysis:
            return "请先执行 AI 分析。"

        overview = self.ai_analysis.get("process_summary_overview", {}) if isinstance(self.ai_analysis.get("process_summary_overview", {}), dict) else {}
        overview_summary = self._clean_sentence(str(overview.get("summary", "")))
        if overview_summary:
            return overview_summary

        workflow_report_markdown = str(self.ai_analysis.get("workflow_report_markdown", "")).strip()
        if workflow_report_markdown:
            return workflow_report_markdown

        lines: list[str] = []
        step_insights = self.ai_analysis.get("step_insights", [])
        if isinstance(step_insights, list) and step_insights:
            lines.append("当前录制主要包含以下步骤:")
            for item in step_insights:
                if not isinstance(item, dict):
                    continue
                step_id = item.get("step_id")
                description = self._clean_sentence(str(item.get("description", "")))
                conclusion = self._clean_sentence(str(item.get("conclusion", "")))
                if isinstance(step_id, int) and description:
                    line = f"步骤 {step_id}: {description}"
                    if conclusion:
                        line += f"；{conclusion}"
                    lines.append(line)

        reusable_modules = self.ai_analysis.get("reusable_modules", [])
        if isinstance(reusable_modules, list) and reusable_modules:
            lines.append("")
            lines.append("AI 识别出的可复用模块:")
            for item in reusable_modules:
                if not isinstance(item, dict):
                    continue
                name = self._clean_sentence(str(item.get("module_name", "未命名模块"))) or "未命名模块"
                start_step = item.get("start_step", "?")
                end_step = item.get("end_step", "?")
                reason = self._clean_sentence(str(item.get("reason", "")))
                line = f"步骤 {start_step}-{end_step}: {name}"
                if reason:
                    line += f"；{reason}"
                lines.append(line)

        analysis_notes = self.ai_analysis.get("analysis_notes", [])
        if isinstance(analysis_notes, list) and analysis_notes:
            lines.append("")
            lines.append("AI 补充说明:")
            for note in analysis_notes:
                if isinstance(note, str) and self._clean_sentence(note):
                    lines.append(f"- {self._clean_sentence(note)}")

        return "\n".join(lines) if lines else "AI 分析结果中暂时没有可展示的流程总结。"

    def _try_load_historical_suggestions(self) -> None:
        if not self.session_dir:
            return
        suggestion_path = self.session_dir / "conversion_suggestions.json"
        if not suggestion_path.exists():
            self.suggestion_result = None
            self.step_method_suggestions = {}
            self.step_module_suggestions = {}
            self.step_parameter_summaries = {}
            self._clear_parameter_chat_history()
            return
        try:
            result = self.suggestion_service.load_result_file(suggestion_path)
        except Exception:
            self.suggestion_result = None
            self.step_method_suggestions = {}
            self.step_module_suggestions = {}
            self.step_parameter_summaries = {}
            self._clear_parameter_chat_history()
            self.suggestion_var.set("历史调用建议结果读取失败")
            return
        self.suggestion_result = result
        self.step_method_suggestions = self._build_method_suggestion_map(result)
        self.step_module_suggestions = self._build_module_suggestion_map(result)
        self.step_parameter_summaries = self._build_parameter_suggestion_map(result)
        self.suggestion_var.set(self._build_suggestion_summary_text(result))
        self._refresh_selected_suggestion_panel()

    def _generate_method_suggestions_async(
        self,
        selected_rows: list[int] | None = None,
        status_message: str | None = None,
        interactive: bool = False,
    ) -> None:
        if self.suggestion_generation_running:
            if interactive:
                messagebox.showinfo("提示", "方法建议正在生成中，请稍候。", parent=self.window)
            return
        if not self.session_dir or not self.session_data:
            self.suggestion_var.set("未生成调用建议")
            if interactive:
                messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        registry_paths = self._resolve_suggestion_registry_paths()
        if not registry_paths:
            self.suggestion_var.set("未找到可用 registry，跳过调用建议生成")
            if interactive:
                messagebox.showerror("生成失败", "未找到可用 registry。", parent=self.window)
            return
        normalized_selected_rows = self._normalize_selected_analysis_rows(selected_rows)
        if selected_rows is not None and not normalized_selected_rows:
            if interactive:
                messagebox.showinfo("提示", "所选步骤无可生成方法建议的内容。", parent=self.window)
            return
        suggestion_session_data, step_id_mapping = self._build_suggestion_session_data(normalized_selected_rows)
        methods_path, _scripts_path = registry_paths
        self.suggestion_generation_running = True
        self.generate_suggestion_button.configure(state=tk.DISABLED)
        if status_message:
            self.suggestion_var.set(status_message)

        def worker() -> None:
            try:
                result = self.suggestion_service.build_method_selection_from_session_data(
                    session_id=str(suggestion_session_data.get("session_id", self.session_dir.name)),
                    session_data=suggestion_session_data,
                    methods_registry_path=methods_path,
                )
            except Exception as exc:
                message = str(exc)
                self.window.after(0, lambda message=message, interactive=interactive: self._on_suggestion_generation_failed(message, interactive=interactive))
                return
            self.window.after(
                0,
                lambda result=result, selected_rows=normalized_selected_rows, step_id_mapping=step_id_mapping, interactive=interactive: self._on_suggestion_generation_success(
                    result,
                    selected_rows,
                    step_id_mapping,
                    interactive=interactive,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_suggestion_generation_success(
        self,
        result,
        selected_rows: list[int] | None = None,
        step_id_mapping: dict[int, int] | None = None,
        interactive: bool = False,
    ) -> None:
        self.suggestion_generation_running = False
        self.generate_suggestion_button.configure(state=tk.NORMAL)
        merged_result = self._merge_suggestion_result_preserving_parameters(result, selected_rows, step_id_mapping)
        self.suggestion_result = merged_result
        self._persist_suggestion_result()
        self._refresh_selected_suggestion_panel()
        self._reload_tree()
        if interactive:
            selected_step_ids = {row_index + 1 for row_index in selected_rows or []}
            matched_count = sum(
                1
                for item in getattr(merged_result, "suggestions", [])
                if int(getattr(item, "step_id", 0) or 0) in selected_step_ids
                and (
                    str(getattr(item, "method_name", "") or "").strip()
                    or str(getattr(item, "script_name", "") or "").strip()
                )
            )
            if selected_step_ids and matched_count == 0:
                messagebox.showinfo("生成完成", "已执行方法建议生成，但当前所选步骤没有匹配到可展示的方法或模块建议。", parent=self.window)

    def _on_suggestion_generation_failed(self, message: str, interactive: bool = False) -> None:
        self.suggestion_generation_running = False
        self.generate_suggestion_button.configure(state=tk.NORMAL)
        self.suggestion_var.set(f"调用建议生成失败: {message}")
        self.parameter_status_var.set("参数推荐不可用，请先修复调用建议生成。")
        self._refresh_selected_suggestion_panel()
        self._reload_tree()
        if interactive:
            messagebox.showerror("调用建议生成失败", message, parent=self.window)

    def _load_existing_suggestion_result(self):
        if self.suggestion_result is not None:
            return copy.deepcopy(self.suggestion_result)
        if not self.session_dir:
            return None
        suggestion_path = self.session_dir / "conversion_suggestions.json"
        if not suggestion_path.exists():
            return None
        try:
            return self.suggestion_service.load_result_file(suggestion_path)
        except Exception:
            return None

    def _merge_suggestion_result_preserving_parameters(self, new_result, selected_rows: list[int] | None = None, step_id_mapping: dict[int, int] | None = None):
        existing_result = self._load_existing_suggestion_result()
        if step_id_mapping:
            for item in new_result.suggestions:
                step_id = int(getattr(item, "step_id", 0) or 0)
                if step_id in step_id_mapping:
                    item.step_id = step_id_mapping[step_id]
        if existing_result is None:
            return new_result

        existing_by_step = {
            int(item.step_id): item
            for item in existing_result.suggestions
            if int(getattr(item, "step_id", 0) or 0) > 0
        }
        selected_step_ids = {row_index + 1 for row_index in selected_rows or []}
        new_step_ids = {
            int(item.step_id)
            for item in new_result.suggestions
            if int(getattr(item, "step_id", 0) or 0) > 0
        }
        changed_row_indexes: set[int] = set()
        merged_suggestions = []

        if selected_step_ids:
            for item in existing_result.suggestions:
                step_id = int(getattr(item, "step_id", 0) or 0)
                if step_id <= 0 or step_id in selected_step_ids:
                    continue
                merged_suggestions.append(copy.deepcopy(item))

        for item in new_result.suggestions:
            step_id = int(getattr(item, "step_id", 0) or 0)
            if step_id <= 0:
                continue
            existing_item = existing_by_step.get(step_id)
            if existing_item is None:
                changed_row_indexes.add(step_id - 1)
                merged_suggestions.append(item)
                continue

            new_method_name = str(getattr(item, "method_name", "") or "").strip()
            existing_method_name = str(getattr(existing_item, "method_name", "") or "").strip()
            if new_method_name != existing_method_name:
                changed_row_indexes.add(step_id - 1)
                item.parameters = []
                if isinstance(item.candidate_payload, dict):
                    item.candidate_payload.pop("viewer_parameter_summary_override", None)
                merged_suggestions.append(item)
                continue

            item.parameters = copy.deepcopy(existing_item.parameters)
            existing_payload = existing_item.candidate_payload if isinstance(existing_item.candidate_payload, dict) else {}
            new_payload = item.candidate_payload if isinstance(item.candidate_payload, dict) else {}
            merged_payload = dict(new_payload)
            if "viewer_parameter_summary_override" in existing_payload:
                merged_payload["viewer_parameter_summary_override"] = existing_payload["viewer_parameter_summary_override"]
            item.candidate_payload = merged_payload

            merged_suggestions.append(item)

        if not selected_step_ids:
            merged_suggestions = list(new_result.suggestions)

        removed_step_ids = (selected_step_ids or set(existing_by_step)) - new_step_ids
        changed_row_indexes.update(step_id - 1 for step_id in removed_step_ids if step_id > 0)
        for row_index in changed_row_indexes:
            self.parameter_prompt_by_step.pop(row_index, None)
            self.parameter_response_by_step.pop(row_index, None)

        notes = [str(item) for item in getattr(existing_result, "notes", []) if str(item).strip()] if selected_step_ids else []
        notes.extend(str(item) for item in getattr(new_result, "notes", []) if str(item).strip())
        if selected_step_ids:
            deduped_notes: list[str] = []
            seen_notes: set[str] = set()
            for note in notes:
                if note in seen_notes:
                    continue
                deduped_notes.append(note)
                seen_notes.add(note)
            new_result.notes = deduped_notes
        new_result.suggestions = sorted(
            merged_suggestions,
            key=lambda item: int(getattr(item, "step_id", 0) or 0),
        )

        return new_result

    def _build_method_suggestion_map(self, result) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for item in result.suggestions:
            if item.step_id <= 0:
                continue
            mapping[item.step_id - 1] = item.method_name or ""
        return mapping

    def _build_module_suggestion_map(self, result) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for item in result.suggestions:
            if item.step_id <= 0:
                continue
            mapping[item.step_id - 1] = item.script_name or ""
        return mapping

    def _build_parameter_suggestion_map(self, result) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for item in result.suggestions:
            if item.step_id <= 0:
                continue
            override = item.candidate_payload.get("viewer_parameter_summary_override", "") if isinstance(item.candidate_payload, dict) else ""
            mapping[item.step_id - 1] = str(override).strip() or self._summarize_parameter_suggestions(item.parameters)
        return mapping

    def _build_suggestion_summary_text(self, result) -> str:
        method_count = sum(1 for item in result.suggestions if item.method_name)
        module_count = sum(1 for item in result.suggestions if item.script_name)
        return f"调用建议已生成: 方法建议 {method_count} | 模块建议 {module_count}"

    def _describe_method_suggestion_for_view(self, row_index: int) -> str:
        return self.step_method_suggestions.get(row_index, "")

    def _describe_module_suggestion_for_view(self, row_index: int) -> str:
        return self.step_module_suggestions.get(row_index, "")

    def _describe_parameter_suggestion_for_view(self, row_index: int) -> str:
        return self.step_parameter_summaries.get(row_index, "")

    def _merge_analysis_extras(self, base_analysis: dict[str, object], new_analysis: dict[str, object]) -> dict[str, object]:
        merged = copy.deepcopy(new_analysis) if isinstance(new_analysis, dict) else {}
        if "process_summaries" not in merged and isinstance(base_analysis, dict):
            merged["process_summaries"] = [item for item in base_analysis.get("process_summaries", []) if isinstance(item, dict)]
        if "process_summary_overview" not in merged and isinstance(base_analysis, dict):
            merged["process_summary_overview"] = copy.deepcopy(base_analysis.get("process_summary_overview", {})) if isinstance(base_analysis.get("process_summary_overview", {}), dict) else {}
        return merged

    def _summarize_parameter_suggestions(self, parameters) -> str:
        if not parameters:
            return ""
        return "; ".join(
            f"{item.name}={json.dumps(item.suggested_value, ensure_ascii=False, default=str)}"
            for item in parameters
        )

    def _format_parameter_detail_text(self, suggestion) -> str:
        override = suggestion.candidate_payload.get("viewer_parameter_summary_override", "") if isinstance(suggestion.candidate_payload, dict) else ""
        lines = [
            f"Step: {suggestion.step_id}",
            f"方法: {suggestion.method_name or '(无)'}",
            f"方法摘要: {suggestion.method_summary or '(无)'}",
            f"模块建议: {suggestion.script_name or '(无)'}",
            f"原因: {suggestion.reason or '(无)'}",
            f"置信度: {suggestion.confidence:.2f}",
        ]
        if str(override).strip():
            lines.extend(["", f"参数建议摘要(人工修改): {str(override).strip()}"])
        if suggestion.parameters:
            lines.extend(["", "参数推荐:"])
            for item in suggestion.parameters:
                lines.append(f"- {item.name}: {json.dumps(item.suggested_value, ensure_ascii=False, default=str)} | confidence={item.confidence:.2f}")
                if item.evidence:
                    lines.append(f"  evidence: {' | '.join(item.evidence)}")
                if item.missing_reason:
                    lines.append(f"  missing_reason: {item.missing_reason}")
        else:
            lines.extend(["", "参数推荐: (尚未生成)"])
        return "\n".join(lines)

    def run_parameter_recommendation(self) -> None:
        if self.parameter_recommendation_running:
            return
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        if not self.suggestion_result:
            self._try_load_historical_suggestions()
        if not self.suggestion_result:
            messagebox.showinfo("提示", "请先生成调用建议。", parent=self.window)
            return
        row_indexes = self._get_selected_row_indexes()
        if not row_indexes:
            messagebox.showinfo("提示", "请先选择至少一个步骤。", parent=self.window)
            return
        suggestion_rows = [row_index for row_index in row_indexes if self._find_suggestion_by_row_index(row_index) is not None]
        if not suggestion_rows:
            messagebox.showinfo("提示", "当前所选步骤没有可用的方法建议。", parent=self.window)
            return

        find_control_rows: list[int] = []
        for row_index in suggestion_rows:
            suggestion = self._find_suggestion_by_row_index(row_index)
            if suggestion is None:
                continue
            if str(suggestion.method_name or "").strip() == "FindControlByName":
                find_control_rows.append(row_index)

        if find_control_rows and not self.ai_analysis:
            preview_text = ", ".join(str(row_index + 1) for row_index in find_control_rows[:8])
            suffix = "..." if len(find_control_rows) > 8 else ""
            messagebox.showinfo(
                "提示",
                f"所选步骤中包含 FindControlByName（步骤 {preview_text}{suffix}），请先执行 AI 分析后再生成参数推荐。",
                parent=self.window,
            )
            return

        self.parameter_prompt_by_step = {}
        self.parameter_response_by_step = {}
        self.parameter_recommendation_running = True
        self.parameter_recommend_button.configure(state=tk.DISABLED)
        total = len(suggestion_rows)
        self.parameter_progress_var.set(f"参数推荐批处理已启动: 0/{total} | 已选步骤 {', '.join(str(row_index + 1) for row_index in suggestion_rows[:8])}{'...' if len(suggestion_rows) > 8 else ''}")
        self.parameter_status_var.set(f"正在批量生成参数推荐 0/{total}...")

        def worker() -> None:
            completed_rows: list[int] = []
            failed_rows: list[str] = []
            try:
                for position, row_index in enumerate(suggestion_rows, start=1):
                    suggestion = self._find_suggestion_by_row_index(row_index)
                    if suggestion is None:
                        continue
                    self.window.after(0, lambda position=position, total=total, row_index=row_index: self._on_parameter_recommendation_progress(position, total, row_index))
                    try:
                        event = self.event_rows[row_index] if 0 <= row_index < len(self.event_rows) and isinstance(self.event_rows[row_index], dict) else {}
                        ai_observation = self.ai_step_texts.get(row_index, "")
                        self.suggestion_service.recommend_parameters_from_context(
                            suggestion=suggestion,
                            event=event,
                            ai_observation_text=ai_observation,
                        )
                        completed_rows.append(row_index)
                        self.parameter_prompt_by_step.pop(row_index, None)
                        self.parameter_response_by_step.pop(row_index, None)
                        self.suggestion_service.write_result_file(self.session_dir / "conversion_suggestions.json", self.suggestion_result)
                    except Exception as exc:
                        failed_rows.append(f"步骤 {row_index + 1}: {exc}")
            except Exception as exc:
                message = str(exc)
                self.window.after(0, lambda message=message: self._on_parameter_recommendation_failed(message))
                return
            self.window.after(
                0,
                lambda completed_rows=completed_rows, failed_rows=failed_rows, requested_rows=suggestion_rows: self._on_parameter_recommendation_success(completed_rows, failed_rows, requested_rows),
            )

        threading.Thread(target=worker, daemon=True).start()

    def export_atframework_yaml(self) -> None:
        if self.export_yaml_running:
            return
        if not self.session_dir:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return

        metadata_payload = self._get_session_metadata()

        testcase_id = str(metadata_payload.get("testcase_id", "")).strip()
        if not testcase_id:
            messagebox.showerror("导出失败", "Session 元数据中的 Testcase ID 不能为空。", parent=self.window)
            return

        export_root = self._prompt_export_root_directory()
        if export_root is None:
            return

        export_dir = export_root / testcase_id
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("导出失败", f"创建导出目录失败:\n{exc}", parent=self.window)
            return

        output_path = export_dir / "atframework_steps.yaml"
        self.export_yaml_running = True
        self.export_yaml_button.configure(state=tk.DISABLED)
        self.load_status_var.set(f"正在准备导出 ATFramework YAML: {output_path}")

        def worker() -> None:
            try:
                suggestion_result = self._load_suggestion_result_for_export()
                step_count = export_suggestions_to_atframework_yaml(suggestion_result, output_path)
            except Exception as exc:
                message = str(exc)
                self.window.after(0, lambda message=message: self._on_export_atframework_yaml_failed(message))
                return
            self.window.after(0, lambda output_path=output_path, step_count=step_count: self._on_export_atframework_yaml_success(output_path, step_count))

        threading.Thread(target=worker, daemon=True).start()

    def _load_suggestion_result_for_export(self):
        if self.suggestion_result is not None:
            return copy.deepcopy(self.suggestion_result)
        if not self.session_dir:
            raise ValueError("请先加载 Session。")
        suggestion_path = self.session_dir / "conversion_suggestions.json"
        if not suggestion_path.exists():
            raise ValueError("请先生成或加载调用建议。")
        result = self.suggestion_service.load_result_file(suggestion_path)
        self.window.after(0, lambda result=result: self._apply_loaded_suggestion_result(result))
        return copy.deepcopy(result)

    def _apply_loaded_suggestion_result(self, result) -> None:
        self.suggestion_result = result
        self.step_method_suggestions = self._build_method_suggestion_map(result)
        self.step_module_suggestions = self._build_module_suggestion_map(result)
        self.step_parameter_summaries = self._build_parameter_suggestion_map(result)
        self.suggestion_var.set(self._build_suggestion_summary_text(result))
        self._refresh_selected_suggestion_panel()

    def _prompt_export_root_directory(self) -> Path | None:
        default_root = Path.home() / "Desktop"
        if not default_root.exists():
            default_root = Path.home()
        while True:
            raw_value = simpledialog.askstring(
                "导出目录",
                "请输入导出根目录路径。\n将会在该目录下创建 TestcaseID 子文件夹。",
                parent=self.window,
                initialvalue=str(default_root),
            )
            if raw_value is None:
                return None
            candidate = Path(raw_value.strip().strip('"'))
            if not str(candidate).strip():
                messagebox.showerror("导出失败", "导出目录不能为空。", parent=self.window)
                continue
            if not candidate.exists():
                messagebox.showerror("导出失败", f"导出目录不存在:\n{candidate}", parent=self.window)
                continue
            if not candidate.is_dir():
                messagebox.showerror("导出失败", f"导出路径不是文件夹:\n{candidate}", parent=self.window)
                continue
            return candidate

    def _get_target_row_indexes_for_current_steps(self) -> list[int]:
        return self._get_selected_row_indexes()

    def _build_selected_suggestion_result(self, row_indexes: list[int]):
        base_result = self._load_suggestion_result_for_export()
        selected_step_ids = {row_index + 1 for row_index in row_indexes}
        base_result.suggestions = [
            item for item in base_result.suggestions
            if int(getattr(item, "step_id", 0) or 0) in selected_step_ids and str(getattr(item, "method_name", "") or "").strip()
        ]
        return base_result

    def _convert_atframework_step_to_debug_payload(self, step: dict[str, object]) -> dict[str, object]:
        return {
            "ControlName": step.get("ControlName", "Null"),
            "Action": step.get("Action", "Null"),
            "ParameterValue": step.get("Parameter Value", ""),
            "Check": step.get("Check", "Null"),
            "CheckParameterValue": step.get("Check Parameter Value", ""),
            "StepDescription": step.get("Step Description", ""),
            "Expectresult": step.get("Expect result", ""),
        }

    def _build_debug_request_payload(self, debug_payloads: list[dict[str, object]]) -> dict[str, object] | list[dict[str, object]]:
        if len(debug_payloads) == 1:
            return debug_payloads[0]
        return debug_payloads

    def debug_atframework_steps(self) -> None:
        if self.debug_run_running:
            return
        if not self.session_dir:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return

        row_indexes = self._get_target_row_indexes_for_current_steps()
        if not row_indexes:
            messagebox.showinfo("提示", "请先选择至少一个步骤。", parent=self.window)
            return

        try:
            suggestion_result = self._build_selected_suggestion_result(row_indexes)
            yaml_payload = build_atframework_yaml_dict(suggestion_result)
            steps = yaml_payload.get("Steps", []) if isinstance(yaml_payload, dict) else []
            if not isinstance(steps, list):
                steps = []
            debug_payloads = [self._convert_atframework_step_to_debug_payload(step) for step in steps if isinstance(step, dict)]
        except Exception as exc:
            messagebox.showerror("调试失败", str(exc), parent=self.window)
            self.load_status_var.set(f"本地ATFramework调试调用失败: {exc}")
            return

        if not debug_payloads:
            messagebox.showinfo("提示", "当前选中步骤没有可调试的 ATFramework 方法建议。", parent=self.window)
            return

        self.debug_run_running = True
        self.debug_run_button.configure(state=tk.DISABLED)
        request_payload = self._build_debug_request_payload(debug_payloads)
        self.load_status_var.set(f"正在调用本地ATFramework调试: 准备发送 {len(debug_payloads)} 条步骤")
        self.cleaning_var.set(f"本地ATFramework调试中: 准备发送 {len(debug_payloads)} 条步骤")

        def worker() -> None:
            failures: list[str] = []
            success_count = 0
            try:
                response = requests.post(
                    "http://127.0.0.1:38002/runteststeps",
                    headers={"Content-Type": "application/json"},
                    json=request_payload,
                    timeout=20,
                )
                response.raise_for_status()
                success_count = len(debug_payloads)
            except Exception as exc:
                failures.append(f"调试调用失败: {exc} | payload={json.dumps(request_payload, ensure_ascii=False)}")
                self.window.after(0, lambda message=str(exc): self.load_status_var.set(f"本地ATFramework调试调用失败: {message}"))

            self.window.after(
                0,
                lambda success_count=success_count, total=len(debug_payloads), failures=failures: self._on_debug_atframework_steps_finished(
                    success_count,
                    total,
                    failures,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_debug_atframework_steps_finished(self, success_count: int, total: int, failures: list[str]) -> None:
        self.debug_run_running = False
        self.debug_run_button.configure(state=tk.NORMAL)
        if failures:
            self.load_status_var.set(f"本地ATFramework调试调用失败: 成功 {success_count}/{total}")
            self.cleaning_var.set(f"本地ATFramework调试调用失败: 成功 {success_count}/{total} | {failures[0]}")
            return
        self.load_status_var.set(f"本地ATFramework调试完成: 成功 {success_count}/{total}")
        self.cleaning_var.set(f"本地ATFramework调试完成: {success_count}/{total}")

    def _on_export_atframework_yaml_success(self, output_path: Path, step_count: int) -> None:
        self.export_yaml_running = False
        self.export_yaml_button.configure(state=tk.NORMAL)
        self.load_status_var.set(f"已导出 ATFramework YAML: {output_path} | 步骤数 {step_count}")
        messagebox.showinfo("导出完成", f"已导出 ATFramework YAML:\n{output_path}\n\n步骤数: {step_count}", parent=self.window)

    def _on_export_atframework_yaml_failed(self, message: str) -> None:
        self.export_yaml_running = False
        self.export_yaml_button.configure(state=tk.NORMAL)
        self.load_status_var.set(f"导出 ATFramework YAML 失败: {message}")
        messagebox.showerror("导出失败", message or "未知错误", parent=self.window)

    def _on_parameter_recommendation_progress(self, position: int, total: int, row_index: int) -> None:
        self.parameter_progress_var.set(f"参数推荐批处理中: {position}/{total} | 当前步骤 {row_index + 1}")
        self.parameter_status_var.set(f"正在为步骤 {row_index + 1} 生成参数推荐... {position}/{total}")

    def _on_parameter_recommendation_success(self, completed_rows: list[int], failed_rows: list[str], requested_rows: list[int]) -> None:
        self.parameter_recommendation_running = False
        self.parameter_recommend_button.configure(state=tk.NORMAL)
        success_count = len(completed_rows)
        fail_count = len(failed_rows)
        self.parameter_progress_var.set(f"参数推荐批处理完成: 成功 {success_count}/{len(requested_rows)} | 失败 {fail_count}")
        self.parameter_status_var.set(f"批量参数推荐完成: 成功 {success_count} 条, 失败 {fail_count} 条")
        if self.suggestion_result is not None:
            self.step_method_suggestions = self._build_method_suggestion_map(self.suggestion_result)
            self.step_module_suggestions = self._build_module_suggestion_map(self.suggestion_result)
            self.step_parameter_summaries = self._build_parameter_suggestion_map(self.suggestion_result)
            self.suggestion_var.set(self._build_suggestion_summary_text(self.suggestion_result))
        self._refresh_selected_suggestion_panel()
        self._reload_tree()
        selectable_rows = [str(row_index) for row_index in requested_rows if self.tree.exists(str(row_index))]
        if selectable_rows:
            self.tree.selection_set(selectable_rows)
            self.tree.focus(selectable_rows[0])
            self.tree.see(selectable_rows[0])
        self.on_select_event(None)
        self._refresh_ai_chat_panel()
        if failed_rows:
            success_text = ", ".join(str(row_index + 1) for row_index in completed_rows) if completed_rows else "(无)"
            self._show_text_dialog("批量参数推荐结果", "\n".join([f"成功步骤: {success_text}", "", "失败详情:", *failed_rows]))

    def _on_parameter_recommendation_failed(self, message: str) -> None:
        self.parameter_recommendation_running = False
        self.parameter_recommend_button.configure(state=tk.NORMAL)
        self.parameter_progress_var.set(f"参数推荐批处理失败: {message}")
        self.parameter_status_var.set(f"参数推荐失败: {message}")
        messagebox.showerror("参数推荐失败", message or "未知错误", parent=self.window)

    def _find_suggestion_by_row_index(self, row_index: int):
        if not self.suggestion_result:
            return None
        target_step = row_index + 1
        return next((item for item in self.suggestion_result.suggestions if item.step_id == target_step), None)

    def _refresh_selected_suggestion_panel(self) -> None:
        if not hasattr(self, "parameter_result_text"):
            return
        row_indexes = self._get_selected_row_indexes()
        if not row_indexes:
            self.parameter_status_var.set("请选择左侧步骤并先生成调用建议。")
            self._set_text_widget(self.parameter_result_text, "")
            return
        row_index = self._get_primary_selected_row_index()
        if row_index is None:
            self.parameter_status_var.set("请选择左侧步骤并先生成调用建议。")
            self._set_text_widget(self.parameter_result_text, "")
            return
        suggestion = self._find_suggestion_by_row_index(row_index)
        if suggestion is None:
            prefix = f"已选择 {len(row_indexes)} 步 | " if len(row_indexes) > 1 else ""
            self.parameter_status_var.set(f"{prefix}步骤 {row_index + 1} 暂无调用建议")
            self._set_text_widget(self.parameter_result_text, "")
            return
        prefix = f"已选择 {len(row_indexes)} 步 | " if len(row_indexes) > 1 else ""
        self.parameter_status_var.set(f"{prefix}当前显示步骤 {row_index + 1} | 方法建议: {suggestion.method_name or '(无)'}")
        self._set_text_widget(self.parameter_result_text, self._format_parameter_detail_text(suggestion))

    def _resolve_suggestion_registry_paths(self) -> tuple[Path, Path] | None:
        registry_root = self.project_root / "converter_assets" / "registry"
        pilot_scripts = registry_root / "pilot_scripts.yaml"
        full_methods = registry_root / "control_action_methods.yaml"
        full_scripts = registry_root / "scripts.yaml"
        methods_path = full_methods if self._registry_has_entries(full_methods) else None
        scripts_path = pilot_scripts if self._registry_has_entries(pilot_scripts) else full_scripts if self._registry_has_entries(full_scripts) else None
        if methods_path and scripts_path:
            return methods_path, scripts_path
        return None

    def _registry_has_entries(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        return isinstance(entries, list) and bool(entries)

    def run_coverage_check(self) -> None:
        if self.coverage_query_running:
            return
        if not self.ai_analysis:
            messagebox.showinfo("提示", "请先执行 AI 分析。", parent=self.window)
            return

        target_text = self.coverage_input_text.get("1.0", tk.END).strip()
        if not target_text:
            messagebox.showerror("判断失败", "请输入需要判断是否覆盖的内容。", parent=self.window)
            return

        self.coverage_query_running = True
        self.coverage_button.configure(state=tk.DISABLED)
        self.coverage_status_var.set("AI 正在判断覆盖情况...")

        def worker() -> None:
            try:
                settings = self.settings_store.load()
                client = OpenAICompatibleAIClient(settings)
                workflow_summary = self._build_workflow_summary_text()
                prompt = (
                    "以下是某次 Recorder 执行流程的 AI 总结。\n\n"
                    f"流程总结:\n{workflow_summary}\n\n"
                    f"用户希望覆盖的内容:\n{target_text}\n\n"
                    "请判断当前录制是否已经覆盖用户输入的内容。"
                    "请严格返回 JSON 对象，不要加 markdown 代码块，不要补充额外说明。"
                    "JSON 字段如下:\n"
                    "{\n"
                    '  "conclusion": "覆盖/部分覆盖/未覆盖",\n'
                    '  "reason": "一句话原因",\n'
                    '  "evidence": "你判断所依据的流程摘要",\n'
                    '  "covered": ["已经覆盖的点1", "已经覆盖的点2"],\n'
                    '  "partial": ["仅部分覆盖的点1"],\n'
                    '  "gaps": ["仍未覆盖的缺口1", "仍未覆盖的缺口2"],\n'
                    '  "suggestions": ["建议补录步骤1", "建议补录步骤2"]\n'
                    "}"
                )
                result = client.query(
                    user_prompt=prompt,
                    system_prompt="你是自动化测试覆盖分析助手。请基于给定流程总结，判断覆盖关系并用清晰中文输出。",
                )
            except Exception as exc:
                message = str(exc)
                self.window.after(0, lambda message=message: self._on_coverage_failed(message))
                return
            response_text = str(result.get("response_text", ""))
            self.window.after(0, lambda response_text=response_text: self._on_coverage_success(response_text))

        threading.Thread(target=worker, daemon=True).start()

    def _on_coverage_success(self, response_text: str) -> None:
        self.coverage_query_running = False
        self.coverage_button.configure(state=tk.NORMAL)
        payload = self._parse_coverage_response(response_text)
        conclusion = self._extract_coverage_text(payload.get("conclusion")) or "未明确"
        self.coverage_status_var.set(f"覆盖判断完成: {conclusion}")
        self._set_text_widget(self.coverage_result_text, self._format_coverage_result(payload))

    def _on_coverage_failed(self, message: str) -> None:
        self.coverage_query_running = False
        self.coverage_button.configure(state=tk.NORMAL)
        self.coverage_status_var.set("覆盖判断失败")
        messagebox.showerror("覆盖判断失败", message, parent=self.window)

    def _build_event_cn_summary(self, event: dict[str, object]) -> str:
        event_type = self._extract_event_type(event)
        ui_element = event.get("ui_element", {})
        window = event.get("window", {})
        mouse = event.get("mouse", {})
        keyboard = event.get("keyboard", {})
        scroll = event.get("scroll", {})
        note = self._clean_sentence(str(event.get("note", "")))
        modifier_prefix = self._format_modifier_prefix(event)
        action_value = format_recorded_action(event.get("action", "")).strip().lower()

        if event_type == "controlOperation":
            target = self._format_click_target_name(ui_element, window)
            if target:
                return f"{modifier_prefix}单击{target}"
            x = mouse.get("x") if isinstance(mouse, dict) else None
            y = mouse.get("y") if isinstance(mouse, dict) else None
            if isinstance(x, int) and isinstance(y, int):
                return f"{modifier_prefix}单击坐标 ({x}, {y})"
            return f"{modifier_prefix}执行鼠标单击"
        if event_type == "mouseAction" and action_value != "mouse_scroll":
            target = self._format_target_name(ui_element, window)
            start_x = mouse.get("start_x") if isinstance(mouse, dict) else None
            start_y = mouse.get("start_y") if isinstance(mouse, dict) else None
            end_x = mouse.get("end_x") if isinstance(mouse, dict) else None
            end_y = mouse.get("end_y") if isinstance(mouse, dict) else None
            if target:
                return f"{modifier_prefix}在{target}区域执行拖拽"
            if all(isinstance(value, int) for value in (start_x, start_y, end_x, end_y)):
                return f"{modifier_prefix}从 ({start_x}, {start_y}) 拖拽到 ({end_x}, {end_y})"
            return f"{modifier_prefix}执行鼠标拖拽"
        if event_type == "mouseAction" and action_value == "mouse_scroll":
            target = self._format_target_name(ui_element, window)
            dy = scroll.get("dy") if isinstance(scroll, dict) else None
            direction = "向下滚动" if isinstance(dy, int) and dy < 0 else "向上滚动"
            step_count = scroll.get("step_count") if isinstance(scroll, dict) else None
            if isinstance(step_count, int) and step_count > 1:
                direction = f"{direction} {step_count} 次"
            if target:
                return f"{modifier_prefix}在{target}区域{direction}"
            return f"{modifier_prefix}{direction}"
        if event_type == "input" and action_value == "press":
            if isinstance(keyboard, dict):
                char = keyboard.get("char")
                key_name = self._clean_key_name(str(keyboard.get("key_name", "")))
                if isinstance(char, str) and len(char) == 1 and char.isprintable():
                    return f"输入字符“{char}”"
                if key_name:
                    return f"按下“{key_name}”键"
            return "按下按键"
        if event_type == "input" and action_value == "type_input":
            if isinstance(keyboard, dict):
                text = keyboard.get("text", "")
                if isinstance(text, str) and text != "":
                    return f"输入“{self._format_input_text(text)}”"
                sequence = keyboard.get("sequence")
                if isinstance(sequence, list) and sequence:
                    return f"输入序列：{' '.join(str(item) for item in sequence)}"
            return "输入文本"
        if event_type == "getScreenshot":
            return "记录截图"
        if event_type == "comment":
            return f"添加 Comment：“{note}”" if note else "添加 Comment"
        if event_type == "checkpoint":
            checkpoint = event.get("checkpoint", {})
            query = self._clean_sentence(str((checkpoint or {}).get("query", ""))) if isinstance(checkpoint, dict) else ""
            if query:
                return f"添加 AI Checkpoint：“{query}”"
            return "添加 AI Checkpoint"
        if note:
            return note
        action = self._clean_sentence(format_recorded_action(event.get("action", "")))
        return f"执行 {action}" if action else "执行一步操作"

    def _format_modifier_prefix(self, event: dict[str, object]) -> str:
        details = event.get("additional_details", {})
        if not isinstance(details, dict):
            return ""
        modifiers = details.get("modifiers", [])
        if not isinstance(modifiers, list) or not modifiers:
            return ""
        readable = [self._clean_key_name(str(item)) for item in modifiers if str(item).strip()]
        readable = [item for item in readable if item]
        if not readable:
            return ""
        return f"按住 {' + '.join(readable)} 后"

    def _format_click_target_name(self, ui_element: object, window: object) -> str:
        element = ui_element if isinstance(ui_element, dict) else {}
        window_info = window if isinstance(window, dict) else {}
        name = self._clean_sentence(str(element.get("name", "")))
        control_type = str(element.get("control_type", ""))

        inferred = self._infer_clickable_target(name, control_type)
        if inferred:
            return inferred

        fallback = self._format_target_name(ui_element, window)
        if fallback and not fallback.endswith("文本"):
            return fallback

        title = self._clean_sentence(str(window_info.get("title", "")))
        return f"窗口“{title}”中的控件" if title else "控件"

    def _format_target_name(self, ui_element: object, window: object) -> str:
        element = ui_element if isinstance(ui_element, dict) else {}
        window_info = window if isinstance(window, dict) else {}
        name = self._clean_sentence(str(element.get("name", "")))
        control_type = self._control_type_text(str(element.get("control_type", "")))
        if name and control_type:
            return f"{name} {control_type}"
        if name:
            return name
        if control_type:
            return control_type
        title = self._clean_sentence(str(window_info.get("title", "")))
        return f"窗口“{title}”" if title else ""

    def _infer_clickable_target(self, name: str, control_type: str) -> str:
        if control_type in {"Button", "CheckBox", "RadioButton", "ComboBox", "Edit", "ListItem", "TabItem", "MenuItem"}:
            label = self._control_type_text(control_type)
            if name and label:
                return f"“{name}”{label}"
            return label

        lowered = name.lower()
        input_keywords = [
            "name", "last name", "first name", "email", "phone", "address", "search", "language",
            "password", "username", "account", "姓", "名", "邮箱", "电话", "地址", "搜索", "语言", "密码", "账号",
        ]
        button_keywords = [
            "clear", "ok", "cancel", "save", "submit", "search", "browse", "apply", "next", "back",
            "add", "delete", "edit", "start", "stop", "upload", "download", "确定", "取消", "保存", "提交", "清空",
            "应用", "下一步", "上一步", "添加", "删除", "编辑", "开始", "停止", "上传", "下载",
        ]
        combo_keywords = ["language", "lang", "country", "region", "voice", "type", "category", "语言", "地区", "国家", "类型", "分类"]

        if name:
            if any(keyword in lowered for keyword in combo_keywords):
                return f"“{name}”下拉列表框"
            if any(keyword in lowered for keyword in input_keywords):
                return f"“{name}”输入框"
            if any(keyword in lowered for keyword in button_keywords):
                return f"“{name}”按钮"
            if control_type == "Text":
                return f"“{name}”所在控件"
            return f"“{name}”"

        if control_type == "Text":
            return "所在控件"
        return ""

    def _control_type_text(self, control_type: str) -> str:
        mapping = {
            "Button": "按钮",
            "Edit": "输入框",
            "ComboBox": "下拉列表框",
            "ListItem": "列表项",
            "List": "列表",
            "CheckBox": "复选框",
            "RadioButton": "单选按钮",
            "TabItem": "标签页",
            "Tab": "标签",
            "MenuItem": "菜单项",
            "Hyperlink": "链接",
            "Image": "图片",
            "Pane": "区域",
            "Window": "窗口",
            "Document": "文档区域",
            "DataItem": "数据项",
            "TreeItem": "树节点",
        }
        return mapping.get(control_type, control_type)

    def _clean_key_name(self, key_name: str) -> str:
        if not key_name:
            return ""
        key_name = normalize_keyboard_key_name(key_name)
        mapping = {
            "enter": "Enter",
            "tab": "Tab",
            "esc": "Esc",
            "space": "Space",
            "backspace": "Backspace",
            "delete": "Delete",
            "ctrl": "Ctrl",
            "shift": "Shift",
            "ctrl_l": "Ctrl",
            "ctrl_r": "Ctrl",
            "alt": "Alt",
            "alt_l": "Alt",
            "alt_r": "Alt",
            "cmd": "Win",
        }
        return mapping.get(key_name, key_name)

    def _format_input_text(self, text: str) -> str:
        return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")

    def _decision_text(self, decision: str) -> str:
        return {
            "delete": "建议删除",
            "keep": "建议保留",
            "review": "建议复核",
        }.get(decision, "建议复核")

    def _clean_sentence(self, value: str) -> str:
        return " ".join(value.replace("\n", " ").split()).strip("；;，,。 ")

    def _deduplicate_texts(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = self._clean_sentence(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _refresh_ai_panels(self) -> None:
        if not hasattr(self, "ai_summary_text"):
            return
        if not self.ai_analysis:
            self._set_text_widget(self.ai_summary_text, "请先执行 AI 分析。")
            return
        self._set_text_widget(self.ai_summary_text, self._build_workflow_summary_text())

    def _clear_tree(self, tree: ttk.Treeview) -> None:
        for item_id in tree.get_children():
            tree.delete(item_id)

    def _on_ai_invalid_double_click(self, _event: tk.Event) -> None:
        if not hasattr(self, "invalid_tree"):
            return
        selection = self.invalid_tree.selection()
        if not selection or not self.ai_analysis:
            return
        item = self.ai_analysis.get("invalid_steps", [])[int(selection[0].split("_")[-1])]
        if not isinstance(item, dict):
            return
        step_ids = item.get("step_ids", [])
        if isinstance(step_ids, list) and step_ids:
            self._select_step(step_ids[0])

    def _on_ai_module_double_click(self, _event: tk.Event) -> None:
        if not hasattr(self, "module_tree"):
            return
        selection = self.module_tree.selection()
        if not selection or not self.ai_analysis:
            return
        item = self.ai_analysis.get("reusable_modules", [])[int(selection[0].split("_")[-1])]
        if not isinstance(item, dict):
            return
        start_step = item.get("start_step")
        if isinstance(start_step, int):
            self._select_step(start_step)

    def _on_ai_wait_double_click(self, _event: tk.Event) -> None:
        if not hasattr(self, "wait_tree"):
            return
        selection = self.wait_tree.selection()
        if not selection or not self.ai_analysis:
            return
        item = self.ai_analysis.get("wait_suggestions", [])[int(selection[0].split("_")[-1])]
        if not isinstance(item, dict):
            return
        step_id = item.get("step_id")
        if isinstance(step_id, int):
            self._select_step(step_id)

    def _select_step(self, step_id: int) -> None:
        row_id = str(step_id - 1)
        if not self.tree.exists(row_id):
            return
        self.tree.selection_set(row_id)
        self.tree.focus(row_id)
        self.tree.see(row_id)
        self.on_select_event(None)


def open_viewer_window(master: tk.Misc, initial_path: Path | None = None) -> RecorderViewerWindow:
    return RecorderViewerWindow(master, initial_path)


def launch_viewer(initial_path: Path | None = None) -> None:
    root = tk.Tk()
    root.withdraw()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    viewer = RecorderViewerWindow(root, initial_path)
    viewer.set_close_callback(root.destroy)
    root.mainloop()