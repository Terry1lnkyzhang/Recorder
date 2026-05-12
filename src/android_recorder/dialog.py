from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from .backend import AndroidDevice, AndroidExecutableStatus, AndroidHierarchySnapshot, AndroidRecorderBackend, AndroidRecorderError
from .operation_recorder import AndroidOperationRecorder
from src.recorder.dialogs import SessionMetadataDraft, open_session_metadata_dialog
from src.recorder.i18n import pick_text
from src.recorder.session import SessionStore
from src.recorder.settings import SettingsStore


class AndroidRecorderDialog:
    def __init__(
        self,
        parent: tk.Misc,
        output_dir: Path,
        ui_language: str,
        settings_store: SettingsStore,
        is_main_recording_active: Callable[[], bool] | None = None,
        get_main_session_metadata_draft: Callable[[], SessionMetadataDraft] | None = None,
        get_main_session_store: Callable[[], SessionStore | None] | None = None,
    ) -> None:
        self.parent = parent
        self.ui_language = ui_language
        self.settings_store = settings_store
        self.is_main_recording_active = is_main_recording_active or (lambda: False)
        self.get_main_session_metadata_draft = get_main_session_metadata_draft or (lambda: SessionMetadataDraft())
        self.get_main_session_store = get_main_session_store or (lambda: None)
        self.backend = AndroidRecorderBackend(output_dir=output_dir)
        self.operation_recorder = AndroidOperationRecorder(output_dir=output_dir, backend=self.backend, status_callback=self._handle_engine_status)
        self.devices: list[AndroidDevice] = []
        self._device_labels: dict[str, AndroidDevice] = {}
        self.session_metadata_draft = SessionMetadataDraft()

        self.window = tk.Toplevel(parent)
        self.window.geometry("920x620")
        self.window.minsize(820, 540)
        self.window.transient(parent.winfo_toplevel())
        self.window.protocol("WM_DELETE_WINDOW", self._handle_close)

        adb_executable, scrcpy_executable = self.backend.get_effective_executables()
        self.adb_path_var = tk.StringVar(value=adb_executable)
        self.scrcpy_path_var = tk.StringVar(value=scrcpy_executable)
        self.adb_status_var = tk.StringVar()
        self.scrcpy_status_var = tk.StringVar()
        self.device_var = tk.StringVar()
        self.recording_var = tk.StringVar(value=self._t("未在录制", "Not recording"))
        self.output_var = tk.StringVar(value=str((output_dir / "android").resolve()))
        self.xpath_var = tk.StringVar()
        self.xpath_result_var = tk.StringVar(value=self._t("尚未执行 XPath 查询", "No XPath query has been executed yet"))

        self._build_ui()
        self._refresh_tool_status_view(log_change=False)
        self._append_log(self._t("Android 面板已打开。镜像建议使用 scrcpy，XPath 和页面树使用 uiautomator2。", "Android panel opened. Use scrcpy for mirroring and uiautomator2 for XPath and hierarchy."))
        self._append_log(self._t("当前 Android 模式为操作录制。你需要通过 scrcpy 窗口操作手机，Recorder 会按操作生成事件和截图。", "Android is now in operation-recording mode. Operate the phone through the scrcpy window and Recorder will generate events and screenshots."))
        self._refresh_controls()
        self.refresh_devices()

    def is_open(self) -> bool:
        return bool(self.window.winfo_exists())

    def focus(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def _build_ui(self) -> None:
        wrapper = ttk.Frame(self.window, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(3, weight=1)

        self.title_label = ttk.Label(wrapper, text=self._t("Android 操作录制与 XPath 面板", "Android Operation Recording and XPath Panel"), font=("Segoe UI", 16, "bold"))
        self.title_label.grid(row=0, column=0, sticky="w")

        tools_frame = ttk.LabelFrame(wrapper, text=self._t("工具配置", "Tool Configuration"), padding=12)
        tools_frame.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        tools_frame.columnconfigure(1, weight=1)

        ttk.Label(tools_frame, text=self._t("ADB 路径", "ADB Path")).grid(row=0, column=0, sticky="w")
        ttk.Entry(tools_frame, textvariable=self.adb_path_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(tools_frame, text=self._t("重新检测", "Re-detect"), command=self.refresh_tool_status).grid(row=0, column=2, rowspan=2, sticky="ns")
        ttk.Label(tools_frame, textvariable=self.adb_status_var, justify=tk.LEFT, wraplength=760).grid(row=1, column=1, sticky="w", padx=(8, 8), pady=(4, 10))

        ttk.Label(tools_frame, text=self._t("scrcpy 路径", "scrcpy Path")).grid(row=2, column=0, sticky="w")
        ttk.Entry(tools_frame, textvariable=self.scrcpy_path_var, state="readonly").grid(row=2, column=1, sticky="ew", padx=(8, 8))
        ttk.Label(tools_frame, textvariable=self.scrcpy_status_var, justify=tk.LEFT, wraplength=760).grid(row=3, column=1, sticky="w", padx=(8, 8), pady=(4, 0))

        device_frame = ttk.LabelFrame(wrapper, text=self._t("设备与操作", "Device and Actions"), padding=12)
        device_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        device_frame.columnconfigure(1, weight=1)

        ttk.Label(device_frame, text=self._t("当前设备", "Current Device")).grid(row=0, column=0, sticky="w")
        self.device_combo = ttk.Combobox(device_frame, textvariable=self.device_var, state="readonly")
        self.device_combo.grid(row=0, column=1, sticky="ew", padx=(8, 10))
        self.refresh_devices_button = ttk.Button(device_frame, text=self._t("刷新设备", "Refresh Devices"), command=self.refresh_devices)
        self.refresh_devices_button.grid(row=0, column=2, sticky="ew")

        action_bar = ttk.Frame(device_frame)
        action_bar.grid(row=1, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.open_mirror_button = ttk.Button(action_bar, text=self._t("打开镜像", "Open Mirror"), command=self.launch_mirror)
        self.open_mirror_button.pack(side=tk.LEFT)
        self.start_button = ttk.Button(action_bar, text=self._t("开始操作录制", "Start Operation Recording"), command=self.start_recording)
        self.start_button.pack(side=tk.LEFT, padx=(10, 0))
        self.stop_button = ttk.Button(action_bar, text=self._t("停止操作录制", "Stop Operation Recording"), command=self.stop_recording)
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))
        self.capture_hierarchy_button = ttk.Button(action_bar, text=self._t("抓取页面树", "Dump Hierarchy"), command=self.capture_hierarchy)
        self.capture_hierarchy_button.pack(side=tk.LEFT, padx=(10, 0))
        self.open_output_button = ttk.Button(action_bar, text=self._t("打开输出目录", "Open Output Folder"), command=self.open_output_folder)
        self.open_output_button.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(device_frame, text=self._t("录制状态", "Recording Status")).grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Label(device_frame, textvariable=self.recording_var).grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(12, 0))
        ttk.Label(device_frame, text=self._t("输出目录", "Output Folder")).grid(row=3, column=0, sticky="nw", pady=(8, 0))
        ttk.Label(device_frame, textvariable=self.output_var, wraplength=600).grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0))

        lower_frame = ttk.Frame(wrapper)
        lower_frame.grid(row=3, column=0, sticky="nsew")
        lower_frame.columnconfigure(0, weight=1)
        lower_frame.columnconfigure(1, weight=1)
        lower_frame.rowconfigure(1, weight=1)

        xpath_frame = ttk.LabelFrame(lower_frame, text=self._t("XPath 调试", "XPath Debug"), padding=12)
        xpath_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        xpath_frame.columnconfigure(0, weight=1)
        ttk.Entry(xpath_frame, textvariable=self.xpath_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(xpath_frame, text=self._t("执行 XPath", "Run XPath"), command=self.query_xpath).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(xpath_frame, textvariable=self.xpath_result_var, justify=tk.LEFT, wraplength=390).grid(row=1, column=0, columnspan=2, sticky="nw", pady=(10, 0))
        ttk.Label(xpath_frame, text=self._t("建议把 XPath 当辅助定位字段，录制主数据仍保留 bounds、resource-id、text。", "Use XPath as an auxiliary locator. Keep bounds, resource-id, and text as primary recording data.")).grid(row=2, column=0, columnspan=2, sticky="nw", pady=(12, 0))

        log_frame = ttk.LabelFrame(lower_frame, text=self._t("执行日志", "Execution Log"), padding=12)
        log_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap=tk.WORD, height=18, state=tk.DISABLED, font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def refresh_devices(self) -> None:
        self._refresh_tool_status_view(log_change=True)
        self._refresh_controls()
        self._run_async(
            action=self.backend.discover_devices,
            busy_message=self._t("正在刷新 Android 设备列表...", "Refreshing Android devices..."),
            on_success=self._on_devices_loaded,
            error_title=self._t("刷新设备失败", "Refresh devices failed"),
        )

    def refresh_tool_status(self) -> None:
        self._refresh_tool_status_view(log_change=True)
        self._refresh_controls()

    def launch_mirror(self) -> None:
        serial = self._require_selected_device()
        if not serial:
            return
        self._apply_backend_settings()
        self._run_async(
            action=lambda: self.backend.launch_mirror(serial),
            busy_message=self._t(f"正在打开设备 {serial} 的镜像...", f"Opening mirror for {serial}..."),
            on_success=lambda _result, current_serial=serial: self._on_mirror_launched(current_serial),
            error_title=self._t("打开镜像失败", "Open mirror failed"),
        )

    def start_recording(self) -> None:
        serial = self._require_selected_device()
        if not serial:
            return
        if self.operation_recorder.is_recording:
            return
        main_session_store: SessionStore | None = None
        if self.is_main_recording_active():
            metadata_draft = self._clone_metadata_draft(self.get_main_session_metadata_draft())
            main_session_store = self.get_main_session_store()
            if main_session_store is None or main_session_store.session_dir is None or main_session_store.data is None:
                messagebox.showerror(
                    self._t("启动操作录制失败", "Start operation recording failed"),
                    self._t("主 Recorder 当前没有可复用的活动 Session。", "The main Recorder does not have an active reusable session."),
                    parent=self.window,
                )
                return
            self._append_log(
                self._t(
                    "检测到主 Recorder 正在录制，Android 操作录制将复用当前 Session 元数据，并把事件写入主 Session。",
                    "The main recorder is already recording. Android operation recording will reuse the current session metadata and write events into the main session.",
                )
            )
        else:
            metadata_draft = open_session_metadata_dialog(self.window, self.session_metadata_draft, settings_store=self.settings_store)
            if metadata_draft is None:
                self._append_log(self._t("已取消开始 Android 操作录制。", "Android operation recording start was cancelled."))
                return
        self.session_metadata_draft = metadata_draft
        self._apply_backend_settings()
        self._run_async(
            action=lambda current_serial=serial, current_metadata=metadata_draft.to_dict(), current_store=main_session_store: self.operation_recorder.start(current_serial, current_metadata, existing_store=current_store),
            busy_message=self._t("正在启动 Android 操作录制...", "Starting Android operation recording..."),
            on_success=self._on_recording_started,
            error_title=self._t("启动操作录制失败", "Start operation recording failed"),
        )

    def stop_recording(self) -> None:
        if not self.operation_recorder.is_recording:
            return
        self._run_async(
            action=self.operation_recorder.stop,
            busy_message=self._t("正在停止 Android 操作录制并落盘...", "Stopping Android operation recording and saving the session..."),
            on_success=self._on_recording_stopped,
            error_title=self._t("停止操作录制失败", "Stop operation recording failed"),
        )

    def capture_hierarchy(self) -> None:
        serial = self._require_selected_device()
        if not serial:
            return
        self._apply_backend_settings()
        self._run_async(
            action=lambda: self.backend.capture_hierarchy(serial),
            busy_message=self._t("正在抓取当前 Android 页面树...", "Capturing the current Android hierarchy..."),
            on_success=self._on_hierarchy_captured,
            error_title=self._t("抓取页面树失败", "Capture hierarchy failed"),
        )

    def query_xpath(self) -> None:
        serial = self._require_selected_device()
        if not serial:
            return
        query = self.xpath_var.get().strip()
        if not query:
            messagebox.showinfo(self._t("提示", "Notice"), self._t("请输入 XPath。", "Enter an XPath query."), parent=self.window)
            return
        self._apply_backend_settings()
        self._run_async(
            action=lambda: self.backend.query_xpath(serial, query),
            busy_message=self._t("正在执行 XPath 查询...", "Running XPath query..."),
            on_success=self._on_xpath_queried,
            error_title=self._t("XPath 查询失败", "XPath query failed"),
        )

    def open_output_folder(self) -> None:
        if self.operation_recorder.session_dir is not None:
            target = self.operation_recorder.session_dir
        else:
            serial = self._selected_device().serial if self._selected_device() else "default"
            target = self.backend.get_device_output_dir(serial)
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))
        except OSError as exc:
            messagebox.showerror(self._t("打开目录失败", "Open folder failed"), str(exc), parent=self.window)

    def _on_devices_loaded(self, devices: list[AndroidDevice]) -> None:
        self.devices = devices
        self._device_labels = {device.label: device for device in devices}
        labels = list(self._device_labels)
        self.device_combo.configure(values=labels)
        if labels:
            if self.device_var.get() not in self._device_labels:
                self.device_var.set(labels[0])
            self._append_log(self._t(f"已发现 {len(labels)} 台 Android 设备。", f"Detected {len(labels)} Android devices."))
        else:
            self.device_var.set("")
            self._append_log(self._t("未发现可用 Android 设备。", "No Android devices were detected."))
        self._refresh_controls()

    def _on_mirror_launched(self, serial: str) -> None:
        self._append_log(self._t(f"已启动 scrcpy 镜像: {serial}", f"scrcpy mirror launched: {serial}"))
        if self.operation_recorder.is_recording:
            handle = self.operation_recorder.refresh_target_window_binding(serial)
            if handle:
                self._append_log(
                    self._t(
                        f"已刷新录制器的镜像窗口绑定: {serial} | handle={handle}",
                        f"Recorder mirror window binding refreshed: {serial} | handle={handle}",
                    )
                )
            else:
                self._append_log(
                    self._t(
                        f"镜像已启动，但当前还没有绑定到 scrcpy 窗口: {serial}",
                        f"Mirror started, but the recorder has not bound to a scrcpy window yet: {serial}",
                    )
                )

    def _on_recording_started(self, result: tuple[str, Path | None]) -> None:
        session_id, session_dir = result
        serial = self._selected_device().serial if self._selected_device() else self._t("未知设备", "unknown device")
        self.recording_var.set(self._t(f"录制中: {serial} | Session={session_id}", f"Recording: {serial} | Session={session_id}"))
        if session_dir is not None:
            self.output_var.set(str(session_dir))
        self._append_log(
            self._t(
                f"Android 操作录制已开始。\n当前 Session: {session_id}\n输出目录: {session_dir}",
                f"Android operation recording started.\nCurrent session: {session_id}\nOutput folder: {session_dir}",
            )
        )
        self._refresh_controls()

    def _on_recording_stopped(self, output_path: Path) -> None:
        self.recording_var.set(self._t("未在录制", "Not recording"))
        self.output_var.set(str(output_path))
        self._append_log(self._t(f"Android 操作录制已保存: {output_path}", f"Android operation recording saved: {output_path}"))
        self._refresh_controls()

    def stop_capture_for_main(self) -> Path | None:
        if not self.operation_recorder.is_recording:
            return None
        output_path = self.operation_recorder.stop()
        if self.window.winfo_exists():
            self.window.after(0, lambda current_output=output_path: self._on_recording_stopped(current_output))
        return output_path

    def _on_hierarchy_captured(self, snapshot: AndroidHierarchySnapshot) -> None:
        self._append_log(
            self._t(
                f"页面树已保存: {snapshot.hierarchy_path}\n当前页面: {snapshot.package_name}/{snapshot.activity}\n节点数: {snapshot.node_count}",
                f"Hierarchy saved: {snapshot.hierarchy_path}\nCurrent page: {snapshot.package_name}/{snapshot.activity}\nNode count: {snapshot.node_count}",
            )
        )

    def _on_xpath_queried(self, results: list[dict[str, object]]) -> None:
        if not results:
            self.xpath_result_var.set(self._t("未匹配到任何节点。", "No nodes matched the query."))
            self._append_log(self._t("XPath 查询完成，未命中节点。", "XPath query completed with no matches."))
            return
        lines = []
        for index, item in enumerate(results, start=1):
            lines.append(
                self._t(
                    f"{index}. text={item.get('text', '')} | id={item.get('resource_id', '')} | class={item.get('class_name', '')} | bounds={item.get('bounds', '')}",
                    f"{index}. text={item.get('text', '')} | id={item.get('resource_id', '')} | class={item.get('class_name', '')} | bounds={item.get('bounds', '')}",
                )
            )
        summary = "\n".join(lines)
        self.xpath_result_var.set(summary)
        self._append_log(self._t(f"XPath 查询命中 {len(results)} 个节点。", f"XPath query matched {len(results)} nodes."))

    def _apply_backend_settings(self) -> None:
        self.backend.refresh_executables()
        self._refresh_tool_status_view(log_change=False)

    def _refresh_tool_status_view(self, log_change: bool) -> None:
        self.backend.refresh_executables()
        adb_status, scrcpy_status = self.backend.get_executable_statuses()
        self.adb_path_var.set(adb_status.resolved_value)
        self.scrcpy_path_var.set(scrcpy_status.resolved_value)
        self.adb_status_var.set(self._format_status_text(adb_status))
        self.scrcpy_status_var.set(self._format_status_text(scrcpy_status))
        if log_change:
            self._append_log(
                self._t(
                    f"工具检测结果\nADB: {self._format_status_text(adb_status)}\nscrcpy: {self._format_status_text(scrcpy_status)}",
                    f"Tool detection\nADB: {self._format_status_text(adb_status)}\nscrcpy: {self._format_status_text(scrcpy_status)}",
                )
            )
        self._refresh_controls()

    def _format_status_text(self, status: AndroidExecutableStatus) -> str:
        if status.found:
            if status.source == "bundled":
                return self._t("状态: 已找到 | 来源: 程序自带工具", "Status: Found | Source: Bundled tool")
            if status.source == "path":
                return self._t("状态: 已找到 | 来源: 系统 PATH", "Status: Found | Source: System PATH")
            return self._t("状态: 已找到 | 来源: 显式路径", "Status: Found | Source: Explicit path")
        return self._t("状态: 未找到 | 请把 adb/scrcpy 放进 converter_assets/android_tools 或安装到系统", "Status: Missing | Put adb/scrcpy into converter_assets/android_tools or install them on the system")

    def _selected_device(self) -> AndroidDevice | None:
        return self._device_labels.get(self.device_var.get())

    def _refresh_controls(self) -> None:
        adb_status, scrcpy_status = self.backend.get_executable_statuses()
        has_device_tools = adb_status.found
        has_mirror_tools = adb_status.found and scrcpy_status.found
        is_recording = self.operation_recorder.is_recording
        self.refresh_devices_button.configure(state=tk.NORMAL if has_device_tools and not is_recording else tk.DISABLED)
        self.open_mirror_button.configure(state=tk.NORMAL if has_mirror_tools and self._selected_device() is not None else tk.DISABLED)
        self.start_button.configure(state=tk.NORMAL if has_mirror_tools and not is_recording and self._selected_device() is not None else tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL if is_recording else tk.DISABLED)
        self.capture_hierarchy_button.configure(state=tk.NORMAL if has_device_tools and self._selected_device() is not None and not is_recording else tk.DISABLED)
        self.open_output_button.configure(state=tk.NORMAL)

    def _handle_engine_status(self, message: str) -> None:
        if self.window.winfo_exists():
            self.window.after(0, lambda text=message: self._append_log(text))

    def _require_selected_device(self) -> str | None:
        device = self._selected_device()
        if device is None:
            messagebox.showinfo(self._t("提示", "Notice"), self._t("请先选择一个 Android 设备。", "Select an Android device first."), parent=self.window)
            return None
        if device.status != "device":
            messagebox.showerror(self._t("设备不可用", "Device unavailable"), self._t(f"当前设备状态不是 device，而是 {device.status}。", f"The selected device is not ready. Current status: {device.status}."), parent=self.window)
            return None
        return device.serial

    def _run_async(
        self,
        action,
        busy_message: str,
        on_success,
        error_title: str,
    ) -> None:
        self._append_log(busy_message)

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self.window.after(0, lambda err=exc: self._on_error(error_title, err))
                return
            self.window.after(0, lambda: on_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_error(self, title: str, exc: Exception) -> None:
        if isinstance(exc, AndroidRecorderError):
            message = str(exc)
        else:
            message = str(exc) or exc.__class__.__name__
        self._append_log(self._t(f"操作失败: {message}", f"Operation failed: {message}"))
        messagebox.showerror(title, message, parent=self.window)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message.rstrip() + "\n\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _handle_close(self) -> None:
        if self.operation_recorder.is_recording:
            messagebox.showinfo(self._t("提示", "Notice"), self._t("当前还有 Android 操作录制在进行中，请先停止录制。", "An Android operation recording is still running. Stop it first."), parent=self.window)
            return
        self.window.destroy()

    @staticmethod
    def _clone_metadata_draft(draft: SessionMetadataDraft) -> SessionMetadataDraft:
        payload = draft.to_dict()
        return SessionMetadataDraft(
            is_prs_recording=bool(payload.get("is_prs_recording", True)),
            testcase_id=str(payload.get("testcase_id", "")),
            version_number=str(payload.get("version_number", "")),
            project=str(payload.get("project", "Taichi") or "Taichi"),
            baseline_name=str(payload.get("baseline_name", "")),
            name=str(payload.get("name", "")),
            recorder_person=str(payload.get("recorder_person", "")),
            converter_person=str(payload.get("converter_person", "")),
            design_steps=str(payload.get("design_steps", "")),
            preconditions=str(payload.get("preconditions", "")),
            configuration_requirements=str(payload.get("configuration_requirements", "")),
            extra_devices=str(payload.get("extra_devices", "")),
            scope=str(payload.get("scope", "All") or "All"),
        )

    def _t(self, zh_text: str, en_text: str) -> str:
        return pick_text(self.ui_language, zh_text, en_text)