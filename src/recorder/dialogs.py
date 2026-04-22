from __future__ import annotations

import time
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageGrab

from src.common.prompt_templates import PromptTemplateRecord, load_checkpoint_prompt_templates
from src.database import fetch_distinct_baseline_names, fetch_latest_testcase_management_record
from src.ai.client import OpenAICompatibleAIClient
from src.ai.errors import AIClientError
from src.ai.remote_service_client import RemoteAIServiceClient
from .settings import Settings, SettingsStore
from .session_metadata_ai import (
    analyze_session_metadata,
    build_missing_summary,
    merge_keyword_text,
    should_prompt_ai_analysis,
)
from src.common.image_widgets import ZoomableImageView
from src.common.media_utils import load_video_preview_frame

from .capture import RegionVideoRecorder, select_region
from .recorder import RecorderEngine
from .system_info import safe_relpath


AI_CHECKPOINT_CT_VALIDATION_PROMPT = """你是一名严谨的CT验证工程师。

请仅根据图像中直接可见的内容，判断以下问题是否成立：
{query}

判定原则：
- 只能依据图像中清晰可见的信息进行判断。
- 不得依赖猜测、经验、常识、上下文补全或任何图像外信息。
- 如果图像中没有足够证据支持判断，则必须返回 False。

请严格按照以下格式输出，不要输出任何额外内容：

如果可以确认成立：
result: True
reason: <图像中直接可见的依据>

如果无法确认或不成立：
result: False
reason: <图像中信息不足或无法直接确认的原因>"""

AI_CHECKPOINT_EXTRACTION_PROMPT = """你是一名严谨的图像信息提取助手。

请仅根据图像中直接可见的内容，提取以下信息：
{query}

返回要求：
- 只返回一个 JSON 对象。
- JSON 中只能包含一个字段：value。
- 不要输出任何额外解释、注释、标题或 Markdown 代码块。
- 如果无法从图像中直接确认该值，则返回 {\"value\": null}。

示例：
{\"value\": 12}
{\"value\": 12.5}
{\"value\": \"ABC\"}
{\"value\": true}
{"value": null}"""

SESSION_SCOPE_OPTIONS = ["All", "Sub"]
PRS_RECORDING_OPTIONS = [("是", True), ("否", False)]
SESSION_PROJECT_OPTIONS = ["Taichi", "Kylin", "Earth_Kylin", "Earth_Taichi", "Earth"]
MAX_AI_CHECKPOINT_IMAGES = 5
AI_CHECKPOINT_PREVIEW_HEIGHT = 220
AI_CHECKPOINT_SCROLLBAR_WIDTH = 18


def _set_text(widget: tk.Text, text: str) -> None:
    widget.delete("1.0", tk.END)
    widget.insert(tk.END, text)


@dataclass(slots=True)
class RegionCaptureWindowState:
    dialog_was_iconic: bool
    parent_was_iconic: bool


@dataclass(slots=True)
class AICheckpointPromptTemplateOption:
    key: str
    label: str
    prompt_template: str


def _get_builtin_ai_checkpoint_prompt_templates() -> list[AICheckpointPromptTemplateOption]:
    return [
        AICheckpointPromptTemplateOption("ct_validation", "CT 验证判定模板", AI_CHECKPOINT_CT_VALIDATION_PROMPT),
        AICheckpointPromptTemplateOption("json_extraction", "JSON 信息提取模板", AI_CHECKPOINT_EXTRACTION_PROMPT),
        AICheckpointPromptTemplateOption("empty", "空提示词", "{query}"),
    ]


def _convert_prompt_template_records(records: list[PromptTemplateRecord]) -> list[AICheckpointPromptTemplateOption]:
    options = [
        AICheckpointPromptTemplateOption(record.key, record.label, record.content)
        for record in records
        if record.content.strip()
    ]
    options.append(AICheckpointPromptTemplateOption("empty", "空提示词", "{query}"))
    return options


def _select_region_with_window_management(
    parent: tk.Misc,
    dialog: tk.Misc,
    prompt: str,
    *,
    dialog_hide_mode: str = "iconify",
    parent_restore_mode: str = "restore_if_visible",
    settle_delay_seconds: float = 0.15,
):
    state = RegionCaptureWindowState(
        dialog_was_iconic=dialog.wm_state() == "iconic",
        parent_was_iconic=parent.wm_state() == "iconic",
    )
    active_grab = dialog.grab_current()
    if active_grab is not None:
        try:
            active_grab.grab_release()
        except Exception:
            active_grab = None

    if not state.dialog_was_iconic:
        if dialog_hide_mode == "withdraw":
            dialog.withdraw()
        else:
            dialog.iconify()
    if not state.parent_was_iconic:
        parent.iconify()

    dialog.update_idletasks()
    parent.update_idletasks()
    time.sleep(settle_delay_seconds)

    try:
        return select_region(parent, prompt)
    finally:
        if parent_restore_mode == "restore_if_visible" and not state.parent_was_iconic:
            parent.deiconify()
            parent.lift()
        elif parent_restore_mode == "keep_iconified" and not state.parent_was_iconic:
            parent.iconify()

        if not state.dialog_was_iconic:
            dialog.deiconify()
        dialog.lift()
        dialog.focus_force()
        if active_grab is not None and active_grab.winfo_exists():
            try:
                active_grab.grab_set()
            except Exception:
                pass


@dataclass(slots=True)
class AICheckpointDraft:
    title: str = ""
    prompt: str = ""
    query_text: str = ""
    design_steps: str = ""
    step_comment: str = ""
    prompt_template_key: str = "ct_validation"
    response_text: str = ""
    query_status: str = "未查询"
    image_selections: list[tuple[Path, dict[str, int]]] = field(default_factory=list)
    video_path: Path | None = None
    video_region: dict[str, int] | None = None
    video_status: str = "未录制视频"
    query_result: dict[str, object] | None = None

    def clear(self) -> None:
        self.title = ""
        self.prompt = ""
        self.query_text = ""
        self.design_steps = ""
        self.step_comment = ""
        self.prompt_template_key = "ct_validation"
        self.response_text = ""
        self.query_status = "未查询"
        self.image_selections = []
        self.video_path = None
        self.video_region = None
        self.video_status = "未录制视频"
        self.query_result = None


@dataclass(slots=True)
class SessionMetadataDraft:
    is_prs_recording: bool = True
    testcase_id: str = ""
    version_number: str = ""
    project: str = "Taichi"
    baseline_name: str = ""
    name: str = ""
    recorder_person: str = ""
    design_steps: str = ""
    preconditions: str = ""
    configuration_requirements: str = ""
    extra_devices: str = ""
    scope: str = "All"

    def to_dict(self) -> dict[str, object]:
        scope = self.scope if self.scope in SESSION_SCOPE_OPTIONS else "All"
        return {
            "is_prs_recording": self.is_prs_recording,
            "testcase_id": self.testcase_id.strip() if self.is_prs_recording else "",
            "version_number": self.version_number.strip() if self.is_prs_recording else "",
            "project": self.project.strip(),
            "baseline_name": self.baseline_name.strip(),
            "name": "" if self.is_prs_recording else self.name.strip(),
            "recorder_person": self.recorder_person.strip(),
            "design_steps": self.design_steps.strip(),
            "preconditions": self.preconditions.strip(),
            "configuration_requirements": self.configuration_requirements.strip(),
            "extra_devices": self.extra_devices.strip(),
            "scope": scope,
        }

    def validate(self) -> str | None:
        if self.is_prs_recording:
            if not self.testcase_id.strip():
                return "请输入 Testcase ID。"
            if not self.version_number.strip():
                return "请输入 Version Number。"
        else:
            return "当前版本仅支持 PRS 用例录制。"
        if self.project.strip() and self.project.strip() not in SESSION_PROJECT_OPTIONS:
            return "请选择合法的 Project。"
        if not self.recorder_person.strip():
            return "请输入录制人员。"
        if not self.design_steps.strip():
            return "请输入 Design Steps。"
        if self.scope not in SESSION_SCOPE_OPTIONS:
            return "请选择 Scope。"
        return None


