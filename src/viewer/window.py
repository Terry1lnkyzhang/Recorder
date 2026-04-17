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

import yaml
from PIL import Image

from src.ai import AISuggestionService
from src.ai.client import OpenAICompatibleAIClient
from src.ai.remote_service_client import RemoteAIServiceClient
from src.ai.session_analyzer import SessionWorkflowAnalyzer
from src.common.display_utils import prepare_image_path_for_ai
from src.common.image_widgets import ZoomableImageView
from src.common.media_utils import load_video_preview_frame
from src.common.runtime_paths import get_recordings_dir, get_resource_root, get_settings_path
from src.converter.compiler import export_suggestions_to_atframework_yaml
from src.recorder.dialogs import AICheckpointDraft, open_ai_checkpoint_dialog, open_ai_checkpoint_editor_dialog, open_comment_dialog
from src.recorder.models import format_recorded_action
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
        self.suggestion_service = AISuggestionService()
        self.suggestion_result = None
        self.step_method_suggestions: dict[int, str] = {}
        self.step_module_suggestions: dict[int, str] = {}
        self.step_parameter_summaries: dict[int, str] = {}
        self.parameter_prompt_by_step: dict[int, str] = {}
        self.parameter_response_by_step: dict[int, str] = {}
        self.current_analyzer: SessionWorkflowAnalyzer | None = None
        self.close_callback = None
        self.analysis_running = False
        self.coverage_query_running = False
        self.parameter_recommendation_running = False
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
        self.load_ai_button = ttk.Button(toolbar, text="加载历史AI结果", command=self.load_historical_ai_analysis, state=tk.DISABLED)
        self.load_ai_button.pack(side=tk.LEFT, padx=(8, 0))
        self.cancel_ai_button = ttk.Button(toolbar, text="终止AI分析", command=self.cancel_ai_analysis, state=tk.DISABLED)
        self.cancel_ai_button.pack(side=tk.LEFT, padx=(8, 0))
        self.parameter_recommend_button = ttk.Button(toolbar, text="为当前步骤生成参数推荐", command=self.run_parameter_recommendation)
        self.parameter_recommend_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="转成ATFramework YAML", command=self.export_atframework_yaml).pack(side=tk.LEFT, padx=(8, 0))
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
            columns=("idx", "action", "time", "process_name", "comment", "method_suggestion", "module_suggestion", "parameter_suggestion", "ai_note"),
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("action", text=self._build_filter_heading_text("action"))
        self.tree.heading("time", text="时间")
        self.tree.heading("process_name", text=self._build_filter_heading_text("process"))
        self.tree.heading("comment", text="Comment")
        self.tree.heading("method_suggestion", text="方法建议")
        self.tree.heading("module_suggestion", text="模块建议")
        self.tree.heading("parameter_suggestion", text="参数建议")
        self.tree.heading("ai_note", text="AI看图")
        self.tree.column("idx", width=42, minwidth=36, anchor=tk.CENTER, stretch=False)
        self.tree.column("action", width=92, minwidth=76, anchor=tk.W, stretch=False)
        self.tree.column("time", width=132, minwidth=118, anchor=tk.W, stretch=False)
        self.tree.column("process_name", width=150, minwidth=120, anchor=tk.W, stretch=False)
        self.tree.column("comment", width=180, minwidth=120, anchor=tk.W, stretch=False)
        self.tree.column("method_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)
        self.tree.column("module_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)
        self.tree.column("parameter_suggestion", width=320, minwidth=180, anchor=tk.W, stretch=True)
        self.tree.column("ai_note", width=420, minwidth=240, anchor=tk.W, stretch=False)
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
            columns=("idx", "action", "time", "process_name", "comment", "method_suggestion", "module_suggestion", "parameter_suggestion", "ai_note"),
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
        tree.heading("action", text=self._build_filter_heading_text("action"))
        tree.heading("time", text="时间")
        tree.heading("process_name", text="进程")
        tree.heading("comment", text="Comment")
        tree.heading("method_suggestion", text="方法建议")
        tree.heading("module_suggestion", text="模块建议")
        tree.heading("parameter_suggestion", text="参数建议")
        tree.heading("ai_note", text="AI看图")
        tree.column("idx", width=42, minwidth=36, anchor=tk.CENTER, stretch=False)
        tree.column("action", width=92, minwidth=76, anchor=tk.W, stretch=False)
        tree.column("time", width=132, minwidth=118, anchor=tk.W, stretch=False)
        tree.column("process_name", width=150, minwidth=120, anchor=tk.W, stretch=False)
        tree.column("comment", width=180, minwidth=120, anchor=tk.W, stretch=False)
        tree.column("method_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)
        tree.column("module_suggestion", width=180, minwidth=120, anchor=tk.W, stretch=False)
        tree.column("parameter_suggestion", width=320, minwidth=180, anchor=tk.W, stretch=True)
        tree.column("ai_note", width=420, minwidth=240, anchor=tk.W, stretch=False)

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
        if not base_dir.exists():
            return []

        candidates: list[dict[str, object]] = []
        for session_json in self._iter_session_json_paths(base_dir):
            session_dir = session_json.parent
            try:
                session_stat = session_json.stat()
                dir_stat = session_dir.stat()
            except OSError:
                continue

            cache_key = str(session_json.resolve())
            cache_stamp = (session_stat.st_mtime_ns, session_stat.st_size)
            cached = None if force_refresh else self._session_candidate_cache.get(cache_key)

            if cached and cached.get("stamp") == cache_stamp:
                event_count = cached.get("events", "")
            else:
                event_count: object = ""
                try:
                    payload = json.loads(session_json.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        events = payload.get("events", [])
                        if isinstance(events, list):
                            event_count = len(events)
                except Exception:
                    event_count = "?"
                self._session_candidate_cache[cache_key] = {
                    "stamp": cache_stamp,
                    "events": event_count,
                }

            candidates.append(
                {
                    "name": self._format_session_candidate_name(base_dir, session_dir),
                    "modified": datetime.fromtimestamp(dir_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "modified_ts": dir_stat.st_mtime,
                    "events": event_count,
                    "path": str(session_dir),
                }
            )

        candidates.sort(key=lambda item: float(item.get("modified_ts", 0.0) or 0.0), reverse=True)
        for item in candidates:
            item.pop("modified_ts", None)
        return candidates

    def _find_latest_session(self, base_dir: Path) -> Path | None:
        candidates = [item.parent for item in self._iter_session_json_paths(base_dir)]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.stat().st_mtime)

    def _iter_session_json_paths(self, base_dir: Path):
        for root, _dirs, files in os.walk(base_dir):
            if "session.json" not in files:
                continue
            yield Path(root) / "session.json"

    @staticmethod
    def _format_session_candidate_name(base_dir: Path, item: Path) -> str:
        try:
            return item.relative_to(base_dir).as_posix()
        except ValueError:
            return item.name

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
        self._refresh_coverage_summary()
        self._refresh_filter_options()
        self._load_session_metadata_editor()
        self.summary_var.set(self._build_session_summary_text())
        self._try_load_historical_suggestions()
        self._reload_tree()
        self._reload_event_list_popup()

    def _build_session_summary_text(self) -> str:
        if not self.session_data:
            return "请选择录制目录"
        metadata = self._get_session_metadata()
        is_prs_recording = metadata.get("is_prs_recording", True)
        testcase_id = metadata.get("testcase_id", "")
        version_number = metadata.get("version_number", "")
        name = metadata.get("name", "")
        recorder_person = metadata.get("recorder_person", "")
        metadata_bits: list[str] = []
        if is_prs_recording and testcase_id:
            metadata_bits.append(f"TestcaseID={testcase_id}")
        if is_prs_recording and version_number:
            metadata_bits.append(f"Version={version_number}")
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
        return {
            "is_prs_recording": self._is_session_prs_recording_selected(),
            "testcase_id": self.session_testcase_id_var.get().strip(),
            "version_number": self.session_version_number_var.get().strip(),
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
            self.tree.heading("process_name", text=self._build_filter_heading_text("process"))
            self.tree.heading("action", text=self._build_filter_heading_text("action"))
        if self.event_list_tree and self.event_list_tree.winfo_exists():
            self.event_list_tree.heading("process_name", text=self._build_filter_heading_text("process"))
            self.event_list_tree.heading("action", text=self._build_filter_heading_text("action"))

    def _on_event_tree_mouse_down(self, event: tk.Event) -> str | None:
        tree = event.widget if isinstance(event.widget, ttk.Treeview) else None
        if tree is None:
            return None
        region = tree.identify_region(event.x, event.y)
        if region != "heading":
            return None
        column_id = tree.identify_column(event.x)
        column_map = {"#2": "action", "#4": "process"}
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
        return str(event.get("event_type", "")).strip()

    def _extract_event_action(self, event: dict[str, object]) -> str:
        combined_action = self._extract_combined_action(event)
        if combined_action:
            return combined_action
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
        readable_modifiers = [self._clean_key_name(str(item)) for item in modifiers if str(item).strip()]
        readable_modifiers = [item for item in readable_modifiers if item]
        if not readable_modifiers:
            return ""

        if event_type == "mouse_click":
            mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
            button = str(mouse.get("button", action)).strip() or action
            return f"{' + '.join(readable_modifiers)} + {button}"
        if event_type == "mouse_drag":
            mouse = event.get("mouse", {}) if isinstance(event.get("mouse", {}), dict) else {}
            button = str(mouse.get("button", action)).strip() or action
            return f"{' + '.join(readable_modifiers)} + {button}"
        if event_type == "scroll":
            return f"{' + '.join(readable_modifiers)} + mouse_scroll"
        if event_type == "key_press":
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
        self.ai_var.set(f"已加载历史 AI 分析结果 | {self._build_ai_summary_text(analysis)}")
        self._try_load_historical_suggestions()
        self._refresh_selected_suggestion_panel()
        self._refresh_coverage_summary()
        self._reload_tree()

    def _update_historical_ai_button_state(self) -> None:
        has_history = bool(self.session_dir and (self.session_dir / "ai_analysis.json").exists())
        self.load_ai_button.configure(state=tk.NORMAL if has_history else tk.DISABLED)

    def _reload_tree(self) -> None:
        self._cancel_tree_reload()
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
            visible_indexes = self._visible_row_indexes()
            if visible_indexes and self.tree.exists(str(visible_indexes[0])):
                self.tree.selection_set(str(visible_indexes[0]))
                self.on_select_event(None)
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

        if column_id == "#8":
            suggestion = self._find_suggestion_by_row_index(int(row_id))
            if suggestion is None:
                return
            self.details_notebook.select(self.ai_tab)
            self.tree.selection_set(row_id)
            self.tree.focus(row_id)
            self.on_select_event(None)
            self._show_text_dialog(f"步骤 {int(row_id) + 1} 参数建议", self._format_parameter_detail_text(suggestion))
            return

        if column_id == "#9":
            full_text = self._describe_event_for_view(int(row_id), self.event_rows[int(row_id)])
            if full_text:
                self._show_text_dialog(f"步骤 {int(row_id) + 1} AI看图", full_text)
            return

        if column_id in {"#6", "#7"}:
            values = tree.item(row_id, "values")
            column_index = int(column_id.replace("#", "")) - 1
            full_text = str(values[column_index]) if column_index < len(values) else ""
            if full_text:
                title = "方法建议" if column_id == "#6" else "模块建议"
                self._show_text_dialog(f"步骤 {int(row_id) + 1} {title}", full_text)
            return

        if column_id != "#5":
            return

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

    def _build_event_row_values(self, row_index: int, event: dict[str, object]) -> tuple[object, ...]:
        comment = self._extract_comment(event)
        method_suggestion = self._describe_method_suggestion_for_view(row_index)
        module_suggestion = self._describe_module_suggestion_for_view(row_index)
        parameter_suggestion = self._describe_parameter_suggestion_for_view(row_index)
        ai_note = self._describe_event_for_view(row_index, event)
        return (
            row_index + 1,
            self._extract_event_action(event),
            self._format_timestamp(event.get("timestamp", "")),
            self._extract_process_name(event),
            comment,
            method_suggestion,
            module_suggestion,
            parameter_suggestion,
            ai_note,
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
        self.suggestion_result = None
        self.step_method_suggestions = {}
        self.step_module_suggestions = {}
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        self.ai_var.set("AI 分析结果已过期，请重新执行 AI 分析")
        self.suggestion_var.set("调用建议结果已过期，请重新执行 AI 分析或重新生成建议")
        self.parameter_progress_var.set("参数推荐批处理结果已过期，请重新生成")
        self.parameter_status_var.set("参数推荐结果已过期，请重新执行 AI 分析或重新生成建议")
        self._set_text_widget(self.parameter_result_text, "")
        self._refresh_coverage_summary()
        self._persist_session()
        self.cleaning_var.set(f"已应用 {len(self.cleaning_suggestions)} 条清洗建议")
        self.cleaning_suggestions = []
        self._reload_tree()

    def clear_cleaning_highlight(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.item(item_id, tags=self._build_row_tags(int(item_id), include_cleaning=False))
        if self.event_list_tree and self.event_list_tree.winfo_exists():
            for item_id in self.event_list_tree.get_children():
                self.event_list_tree.item(item_id, tags=self._build_row_tags(int(item_id), include_cleaning=False))

    def run_ai_analysis(self) -> None:
        self._start_ai_analysis()

    def run_selected_ai_analysis(self) -> None:
        if not self.session_dir or not self.session_data:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        selected_rows = self._get_selected_row_indexes()
        if not selected_rows:
            messagebox.showinfo("提示", "请先选择至少一行事件。", parent=self.window)
            return
        self._start_ai_analysis(selected_rows)

    def _start_ai_analysis(self, selected_rows: list[int] | None = None) -> None:
        if not self.session_dir or not self.session_data:
            return
        if self.analysis_running:
            return

        partial_row_indexes = self._normalize_selected_analysis_rows(selected_rows)
        partial_mode = partial_row_indexes is not None
        if partial_mode and not partial_row_indexes:
            messagebox.showinfo("提示", "所选行无可分析事件。", parent=self.window)
            return

        analysis_session_data, step_id_mapping = self._build_analysis_session_data(partial_row_indexes)

        self.analysis_running = True
        self.ai_button.configure(state=tk.DISABLED)
        self.selected_ai_button.configure(state=tk.DISABLED)
        self.cancel_ai_button.configure(state=tk.NORMAL)
        self.analysis_started_at = time.time()
        self.analysis_status_base = "AI 分析预处理中（选中行）" if partial_mode else "AI 分析预处理中"
        self.analysis_status_token += 1
        self._refresh_analysis_status(self.analysis_status_token)

        def worker() -> None:
            staged_session_dir: Path | None = None
            try:
                settings = self.settings_store.load()
                if settings.use_remote_ai_service:
                    self.current_analyzer = None
                    progress_payload = {"transport": "remote_service"}
                    if partial_mode:
                        progress_payload["selected_count"] = len(partial_row_indexes or [])
                    self.window.after(0, lambda payload=progress_payload: self._on_ai_analysis_progress("send_request", payload))
                    result = RemoteAIServiceClient(settings).analyze_session(self.session_dir, analysis_session_data)
                    self.window.after(0, lambda payload=progress_payload: self._on_ai_analysis_progress("done", payload))
                else:
                    analyzer = SessionWorkflowAnalyzer(settings)
                    self.current_analyzer = analyzer
                    analysis_dir = self.session_dir
                    if partial_mode:
                        staged_session_dir = self._create_temp_analysis_session_dir(analysis_session_data)
                        analysis_dir = staged_session_dir
                    result = analyzer.analyze(
                        analysis_dir,
                        analysis_session_data,
                        progress_callback=lambda stage, payload: self.window.after(
                            0,
                            lambda stage=stage, payload=payload: self._on_ai_analysis_progress(stage, payload),
                        ),
                    )
            except Exception as exc:
                message = str(exc)
                self.window.after(0, lambda message=message: self._on_ai_analysis_failed(message))
                return
            finally:
                if staged_session_dir is not None:
                    shutil.rmtree(staged_session_dir.parent, ignore_errors=True)
            analysis = result.to_dict()
            if partial_mode:
                analysis = self._remap_analysis_step_ids(analysis, step_id_mapping)
                self.window.after(
                    0,
                    lambda analysis=analysis, partial_row_indexes=partial_row_indexes: self._on_selected_ai_analysis_success(analysis, partial_row_indexes or []),
                )
                return
            self.window.after(0, lambda analysis=analysis: self._on_ai_analysis_success(analysis))

        threading.Thread(target=worker, daemon=True).start()

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

    def _on_selected_ai_analysis_success(self, analysis: dict[str, object], selected_rows: list[int]) -> None:
        selected_step_ids = {row_index + 1 for row_index in selected_rows}
        base_analysis = copy.deepcopy(self.ai_analysis) if isinstance(self.ai_analysis, dict) else self._load_ai_analysis(self.session_dir) or {}
        merged_analysis = self._merge_selected_ai_analysis(base_analysis, analysis, selected_step_ids)
        self._persist_ai_analysis(merged_analysis)

        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "选中行 AI 分析完成"
        self.analysis_status_token += 1
        self.ai_analysis = merged_analysis
        self.ai_step_tags = self._build_ai_step_tags(merged_analysis)
        self.ai_step_texts = self._build_ai_step_texts(merged_analysis)
        self.ai_var.set(f"已完成选中 {len(selected_rows)} 行 AI 分析 | {self._build_ai_summary_text(merged_analysis)}")
        self._invalidate_suggestion_outputs(message="局部 AI 分析已更新，调用建议需重新全量生成")
        self._refresh_coverage_summary()
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

        merged["analysis_notes"].append(
            f"局部刷新步骤: {', '.join(str(step_id) for step_id in sorted(selected_step_ids))}"
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

    def cancel_ai_analysis(self) -> None:
        if not self.analysis_running:
            return
        self.analysis_status_base = "正在请求取消 AI 分析"
        self._refresh_analysis_status(self.analysis_status_token)
        analyzer = self.current_analyzer
        if analyzer is not None:
            analyzer.cancel()
        elif self.settings_store.load().use_remote_ai_service:
            self.analysis_status_base = "远端共享服务当前不支持取消，等待本次请求结束"
            self._refresh_analysis_status(self.analysis_status_token)

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
        self.event_rows.insert(insert_at, inserted_event)
        self.session_data["events"] = self.event_rows
        self._sync_checkpoint_collection_entry({}, inserted_event)
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        self._invalidate_derived_outputs()
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
            "将开始一段临时录制。\n\n完成操作后，请在弹出的控制窗口中点击“停止并插入”。\n临时录制过程中也支持添加 Comment / AI Checkpoint，它们会一起插入事件列表。\n\n是否继续？",
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

        removed_events: list[dict[str, object]] = []
        for row_index in valid_indexes:
            removed_events.append(self.event_rows[row_index])
            del self.event_rows[row_index]

        self.session_data["events"] = self.event_rows
        self._remove_checkpoint_collection_entries(removed_events)
        self._remove_comment_collection_entries(removed_events)
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        self._invalidate_derived_outputs()
        self.media_cache.clear()
        self._reload_tree()

        if self.event_rows:
            next_index = min(valid_indexes[-1], len(self.event_rows) - 1)
            self._select_row_index(next_index)

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
            text="临时录制已开始。\n可直接执行真实操作，也可以在这里补充 Comment / AI Checkpoint。\n完成后点击“停止并插入”；若放弃本次录制，点击“取消”。",
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
        self.event_rows[insert_at:insert_at] = inserted_events
        self.session_data["events"] = self.event_rows
        self._sync_auxiliary_collections_for_inserted_events(inserted_events)
        self.summary_var.set(self._build_session_summary_text())
        self._persist_session()
        self._invalidate_derived_outputs()
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
        prefix = self._derive_event_id_prefix(str(event.get("event_id", "")), str(event.get("event_type", "")))
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
            and str(left.get("event_type", "")) == str(right.get("event_type", ""))
            and format_recorded_action(left.get("action", "")) == format_recorded_action(right.get("action", ""))
            and str(left.get("note", "")) == str(right.get("note", ""))
        )

    def _invalidate_derived_outputs(self) -> None:
        self.ai_analysis = None
        self.ai_step_tags = {}
        self.ai_step_texts = {}
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
        container = ttk.Frame(parent)
        container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=body, anchor=tk.NW)

        def sync_scrollregion(_event: tk.Event | None = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def sync_width(event: tk.Event) -> None:
            canvas.itemconfigure(canvas_window, width=event.width)

        body.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", sync_width)

        self.coverage_canvas = canvas

        summary_frame = ttk.LabelFrame(body, text="当前录制流程总结")
        summary_frame.pack(fill=tk.BOTH, expand=True)
        self.coverage_summary_text = tk.Text(summary_frame, height=8, wrap=tk.WORD, font=("Segoe UI", 10))
        self.coverage_summary_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=8, pady=8)
        summary_scroll = ttk.Scrollbar(summary_frame, orient=tk.VERTICAL, command=self.coverage_summary_text.yview)
        summary_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        self.coverage_summary_text.configure(yscrollcommand=summary_scroll.set, state=tk.DISABLED)

        input_frame = ttk.LabelFrame(body, text="覆盖目标判断")
        input_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
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
        for widget in [canvas, body, summary_frame, input_frame, self.coverage_summary_text, self.coverage_input_text, self.coverage_result_text]:
            widget.bind("<MouseWheel>", self._on_coverage_mousewheel, add="+")
        self._set_text_widget(self.coverage_summary_text, "请先执行 AI 分析。")
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
        for item in self.ai_analysis.get("batches", []):
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
        self.suggestion_var.set("调用建议结果已过期，请重新执行 AI 分析或重新生成建议")
        self.parameter_progress_var.set("参数推荐批处理结果已过期，请重新生成")
        self.parameter_status_var.set("参数推荐结果已过期，请重新执行 AI 分析或重新生成建议")
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
                    relative_position = str(item.get("relative_position", "")).strip()
                    need_scroll = item.get("need_scroll")
                    is_table = item.get("is_table")
                    parts: list[str] = []
                    if label:
                        parts.append(f"label={label}")
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
            return "检测到历史调用建议结果，可在加载 AI 结果后直接使用。"
        return "未生成调用建议"

    def _on_ai_analysis_success(self, analysis: dict[str, object]) -> None:
        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "AI 分析完成"
        self.analysis_status_token += 1
        self.ai_analysis = analysis
        self.ai_step_tags = self._build_ai_step_tags(analysis)
        self.ai_step_texts = self._build_ai_step_texts(analysis)
        self.ai_var.set(self._build_ai_summary_text(analysis))
        self.suggestion_var.set("AI 分析完成，正在生成调用建议...")
        self.parameter_progress_var.set("等待调用建议生成完成后再执行参数推荐")
        self.parameter_status_var.set("请等待调用建议生成完成。")
        self._set_text_widget(self.parameter_result_text, "")
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        self._refresh_coverage_summary()
        self._reload_tree()
        self._generate_method_suggestions_async(analysis)
        messagebox.showinfo("AI 分析完成", "已输出 ai_analysis.json 和 ai_analysis.yaml，并更新 viewer 高亮。", parent=self.window)

    def _on_ai_analysis_failed(self, message: str) -> None:
        self.analysis_running = False
        self.current_analyzer = None
        self.ai_button.configure(state=tk.NORMAL)
        self.selected_ai_button.configure(state=tk.NORMAL)
        self.cancel_ai_button.configure(state=tk.DISABLED)
        self.analysis_status_base = "AI 分析失败"
        self.analysis_status_token += 1
        if message == "AI 分析已取消。":
            self.analysis_status_base = "AI 分析已取消"
            self.ai_var.set("AI 分析已取消")
            self.suggestion_var.set("未生成调用建议")
            self.parameter_progress_var.set("参数推荐批处理未执行")
            self.parameter_status_var.set("参数推荐未执行。")
            self._set_text_widget(self.parameter_result_text, "")
            return
        partial_analysis = self._load_ai_analysis(self.session_dir) if self.session_dir else None
        has_partial = bool(partial_analysis and isinstance(partial_analysis.get("step_insights", []), list) and partial_analysis.get("step_insights", []))
        if has_partial and partial_analysis is not None:
            self.ai_analysis = partial_analysis
            self.ai_step_tags = self._build_ai_step_tags(partial_analysis)
            self.ai_step_texts = self._build_ai_step_texts(partial_analysis)
            self.ai_var.set(f"AI 分析部分成功: 已加载成功步骤 | {self._build_ai_summary_text(partial_analysis)}")
            self.suggestion_result = None
            self.step_method_suggestions = {}
            self.step_module_suggestions = {}
            self.step_parameter_summaries = {}
            self.suggestion_var.set("AI 分析未完整完成，暂不生成调用建议")
            self.parameter_progress_var.set("AI 分析部分成功，参数推荐需等待完整分析后再执行")
            self.parameter_status_var.set("已加载成功的 AI 建议；当前分析未完整完成。")
            self._set_text_widget(self.parameter_result_text, "")
            self._clear_parameter_chat_history()
            self._refresh_coverage_summary()
            self._reload_tree()
        elif not has_partial:
            self.ai_var.set(f"AI 分析失败: {message}")
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

        if stage == "start":
            event_count = payload.get("event_count", 0)
            batch_size = payload.get("batch_size", 0)
            total_batches = payload.get("total_batches", 0)
            return f"AI 分析启动: 共 {event_count} 步，batch_size={batch_size}，预计 {total_batches} 批"
        if stage == "batch_preprocess_start":
            start_step = payload.get("start_step", "?")
            end_step = payload.get("end_step", "?")
            return f"{batch_prefix}预处理中: 步骤 {start_step}-{end_step}，正在整理事件与定位发送图片"
        if stage == "batch_preprocess_done":
            start_step = payload.get("start_step", "?")
            end_step = payload.get("end_step", "?")
            image_count = payload.get("image_count", 0)
            cropped_monitor_count = payload.get("cropped_monitor_count", 0)
            return f"{batch_prefix}预处理完成: 步骤 {start_step}-{end_step}，发送图片 {image_count} 张，其中单屏裁切 {cropped_monitor_count} 张"
        if stage == "prepare_media":
            image_count = payload.get("image_count", 0)
            inline_image_count = payload.get("inline_image_count", 0)
            has_video = payload.get("has_video", False)
            return f"{batch_prefix}准备请求媒体: 文件图片 {image_count} 张，临时图片 {inline_image_count} 张，视频 {'有' if has_video else '无'}"
        if stage == "send_request":
            timeout_seconds = payload.get("timeout_seconds", 0)
            return f"{batch_prefix}已发送到模型，等待响应中，单批超时 {timeout_seconds} 秒"
        if stage == "response_received":
            status_code = payload.get("status_code", "?")
            return f"{batch_prefix}模型已返回 HTTP {status_code}，正在读取响应"
        if stage == "parse_response":
            return f"{batch_prefix}正在解析模型返回 JSON"
        if stage == "batch_parse":
            return f"{batch_prefix}正在解析当前步骤分析结果"
        if stage == "batch_done":
            step_insight_count = payload.get("step_insight_count", 0)
            return f"{batch_prefix}当前步骤分析完成，累计生成步骤总结 {step_insight_count} 条"
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
        if event.get("event_type") in {"comment", "wait"}:
            return str(event.get("note", ""))
        details = event.get("additional_details", {})
        if isinstance(details, dict):
            return str(details.get("viewer_comment", ""))
        return ""

    def _update_event_comment(self, index: int, comment: str) -> None:
        event = self.event_rows[index]
        if event.get("event_type") == "comment":
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

    def _refresh_coverage_summary(self) -> None:
        summary = self._build_workflow_summary_text()
        self._set_text_widget(self.coverage_summary_text, summary)
        if self.ai_analysis:
            self.coverage_status_var.set("可输入目标，让 AI 判断当前录制是否已覆盖。")
        else:
            self.coverage_status_var.set("请先执行 AI 分析，再进行覆盖判断")
        self._set_text_widget(self.coverage_result_text, "")

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

    def _generate_method_suggestions_async(self, analysis: dict[str, object]) -> None:
        if not self.session_dir or not self.session_data:
            self.suggestion_var.set("未生成调用建议")
            return
        registry_paths = self._resolve_suggestion_registry_paths()
        if not registry_paths:
            self.suggestion_var.set("未找到可用 registry，跳过调用建议生成")
            return
        methods_path, scripts_path = registry_paths
        ai_analysis_path = self.session_dir / "ai_analysis.json"
        session_path = self.session_dir / "session.json"

        def worker() -> None:
            try:
                settings = self.settings_store.load()
                if settings.use_remote_ai_service:
                    result = RemoteAIServiceClient(settings).build_method_suggestions(
                        session_dir=self.session_dir,
                        session_data=self.session_data,
                        ai_analysis_path=ai_analysis_path,
                        session_path=session_path if session_path.exists() else None,
                        methods_registry_path=methods_path,
                        scripts_registry_path=scripts_path,
                        top_k_methods=3,
                        top_k_scripts=2,
                    )
                else:
                    result = self.suggestion_service.build_method_selection_from_files(
                        session_id=str(self.session_data.get("session_id", self.session_dir.name)),
                        ai_analysis_path=ai_analysis_path,
                        session_path=session_path if session_path.exists() else None,
                        methods_registry_path=methods_path,
                        scripts_registry_path=scripts_path,
                        top_k_methods=3,
                        top_k_scripts=2,
                    )
                self.suggestion_service.write_result_file(self.session_dir / "conversion_suggestions.json", result)
            except Exception as exc:
                message = str(exc)
                self.window.after(0, lambda message=message: self._on_suggestion_generation_failed(message))
                return
            self.window.after(0, lambda result=result: self._on_suggestion_generation_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_suggestion_generation_success(self, result) -> None:
        self.suggestion_result = result
        self.step_method_suggestions = self._build_method_suggestion_map(result)
        self.step_module_suggestions = self._build_module_suggestion_map(result)
        self.step_parameter_summaries = self._build_parameter_suggestion_map(result)
        self.suggestion_var.set(self._build_suggestion_summary_text(result))
        self._refresh_selected_suggestion_panel()
        self._reload_tree()

    def _on_suggestion_generation_failed(self, message: str) -> None:
        self.suggestion_result = None
        self.step_method_suggestions = {}
        self.step_module_suggestions = {}
        self.step_parameter_summaries = {}
        self._clear_parameter_chat_history()
        self.suggestion_var.set(f"调用建议生成失败: {message}")
        self.parameter_status_var.set("参数推荐不可用，请先修复调用建议生成。")
        self._set_text_widget(self.parameter_result_text, "")
        self._reload_tree()

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
            mapping[item.step_id - 1] = self._summarize_parameter_suggestions(item.parameters)
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

    def _summarize_parameter_suggestions(self, parameters) -> str:
        if not parameters:
            return ""
        return "; ".join(
            f"{item.name}={json.dumps(item.suggested_value, ensure_ascii=False, default=str)}"
            for item in parameters
        )

    def _format_parameter_detail_text(self, suggestion) -> str:
        lines = [
            f"Step: {suggestion.step_id}",
            f"方法: {suggestion.method_name or '(无)'}",
            f"方法摘要: {suggestion.method_summary or '(无)'}",
            f"模块建议: {suggestion.script_name or '(无)'}",
            f"原因: {suggestion.reason or '(无)'}",
            f"置信度: {suggestion.confidence:.2f}",
        ]
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
        if not self.session_dir or not self.session_data or not self.ai_analysis or not self.suggestion_result:
            messagebox.showinfo("提示", "请先执行 AI 分析并生成调用建议。", parent=self.window)
            return
        row_indexes = self._get_selected_row_indexes()
        if not row_indexes:
            messagebox.showinfo("提示", "请先选择至少一个步骤。", parent=self.window)
            return
        suggestion_rows = [row_index for row_index in row_indexes if self._find_suggestion_by_row_index(row_index) is not None]
        if not suggestion_rows:
            messagebox.showinfo("提示", "所选步骤里没有可用的方法建议。", parent=self.window)
            return
        registry_paths = self._resolve_suggestion_registry_paths()
        if not registry_paths:
            messagebox.showerror("参数推荐失败", "未找到可用 registry。", parent=self.window)
            return
        methods_path, scripts_path = registry_paths
        ai_analysis_path = self.session_dir / "ai_analysis.json"
        session_path = self.session_dir / "session.json"
        self.parameter_recommendation_running = True
        self.parameter_recommend_button.configure(state=tk.DISABLED)
        total = len(suggestion_rows)
        self.parameter_progress_var.set(f"参数推荐批处理已启动: 0/{total} | 已选步骤 {', '.join(str(row_index + 1) for row_index in suggestion_rows[:8])}{'...' if len(suggestion_rows) > 8 else ''}")
        self.parameter_status_var.set(f"正在批量生成参数推荐 0/{total}...")

        def worker() -> None:
            completed_rows: list[int] = []
            failed_rows: list[str] = []
            try:
                settings = self.settings_store.load()
                client = OpenAICompatibleAIClient(settings)
                remote_client = RemoteAIServiceClient(settings) if settings.use_remote_ai_service else None
                for position, row_index in enumerate(suggestion_rows, start=1):
                    suggestion = self._find_suggestion_by_row_index(row_index)
                    if suggestion is None:
                        continue
                    self.window.after(0, lambda position=position, total=total, row_index=row_index: self._on_parameter_recommendation_progress(position, total, row_index))
                    try:
                        if remote_client is not None:
                            response = remote_client.recommend_parameters(
                                session_dir=self.session_dir,
                                session_data=self.session_data,
                                suggestion=suggestion,
                                ai_analysis_path=ai_analysis_path,
                                methods_registry_path=methods_path,
                                session_path=session_path if session_path.exists() else None,
                                scripts_registry_path=scripts_path,
                                top_k_methods=3,
                                top_k_scripts=2,
                            )
                            updated = response.get("suggestion", {}) if isinstance(response, dict) else {}
                            if isinstance(updated, dict):
                                refreshed = type(suggestion).from_dict(updated)
                                suggestion.method_name = refreshed.method_name
                                suggestion.score = refreshed.score
                                suggestion.confidence = refreshed.confidence
                                suggestion.reason = refreshed.reason
                                suggestion.step_description = refreshed.step_description
                                suggestion.step_conclusion = refreshed.step_conclusion
                                suggestion.method_summary = refreshed.method_summary
                                suggestion.script_name = refreshed.script_name
                                suggestion.script_summary = refreshed.script_summary
                                suggestion.candidate_payload = refreshed.candidate_payload
                                suggestion.parameters = refreshed.parameters
                            prompt_text = str(response.get("prompt_text", ""))
                            response_text = str(response.get("response_text", ""))
                        else:
                            _notes, _preview, prompt_text, response_text = self.suggestion_service.recommend_parameters_for_suggestion(
                                client=client,
                                suggestion=suggestion,
                                ai_analysis_path=ai_analysis_path,
                                methods_registry_path=methods_path,
                                session_path=session_path if session_path.exists() else None,
                                scripts_registry_path=scripts_path,
                                top_k_methods=3,
                                top_k_scripts=2,
                            )
                        completed_rows.append(row_index)
                        self.parameter_prompt_by_step[row_index] = prompt_text
                        self.parameter_response_by_step[row_index] = response_text
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
        if not self.session_dir:
            messagebox.showinfo("提示", "请先加载 Session。", parent=self.window)
            return
        if not self.suggestion_result:
            self._try_load_historical_suggestions()
        if not self.suggestion_result:
            messagebox.showinfo("提示", "请先生成或加载调用建议。", parent=self.window)
            return
        output_path = self.session_dir / "atframework_steps.yaml"
        try:
            step_count = export_suggestions_to_atframework_yaml(self.suggestion_result, output_path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.window)
            return
        self.load_status_var.set(f"已导出 ATFramework YAML: {output_path} | 步骤数 {step_count}")
        messagebox.showinfo("导出完成", f"已导出 ATFramework YAML:\n{output_path}\n\n步骤数: {step_count}", parent=self.window)

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
        pilot_methods = registry_root / "pilot_methods.yaml"
        pilot_scripts = registry_root / "pilot_scripts.yaml"
        full_methods = registry_root / "control_action_methods.yaml"
        full_scripts = registry_root / "scripts.yaml"
        methods_path = pilot_methods if self._registry_has_entries(pilot_methods) else full_methods if self._registry_has_entries(full_methods) else None
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
        event_type = str(event.get("event_type", ""))
        ui_element = event.get("ui_element", {})
        window = event.get("window", {})
        mouse = event.get("mouse", {})
        keyboard = event.get("keyboard", {})
        scroll = event.get("scroll", {})
        note = self._clean_sentence(str(event.get("note", "")))
        modifier_prefix = self._format_modifier_prefix(event)

        if event_type == "mouse_click":
            target = self._format_click_target_name(ui_element, window)
            if target:
                return f"{modifier_prefix}单击{target}"
            x = mouse.get("x") if isinstance(mouse, dict) else None
            y = mouse.get("y") if isinstance(mouse, dict) else None
            if isinstance(x, int) and isinstance(y, int):
                return f"{modifier_prefix}单击坐标 ({x}, {y})"
            return f"{modifier_prefix}执行鼠标单击"
        if event_type == "mouse_drag":
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
        if event_type == "scroll":
            target = self._format_target_name(ui_element, window)
            dy = scroll.get("dy") if isinstance(scroll, dict) else None
            direction = "向下滚动" if isinstance(dy, int) and dy < 0 else "向上滚动"
            step_count = scroll.get("step_count") if isinstance(scroll, dict) else None
            if isinstance(step_count, int) and step_count > 1:
                direction = f"{direction} {step_count} 次"
            if target:
                return f"{modifier_prefix}在{target}区域{direction}"
            return f"{modifier_prefix}{direction}"
        if event_type == "key_press":
            if isinstance(keyboard, dict):
                char = keyboard.get("char")
                key_name = self._clean_key_name(str(keyboard.get("key_name", "")))
                if isinstance(char, str) and len(char) == 1 and char.isprintable():
                    return f"输入字符“{char}”"
                if key_name:
                    return f"按下“{key_name}”键"
            return "按下按键"
        if event_type == "type_input":
            if isinstance(keyboard, dict):
                text = keyboard.get("text", "")
                if isinstance(text, str) and text != "":
                    return f"输入“{self._format_input_text(text)}”"
                sequence = keyboard.get("sequence")
                if isinstance(sequence, list) and sequence:
                    return f"输入序列：{' '.join(str(item) for item in sequence)}"
            return "输入文本"
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
        if key_name.startswith("Key."):
            key_name = key_name.split(".", 1)[1]
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