class SessionMetadataDialog:
    def __init__(self, parent: tk.Misc, draft: SessionMetadataDraft | None = None, settings_store: SettingsStore | None = None) -> None:
        self.parent = parent
        self.draft = draft or SessionMetadataDraft()
        self.settings_store = settings_store or SettingsStore(Path("recorder_settings.json"))
        self.result: SessionMetadataDraft | None = None
        self.ai_running = False
        self._testcase_lookup_after_id: str | None = None
        self._testcase_lookup_token = 0
        self._baseline_lookup_token = 0

        self.window = tk.Toplevel(parent)
        self.window.title("录制元数据")
        self.window.geometry("800x820")
        self.window.minsize(740, 720)
        self.window.transient(parent)
        self.window.grab_set()

        self.is_prs_recording_var = tk.StringVar(value="是" if self.draft.is_prs_recording else "否")
        self.testcase_id_var = tk.StringVar(value=self.draft.testcase_id)
        self.version_number_var = tk.StringVar(value=self.draft.version_number)
        self.project_var = tk.StringVar(value=self.draft.project or "Taichi")
        self.baseline_name_var = tk.StringVar(value=self.draft.baseline_name)
        self.name_var = tk.StringVar(value=self.draft.name)
        self.recorder_person_var = tk.StringVar(value=self.draft.recorder_person)
        self.scope_var = tk.StringVar(value=self.draft.scope if self.draft.scope in SESSION_SCOPE_OPTIONS else "All")
        self.testcase_lookup_status_var = tk.StringVar(value="")
        self.testcase_lookup_details_var = tk.StringVar(value="")
        self.baseline_lookup_status_var = tk.StringVar(value="BaselineName 加载中...")

        self._build_ui()
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.lift()
        self.window.focus_force()
        self.window.after(0, self._load_baseline_names)

    def _build_ui(self) -> None:
        wrapper = ttk.Frame(self.window, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.columnconfigure(1, weight=1)
        wrapper.rowconfigure(7, weight=1)
        wrapper.rowconfigure(9, weight=1)
        wrapper.rowconfigure(10, weight=1)
        wrapper.rowconfigure(11, weight=1)

        ttk.Label(
            wrapper,
            text="开始录制前可填写本次录制相关元数据，这些字段之后也可以在 Session Viewer 中继续修改。当前版本仅支持 PRS 用例录制。",
            wraplength=640,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 12))

        ttk.Label(wrapper, text="是否PRS用例录制").grid(row=1, column=0, sticky=tk.W, pady=6)
        self.prs_combo = ttk.Combobox(
            wrapper,
            textvariable=self.is_prs_recording_var,
            state="readonly",
            values=[label for label, _value in PRS_RECORDING_OPTIONS],
            width=16,
        )
        self.prs_combo.grid(row=1, column=1, sticky=tk.W, pady=6)
        self.prs_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_prs_mode())

        ttk.Label(wrapper, text="Project").grid(row=2, column=0, sticky=tk.W, pady=6)
        self.project_combo = ttk.Combobox(
            wrapper,
            textvariable=self.project_var,
            state="readonly",
            values=SESSION_PROJECT_OPTIONS,
            width=20,
        )
        self.project_combo.grid(row=2, column=1, sticky=tk.W, pady=6)

        ttk.Label(wrapper, text="BaselineName").grid(row=3, column=0, sticky=tk.W, pady=6)
        baseline_frame = ttk.Frame(wrapper)
        baseline_frame.grid(row=3, column=1, sticky=tk.EW, pady=6)
        baseline_frame.columnconfigure(0, weight=1)
        self.baseline_name_combo = ttk.Combobox(baseline_frame, textvariable=self.baseline_name_var, state="readonly")
        self.baseline_name_combo.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(baseline_frame, textvariable=self.baseline_lookup_status_var).grid(row=1, column=0, sticky=tk.W, pady=(4, 0))

        self.primary_id_label = ttk.Label(wrapper, text="Testcase ID")
        self.primary_id_label.grid(row=4, column=0, sticky=tk.W, pady=6)
        testcase_frame = ttk.Frame(wrapper)
        testcase_frame.grid(row=4, column=1, sticky=tk.EW, pady=6)
        testcase_frame.columnconfigure(0, weight=1)

        self.primary_id_entry = ttk.Entry(testcase_frame, textvariable=self.testcase_id_var)
        self.primary_id_entry.grid(row=0, column=0, sticky=tk.EW)
        self.primary_id_entry.bind("<KeyRelease>", self._on_testcase_id_changed, add="+")
        self.primary_id_entry.bind("<FocusOut>", self._on_testcase_id_focus_out, add="+")

        ttk.Label(testcase_frame, textvariable=self.testcase_lookup_status_var).grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Label(testcase_frame, textvariable=self.testcase_lookup_details_var, wraplength=520, justify=tk.LEFT).grid(
            row=2,
            column=0,
            sticky=tk.W,
            pady=(2, 0),
        )

        self.secondary_id_label = ttk.Label(wrapper, text="Version Number")
        self.secondary_id_label.grid(row=5, column=0, sticky=tk.W, pady=6)
        self.secondary_id_entry = ttk.Entry(wrapper, textvariable=self.version_number_var)
        self.secondary_id_entry.grid(row=5, column=1, sticky=tk.EW, pady=6)

        self.name_label = ttk.Label(wrapper, text="Name")
        self.name_entry = ttk.Entry(wrapper, textvariable=self.name_var)

        ttk.Label(wrapper, text="录制人员").grid(row=6, column=0, sticky=tk.W, pady=6)
        ttk.Entry(wrapper, textvariable=self.recorder_person_var).grid(row=6, column=1, sticky=tk.EW, pady=6)

        ttk.Label(wrapper, text="Design Steps").grid(row=7, column=0, sticky=tk.NW, pady=6)
        self.design_steps_text = tk.Text(wrapper, height=10, wrap=tk.WORD, font=("Consolas", 10))
        self.design_steps_text.grid(row=7, column=1, sticky="nsew", pady=6)
        self.design_steps_text.insert("1.0", self.draft.design_steps)

        ttk.Label(wrapper, text="前置条件").grid(row=9, column=0, sticky=tk.NW, pady=6)
        self.preconditions_text = tk.Text(wrapper, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.preconditions_text.grid(row=9, column=1, sticky="nsew", pady=6)
        self.preconditions_text.insert("1.0", self.draft.preconditions)

        ttk.Label(wrapper, text="配置要求").grid(row=10, column=0, sticky=tk.NW, pady=6)
        self.configuration_requirements_text = tk.Text(wrapper, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.configuration_requirements_text.grid(row=10, column=1, sticky="nsew", pady=6)
        self.configuration_requirements_text.insert("1.0", self.draft.configuration_requirements)

        ttk.Label(wrapper, text="额外设备").grid(row=11, column=0, sticky=tk.NW, pady=6)
        self.extra_devices_text = tk.Text(wrapper, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.extra_devices_text.grid(row=11, column=1, sticky="nsew", pady=6)
        self.extra_devices_text.insert("1.0", self.draft.extra_devices)

        ttk.Label(
            wrapper,
            text="以上 3 项请尽量按‘词语/短语’填写，每行一个，例如：第一次启动、倾斜、systemphantom。",
            wraplength=560,
            justify=tk.LEFT,
        ).grid(row=12, column=1, sticky=tk.W, pady=(2, 0))

        ttk.Label(wrapper, text="Scope").grid(row=13, column=0, sticky=tk.W, pady=6)
        scope_combo = ttk.Combobox(wrapper, textvariable=self.scope_var, state="readonly", values=SESSION_SCOPE_OPTIONS, width=16)
        scope_combo.grid(row=13, column=1, sticky=tk.W, pady=6)

        self.ai_status_var = tk.StringVar(value="")
        ttk.Label(wrapper, textvariable=self.ai_status_var).grid(row=14, column=1, sticky=tk.W, pady=(2, 0))

        buttons = ttk.Frame(wrapper)
        buttons.grid(row=15, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side=tk.RIGHT)
        self.save_button = ttk.Button(buttons, text="开始录制", command=self.save)
        self.save_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.ai_button = ttk.Button(buttons, text="AI分析", command=self.run_ai_analysis)
        self.ai_button.pack(side=tk.RIGHT, padx=(0, 8))

        self._refresh_prs_mode()
        self.primary_id_entry.focus_set()

    def _is_prs_recording_selected(self) -> bool:
        return self.is_prs_recording_var.get().strip() != "否"

    def _refresh_prs_mode(self) -> None:
        is_prs = self._is_prs_recording_selected()
        if is_prs:
            self.name_var.set("")
            self.name_label.grid_remove()
            self.name_entry.grid_remove()
            self.primary_id_label.configure(text="Testcase ID")
            self.secondary_id_label.configure(text="Version Number")
            self.primary_id_label.grid(row=4, column=0, sticky=tk.W, pady=6)
            self.primary_id_entry.master.grid(row=4, column=1, sticky=tk.EW, pady=6)
            self.secondary_id_label.grid(row=5, column=0, sticky=tk.W, pady=6)
            self.secondary_id_entry.grid(row=5, column=1, sticky=tk.EW, pady=6)
            self.primary_id_entry.focus_set()
            self._schedule_testcase_lookup(immediate=True)
            return

        self.testcase_id_var.set("")
        self.version_number_var.set("")
        self.primary_id_label.grid_remove()
        self.primary_id_entry.master.grid_remove()
        self.secondary_id_label.grid_remove()
        self.secondary_id_entry.grid_remove()
        self.testcase_lookup_status_var.set("")
        self.testcase_lookup_details_var.set("")
        self.name_label.grid(row=4, column=0, sticky=tk.W, pady=6)
        self.name_entry.grid(row=4, column=1, sticky=tk.EW, pady=6)
        self.name_entry.focus_set()

    def _load_baseline_names(self) -> None:
        self._baseline_lookup_token += 1
        token = self._baseline_lookup_token
        connection_string = self.settings_store.load().prompt_db_connection_string.strip()
        if not connection_string:
            self.baseline_lookup_status_var.set("未配置数据库连接")
            self.baseline_name_combo.configure(values=[])
            return

        def worker() -> None:
            try:
                baseline_names = fetch_distinct_baseline_names(connection_string)
            except Exception as exc:
                self.window.after(0, lambda: self._finish_baseline_lookup_error(token, str(exc)))
                return
            self.window.after(0, lambda: self._finish_baseline_lookup_success(token, baseline_names))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_baseline_lookup_success(self, token: int, baseline_names: list[str]) -> None:
        if token != self._baseline_lookup_token:
            return
        combo_values = [""] + baseline_names
        self.baseline_name_combo.configure(values=combo_values)
        if baseline_names:
            self.baseline_lookup_status_var.set(f"已加载 {len(baseline_names)} 个 BaselineName")
            current_value = self.baseline_name_var.get().strip()
            if current_value and current_value not in baseline_names:
                self.baseline_name_combo.configure(values=["", *baseline_names, current_value])
        else:
            self.baseline_lookup_status_var.set("未查询到可用的 BaselineName")

    def _finish_baseline_lookup_error(self, token: int, message: str) -> None:
        if token != self._baseline_lookup_token:
            return
        self.baseline_name_combo.configure(values=[])
        self.baseline_lookup_status_var.set(f"BaselineName 加载失败: {message}")

    def _on_testcase_id_changed(self, _event: tk.Event | None = None) -> None:
        self._schedule_testcase_lookup(immediate=False)

    def _on_testcase_id_focus_out(self, _event: tk.Event | None = None) -> None:
        self._schedule_testcase_lookup(immediate=True)

    def _schedule_testcase_lookup(self, *, immediate: bool) -> None:
        if self._testcase_lookup_after_id is not None:
            try:
                self.window.after_cancel(self._testcase_lookup_after_id)
            except Exception:
                pass
            self._testcase_lookup_after_id = None

        testcase_id = self.testcase_id_var.get().strip()
        if not self._is_prs_recording_selected() or not testcase_id:
            self.testcase_lookup_status_var.set("")
            self.testcase_lookup_details_var.set("")
            return

        self.testcase_lookup_status_var.set("测试用例查询中...")
        self.testcase_lookup_details_var.set("")
        delay_ms = 0 if immediate else 350
        self._testcase_lookup_after_id = self.window.after(delay_ms, self._start_testcase_lookup)

    def _start_testcase_lookup(self) -> None:
        self._testcase_lookup_after_id = None
        testcase_id = self.testcase_id_var.get().strip()
        if not self._is_prs_recording_selected() or not testcase_id:
            self.testcase_lookup_status_var.set("")
            self.testcase_lookup_details_var.set("")
            return

        self._testcase_lookup_token += 1
        token = self._testcase_lookup_token
        connection_string = self.settings_store.load().prompt_db_connection_string.strip()

        def worker() -> None:
            try:
                record = fetch_latest_testcase_management_record(connection_string, testcase_id)
            except Exception as exc:
                self.window.after(0, lambda: self._finish_testcase_lookup_error(token, testcase_id, str(exc)))
                return
            self.window.after(0, lambda: self._finish_testcase_lookup_success(token, testcase_id, record))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_testcase_lookup_success(self, token: int, testcase_id: str, record) -> None:
        if token != self._testcase_lookup_token:
            return
        if testcase_id != self.testcase_id_var.get().strip():
            return
        if record is None:
            self.testcase_lookup_status_var.set("未找到该 Testcase ID 的数据库记录")
            self.testcase_lookup_details_var.set("")
            return

        self.testcase_lookup_status_var.set("已匹配到最新数据库记录")
        self.testcase_lookup_details_var.set(
            f"Status: {record.status or '-'} | Designer: {record.designer or '-'} | ScriptVersion: {record.script_version or '-'} | UpdateTime: {record.update_time or '-'}"
        )
        if record.script_version and not self.version_number_var.get().strip():
            self.version_number_var.set(record.script_version)

    def _finish_testcase_lookup_error(self, token: int, testcase_id: str, message: str) -> None:
        if token != self._testcase_lookup_token:
            return
        if testcase_id != self.testcase_id_var.get().strip():
            return
        self.testcase_lookup_status_var.set("数据库查询失败")
        self.testcase_lookup_details_var.set(message)

    def save(self) -> None:
        draft = SessionMetadataDraft(
            is_prs_recording=self._is_prs_recording_selected(),
            testcase_id=self.testcase_id_var.get().strip(),
            version_number=self.version_number_var.get().strip(),
            project=self.project_var.get().strip(),
            baseline_name=self.baseline_name_var.get().strip(),
            name=self.name_var.get().strip(),
            recorder_person=self.recorder_person_var.get().strip(),
            design_steps=self.design_steps_text.get("1.0", tk.END).strip(),
            preconditions=self.preconditions_text.get("1.0", tk.END).strip(),
            configuration_requirements=self.configuration_requirements_text.get("1.0", tk.END).strip(),
            extra_devices=self.extra_devices_text.get("1.0", tk.END).strip(),
            scope=self.scope_var.get().strip() if self.scope_var.get().strip() in SESSION_SCOPE_OPTIONS else "",
        )
        error_message = draft.validate()
        if error_message:
            messagebox.showerror("元数据未填写完整", error_message, parent=self.window)
            return
        payload = draft.to_dict()
        if should_prompt_ai_analysis(payload):
            if messagebox.askyesno("AI分析", "前置条件、配置要求、额外设备当前都为空，是否先让 AI 根据 Design Steps 生成建议？", parent=self.window):
                self._run_ai_analysis(save_after=True)
                return

        self._validate_with_ai_before_save(draft)

    def run_ai_analysis(self) -> None:
        self._run_ai_analysis(save_after=False)

    def _run_ai_analysis(self, save_after: bool) -> None:
        if self.ai_running:
            return

        draft = SessionMetadataDraft(
            is_prs_recording=self._is_prs_recording_selected(),
            testcase_id=self.testcase_id_var.get().strip(),
            version_number=self.version_number_var.get().strip(),
            project=self.project_var.get().strip(),
            baseline_name=self.baseline_name_var.get().strip(),
            name=self.name_var.get().strip(),
            recorder_person=self.recorder_person_var.get().strip(),
            design_steps=self.design_steps_text.get("1.0", tk.END).strip(),
            preconditions=self.preconditions_text.get("1.0", tk.END).strip(),
            configuration_requirements=self.configuration_requirements_text.get("1.0", tk.END).strip(),
            extra_devices=self.extra_devices_text.get("1.0", tk.END).strip(),
            scope=self.scope_var.get().strip() if self.scope_var.get().strip() in SESSION_SCOPE_OPTIONS else "",
        )
        error_message = draft.validate()
        if error_message:
            messagebox.showerror("元数据未填写完整", error_message, parent=self.window)
            return

        self.ai_running = True
        self.ai_status_var.set("AI分析中...")
        self.save_button.configure(state=tk.DISABLED)
        self.ai_button.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = analyze_session_metadata(self.settings_store, draft.to_dict())
            except Exception as exc:
                self.window.after(0, lambda: self._on_ai_analysis_failed(str(exc)))
                return
            self.window.after(0, lambda: self._on_ai_analysis_success(result, draft, save_after))

        threading.Thread(target=worker, daemon=True).start()

    def _on_ai_analysis_success(self, result, draft: SessionMetadataDraft, save_after: bool) -> None:
        self.ai_running = False
        self.ai_status_var.set("AI分析完成")
        self.save_button.configure(state=tk.NORMAL)
        self.ai_button.configure(state=tk.NORMAL)

        self._set_text_content(self.preconditions_text, merge_keyword_text(self.preconditions_text.get("1.0", tk.END), result.preconditions))
        self._set_text_content(
            self.configuration_requirements_text,
            merge_keyword_text(self.configuration_requirements_text.get("1.0", tk.END), result.configuration_requirements),
        )
        self._set_text_content(self.extra_devices_text, merge_keyword_text(self.extra_devices_text.get("1.0", tk.END), result.extra_devices))

        if save_after:
            refreshed_draft = SessionMetadataDraft(
                is_prs_recording=draft.is_prs_recording,
                testcase_id=draft.testcase_id,
                version_number=draft.version_number,
                project=draft.project,
                baseline_name=draft.baseline_name,
                name=draft.name,
                recorder_person=draft.recorder_person,
                design_steps=draft.design_steps,
                preconditions=self.preconditions_text.get("1.0", tk.END).strip(),
                configuration_requirements=self.configuration_requirements_text.get("1.0", tk.END).strip(),
                extra_devices=self.extra_devices_text.get("1.0", tk.END).strip(),
                scope=draft.scope,
            )
            self._finalize_save(refreshed_draft, result)
            return

        summary = build_missing_summary(result)
        if summary:
            messagebox.showinfo("AI分析建议", summary, parent=self.window)

    def _on_ai_analysis_failed(self, message: str) -> None:
        self.ai_running = False
        self.ai_status_var.set("AI分析失败")
        self.save_button.configure(state=tk.NORMAL)
        self.ai_button.configure(state=tk.NORMAL)
        messagebox.showerror("AI分析失败", message, parent=self.window)

    def _validate_with_ai_before_save(self, draft: SessionMetadataDraft) -> None:
        self.ai_running = True
        self.ai_status_var.set("AI校验中...")
        self.save_button.configure(state=tk.DISABLED)
        self.ai_button.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = analyze_session_metadata(self.settings_store, draft.to_dict())
            except Exception as exc:
                self.window.after(0, lambda: self._on_ai_validation_failed(draft, str(exc)))
                return
            self.window.after(0, lambda: self._finalize_save(draft, result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_ai_validation_failed(self, draft: SessionMetadataDraft, message: str) -> None:
        self.ai_running = False
        self.ai_status_var.set("AI校验失败")
        self.save_button.configure(state=tk.NORMAL)
        self.ai_button.configure(state=tk.NORMAL)
        if messagebox.askyesno("AI校验失败", f"AI 校验失败:\n{message}\n\n是否忽略并继续保存？", parent=self.window):
            self.result = draft
            self.window.destroy()

    def _finalize_save(self, draft: SessionMetadataDraft, result) -> None:
        self.ai_running = False
        self.ai_status_var.set("")
        self.save_button.configure(state=tk.NORMAL)
        self.ai_button.configure(state=tk.NORMAL)
        summary = build_missing_summary(result)
        if summary:
            should_continue = messagebox.askyesno(
                "AI校验建议",
                "AI 认为当前内容可能还有遗漏:\n\n" + summary + "\n\n是否仍然继续保存？",
                parent=self.window,
            )
            if not should_continue:
                return
        self.result = draft
        self.window.destroy()

    @staticmethod
    def _set_text_content(widget: tk.Text, text: str) -> None:
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)

    def cancel(self) -> None:
        self.result = None
        self.window.destroy()


class SettingsDialog:
    def __init__(self, parent: tk.Misc, settings_store: SettingsStore) -> None:
        self.parent = parent
        self.settings_store = settings_store
        self.settings = settings_store.load()
        self.saved = False
        self.playground_image_paths: list[Path] = []
        self.playground_inline_images: list[tuple[Image.Image, str]] = []
        self.playground_video_paths: list[Path] = []
        self.connectivity_running = False
        self.query_running = False
        self._settings_canvas: tk.Canvas | None = None
        self._settings_canvas_window: int | None = None

        self.window = tk.Toplevel(parent)
        self.window.title("Settings")
        self.window.geometry("1020x760")
        self.window.minsize(860, 620)

        self.endpoint_var = tk.StringVar(value=self.settings.endpoint)
        self.api_key_var = tk.StringVar(value=self.settings.api_key)
        self.model_var = tk.StringVar(value=self.settings.model)
        self.timeout_var = tk.StringVar(value=str(self.settings.timeout_seconds))
        self.temperature_var = tk.StringVar(value=str(self.settings.temperature))
        self.enable_thinking_var = tk.BooleanVar(value=self.settings.enable_thinking)
        self.video_frames_var = tk.StringVar(value=str(self.settings.video_frame_count))
        self.video_fps_var = tk.StringVar(value=str(self.settings.video_fps))
        self.send_video_directly_var = tk.BooleanVar(value=self.settings.send_video_directly)
        self.analysis_batch_size_var = tk.StringVar(value=str(self.settings.analysis_batch_size))
        self.send_fullscreen_var = tk.BooleanVar(value=self.settings.send_fullscreen_screenshots)
        self.ai_observation_excluded_process_var = tk.StringVar(value=self.settings.ai_observation_excluded_process_names)
        self.exclude_recorder_windows_var = tk.BooleanVar(value=self.settings.exclude_recorder_process_windows)
        self.use_remote_ai_service_var = tk.BooleanVar(value=self.settings.use_remote_ai_service)
        self.remote_ai_service_url_var = tk.StringVar(value=self.settings.remote_ai_service_url)
        self.remote_ai_service_api_key_var = tk.StringVar(value=self.settings.remote_ai_service_api_key)
        self.remote_ai_service_timeout_var = tk.StringVar(value=str(self.settings.remote_ai_service_timeout_seconds))
        self.show_design_steps_overlay_var = tk.BooleanVar(value=self.settings.show_design_steps_overlay)
        self.design_steps_overlay_width_var = tk.StringVar(value=str(self.settings.design_steps_overlay_width))
        self.design_steps_overlay_height_var = tk.StringVar(value=str(self.settings.design_steps_overlay_height))
        self.design_steps_overlay_bg_color_var = tk.StringVar(value=self.settings.design_steps_overlay_bg_color)
        self.design_steps_overlay_opacity_var = tk.StringVar(value=str(self.settings.design_steps_overlay_opacity))
        self.connection_status_var = tk.StringVar(value="AI 连接状态: 未检测")
        self.prompt_db_connection_var = tk.StringVar(value=self.settings.prompt_db_connection_string)
        self.checkpoint_prompt_table_var = tk.StringVar(value=self.settings.checkpoint_prompt_table)
        self.checkpoint_prompt_key_column_var = tk.StringVar(value=self.settings.checkpoint_prompt_key_column)
        self.checkpoint_prompt_label_column_var = tk.StringVar(value=self.settings.checkpoint_prompt_label_column)
        self.checkpoint_prompt_content_column_var = tk.StringVar(value=self.settings.checkpoint_prompt_content_column)

        self._build_ui()
        self.window.after(200, self.check_connection)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.window, padding=0)
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._settings_canvas = canvas

        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=scrollbar.set)

        wrapper = ttk.Frame(canvas, padding=16)
        wrapper.columnconfigure(0, weight=1)
        self._settings_canvas_window = canvas.create_window((0, 0), window=wrapper, anchor=tk.NW)

        wrapper.bind("<Configure>", self._on_settings_wrapper_configure)
        canvas.bind("<Configure>", self._on_settings_canvas_configure)
        canvas.bind_all("<MouseWheel>", self._on_settings_mousewheel, add="+")
        self.window.bind("<Destroy>", self._on_settings_window_destroy, add="+")

        notebook = ttk.Notebook(wrapper)
        notebook.grid(row=0, column=0, sticky="nsew")

        config_tab = ttk.Frame(notebook, padding=12)
        prompts_tab = ttk.Frame(notebook, padding=12)
        playground_tab = ttk.Frame(notebook, padding=12)
        notebook.add(config_tab, text="配置")
        notebook.add(prompts_tab, text="Prompts")
        notebook.add(playground_tab, text="Playground")

        self._build_config_tab(config_tab)
        self._build_prompts_tab(prompts_tab)
        self._build_playground_tab(playground_tab)

        buttons = ttk.Frame(wrapper)
        buttons.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=self.save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=self.window.destroy).pack(side=tk.RIGHT, padx=(0, 8))

    def _on_settings_wrapper_configure(self, _event: tk.Event | None = None) -> None:
        if self._settings_canvas is None:
            return
        self._settings_canvas.configure(scrollregion=self._settings_canvas.bbox("all"))

    def _on_settings_canvas_configure(self, event: tk.Event) -> None:
        if self._settings_canvas is None or self._settings_canvas_window is None:
            return
        self._settings_canvas.itemconfigure(self._settings_canvas_window, width=event.width)

    def _on_settings_mousewheel(self, event: tk.Event) -> None:
        if self._settings_canvas is None or not self.window.winfo_exists():
            return
        if self.window.focus_displayof() is None:
            return
        try:
            widget = self.window.winfo_containing(event.x_root, event.y_root)
        except Exception:
            widget = None
        if widget is None:
            return
        if not str(widget).startswith(str(self.window)):
            return
        delta = int(-event.delta / 120) if event.delta else 0
        if delta:
            self._settings_canvas.yview_scroll(delta, "units")

    def _on_settings_window_destroy(self, _event: tk.Event | None = None) -> None:
        if self._settings_canvas is None:
            return
        try:
            self._settings_canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass

    def _build_config_tab(self, parent: ttk.Frame) -> None:
        form = ttk.Frame(parent)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)

        rows = [
            ("Endpoint", self.endpoint_var),
            ("API Key", self.api_key_var),
            ("Model", self.model_var),
            ("Timeout(s)", self.timeout_var),
            ("Temperature", self.temperature_var),
            ("视频抽帧数", self.video_frames_var),
            ("视频录制FPS", self.video_fps_var),
            ("分析批次步数", self.analysis_batch_size_var),
        ]
        for row_index, (label, variable) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row_index, column=0, sticky=tk.W, pady=6)
            show = "*" if label == "API Key" else ""
            ttk.Entry(form, textvariable=variable, show=show).grid(row=row_index, column=1, sticky=tk.EW, pady=6)

        check_frame = ttk.Frame(parent)
        check_frame.pack(fill=tk.X, pady=(12, 0))
        ttk.Checkbutton(check_frame, text="enable_thinking", variable=self.enable_thinking_var).pack(side=tk.LEFT)
        ttk.Checkbutton(check_frame, text="直接发送原始视频给 AI", variable=self.send_video_directly_var).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(check_frame, text="发送全屏截图给 AI", variable=self.send_fullscreen_var).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(check_frame, textvariable=self.connection_status_var).pack(side=tk.LEFT, padx=12)
        ttk.Button(check_frame, text="检测连接", command=self.check_connection).pack(side=tk.RIGHT)

        ttk.Label(
            parent,
            text="提示: 默认会直接发送原始视频给模型；如服务端不兼容，可关闭该选项回退到旧的抽帧分析逻辑。双屏整图场景下默认只发送当前操作所在屏幕，也可切换为发送全屏截图。",
            wraplength=860,
        ).pack(anchor=tk.W, pady=(8, 0))

        observation_filter_frame = ttk.LabelFrame(parent, text="AI看图发送过滤", padding=12)
        observation_filter_frame.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            observation_filter_frame,
            text="以下进程名命中时，对应步骤截图不会发送给 AI看图。每行一个，默认排除 explorer 和 msedge。",
            wraplength=820,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        self.ai_observation_excluded_process_text = tk.Text(observation_filter_frame, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.ai_observation_excluded_process_text.pack(fill=tk.X, pady=(8, 0))
        _set_text(self.ai_observation_excluded_process_text, self.settings.ai_observation_excluded_process_names)

        remote_service_frame = ttk.LabelFrame(parent, text="远端共享 AI 服务", padding=12)
        remote_service_frame.pack(fill=tk.X, pady=(12, 0))
        remote_service_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            remote_service_frame,
            text="启用远端共享服务处理 AI 分析/方法建议/参数推荐",
            variable=self.use_remote_ai_service_var,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W)
        remote_rows = [
            ("服务地址", self.remote_ai_service_url_var, ""),
            ("服务 API Key", self.remote_ai_service_api_key_var, "*"),
            ("服务超时(s)", self.remote_ai_service_timeout_var, ""),
        ]
        for row_index, (label, variable, show) in enumerate(remote_rows, start=1):
            ttk.Label(remote_service_frame, text=label).grid(row=row_index, column=0, sticky=tk.W, pady=6)
            ttk.Entry(remote_service_frame, textvariable=variable, show=show).grid(row=row_index, column=1, sticky=tk.EW, pady=6)
        ttk.Label(
            remote_service_frame,
            text="启用后，Viewer 的 AI 分析、方法建议和参数推荐会统一走远端共享服务；AI Checkpoint 仍使用下方配置的模型 endpoint。",
            wraplength=820,
            justify=tk.LEFT,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        prompt_db_frame = ttk.LabelFrame(parent, text="AI Checkpoint 模板数据库", padding=12)
        prompt_db_frame.pack(fill=tk.X, pady=(12, 0))
        prompt_db_frame.columnconfigure(1, weight=1)
        database_rows = [
            ("MySQL 连接串", self.prompt_db_connection_var),
            ("模板表名", self.checkpoint_prompt_table_var),
            ("Key 列名", self.checkpoint_prompt_key_column_var),
            ("显示列名", self.checkpoint_prompt_label_column_var),
            ("内容列名", self.checkpoint_prompt_content_column_var),
        ]
        for row_index, (label, variable) in enumerate(database_rows):
            ttk.Label(prompt_db_frame, text=label).grid(row=row_index, column=0, sticky=tk.W, pady=6)
            ttk.Entry(prompt_db_frame, textvariable=variable).grid(row=row_index, column=1, sticky=tk.EW, pady=6)

        ttk.Label(
            prompt_db_frame,
            text=(
                "连接串示例: mysql+pymysql://root:password@130.147.129.203:3306/ATFrameworkDB?charset=utf8mb4\n"
                "默认表名为 agentprompt；如果 Key 列名 / 显示列名留空，会自动尝试常见列名。"
            ),
            wraplength=820,
            justify=tk.LEFT,
        ).grid(row=len(database_rows), column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        overlay_frame = ttk.LabelFrame(parent, text="Design Steps 悬浮窗", padding=12)
        overlay_frame.pack(fill=tk.X, pady=(12, 0))
        overlay_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            overlay_frame,
            text="录制时显示 Design Steps 悬浮窗",
            variable=self.show_design_steps_overlay_var,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W)
        overlay_rows = [
            ("宽度", self.design_steps_overlay_width_var),
            ("高度", self.design_steps_overlay_height_var),
            ("背景色", self.design_steps_overlay_bg_color_var),
            ("透明度(0-1)", self.design_steps_overlay_opacity_var),
        ]
        for row_index, (label, variable) in enumerate(overlay_rows, start=1):
            ttk.Label(overlay_frame, text=label).grid(row=row_index, column=0, sticky=tk.W, pady=6)
            ttk.Entry(overlay_frame, textvariable=variable).grid(row=row_index, column=1, sticky=tk.EW, pady=6)
        ttk.Label(
            overlay_frame,
            text="背景色支持 #RRGGBB；宽高为像素；透明度范围为 0 到 1。",
            wraplength=820,
            justify=tk.LEFT,
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        exclusion_frame = ttk.LabelFrame(parent, text="录制排除规则", padding=12)
        exclusion_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        ttk.Checkbutton(
            exclusion_frame,
            text="默认排除 Automation Recorder 自身进程的所有窗口和弹窗",
            variable=self.exclude_recorder_windows_var,
        ).pack(anchor=tk.W)
        ttk.Label(
            exclusion_frame,
            text="排除进程名关键字，每行一个；命中后该进程窗口内的键盘/鼠标/滚轮事件都不录制。",
            wraplength=840,
        ).pack(anchor=tk.W, pady=(10, 4))
        self.excluded_process_text = tk.Text(exclusion_frame, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.excluded_process_text.pack(fill=tk.X)
        _set_text(self.excluded_process_text, self.settings.excluded_process_names)

        ttk.Label(
            exclusion_frame,
            text="排除窗口关键字，每行一个；会匹配窗口标题和窗口类名。",
            wraplength=840,
        ).pack(anchor=tk.W, pady=(10, 4))
        self.excluded_window_text = tk.Text(exclusion_frame, height=5, wrap=tk.WORD, font=("Consolas", 10))
        self.excluded_window_text.pack(fill=tk.BOTH, expand=True)
        _set_text(self.excluded_window_text, self.settings.excluded_window_keywords)

    def _build_prompts_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Default System Prompt").pack(anchor=tk.W, pady=(4, 4))
        self.prompt_text = tk.Text(parent, height=10, wrap=tk.WORD, font=("Segoe UI", 10))
        self.prompt_text.pack(fill=tk.BOTH, expand=False)
        _set_text(self.prompt_text, self.settings.default_system_prompt)

        ttk.Label(parent, text="Viewer Analysis System Prompt").pack(anchor=tk.W, pady=(14, 4))
        self.analysis_prompt_text = tk.Text(parent, height=10, wrap=tk.WORD, font=("Segoe UI", 10))
        self.analysis_prompt_text.pack(fill=tk.BOTH, expand=False)
        _set_text(self.analysis_prompt_text, self.settings.analysis_system_prompt)

        ttk.Label(parent, text="Extra Headers JSON").pack(anchor=tk.W, pady=(14, 4))
        self.headers_text = tk.Text(parent, height=8, wrap=tk.WORD, font=("Consolas", 10))
        self.headers_text.pack(fill=tk.BOTH, expand=True)
        _set_text(self.headers_text, self.settings.extra_headers_json)

    def _build_playground_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        controls = ttk.Frame(parent)
        controls.pack(fill=tk.X)
        ttk.Button(controls, text="选择图片", command=self.add_playground_images).pack(side=tk.LEFT)
        ttk.Button(controls, text="从剪切板粘贴图片", command=self.paste_playground_images).pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="选择视频", command=self.add_playground_videos).pack(side=tk.LEFT)
        ttk.Button(controls, text="清空附件", command=self.clear_playground_attachments).pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="发送到模型", command=self.run_playground_query).pack(side=tk.RIGHT)

        self.attachment_var = tk.StringVar(value="未添加附件")
        ttk.Label(parent, textvariable=self.attachment_var).pack(anchor=tk.W, pady=(8, 8))

        attachment_frame = ttk.LabelFrame(parent, text="附件列表")
        attachment_frame.pack(fill=tk.BOTH, expand=False)
        self.attachment_list = tk.Listbox(attachment_frame, height=6)
        self.attachment_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        ttk.Label(parent, text="Playground System Prompt").pack(anchor=tk.W, pady=(12, 4))
        self.playground_system_prompt_text = tk.Text(parent, height=6, wrap=tk.WORD, font=("Segoe UI", 10))
        self.playground_system_prompt_text.pack(fill=tk.BOTH, expand=False)
        _set_text(self.playground_system_prompt_text, self.settings.default_system_prompt)

        self.playground_status_var = tk.StringVar(value="未发送")
        ttk.Label(parent, textvariable=self.playground_status_var).pack(anchor=tk.W, pady=(8, 4))

        ttk.Label(parent, text="模型回答").pack(anchor=tk.W, pady=(8, 4))
        self.playground_response_text = tk.Text(parent, height=16, wrap=tk.WORD, font=("Consolas", 10))
        self.playground_response_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(parent, text="对话输入").pack(anchor=tk.W, pady=(12, 4))
        self.playground_input_text = tk.Text(parent, height=10, wrap=tk.WORD, font=("Segoe UI", 11))
        self.playground_input_text.pack(fill=tk.BOTH, expand=False)

    def save(self) -> None:
        try:
            settings = Settings(
                endpoint=self.endpoint_var.get().strip(),
                api_key=self.api_key_var.get().strip(),
                model=self.model_var.get().strip(),
                timeout_seconds=int(self.timeout_var.get().strip()),
                temperature=float(self.temperature_var.get().strip()),
                enable_thinking=self.enable_thinking_var.get(),
                default_system_prompt=self.prompt_text.get("1.0", tk.END).strip(),
                analysis_system_prompt=self.analysis_prompt_text.get("1.0", tk.END).strip(),
                extra_headers_json=self.headers_text.get("1.0", tk.END).strip() or "{}",
                video_frame_count=int(self.video_frames_var.get().strip()),
                video_fps=int(self.video_fps_var.get().strip()),
                send_video_directly=self.send_video_directly_var.get(),
                analysis_batch_size=int(self.analysis_batch_size_var.get().strip()),
                send_fullscreen_screenshots=self.send_fullscreen_var.get(),
                ai_observation_excluded_process_names=self.ai_observation_excluded_process_text.get("1.0", tk.END).strip(),
                exclude_recorder_process_windows=self.exclude_recorder_windows_var.get(),
                excluded_process_names=self.excluded_process_text.get("1.0", tk.END).strip(),
                excluded_window_keywords=self.excluded_window_text.get("1.0", tk.END).strip(),
                use_remote_ai_service=self.use_remote_ai_service_var.get(),
                remote_ai_service_url=self.remote_ai_service_url_var.get().strip(),
                remote_ai_service_api_key=self.remote_ai_service_api_key_var.get().strip(),
                remote_ai_service_timeout_seconds=int(self.remote_ai_service_timeout_var.get().strip()),
                show_design_steps_overlay=self.show_design_steps_overlay_var.get(),
                design_steps_overlay_width=int(self.design_steps_overlay_width_var.get().strip()),
                design_steps_overlay_height=int(self.design_steps_overlay_height_var.get().strip()),
                design_steps_overlay_bg_color=self.design_steps_overlay_bg_color_var.get().strip(),
                design_steps_overlay_opacity=float(self.design_steps_overlay_opacity_var.get().strip()),
                prompt_db_connection_string=self.prompt_db_connection_var.get().strip(),
                checkpoint_prompt_table=self.checkpoint_prompt_table_var.get().strip() or "agentprompt",
                checkpoint_prompt_key_column=self.checkpoint_prompt_key_column_var.get().strip(),
                checkpoint_prompt_label_column=self.checkpoint_prompt_label_column_var.get().strip(),
                checkpoint_prompt_content_column=self.checkpoint_prompt_content_column_var.get().strip() or "PromptContent",
            )
            SettingsStore.parse_extra_headers(settings.extra_headers_json)
            SettingsStore.parse_pattern_list(settings.ai_observation_excluded_process_names)
            SettingsStore.parse_pattern_list(settings.excluded_process_names)
            SettingsStore.parse_pattern_list(settings.excluded_window_keywords)
            if settings.design_steps_overlay_width < 320:
                raise ValueError("Design Steps 悬浮窗宽度不能小于 320")
            if settings.design_steps_overlay_height < 160:
                raise ValueError("Design Steps 悬浮窗高度不能小于 160")
            if not 0.1 <= settings.design_steps_overlay_opacity <= 1.0:
                raise ValueError("Design Steps 悬浮窗透明度必须在 0.1 到 1 之间")
            self.window.winfo_rgb(settings.design_steps_overlay_bg_color)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.window)
            return

        self.settings_store.save(settings)
        self.saved = True
        self.window.destroy()

    def check_connection(self) -> None:
        if self.connectivity_running:
            return
        self.connectivity_running = True
        self.connection_status_var.set("AI 连接状态: 检测中...")

        def worker() -> None:
            try:
                settings = self._build_settings_from_ui()
                if settings.use_remote_ai_service:
                    ok, message = RemoteAIServiceClient(settings).check_connection()
                else:
                    client = OpenAICompatibleAIClient(settings)
                    ok, message = client.check_connection()
            except Exception as exc:
                ok, message = False, str(exc)
            self.window.after(0, lambda: self._on_connection_checked(ok, message))

        threading.Thread(target=worker, daemon=True).start()

    def add_playground_images(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.window,
            title="选择图片",
            filetypes=[("Image Files", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"), ("All Files", "*.*")],
        )
        for path in paths:
            image_path = Path(path)
            if image_path not in self.playground_image_paths:
                self.playground_image_paths.append(image_path)
        self._refresh_playground_attachments()

    def paste_playground_images(self) -> None:
        clipboard_data = ImageGrab.grabclipboard()
        if isinstance(clipboard_data, Image.Image):
            self.playground_inline_images.append((clipboard_data.copy(), f"clipboard_{int(time.time() * 1000)}.png"))
            self._refresh_playground_attachments()
            return
        if isinstance(clipboard_data, list):
            for item in clipboard_data:
                path = Path(item)
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp"} and path not in self.playground_image_paths:
                    self.playground_image_paths.append(path)
            self._refresh_playground_attachments()
            return
        messagebox.showinfo("提示", "剪切板中没有可用图片。", parent=self.window)

    def add_playground_videos(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.window,
            title="选择视频",
            filetypes=[("Video Files", "*.mp4;*.mov;*.avi;*.mkv;*.webm"), ("All Files", "*.*")],
        )
        for path in paths:
            video_path = Path(path)
            if video_path not in self.playground_video_paths:
                self.playground_video_paths.append(video_path)
        self._refresh_playground_attachments()

    def clear_playground_attachments(self) -> None:
        self.playground_image_paths = []
        self.playground_inline_images = []
        self.playground_video_paths = []
        self._refresh_playground_attachments()

    def run_playground_query(self) -> None:
        if self.query_running:
            return
        prompt = self.playground_input_text.get("1.0", tk.END).strip()
        if not prompt:
            messagebox.showerror("发送失败", "请输入对话内容。", parent=self.window)
            return

        self.query_running = True
        self.playground_status_var.set("发送中...")

        def worker() -> None:
            try:
                client = OpenAICompatibleAIClient(self._build_settings_from_ui())
                combined_video = self.playground_video_paths[0] if self.playground_video_paths else None
                result = client.query(
                    user_prompt=prompt,
                    image_paths=self.playground_image_paths,
                    inline_images=[item[0] for item in self.playground_inline_images],
                    video_path=combined_video,
                    system_prompt=self.playground_system_prompt_text.get("1.0", tk.END).strip() or self.prompt_text.get("1.0", tk.END).strip(),
                )
            except Exception as exc:
                self.window.after(0, lambda: self._on_playground_query_failed(str(exc)))
                return
            self.window.after(0, lambda: self._on_playground_query_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _build_settings_from_ui(self) -> Settings:
        return Settings(
            endpoint=self.endpoint_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
            timeout_seconds=int(self.timeout_var.get().strip()),
            temperature=float(self.temperature_var.get().strip()),
            enable_thinking=self.enable_thinking_var.get(),
            default_system_prompt=self.prompt_text.get("1.0", tk.END).strip(),
            analysis_system_prompt=self.analysis_prompt_text.get("1.0", tk.END).strip(),
            extra_headers_json=self.headers_text.get("1.0", tk.END).strip() or "{}",
            video_frame_count=int(self.video_frames_var.get().strip()),
            video_fps=int(self.video_fps_var.get().strip()),
            send_video_directly=self.send_video_directly_var.get(),
            analysis_batch_size=int(self.analysis_batch_size_var.get().strip()),
            send_fullscreen_screenshots=self.send_fullscreen_var.get(),
            ai_observation_excluded_process_names=self.ai_observation_excluded_process_text.get("1.0", tk.END).strip(),
            exclude_recorder_process_windows=self.exclude_recorder_windows_var.get(),
            excluded_process_names=self.excluded_process_text.get("1.0", tk.END).strip(),
            excluded_window_keywords=self.excluded_window_text.get("1.0", tk.END).strip(),
            use_remote_ai_service=self.use_remote_ai_service_var.get(),
            remote_ai_service_url=self.remote_ai_service_url_var.get().strip(),
            remote_ai_service_api_key=self.remote_ai_service_api_key_var.get().strip(),
            remote_ai_service_timeout_seconds=int(self.remote_ai_service_timeout_var.get().strip()),
            prompt_db_connection_string=self.prompt_db_connection_var.get().strip(),
            checkpoint_prompt_table=self.checkpoint_prompt_table_var.get().strip() or "agentprompt",
            checkpoint_prompt_key_column=self.checkpoint_prompt_key_column_var.get().strip(),
            checkpoint_prompt_label_column=self.checkpoint_prompt_label_column_var.get().strip(),
            checkpoint_prompt_content_column=self.checkpoint_prompt_content_column_var.get().strip() or "PromptContent",
        )

    def _on_connection_checked(self, ok: bool, message: str) -> None:
        self.connectivity_running = False
        prefix = "连接状态: 正常" if ok else "连接状态: 失败"
        self.connection_status_var.set(f"{prefix} | {message}")

    def _refresh_playground_attachments(self) -> None:
        self.attachment_list.delete(0, tk.END)
        for path in self.playground_image_paths:
            self.attachment_list.insert(tk.END, f"image: {path}")
        for _, label in self.playground_inline_images:
            self.attachment_list.insert(tk.END, f"image(clipboard): {label}")
        for path in self.playground_video_paths:
            self.attachment_list.insert(tk.END, f"video: {path}")
        total = len(self.playground_image_paths) + len(self.playground_inline_images)
        self.attachment_var.set(f"图片 {total} 张 | 视频 {len(self.playground_video_paths)} 个")

    def _on_playground_query_success(self, result: dict[str, object]) -> None:
        self.query_running = False
        self.playground_status_var.set("发送完成")
        _set_text(self.playground_response_text, str(result.get("response_text", "")))

    def _on_playground_query_failed(self, message: str) -> None:
        self.query_running = False
        self.playground_status_var.set("发送失败")
        messagebox.showerror("发送失败", message, parent=self.window)


class CommentDialog:
    def __init__(self, parent: tk.Misc, engine: RecorderEngine) -> None:
        self.parent = parent
        self.engine = engine
        self.selection = None
        self._parent_was_iconic_on_open = self.parent.wm_state() == "iconic"

        self.window = tk.Toplevel(parent)
        self.window.title("添加 Comment")
        self.window.geometry("900x720")
        self.window.minsize(760, 620)
        self.window.transient(parent)
        self.window.grab_set()

        self._build_ui()
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.bind("<Control-s>", lambda _event: self.save(), add="+")
        self.window.bind("<Control-Return>", lambda _event: self.save(), add="+")
        self.window.lift()
        self.window.focus_force()
        self.window.after(0, self.capture_region)

    def _build_ui(self) -> None:
        wrapper = ttk.Frame(self.window, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(wrapper)
        top.pack(fill=tk.X)
        ttk.Button(top, text="选择截图区域", command=self.capture_region).pack(side=tk.LEFT)
        self.selection_var = tk.StringVar(value="尚未选择区域")
        ttk.Label(top, textvariable=self.selection_var).pack(side=tk.LEFT, padx=12)

        preview_frame = ttk.LabelFrame(wrapper, text="截图预览")
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 12))
        self.preview_view = ZoomableImageView(preview_frame, empty_text="请先选择截图区域")
        self.preview_view.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        ttk.Label(wrapper, text="Comment 文本").pack(anchor=tk.W)
        self.note_text = tk.Text(wrapper, height=10, wrap=tk.WORD, font=("Segoe UI", 11))
        self.note_text.pack(fill=tk.BOTH, expand=False, pady=(4, 12))

        buttons = ttk.Frame(wrapper)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="保存", command=self.save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=self._close).pack(side=tk.RIGHT, padx=(0, 8))

    def capture_region(self) -> None:
        self.selection = _select_region_with_window_management(
            self.parent,
            self.window,
            "选择 Comment 截图区域",
            dialog_hide_mode="withdraw",
            parent_restore_mode="keep_iconified",
        )

        if not self.selection:
            return

        region = self.selection.to_region_dict()
        self.selection_var.set(f"区域: {region['width']}x{region['height']} @ ({region['left']}, {region['top']})")
        self.preview_view.set_image(self.selection.image)

    def save(self) -> None:
        note = self.note_text.get("1.0", tk.END).strip()
        if not self.selection:
            messagebox.showerror("保存失败", "请先选择截图区域。", parent=self.window)
            return
        if not note:
            messagebox.showerror("保存失败", "请输入 comment 文本。", parent=self.window)
            return

        self.engine.add_comment_with_media(note, self.selection.image, self.selection.to_region_dict())
        self._close()

    def _close(self) -> None:
        if not self._parent_was_iconic_on_open:
            self.parent.deiconify()
            self.parent.lift()
        self.window.destroy()


class WaitForImageDialog:
    def __init__(self, parent: tk.Misc, engine: RecorderEngine) -> None:
        self.parent = parent
        self.engine = engine
        self.selection = None
        self._parent_was_iconic_on_open = self.parent.wm_state() == "iconic"

        self.window = tk.Toplevel(parent)
        self.window.title("添加等待事件")
        self.window.geometry("900x720")
        self.window.minsize(760, 620)
        self.window.transient(parent)
        self.window.grab_set()
        self.timeout_seconds_var = tk.StringVar(value="120")

        self._build_ui()
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.bind("<Control-s>", lambda _event: self.save(), add="+")
        self.window.bind("<Control-Return>", lambda _event: self.save(), add="+")
        self.window.lift()
        self.window.focus_force()
        self.window.after(0, self.capture_region)

    def _build_ui(self) -> None:
        wrapper = ttk.Frame(self.window, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(2, weight=1)

        header = ttk.Frame(wrapper)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="添加等待事件", font=("Segoe UI", 13, "bold")).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            header,
            text="框选等待区域后，可填写等待说明和最大等待时间，再保存为新的等待事件。",
            justify=tk.LEFT,
            wraplength=820,
        ).grid(row=1, column=0, sticky=tk.W, pady=(6, 0))

        top = ttk.Frame(wrapper)
        top.grid(row=1, column=0, sticky="ew", pady=(14, 12))
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="选择等待区域", command=self.capture_region).grid(row=0, column=0, sticky=tk.W)
        self.selection_var = tk.StringVar(value="尚未选择区域")
        ttk.Label(top, textvariable=self.selection_var, wraplength=640, justify=tk.LEFT).grid(row=0, column=1, sticky="ew", padx=(12, 0))

        content = ttk.Frame(wrapper)
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)

        preview_frame = ttk.LabelFrame(content, text="等待区域截图预览")
        preview_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.preview_view = ZoomableImageView(preview_frame, empty_text="请先选择等待区域")
        self.preview_view.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        form_frame = ttk.LabelFrame(content, text="等待配置")
        form_frame.grid(row=0, column=1, sticky="nsew")
        form_frame.columnconfigure(1, weight=1)
        form_frame.rowconfigure(1, weight=1)

        ttk.Label(form_frame, text="最大等待时间").grid(row=0, column=0, sticky=tk.W, padx=12, pady=(12, 6))
        timeout_input = ttk.Frame(form_frame)
        timeout_input.grid(row=0, column=1, sticky="w", padx=12, pady=(12, 6))
        ttk.Entry(timeout_input, textvariable=self.timeout_seconds_var, width=10).pack(side=tk.LEFT)
        ttk.Label(timeout_input, text="秒").pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(form_frame, text="等待说明").grid(row=1, column=0, sticky=tk.NW, padx=12, pady=(4, 12))
        self.note_text = tk.Text(form_frame, height=5, wrap=tk.WORD, font=("Segoe UI", 11))
        self.note_text.grid(row=1, column=1, sticky="nsew", padx=(12, 12), pady=(4, 12))
        self.note_text.insert("1.0", "等待此区域中的目标图片出现")

        info_frame = ttk.Frame(wrapper)
        info_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(
            info_frame,
            text="说明: 当前第一版仅记录等待图片步骤，会保存框选范围、截图和最大等待时间，用于后续人工审查或转换。",
            wraplength=820,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        buttons = ttk.Frame(wrapper)
        buttons.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(buttons, text="保存", command=self.save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=self._close).pack(side=tk.RIGHT, padx=(0, 8))

    def capture_region(self) -> None:
        self.selection = _select_region_with_window_management(
            self.parent,
            self.window,
            "选择等待事件区域",
            dialog_hide_mode="withdraw",
            parent_restore_mode="keep_iconified",
        )

        if not self.selection:
            return

        region = self.selection.to_region_dict()
        self.selection_var.set(f"区域: {region['width']}x{region['height']} @ ({region['left']}, {region['top']})")
        self.preview_view.set_image(self.selection.image)

    def save(self) -> None:
        note = self.note_text.get("1.0", tk.END).strip()
        if not self.selection:
            messagebox.showerror("保存失败", "请先选择等待区域。", parent=self.window)
            return
        if not note:
            messagebox.showerror("保存失败", "请输入等待说明。", parent=self.window)
            return
        try:
            timeout_seconds = int(self.timeout_seconds_var.get().strip())
        except ValueError:
            messagebox.showerror("保存失败", "最大等待时间必须是整数秒。", parent=self.window)
            return
        if timeout_seconds <= 0:
            messagebox.showerror("保存失败", "最大等待时间必须大于 0 秒。", parent=self.window)
            return

        self.engine.add_wait_for_image_with_media(note, self.selection.image, self.selection.to_region_dict(), timeout_seconds=timeout_seconds)
        self._close()

    def _close(self) -> None:
        if not self._parent_was_iconic_on_open:
            self.parent.deiconify()
            self.parent.lift()
        self.window.destroy()


class AICheckpointDialog:
    def __init__(
        self,
        parent: tk.Misc,
        engine: RecorderEngine,
        settings_store: SettingsStore,
        draft: AICheckpointDraft,
        save_mode: str = "create",
        historical_screenshots_dir: Path | None = None,
    ) -> None:
        self.parent = parent
        self.engine = engine
        self.settings_store = settings_store
        self._parent_state_on_open = self.parent.wm_state() if self.parent.winfo_exists() else "normal"

        self.draft = draft
        self.save_mode = save_mode
        self.historical_screenshots_dir = historical_screenshots_dir.resolve() if isinstance(historical_screenshots_dir, Path) else None
        self.image_selections: list[tuple[Path, dict[str, int]]] = list(draft.image_selections)
        self.video_path: Path | None = draft.video_path
        self.video_region: dict[str, int] | None = draft.video_region
        self.video_recorder: RegionVideoRecorder | None = None
        self.query_result: dict[str, object] | None = draft.query_result
        self.saved = False
        self.result_payload: dict[str, object] | None = None
        self.preview_views: list[ZoomableImageView] = []
        self._middle_pane_ratio_initialized = False

        self.window = tk.Toplevel(parent)
        self.window.title("AI Checkpoint")
        self.window.geometry("1180x860")
        self.window.minsize(980, 760)
        self.window.grab_set()

        self.title_var = tk.StringVar(value=draft.title)
        self.media_var = tk.StringVar(value="尚未选择截图或视频")
        self.query_status_var = tk.StringVar(value=draft.query_status)
        self.video_status_var = tk.StringVar(value=draft.video_status)
        self.prompt_template_var = tk.StringVar(value=draft.prompt_template_key or "ct_validation")
        self.last_effective_prompt = draft.prompt
        self.prompt_template_options: list[AICheckpointPromptTemplateOption] = []
        self.prompt_template_lookup: dict[str, AICheckpointPromptTemplateOption] = {}
        self.prompt_template_label_lookup: dict[str, str] = {}
        self.prompt_template_load_error: str | None = None
        self.default_design_steps = self._get_default_design_steps()

        self._reload_prompt_templates(initial_key=draft.prompt_template_key or "")

        self._build_ui()
        _set_text(self.query_text, draft.query_text)
        _set_text(self.design_steps_text, draft.design_steps or self.default_design_steps)
        _set_text(self.step_comment_text, draft.step_comment)
        _set_text(self.response_text, draft.response_text)
        self._refresh_media_summary()
        self._restore_previews()
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.lift()
        self.window.focus_force()
        self.window.after(0, self._capture_initial_image_if_needed)

    def _build_ui(self) -> None:
        wrapper = ttk.Frame(self.window, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.columnconfigure(0, weight=1)

        form = ttk.Frame(wrapper)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="期望结果:").grid(row=0, column=0, sticky=tk.W, pady=6)
        ttk.Entry(form, textvariable=self.title_var).grid(row=0, column=1, sticky=tk.EW, pady=6)

        controls = ttk.Frame(wrapper)
        controls.pack(fill=tk.X, pady=(8, 8))
        ttk.Button(controls, text="添加截图", command=self.add_image).pack(side=tk.LEFT)
        ttk.Button(controls, text="添加历史截图", command=self.add_historical_image).pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="清空截图", command=self.clear_images).pack(side=tk.LEFT)
        ttk.Button(controls, text="开始视频录制", command=self.start_video).pack(side=tk.LEFT)
        ttk.Button(controls, text="停止视频录制", command=self.stop_video).pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="清空视频", command=self.clear_video).pack(side=tk.LEFT)
        ttk.Button(controls, text="Clear", command=self.clear_ai_history).pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(controls, text="Settings", command=self.open_settings).pack(side=tk.RIGHT)

        ttk.Label(wrapper, textvariable=self.media_var).pack(anchor=tk.W)
        ttk.Label(wrapper, textvariable=self.video_status_var).pack(anchor=tk.W, pady=(2, 0))

        middle = ttk.Panedwindow(wrapper, orient=tk.HORIZONTAL)
        middle.pack(fill=tk.BOTH, expand=True, pady=(10, 10))
        middle.bind("<Configure>", lambda _event: self._set_middle_paned_ratio(middle, 0.4), add="+")

        left = ttk.Frame(middle)
        right = ttk.Frame(middle)
        middle.add(left, weight=2)
        middle.add(right, weight=3)

        preview_frame = ttk.LabelFrame(left, text="媒体预览")
        preview_frame.pack(fill=tk.BOTH, expand=True)

        self.screenshot_preview_container = ttk.Frame(preview_frame)
        self.screenshot_preview_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        self.screenshot_preview_canvas = tk.Canvas(self.screenshot_preview_container, highlightthickness=0)
        self.screenshot_preview_scrollbar = tk.Scrollbar(
            self.screenshot_preview_container,
            orient=tk.VERTICAL,
            command=self.screenshot_preview_canvas.yview,
            width=AI_CHECKPOINT_SCROLLBAR_WIDTH,
            relief=tk.SOLID,
            borderwidth=1,
            background="#d9d9d9",
            activebackground="#bfbfbf",
            troughcolor="#f0f0f0",
        )
        self.screenshot_preview_canvas.configure(yscrollcommand=self.screenshot_preview_scrollbar.set)
        self.screenshot_preview_scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.screenshot_preview_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.screenshot_preview_inner = ttk.Frame(self.screenshot_preview_canvas)
        self.screenshot_preview_inner.columnconfigure(0, weight=1)
        self.screenshot_preview_window = self.screenshot_preview_canvas.create_window(
            (0, 0),
            window=self.screenshot_preview_inner,
            anchor=tk.NW,
        )
        self.screenshot_preview_inner.bind("<Configure>", self._on_screenshot_preview_inner_configure)
        self.screenshot_preview_canvas.bind("<Configure>", self._on_screenshot_preview_canvas_configure)

        for slot_index in range(MAX_AI_CHECKPOINT_IMAGES):
            shot_frame = ttk.LabelFrame(self.screenshot_preview_inner, text=f"截图 {slot_index + 1}")
            shot_frame.grid(row=slot_index, column=0, sticky="nsew", pady=(0, 8) if slot_index < MAX_AI_CHECKPOINT_IMAGES - 1 else 0)
            shot_frame.configure(height=AI_CHECKPOINT_PREVIEW_HEIGHT)
            shot_frame.grid_propagate(False)
            preview_view = ZoomableImageView(shot_frame, empty_text=f"尚未选择截图 {slot_index + 1}")
            preview_view.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
            self._bind_preview_context_menu(preview_view, slot_index)
            self.preview_views.append(preview_view)

        self.video_preview_container = ttk.Frame(preview_frame)
        self.video_preview_frame = ttk.LabelFrame(self.video_preview_container, text="最近录制视频预览")
        self.video_preview_frame.pack(fill=tk.BOTH, expand=True)
        self.video_preview_view = ZoomableImageView(self.video_preview_frame, empty_text="尚未录制视频")
        self.video_preview_view.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        right_content = ttk.Frame(right)
        right_content.pack(fill=tk.BOTH, expand=True)
        right_content.columnconfigure(0, weight=1, uniform="ai_checkpoint_right")
        right_content.columnconfigure(1, weight=1, uniform="ai_checkpoint_right")
        right_content.rowconfigure(0, weight=1, uniform="ai_checkpoint_right_row")
        right_content.rowconfigure(1, weight=1, uniform="ai_checkpoint_right_row")

        preset_frame = ttk.LabelFrame(right_content, text="Prompt / Query")
        preset_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        template_row = ttk.Frame(preset_frame)
        template_row.pack(fill=tk.X, padx=8, pady=(8, 6))
        ttk.Label(template_row, text="模板").pack(side=tk.LEFT)
        self.prompt_template_combo = ttk.Combobox(
            template_row,
            state="readonly",
        )
        self.prompt_template_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self.prompt_template_combo.bind("<<ComboboxSelected>>", self._on_prompt_template_changed)
        self._refresh_prompt_template_combo()

        ttk.Label(preset_frame, text="Query").pack(anchor=tk.W, padx=8)
        self.query_text = tk.Text(preset_frame, height=8, wrap=tk.WORD, font=("Segoe UI", 11))
        self.query_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        query_bar = ttk.Frame(preset_frame)
        query_bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(query_bar, text="Query", command=self.run_query).pack(side=tk.LEFT)
        ttk.Label(query_bar, textvariable=self.query_status_var).pack(side=tk.LEFT, padx=12)

        result_frame = ttk.LabelFrame(right_content, text="查询结果")
        result_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))
        self.response_text = tk.Text(result_frame, height=10, wrap=tk.WORD, font=("Consolas", 10))
        self.response_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        design_steps_frame = ttk.LabelFrame(right_content, text="Design Steps")
        design_steps_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(6, 0))
        self.design_steps_text = tk.Text(design_steps_frame, height=10, wrap=tk.WORD, font=("Consolas", 10))
        self.design_steps_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        step_comment_frame = ttk.LabelFrame(right_content, text="Step Description")
        step_comment_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(6, 0))
        self.step_comment_text = tk.Text(step_comment_frame, height=10, wrap=tk.WORD, font=("Consolas", 10))
        self.step_comment_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        buttons = ttk.Frame(wrapper)
        buttons.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(buttons, text="保存 Checkpoint", command=self.save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=self._close).pack(side=tk.RIGHT, padx=(0, 8))

    def _capture_initial_image_if_needed(self) -> None:
        if self.image_selections or self.video_path:
            return
        if not self.window.winfo_exists():
            return
        self.add_image()

    def _set_middle_paned_ratio(self, paned: ttk.Panedwindow, left_ratio: float) -> None:
        if self._middle_pane_ratio_initialized:
            return
        try:
            total_width = paned.winfo_width()
            if total_width > 1:
                paned.sashpos(0, int(total_width * left_ratio))
                self._middle_pane_ratio_initialized = True
        except Exception:
            return

    def _get_query_text(self) -> str:
        return self.query_text.get("1.0", tk.END).strip()

    def _get_design_steps_text(self) -> str:
        return self.design_steps_text.get("1.0", tk.END).strip()

    def _get_step_comment_text(self) -> str:
        return self.step_comment_text.get("1.0", tk.END).strip()

    def _get_default_design_steps(self) -> str:
        metadata = self.engine.store.data.metadata if self.engine.store.data else None
        if metadata is None:
            return ""
        return str(getattr(metadata, "design_steps", "") or "").strip()

    def _build_effective_query_text(self) -> str:
        query = self._get_query_text()
        sections: list[str] = []
        if query:
            sections.append(query)
        return "\n\n".join(section for section in sections if section).strip()

    def _on_prompt_template_changed(self, _event: tk.Event | None = None) -> None:
        selected_label = self.prompt_template_combo.get().strip()
        selected_key = self.prompt_template_label_lookup.get(selected_label, "")
        if selected_key:
            self.prompt_template_var.set(selected_key)
            return
        if self.prompt_template_options:
            self.prompt_template_var.set(self.prompt_template_options[0].key)

    def _build_prompt_from_selection(self) -> str:
        query = self._build_effective_query_text()
        template_key = self.prompt_template_var.get().strip() or "ct_validation"
        template_option = self.prompt_template_lookup.get(template_key)
        if template_option is None and self.prompt_template_options:
            template_option = self.prompt_template_options[0]
            self.prompt_template_var.set(template_option.key)
        if template_option is None:
            return query

        template_text = template_option.prompt_template
        if template_option.key == "empty":
            return query
        if "{query}" in template_text:
            return template_text.replace("{query}", query)
        if query and query not in template_text:
            return f"{template_text.rstrip()}\n\n{query}".strip()
        return template_text

    def _reload_prompt_templates(self, initial_key: str = "") -> None:
        selected_key = initial_key.strip() or self.prompt_template_var.get().strip()
        self.prompt_template_load_error = None

        try:
            records = load_checkpoint_prompt_templates(self.settings_store.load())
        except Exception as exc:
            self.prompt_template_load_error = str(exc)
            records = []

        if records:
            options = _convert_prompt_template_records(records)
        else:
            options = _get_builtin_ai_checkpoint_prompt_templates()

        self.prompt_template_options = options
        self.prompt_template_lookup = {option.key: option for option in options}
        self.prompt_template_label_lookup = {option.label: option.key for option in options}

        if selected_key not in self.prompt_template_lookup and options:
            selected_key = options[0].key
        self.prompt_template_var.set(selected_key)

    def _refresh_prompt_template_combo(self) -> None:
        labels = [option.label for option in self.prompt_template_options]
        self.prompt_template_combo.configure(values=labels)
        selected_key = self.prompt_template_var.get().strip()
        selected_option = self.prompt_template_lookup.get(selected_key)
        if selected_option is None and self.prompt_template_options:
            selected_option = self.prompt_template_options[0]
            self.prompt_template_var.set(selected_option.key)
        self.prompt_template_combo.set(selected_option.label if selected_option else "")

    def _build_query_result_display(self, prompt: str, response_text: str) -> str:
        return (
            "[发送给模型的内容]\n"
            f"{prompt or '(无)'}\n\n"
            "[模型返回的内容]\n"
            f"{response_text or '(无)'}"
        )

    def add_image(self) -> None:
        self.capture_image(len(self.image_selections))

    def capture_image(self, slot_index: int) -> None:
        if not self._ensure_can_use_images(slot_index):
            return
        if slot_index < 0 or slot_index >= MAX_AI_CHECKPOINT_IMAGES:
            messagebox.showinfo("提示", f"当前最多支持 {MAX_AI_CHECKPOINT_IMAGES} 张截图。", parent=self.window)
            return
        if slot_index > len(self.image_selections):
            messagebox.showinfo("提示", f"请先按顺序添加到截图 {len(self.image_selections) + 1}。", parent=self.window)
            return

        selection = _select_region_with_window_management(
            self.parent,
            self.window,
            f"选择 AI Checkpoint 截图区域 {slot_index + 1}",
            dialog_hide_mode="withdraw",
            parent_restore_mode="keep_iconified",
        )

        if not selection:
            return

        image_path = self.engine.save_manual_image(selection.image, "checkpoint")
        if not image_path:
            messagebox.showerror("保存失败", "截图保存失败。", parent=self.window)
            return

        absolute_path = Path(self.engine.store.session_dir or Path()) / image_path
        item = (absolute_path, selection.to_region_dict())
        self._set_image_selection(item, slot_index)
        self._refresh_media_summary()
        self._restore_previews()

    def add_historical_image(self) -> None:
        self.add_historical_image_to_slot(len(self.image_selections))

    def add_historical_image_to_slot(self, slot_index: int) -> None:
        if not self._ensure_can_use_images(slot_index):
            return
        if slot_index < 0 or slot_index >= MAX_AI_CHECKPOINT_IMAGES:
            messagebox.showinfo("提示", f"当前最多支持 {MAX_AI_CHECKPOINT_IMAGES} 张截图。", parent=self.window)
            return
        if slot_index > len(self.image_selections):
            messagebox.showinfo("提示", f"请先按顺序添加到截图 {len(self.image_selections) + 1}。", parent=self.window)
            return

        screenshots_dir = self._get_session_screenshots_dir()
        if screenshots_dir is None:
            return

        selected_path = filedialog.askopenfilename(
            parent=self.window,
            title="选择历史截图",
            initialdir=str(screenshots_dir),
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif"), ("全部文件", "*.*")],
        )
        if not selected_path:
            return

        resolved_path = Path(selected_path).resolve()
        try:
            resolved_path.relative_to(screenshots_dir)
        except ValueError:
            messagebox.showerror("添加失败", "请选择当前 session 的 screenshots 目录下的图片。", parent=self.window)
            return

        if not resolved_path.is_file():
            messagebox.showerror("添加失败", "所选截图文件不存在。", parent=self.window)
            return

        try:
            with Image.open(resolved_path) as image:
                image.verify()
        except Exception:
            messagebox.showerror("添加失败", "所选文件不是有效的图片。", parent=self.window)
            return

        if any(existing_path.resolve() == resolved_path for existing_path, _region in self.image_selections):
            messagebox.showinfo("提示", "该截图已添加。", parent=self.window)
            return

        self._set_image_selection((resolved_path, {}), slot_index)
        self._refresh_media_summary()
        self._restore_previews()

    def start_video(self) -> None:
        if self.image_selections:
            messagebox.showinfo("提示", "当前已经有截图，视频和截图不能同时存在。请先清空截图。", parent=self.window)
            return
        if self.video_recorder and self.video_recorder.is_recording:
            messagebox.showinfo("提示", "视频录制已经在进行中。", parent=self.window)
            return

        selection = _select_region_with_window_management(
            self.parent,
            self.window,
            "选择视频录制区域",
            dialog_hide_mode="withdraw",
            parent_restore_mode="keep_iconified",
        )

        if not selection:
            return

        self.video_region = selection.to_region_dict()
        output_path = self.engine.allocate_media_path("checkpoint_video", ".mp4")
        recorder = RegionVideoRecorder(
            output_path=output_path,
            region=self.video_region,
            fps=self.settings_store.load().video_fps,
            preview_callback=self._on_video_preview_frame,
        )
        recorder.start()
        self.video_recorder = recorder
        self.video_path = output_path
        self.video_status_var.set(f"视频录制中: {output_path.name}")
        self._refresh_media_summary()
        self.video_preview_view.set_image(selection.image)
        self._switch_preview_mode("video")

    def stop_video(self) -> None:
        if not self.video_recorder or not self.video_recorder.is_recording:
            return
        output_path = self.video_recorder.stop()
        self.video_status_var.set(
            f"视频已保存: {output_path.name} | 帧数={self.video_recorder.frame_count} | 时长={self.video_recorder.duration_seconds:.1f}s"
        )

    def clear_images(self) -> None:
        self.image_selections = []
        self._refresh_media_summary()
        self._restore_previews()

    def delete_image(self, slot_index: int) -> None:
        if slot_index < 0 or slot_index >= len(self.image_selections):
            return
        del self.image_selections[slot_index]
        self._refresh_media_summary()
        self._restore_previews()

    def clear_video(self) -> None:
        if self.video_recorder and self.video_recorder.is_recording:
            self.stop_video()
        self.video_path = None
        self.video_region = None
        self.video_status_var.set("未录制视频")
        self.video_preview_view.clear("尚未录制视频")
        self._refresh_media_summary()
        self._restore_previews()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.window, self.settings_store)
        self.window.wait_window(dialog.window)
        if dialog.saved:
            previous_key = self.prompt_template_var.get().strip()
            self._reload_prompt_templates(initial_key=previous_key)
            self._refresh_prompt_template_combo()
            if self.prompt_template_load_error:
                messagebox.showwarning(
                    "模板加载失败",
                    f"已回退到内置模板。\n\n{self.prompt_template_load_error}",
                    parent=self.window,
                )

    def run_query(self) -> None:
        prompt = self._build_prompt_from_selection()
        if not prompt:
            messagebox.showerror("查询失败", "请输入 query/prompt。", parent=self.window)
            return
        if self.video_recorder and self.video_recorder.is_recording:
            messagebox.showerror("查询失败", "请先停止视频录制，再执行 Query。", parent=self.window)
            return
        if not self.image_selections and not self.video_path:
            messagebox.showerror("查询失败", "请至少提供截图或视频。", parent=self.window)
            return

        self.query_status_var.set("查询中...")
        self.last_effective_prompt = prompt

        def worker() -> None:
            try:
                client = OpenAICompatibleAIClient(self.settings_store.load())
                result = client.query(
                    user_prompt=prompt,
                    image_paths=[item[0] for item in self.image_selections],
                    video_path=self.video_path,
                )
            except (AIClientError, Exception) as exc:
                self.window.after(0, lambda: self._on_query_error(str(exc)))
                return

            self.window.after(0, lambda prompt=prompt, result=result: self._on_query_success(prompt, result))

        threading.Thread(target=worker, daemon=True).start()

    def clear_ai_history(self) -> None:
        _set_text(self.query_text, "")
        _set_text(self.response_text, "")
        self.query_result = None
        self.last_effective_prompt = ""
        self.query_status_var.set("未查询")

    def save(self) -> None:
        try:
            payload = self._build_checkpoint_payload()
            if payload is None:
                return

            if self.save_mode == "edit":
                self.result_payload = payload
            else:
                self.engine.add_checkpoint_with_media(**payload)
            self.saved = True
            self.draft.clear()
            self._close()
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.window)

    def _build_checkpoint_payload(self) -> dict[str, object] | None:
        title = self.title_var.get().strip()
        step_description = self._get_step_comment_text()
        missing_fields: list[str] = []
        if not title:
            missing_fields.append("期望结果")
        if not step_description:
            missing_fields.append("Step Description")
        if missing_fields:
            messagebox.showerror("保存失败", f"请填写: {'、'.join(missing_fields)}。", parent=self.window)
            return None

        prompt = self.last_effective_prompt or self._build_prompt_from_selection()
        response_text = self.response_text.get("1.0", tk.END).strip()
        if self.video_recorder and self.video_recorder.is_recording:
            self.stop_video()
        if not self.image_selections and not self.video_path:
            messagebox.showerror("保存失败", "请至少添加截图或视频。", parent=self.window)
            return None
        session_dir = self.engine.store.session_dir
        if session_dir is None:
            messagebox.showerror("保存失败", "当前没有可用的 session 目录。", parent=self.window)
            return None
        session_dir = session_dir.resolve()

        media: list[dict[str, object]] = []
        for image_path, region in self.image_selections:
            session_image_path = self._materialize_image_for_session(image_path)
            if session_image_path is None:
                return None
            try:
                relative_path = safe_relpath(session_image_path.resolve(), session_dir)
            except Exception as exc:
                messagebox.showerror("保存失败", f"生成截图相对路径失败:\n{exc}", parent=self.window)
                return None
            media.append({"type": "image", "path": relative_path, "region": region})
        if self.video_path and self.video_path.exists():
            try:
                relative_video_path = safe_relpath(self.video_path.resolve(), session_dir)
            except Exception as exc:
                messagebox.showerror("保存失败", f"生成视频相对路径失败:\n{exc}", parent=self.window)
                return None
            media.append({"type": "video", "path": relative_video_path, "region": self.video_region or {}})

        return {
            "title": title,
            "query": self._get_query_text(),
            "prompt": prompt,
            "response_text": response_text,
            "media": media,
            "query_payload": self.query_result,
            "prompt_template_key": self.prompt_template_var.get().strip() or "ct_validation",
            "design_steps": self._get_design_steps_text(),
            "step_description": step_description,
            "step_comment": step_description,
        }

    def _refresh_media_summary(self) -> None:
        parts = [f"截图: {len(self.image_selections)} 张"]
        if self.video_path:
            parts.append(f"视频: {self.video_path.name}")
        else:
            parts.append("视频: 无")
        self.media_var.set(" | ".join(parts))

    def _on_query_success(self, prompt: str, result: dict[str, object]) -> None:
        response_text = str(result.get("response_text", ""))
        display_text = self._build_query_result_display(prompt, response_text)
        self.query_result = {
            **result,
            "request_prompt": prompt,
            "display_text": display_text,
        }
        _set_text(self.response_text, display_text)
        self.query_status_var.set("查询完成")

    def _on_query_error(self, message: str) -> None:
        self.query_status_var.set("查询失败")
        messagebox.showerror("AI 查询失败", message, parent=self.window)

    def _close(self) -> None:
        if self.video_recorder and self.video_recorder.is_recording:
            self.video_recorder.stop()
        if not self.saved:
            self._save_draft()
        if self.parent.winfo_exists() and self._parent_state_on_open not in {"withdrawn", "iconic"}:
            self.parent.deiconify()
            self.parent.lift()
            self.parent.focus_force()
        self.window.destroy()

    def _restore_previews(self) -> None:
        if self.video_path:
            self._switch_preview_mode("video")
            if self.video_path.exists():
                preview_frame = load_video_preview_frame(self.video_path)
                if preview_frame:
                    self.video_preview_view.set_image(preview_frame)
                else:
                    self.video_preview_view.clear(f"视频文件: {self.video_path.name}\n无法读取预览帧")
            else:
                self.video_preview_view.clear("视频文件不存在")
            return

        self._switch_preview_mode("images")
        for slot_index, preview_view in enumerate(self.preview_views):
            if slot_index < len(self.image_selections):
                preview_view.set_image(self._load_preview_image(self.image_selections[slot_index][0]))
            else:
                preview_view.clear(f"尚未选择截图 {slot_index + 1}")

    def _switch_preview_mode(self, mode: str) -> None:
        self.screenshot_preview_container.pack_forget()
        self.video_preview_container.pack_forget()
        if mode == "video":
            self.video_preview_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        else:
            self.screenshot_preview_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

    def _on_video_preview_frame(self, image: Image.Image) -> None:
        self.window.after(0, lambda: self.video_preview_view.set_image(image))

    def _bind_preview_context_menu(self, preview_view: ZoomableImageView, slot_index: int) -> None:
        menu = tk.Menu(self.window, tearoff=0)
        menu.add_command(label="替换为新截图", command=lambda idx=slot_index: self.capture_image(idx))
        menu.add_command(label="替换为历史截图", command=lambda idx=slot_index: self.add_historical_image_to_slot(idx))
        menu.add_separator()
        menu.add_command(label="删除当前截图", command=lambda idx=slot_index: self.delete_image(idx))
        preview_view.canvas.bind("<Button-3>", lambda event, idx=slot_index, current_menu=menu: self._show_preview_context_menu(event, idx, current_menu), add="+")

    def _show_preview_context_menu(self, event: tk.Event, slot_index: int, menu: tk.Menu) -> None:
        menu.entryconfigure("替换为新截图", state=tk.NORMAL if slot_index <= len(self.image_selections) and slot_index < MAX_AI_CHECKPOINT_IMAGES else tk.DISABLED)
        menu.entryconfigure("替换为历史截图", state=tk.NORMAL if slot_index <= len(self.image_selections) and slot_index < MAX_AI_CHECKPOINT_IMAGES else tk.DISABLED)
        menu.entryconfigure("删除当前截图", state=tk.NORMAL if slot_index < len(self.image_selections) else tk.DISABLED)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_screenshot_preview_inner_configure(self, _event: tk.Event) -> None:
        self.screenshot_preview_canvas.configure(scrollregion=self.screenshot_preview_canvas.bbox("all"))

    def _on_screenshot_preview_canvas_configure(self, event: tk.Event) -> None:
        self.screenshot_preview_canvas.itemconfigure(self.screenshot_preview_window, width=event.width)

    def _ensure_can_use_images(self, slot_index: int) -> bool:
        if self.video_path:
            messagebox.showinfo("提示", "当前已经有视频，截图和视频不能同时存在。请先清空视频。", parent=self.window)
            return False
        if slot_index >= len(self.image_selections) and len(self.image_selections) >= MAX_AI_CHECKPOINT_IMAGES:
            messagebox.showinfo("提示", f"当前最多支持 {MAX_AI_CHECKPOINT_IMAGES} 张截图。", parent=self.window)
            return False
        return True

    def _get_session_screenshots_dir(self) -> Path | None:
        if self.historical_screenshots_dir is not None:
            screenshots_dir = self.historical_screenshots_dir
            if not screenshots_dir.exists():
                messagebox.showerror("添加失败", f"未找到截图目录:\n{screenshots_dir}", parent=self.window)
                return None
            return screenshots_dir
        session_dir = self.engine.store.session_dir
        if session_dir is None:
            messagebox.showerror("添加失败", "当前没有可用的 session 目录。", parent=self.window)
            return None
        screenshots_dir = (session_dir / "screenshots").resolve()
        if not screenshots_dir.exists():
            messagebox.showerror("添加失败", f"未找到截图目录:\n{screenshots_dir}", parent=self.window)
            return None
        return screenshots_dir

    def _set_image_selection(self, item: tuple[Path, dict[str, int]], slot_index: int) -> None:
        if slot_index < len(self.image_selections):
            self.image_selections[slot_index] = item
        else:
            self.image_selections.append(item)

    @staticmethod
    def _load_preview_image(image_path: Path) -> Image.Image:
        with Image.open(image_path) as image:
            return image.copy()

    def _materialize_image_for_session(self, image_path: Path) -> Path | None:
        session_dir = self.engine.store.session_dir
        if session_dir is None:
            messagebox.showerror("保存失败", "当前没有可用的 session 目录。", parent=self.window)
            return None
        session_dir = session_dir.resolve()
        image_path = image_path.resolve()
        try:
            image_path.relative_to(session_dir)
            return image_path
        except ValueError:
            pass
        try:
            with Image.open(image_path) as image:
                copied_relative_path = self.engine.save_manual_image(image.copy(), "checkpoint")
        except Exception as exc:
            messagebox.showerror("保存失败", f"复制历史截图失败:\n{exc}", parent=self.window)
            return None
        if not copied_relative_path:
            messagebox.showerror("保存失败", "复制历史截图失败。", parent=self.window)
            return None
        return (session_dir / copied_relative_path).resolve()

    def _save_draft(self) -> None:
        self.draft.title = self.title_var.get().strip()
        self.draft.prompt = self.last_effective_prompt or self._build_prompt_from_selection()
        self.draft.query_text = self._get_query_text()
        self.draft.design_steps = self._get_design_steps_text()
        self.draft.step_comment = self._get_step_comment_text()
        self.draft.prompt_template_key = self.prompt_template_var.get().strip() or "ct_validation"
        self.draft.response_text = self.response_text.get("1.0", tk.END).strip()
        self.draft.query_status = self.query_status_var.get()
        self.draft.image_selections = list(self.image_selections)
        self.draft.video_path = self.video_path
        self.draft.video_region = self.video_region
        self.draft.video_status = self.video_status_var.get()
        self.draft.query_result = self.query_result


def open_settings_dialog(parent: tk.Misc, settings_store: SettingsStore) -> None:
    dialog = SettingsDialog(parent, settings_store)
    parent.wait_window(dialog.window)


def open_comment_dialog(parent: tk.Misc, engine: RecorderEngine) -> None:
    dialog = CommentDialog(parent, engine)
    parent.wait_window(dialog.window)


def open_wait_for_image_dialog(parent: tk.Misc, engine: RecorderEngine) -> None:
    dialog = WaitForImageDialog(parent, engine)
    parent.wait_window(dialog.window)


def capture_manual_screenshot(parent: tk.Misc, engine: RecorderEngine, prompt: str = "选择截图区域") -> str | None:
    parent_was_iconic = parent.wm_state() == "iconic"
    if not parent_was_iconic:
        parent.iconify()
    parent.update_idletasks()
    time.sleep(0.15)
    try:
        selection = select_region(parent, prompt)
    finally:
        if parent.winfo_exists() and not parent_was_iconic:
            parent.deiconify()
            parent.lift()
            parent.focus_force()

    if not selection:
        return None

    relative_path = engine.add_manual_screenshot_with_media(selection.image, selection.to_region_dict())
    if not relative_path:
        messagebox.showerror("保存失败", "截图保存失败。", parent=parent)
        return None
    messagebox.showinfo("截图成功", f"截图已保存:\n{relative_path}", parent=parent)
    return relative_path


def open_session_metadata_dialog(
    parent: tk.Misc,
    draft: SessionMetadataDraft | None = None,
    settings_store: SettingsStore | None = None,
) -> SessionMetadataDraft | None:
    dialog = SessionMetadataDialog(parent, draft, settings_store=settings_store)
    parent.wait_window(dialog.window)
    return dialog.result


def open_ai_checkpoint_dialog(
    parent: tk.Misc,
    engine: RecorderEngine,
    settings_store: SettingsStore,
    draft: AICheckpointDraft,
    historical_screenshots_dir: Path | None = None,
) -> dict[str, object] | None:
    dialog = AICheckpointDialog(parent, engine, settings_store, draft, historical_screenshots_dir=historical_screenshots_dir)
    parent.wait_window(dialog.window)
    return dialog.result_payload if dialog.saved else None


def open_ai_checkpoint_editor_dialog(
    parent: tk.Misc,
    engine: RecorderEngine,
    settings_store: SettingsStore,
    draft: AICheckpointDraft,
    historical_screenshots_dir: Path | None = None,
) -> dict[str, object] | None:
    dialog = AICheckpointDialog(parent, engine, settings_store, draft, save_mode="edit", historical_screenshots_dir=historical_screenshots_dir)
    parent.wait_window(dialog.window)
    return dialog.result_payload if dialog.saved else None