from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..extraction import ExposureFilterConfig, PythonClassMethodExtractor, apply_exposure_filters
from ..pipeline import build_retrieval_preview_from_files
from ..registry.loader import load_method_registry
from ..registry.models import MethodParameter, MethodRegistry, MethodRegistryEntry


class ConverterRegistryWindow:
    def __init__(self, master: tk.Misc) -> None:
        self.window = tk.Toplevel(master)
        self.window.title("Converter Registry Builder")
        self.window.geometry("1480x920")
        self.window.minsize(1240, 760)

        self.extractor = PythonClassMethodExtractor()
        self.current_source_path: Path | None = None
        self.current_previews = []
        self.manual_overrides: dict[str, bool] = {}
        self.manual_metadata_overrides: dict[str, dict[str, object]] = {}
        self.loaded_methods_registry: MethodRegistry | None = None

        self.source_path_var = tk.StringVar()
        self.class_name_var = tk.StringVar()
        self.output_path_var = tk.StringVar(value=str(Path(__file__).resolve().parents[3] / "converter_assets" / "registry" / "generated_methods.yaml"))
        self.existing_registry_path_var = tk.StringVar()
        self.selected_registry_entry_var = tk.StringVar()
        self.methods_registry_path_var = tk.StringVar(value=self._default_registry_path("control_action_methods.yaml"))
        self.scripts_registry_path_var = tk.StringVar(value=self._default_registry_path("scripts.yaml"))
        self.registry_preset_var = tk.StringVar(value="full")
        self.ai_analysis_path_var = tk.StringVar()
        self.session_path_var = tk.StringVar()
        self.top_k_methods_var = tk.IntVar(value=5)
        self.top_k_scripts_var = tk.IntVar(value=0)
        self.status_var = tk.StringVar(value="请选择 Python 文件并加载类。")
        self.include_only_documented_var = tk.BooleanVar(value=False)
        self.exclude_decorator_like_var = tk.BooleanVar(value=True)
        self.exclude_single_callable_var = tk.BooleanVar(value=True)
        self.retrieval_results: list[dict[str, object]] = []

        self._build_ui()

    def _build_ui(self) -> None:
        root = ttk.Frame(self.window, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        top_form = ttk.LabelFrame(root, text="源码与输出")
        top_form.pack(fill=tk.X)
        top_form.columnconfigure(1, weight=1)

        ttk.Label(top_form, text="Python 文件").grid(row=0, column=0, sticky=tk.W, padx=8, pady=8)
        ttk.Entry(top_form, textvariable=self.source_path_var).grid(row=0, column=1, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(top_form, text="选择文件", command=self.choose_source_file).grid(row=0, column=2, padx=8, pady=8)
        ttk.Button(top_form, text="加载类", command=self.load_classes).grid(row=0, column=3, padx=8, pady=8)

        ttk.Label(top_form, text="类名").grid(row=1, column=0, sticky=tk.W, padx=8, pady=8)
        self.class_combo = ttk.Combobox(top_form, textvariable=self.class_name_var, state="readonly")
        self.class_combo.grid(row=1, column=1, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(top_form, text="预览方法", command=self.preview_methods).grid(row=1, column=2, padx=8, pady=8)

        ttk.Label(top_form, text="输出 Registry").grid(row=2, column=0, sticky=tk.W, padx=8, pady=8)
        ttk.Entry(top_form, textvariable=self.output_path_var).grid(row=2, column=1, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(top_form, text="选择输出", command=self.choose_output_file).grid(row=2, column=2, padx=8, pady=8)
        ttk.Button(top_form, text="生成 Registry", command=self.generate_registry).grid(row=2, column=3, padx=8, pady=8)

        ttk.Label(top_form, text="现有 Methods Registry").grid(row=3, column=0, sticky=tk.W, padx=8, pady=8)
        ttk.Entry(top_form, textvariable=self.existing_registry_path_var).grid(row=3, column=1, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(top_form, text="选择现有文件", command=self.choose_existing_registry_file).grid(row=3, column=2, padx=8, pady=8)
        ttk.Button(top_form, text="加载现有 Registry", command=self.load_existing_registry).grid(row=3, column=3, padx=8, pady=8)

        ttk.Label(top_form, text="现有 Entry 名称").grid(row=4, column=0, sticky=tk.W, padx=8, pady=8)
        self.registry_entry_combo = ttk.Combobox(top_form, textvariable=self.selected_registry_entry_var, state="readonly")
        self.registry_entry_combo.grid(row=4, column=1, sticky=tk.EW, padx=8, pady=8)
        self.registry_entry_combo.bind("<<ComboboxSelected>>", self.on_selected_registry_entry_changed)
        ttk.Button(top_form, text="更新当前 Entry", command=self.update_selected_registry_entry).grid(row=4, column=2, padx=8, pady=8)
        ttk.Button(top_form, text="写回当前元数据", command=self.save_selected_method_metadata_to_loaded_registry).grid(row=4, column=3, padx=8, pady=8)
        ttk.Button(top_form, text="删除当前 Entry", command=self.delete_selected_registry_entry).grid(row=4, column=4, padx=8, pady=8)

        middle = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        middle.pack(fill=tk.BOTH, expand=True, pady=(12, 0))

        filter_frame = ttk.LabelFrame(middle, text="二次筛选设置")
        preview_frame = ttk.LabelFrame(middle, text="方法预览")
        middle.add(filter_frame, weight=2)
        middle.add(preview_frame, weight=5)

        self._build_filter_panel(filter_frame)
        self._build_preview_panel(preview_frame)

        retrieval_frame = ttk.LabelFrame(root, text="语义步骤 -> 方法候选")
        retrieval_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self._build_retrieval_panel(retrieval_frame)

        ttk.Label(root, textvariable=self.status_var, anchor=tk.W).pack(fill=tk.X, pady=(10, 0))

    def _build_filter_panel(self, parent: ttk.Frame) -> None:
        wrapper = ttk.Frame(parent, padding=12)
        wrapper.pack(fill=tk.BOTH, expand=True)

        ttk.Checkbutton(wrapper, text="仅暴露有 docstring 的方法", variable=self.include_only_documented_var, command=self.refresh_preview_filters).pack(anchor=tk.W, pady=(0, 6))
        ttk.Checkbutton(wrapper, text="排除疑似装饰器/包装器", variable=self.exclude_decorator_like_var, command=self.refresh_preview_filters).pack(anchor=tk.W, pady=(0, 6))
        ttk.Checkbutton(wrapper, text="排除单 callable 参数方法", variable=self.exclude_single_callable_var, command=self.refresh_preview_filters).pack(anchor=tk.W, pady=(0, 12))

        ttk.Label(wrapper, text="强制包含的方法名（每行一个）").pack(anchor=tk.W)
        self.include_names_text = tk.Text(wrapper, height=6, wrap=tk.WORD, font=("Consolas", 10))
        self.include_names_text.pack(fill=tk.X, pady=(4, 10))
        self.include_names_text.bind("<KeyRelease>", lambda _event: self.refresh_preview_filters())

        ttk.Label(wrapper, text="排除的方法名（每行一个）").pack(anchor=tk.W)
        self.exclude_names_text = tk.Text(wrapper, height=8, wrap=tk.WORD, font=("Consolas", 10))
        self.exclude_names_text.pack(fill=tk.X, pady=(4, 10))
        self.exclude_names_text.bind("<KeyRelease>", lambda _event: self.refresh_preview_filters())

        ttk.Label(wrapper, text="排除正则（每行一个）").pack(anchor=tk.W)
        self.exclude_patterns_text = tk.Text(wrapper, height=8, wrap=tk.WORD, font=("Consolas", 10))
        self.exclude_patterns_text.pack(fill=tk.BOTH, expand=True, pady=(4, 10))
        self.exclude_patterns_text.bind("<KeyRelease>", lambda _event: self.refresh_preview_filters())

        ttk.Label(
            wrapper,
            text="提示: 预览表中双击某一行可手工切换是否暴露，该手工选择优先于规则筛选。tags / when_to_use / stability / domain 不再自动生成；如有需要请人工补充或后续 enrich。",
            wraplength=320,
        ).pack(anchor=tk.W, pady=(8, 0))

    def _build_preview_panel(self, parent: ttk.Frame) -> None:
        wrapper = ttk.Frame(parent, padding=12)
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(0, weight=1)
        wrapper.rowconfigure(2, weight=1)
        wrapper.rowconfigure(3, weight=1)

        self.preview_tree = ttk.Treeview(
            wrapper,
            columns=("exposed", "method", "line", "summary", "reason"),
            show="headings",
            selectmode="browse",
        )
        self.preview_tree.heading("exposed", text="暴露")
        self.preview_tree.heading("method", text="方法")
        self.preview_tree.heading("line", text="行")
        self.preview_tree.heading("summary", text="摘要")
        self.preview_tree.heading("reason", text="原因")
        self.preview_tree.column("exposed", width=70, anchor=tk.CENTER, stretch=False)
        self.preview_tree.column("method", width=180, anchor=tk.W, stretch=False)
        self.preview_tree.column("line", width=70, anchor=tk.CENTER, stretch=False)
        self.preview_tree.column("summary", width=380, anchor=tk.W, stretch=False)
        self.preview_tree.column("reason", width=260, anchor=tk.W, stretch=True)
        self.preview_tree.grid(row=0, column=0, sticky="nsew")
        self.preview_tree.bind("<Double-1>", self.toggle_selected_method)

        y_scroll = ttk.Scrollbar(wrapper, orient=tk.VERTICAL, command=self.preview_tree.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(wrapper, orient=tk.HORIZONTAL, command=self.preview_tree.xview)
        x_scroll.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.preview_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        details = ttk.LabelFrame(wrapper, text="方法详情")
        details.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        details.columnconfigure(0, weight=1)
        details.rowconfigure(0, weight=1)
        self.details_text = tk.Text(details, height=14, wrap=tk.WORD, font=("Consolas", 10))
        self.details_text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        details_scroll = ttk.Scrollbar(details, orient=tk.VERTICAL, command=self.details_text.yview)
        details_scroll.grid(row=0, column=1, sticky="ns", pady=8)
        self.details_text.configure(yscrollcommand=details_scroll.set, state=tk.DISABLED)
        self.preview_tree.bind("<<TreeviewSelect>>", self.show_selected_method_details)

        metadata_frame = ttk.LabelFrame(wrapper, text="手工元数据")
        metadata_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        metadata_frame.columnconfigure(1, weight=1)
        metadata_frame.columnconfigure(3, weight=1)

        self.method_tags_var = tk.StringVar()
        self.method_aliases_var = tk.StringVar()
        self.method_domain_var = tk.StringVar()
        self.method_stability_var = tk.StringVar()

        ttk.Label(metadata_frame, text="Tags").grid(row=0, column=0, sticky=tk.W, padx=8, pady=6)
        ttk.Entry(metadata_frame, textvariable=self.method_tags_var).grid(row=0, column=1, sticky=tk.EW, padx=8, pady=6)
        ttk.Label(metadata_frame, text="Aliases").grid(row=0, column=2, sticky=tk.W, padx=8, pady=6)
        ttk.Entry(metadata_frame, textvariable=self.method_aliases_var).grid(row=0, column=3, sticky=tk.EW, padx=8, pady=6)

        ttk.Label(metadata_frame, text="Domain").grid(row=1, column=0, sticky=tk.W, padx=8, pady=6)
        ttk.Entry(metadata_frame, textvariable=self.method_domain_var).grid(row=1, column=1, sticky=tk.EW, padx=8, pady=6)
        ttk.Label(metadata_frame, text="Stability").grid(row=1, column=2, sticky=tk.W, padx=8, pady=6)
        ttk.Entry(metadata_frame, textvariable=self.method_stability_var).grid(row=1, column=3, sticky=tk.EW, padx=8, pady=6)

        ttk.Label(metadata_frame, text="When To Use").grid(row=2, column=0, sticky=tk.NW, padx=8, pady=6)
        self.when_to_use_text = tk.Text(metadata_frame, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.when_to_use_text.grid(row=2, column=1, sticky="nsew", padx=8, pady=6)
        ttk.Label(metadata_frame, text="When Not To Use").grid(row=2, column=2, sticky=tk.NW, padx=8, pady=6)
        self.when_not_to_use_text = tk.Text(metadata_frame, height=4, wrap=tk.WORD, font=("Consolas", 10))
        self.when_not_to_use_text.grid(row=2, column=3, sticky="nsew", padx=8, pady=6)
        metadata_frame.rowconfigure(2, weight=1)

        metadata_actions = ttk.Frame(metadata_frame)
        metadata_actions.grid(row=3, column=0, columnspan=4, sticky=tk.EW, padx=8, pady=(0, 8))
        ttk.Button(metadata_actions, text="保存当前方法元数据", command=self.save_selected_method_metadata).pack(side=tk.LEFT)
        ttk.Button(metadata_actions, text="清空当前方法元数据", command=self.clear_selected_method_metadata).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(metadata_actions, text="新增到已加载 Registry", command=self.add_selected_method_to_loaded_registry).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            metadata_actions,
            text="说明: 自动提取不会生成这些低置信字段；如果 tags 等对检索有帮助，请在这里按方法人工补充。",
            wraplength=760,
        ).pack(side=tk.LEFT, padx=(16, 0))

    def _build_retrieval_panel(self, parent: ttk.Frame) -> None:
        wrapper = ttk.Frame(parent, padding=12)
        wrapper.pack(fill=tk.BOTH, expand=True)
        wrapper.columnconfigure(1, weight=1)
        wrapper.rowconfigure(5, weight=1)

        ttk.Label(wrapper, text="AI 分析 JSON").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(wrapper, textvariable=self.ai_analysis_path_var).grid(row=0, column=1, sticky=tk.EW, pady=6)
        ttk.Button(wrapper, text="选择", command=self.choose_ai_analysis_file).grid(row=0, column=2, padx=(8, 0), pady=6)

        ttk.Label(wrapper, text="Session JSON").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(wrapper, textvariable=self.session_path_var).grid(row=1, column=1, sticky=tk.EW, pady=6)
        ttk.Button(wrapper, text="选择", command=self.choose_session_file).grid(row=1, column=2, padx=(8, 0), pady=6)

        ttk.Label(wrapper, text="Methods Registry").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(wrapper, textvariable=self.methods_registry_path_var).grid(row=2, column=1, sticky=tk.EW, pady=6)
        ttk.Button(wrapper, text="选择", command=self.choose_methods_registry_file).grid(row=2, column=2, padx=(8, 0), pady=6)

        ttk.Label(wrapper, text="Scripts Registry").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=6)
        ttk.Entry(wrapper, textvariable=self.scripts_registry_path_var).grid(row=3, column=1, sticky=tk.EW, pady=6)
        ttk.Button(wrapper, text="选择", command=self.choose_scripts_registry_file).grid(row=3, column=2, padx=(8, 0), pady=6)

        controls = ttk.Frame(wrapper)
        controls.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(4, 10))
        ttk.Label(controls, text="Registry Preset").pack(side=tk.LEFT)
        preset_combo = ttk.Combobox(controls, textvariable=self.registry_preset_var, state="readonly", width=10)
        preset_combo["values"] = ("full", "pilot", "custom")
        preset_combo.pack(side=tk.LEFT, padx=(8, 8))
        preset_combo.bind("<<ComboboxSelected>>", self.apply_registry_preset)
        ttk.Button(controls, text="应用预设", command=self.apply_registry_preset).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(controls, text="Top-K Methods").pack(side=tk.LEFT)
        ttk.Spinbox(controls, from_=1, to=20, width=6, textvariable=self.top_k_methods_var).pack(side=tk.LEFT, padx=(8, 16))
        ttk.Label(controls, text="Top-K Scripts").pack(side=tk.LEFT)
        ttk.Spinbox(controls, from_=0, to=20, width=6, textvariable=self.top_k_scripts_var).pack(side=tk.LEFT, padx=(8, 16))
        ttk.Button(controls, text="执行候选检索", command=self.run_step_retrieval).pack(side=tk.LEFT)
        ttk.Label(
            controls,
            text="说明: pilot 适合当前用 5 个方法、2 个子模块做链路验证；full 适合后续切回大 registry。",
            wraplength=860,
        ).pack(side=tk.LEFT, padx=(16, 0))

        result_pane = ttk.Panedwindow(wrapper, orient=tk.HORIZONTAL)
        result_pane.grid(row=5, column=0, columnspan=3, sticky="nsew")

        list_frame = ttk.Frame(result_pane)
        details_frame = ttk.LabelFrame(result_pane, text="候选详情")
        result_pane.add(list_frame, weight=3)
        result_pane.add(details_frame, weight=4)

        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.retrieval_tree = ttk.Treeview(
            list_frame,
            columns=("step_id", "description", "best_method", "score", "reason"),
            show="headings",
            selectmode="browse",
        )
        self.retrieval_tree.heading("step_id", text="Step")
        self.retrieval_tree.heading("description", text="语义步骤")
        self.retrieval_tree.heading("best_method", text="Top-1 方法")
        self.retrieval_tree.heading("score", text="得分")
        self.retrieval_tree.heading("reason", text="命中原因")
        self.retrieval_tree.column("step_id", width=60, anchor=tk.CENTER, stretch=False)
        self.retrieval_tree.column("description", width=360, anchor=tk.W, stretch=True)
        self.retrieval_tree.column("best_method", width=160, anchor=tk.W, stretch=False)
        self.retrieval_tree.column("score", width=80, anchor=tk.CENTER, stretch=False)
        self.retrieval_tree.column("reason", width=280, anchor=tk.W, stretch=True)
        self.retrieval_tree.grid(row=0, column=0, sticky="nsew")
        retrieval_y_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.retrieval_tree.yview)
        retrieval_y_scroll.grid(row=0, column=1, sticky="ns")
        retrieval_x_scroll = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=self.retrieval_tree.xview)
        retrieval_x_scroll.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.retrieval_tree.configure(yscrollcommand=retrieval_y_scroll.set, xscrollcommand=retrieval_x_scroll.set)
        self.retrieval_tree.bind("<<TreeviewSelect>>", self.show_selected_retrieval_details)

        details_frame.columnconfigure(0, weight=1)
        details_frame.rowconfigure(0, weight=1)
        self.retrieval_details_text = tk.Text(details_frame, height=16, wrap=tk.WORD, font=("Consolas", 10))
        self.retrieval_details_text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        retrieval_details_scroll = ttk.Scrollbar(details_frame, orient=tk.VERTICAL, command=self.retrieval_details_text.yview)
        retrieval_details_scroll.grid(row=0, column=1, sticky="ns", pady=8)
        self.retrieval_details_text.configure(yscrollcommand=retrieval_details_scroll.set, state=tk.DISABLED)

    def choose_source_file(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self.window,
            title="选择 Python 源文件",
            filetypes=[("Python Files", "*.py"), ("All Files", "*.*")],
        )
        if not file_path:
            return
        self.source_path_var.set(file_path)
        self.current_source_path = Path(file_path)
        if not self.output_path_var.get().strip():
            self.output_path_var.set(str(Path(__file__).resolve().parents[3] / "converter_assets" / "registry" / f"{self.current_source_path.stem}_methods.yaml"))

    def choose_output_file(self) -> None:
        file_path = filedialog.asksaveasfilename(
            parent=self.window,
            title="选择输出 Registry 文件",
            defaultextension=".yaml",
            filetypes=[("YAML Files", "*.yaml"), ("All Files", "*.*")],
            initialfile=Path(self.output_path_var.get() or "generated_methods.yaml").name,
        )
        if file_path:
            self.output_path_var.set(file_path)

    def choose_existing_registry_file(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self.window,
            title="选择现有 methods registry",
            filetypes=[("YAML Files", "*.yaml"), ("All Files", "*.*")],
        )
        if file_path:
            self.existing_registry_path_var.set(file_path)

    def choose_ai_analysis_file(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self.window,
            title="选择 ai_analysis.json",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
        )
        if not file_path:
            return
        self.ai_analysis_path_var.set(file_path)
        session_path = Path(file_path).with_name("session.json")
        if session_path.exists() and not self.session_path_var.get().strip():
            self.session_path_var.set(str(session_path))

    def choose_session_file(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self.window,
            title="选择 session.json",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
        )
        if file_path:
            self.session_path_var.set(file_path)

    def choose_methods_registry_file(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self.window,
            title="选择 methods registry",
            filetypes=[("YAML Files", "*.yaml"), ("All Files", "*.*")],
        )
        if file_path:
            self.registry_preset_var.set("custom")
            self.methods_registry_path_var.set(file_path)

    def choose_scripts_registry_file(self) -> None:
        file_path = filedialog.askopenfilename(
            parent=self.window,
            title="选择 scripts registry",
            filetypes=[("YAML Files", "*.yaml"), ("All Files", "*.*")],
        )
        if file_path:
            self.registry_preset_var.set("custom")
            self.scripts_registry_path_var.set(file_path)

    def apply_registry_preset(self, _event: tk.Event | None = None) -> None:
        preset = self.registry_preset_var.get().strip() or "full"
        presets = self._registry_presets()
        if preset == "custom":
            self.status_var.set("当前使用自定义 registry 路径。")
            return
        config = presets.get(preset)
        if not config:
            return
        self.methods_registry_path_var.set(config["methods"])
        self.scripts_registry_path_var.set(config["scripts"])
        if preset == "pilot":
            self.status_var.set("已切换到 pilot registry，适合当前少量方法/子模块验证。")
        else:
            self.status_var.set("已切换到 full registry，适合更完整的候选检索。")

    def load_classes(self) -> None:
        source_path = self._get_source_path()
        if not source_path:
            return
        try:
            classes = self.extractor.list_classes(source_path)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc), parent=self.window)
            return
        self.class_combo["values"] = classes
        if classes and self.class_name_var.get().strip() not in classes:
            self.class_name_var.set(classes[0])
        self.status_var.set(f"已加载 {len(classes)} 个类")

    def preview_methods(self) -> None:
        source_path = self._get_source_path()
        class_name = self.class_name_var.get().strip()
        if not source_path or not class_name:
            messagebox.showinfo("提示", "请先选择 Python 文件并指定类名。", parent=self.window)
            return
        try:
            self.current_previews = self.extractor.build_method_previews(source_path, class_name)
        except Exception as exc:
            messagebox.showerror("预览失败", str(exc), parent=self.window)
            return
        self.manual_overrides = {}
        self.manual_metadata_overrides = self._build_metadata_overrides_from_loaded_registry()
        self.refresh_preview_filters()

    def load_existing_registry(self) -> None:
        registry_path = self._get_existing_path(self.existing_registry_path_var.get().strip(), "现有 methods registry")
        if not registry_path:
            return
        try:
            registry = load_method_registry(registry_path)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc), parent=self.window)
            return
        self.loaded_methods_registry = registry
        self._refresh_loaded_registry_entry_selector()
        self.manual_metadata_overrides = self._build_metadata_overrides_from_loaded_registry()
        self.output_path_var.set(str(registry_path))
        self.methods_registry_path_var.set(str(registry_path))
        if self.selected_registry_entry_var.get().strip():
            self.on_selected_registry_entry_changed(None)
        self.status_var.set(f"已加载现有 registry: {registry_path.name} | entries {len(registry.entries)}")

    def refresh_preview_filters(self) -> None:
        if not self.current_previews:
            return
        config = self._build_filter_config()
        for preview in self.current_previews:
            preview.manual_exposed = self.manual_overrides.get(preview.name)
        apply_exposure_filters(self.current_previews, config)
        self._reload_preview_tree()

    def toggle_selected_method(self, _event: tk.Event | None = None) -> None:
        selection = self.preview_tree.selection()
        if not selection:
            return
        method_name = selection[0]
        preview = next((item for item in self.current_previews if item.name == method_name), None)
        if not preview:
            return
        current = preview.manual_exposed if preview.manual_exposed is not None else preview.exposed
        self.manual_overrides[method_name] = not current
        self.refresh_preview_filters()
        if self.preview_tree.exists(method_name):
            self.preview_tree.selection_set(method_name)
            self.show_selected_method_details(None)

    def show_selected_method_details(self, _event: tk.Event | None) -> None:
        selection = self.preview_tree.selection()
        if not selection:
            active_name = self.selected_registry_entry_var.get().strip()
            if active_name:
                self._show_registry_entry_details(active_name)
            else:
                self._set_details_text("")
            return
        method_name = selection[0]
        preview = next((item for item in self.current_previews if item.name == method_name), None)
        if not preview:
            self._set_details_text("")
            return
        lines = [
            f"方法: {preview.name}",
            f"行号: {preview.source_line}",
            f"当前是否暴露: {'是' if preview.exposed else '否'}",
            f"筛选原因: {preview.filter_reason}",
            f"是否有 docstring: {'是' if preview.has_docstring else '否'}",
            f"疑似装饰器/包装器: {'是' if preview.is_decorator_like else '否'}",
            f"参数: {', '.join(preview.param_names) if preview.param_names else '(无)'}",
            f"decorators: {', '.join(preview.decorator_names) if preview.decorator_names else '(无)'}",
            "",
            preview.description or "(无 docstring)",
        ]
        metadata = self._get_method_metadata(preview.name)
        if any(metadata.values()):
            lines.extend(
                [
                    "",
                    "手工元数据:",
                    f"tags: {', '.join(metadata['tags']) if metadata['tags'] else '(无)'}",
                    f"aliases: {', '.join(metadata['aliases']) if metadata['aliases'] else '(无)'}",
                    f"domain: {metadata['domain'] or '(无)'}",
                    f"stability: {metadata['stability'] or '(无)'}",
                    f"when_to_use: {' | '.join(metadata['when_to_use']) if metadata['when_to_use'] else '(无)'}",
                    f"when_not_to_use: {' | '.join(metadata['when_not_to_use']) if metadata['when_not_to_use'] else '(无)'}",
                ]
            )
        self._set_details_text("\n".join(lines))
        self._load_selected_method_metadata(preview.name)

    def generate_registry(self) -> None:
        source_path = self._get_source_path()
        class_name = self.class_name_var.get().strip()
        output_path_text = self.output_path_var.get().strip()
        if not source_path or not class_name or not output_path_text:
            messagebox.showinfo("提示", "请先选择 Python 文件、类名和输出文件。", parent=self.window)
            return
        try:
            registry = self.extractor.extract_method_registry(
                source_path=source_path,
                class_name=class_name,
                registry_name=f"{class_name}-method-registry",
                description=f"Extracted public methods from {class_name} in {source_path.name}",
                filter_config=self._build_filter_config(),
                manual_overrides=dict(self.manual_overrides),
            )
            self._apply_manual_metadata_to_registry(registry.entries)
            output_path = Path(output_path_text)
            self.extractor.dump_registry_yaml(registry, output_path)
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc), parent=self.window)
            return
        self.status_var.set(f"已生成 registry: {output_path_text} | 暴露方法数 {len(registry.entries)}")
        self.methods_registry_path_var.set(output_path_text)
        should_open = messagebox.askyesno("生成完成", f"已生成 registry:\n{output_path_text}\n\n是否打开文件所在目录？", parent=self.window)
        if should_open:
            self._open_path(Path(output_path_text).parent)

    def run_step_retrieval(self) -> None:
        ai_analysis_path = self._get_existing_path(self.ai_analysis_path_var.get().strip(), "AI 分析文件")
        methods_registry_path = self._get_existing_path(self.methods_registry_path_var.get().strip(), "Methods registry")
        if not ai_analysis_path or not methods_registry_path:
            return
        session_text = self.session_path_var.get().strip()
        scripts_text = self.scripts_registry_path_var.get().strip()
        session_path = self._get_existing_path(session_text, "Session 文件", allow_empty=True)
        scripts_path = self._get_existing_path(scripts_text, "Scripts registry", allow_empty=True)
        try:
            preview = build_retrieval_preview_from_files(
                ai_analysis_path=ai_analysis_path,
                methods_registry_path=methods_registry_path,
                session_path=session_path,
                scripts_registry_path=scripts_path,
                top_k_methods=max(1, int(self.top_k_methods_var.get())),
                top_k_scripts=max(0, int(self.top_k_scripts_var.get())),
            )
        except Exception as exc:
            messagebox.showerror("检索失败", str(exc), parent=self.window)
            return
        self.retrieval_results = [item for item in preview.get("steps", []) if isinstance(item, dict)]
        self._reload_retrieval_tree()
        self.status_var.set(f"已完成候选检索 | 步骤数 {len(self.retrieval_results)} | Methods Top-K {self.top_k_methods_var.get()}")

    def show_selected_retrieval_details(self, _event: tk.Event | None) -> None:
        selection = self.retrieval_tree.selection()
        if not selection:
            self._set_retrieval_details_text("")
            return
        step_key = selection[0]
        result = next((item for item in self.retrieval_results if str(item.get("step_id")) == step_key), None)
        if not result:
            self._set_retrieval_details_text("")
            return
        method_candidates = result.get("top_method_candidates", []) if isinstance(result.get("top_method_candidates"), list) else []
        script_candidates = result.get("top_script_candidates", []) if isinstance(result.get("top_script_candidates"), list) else []
        lines = [
            f"Step: {result.get('step_id', '')}",
            f"描述: {result.get('description', '')}",
            f"结论: {result.get('conclusion', '')}",
            f"event_type: {result.get('event_type', '')}",
            f"control_type: {result.get('control_type', '')}",
            f"window_title: {result.get('window_title', '')}",
            f"tags: {', '.join(result.get('tags', [])) if isinstance(result.get('tags', []), list) else ''}",
            "",
            "Top Method Candidates:",
        ]
        if method_candidates:
            for index, candidate in enumerate(method_candidates, start=1):
                payload = candidate.get("payload", {}) if isinstance(candidate.get("payload"), dict) else {}
                parameter_names = [item.get("name", "") for item in payload.get("parameters", []) if isinstance(item, dict)]
                lines.extend(
                    [
                        f"{index}. {candidate.get('name', '')} | score={candidate.get('score', '')}",
                        f"   摘要: {candidate.get('summary', '')}",
                        f"   原因: {candidate.get('reason', '')}",
                        f"   exposed_keyword: {payload.get('exposed_keyword', '')}",
                        f"   参数: {', '.join([name for name in parameter_names if name]) or '(无)'}",
                    ]
                )
        else:
            lines.append("(无方法候选)")
        lines.extend(["", "Top Script Candidates:"])
        if script_candidates:
            for index, candidate in enumerate(script_candidates, start=1):
                lines.extend(
                    [
                        f"{index}. {candidate.get('name', '')} | score={candidate.get('score', '')}",
                        f"   摘要: {candidate.get('summary', '')}",
                        f"   原因: {candidate.get('reason', '')}",
                    ]
                )
        else:
            lines.append("(无脚本候选)")
        self._set_retrieval_details_text("\n".join(lines))

    def _reload_preview_tree(self) -> None:
        for item_id in self.preview_tree.get_children():
            self.preview_tree.delete(item_id)
        exposed_count = 0
        for preview in self.current_previews:
            if preview.exposed:
                exposed_count += 1
            self.preview_tree.insert(
                "",
                tk.END,
                iid=preview.name,
                values=(
                    "是" if preview.exposed else "否",
                    preview.name,
                    preview.source_line,
                    preview.summary,
                    preview.filter_reason,
                ),
            )
        self.status_var.set(f"方法总数 {len(self.current_previews)} | 当前暴露 {exposed_count}")
        if self.current_previews:
            first_name = self.current_previews[0].name
            if self.preview_tree.exists(first_name):
                self.preview_tree.selection_set(first_name)
                self.show_selected_method_details(None)

    def _build_filter_config(self) -> ExposureFilterConfig:
        return ExposureFilterConfig(
            exclude_names=_read_multiline_values(self.exclude_names_text),
            include_names=_read_multiline_values(self.include_names_text),
            exclude_patterns=_read_multiline_values(self.exclude_patterns_text),
            include_only_documented=self.include_only_documented_var.get(),
            exclude_decorator_like=self.exclude_decorator_like_var.get(),
            exclude_single_callable_parameter=self.exclude_single_callable_var.get(),
        )

    def _get_source_path(self) -> Path | None:
        text = self.source_path_var.get().strip()
        if not text:
            return None
        path = Path(text)
        if not path.exists():
            messagebox.showerror("路径无效", f"文件不存在:\n{text}", parent=self.window)
            return None
        self.current_source_path = path
        return path

    def _get_existing_path(self, text: str, display_name: str, allow_empty: bool = False) -> Path | None:
        if not text:
            if allow_empty:
                return None
            messagebox.showinfo("提示", f"请选择{display_name}。", parent=self.window)
            return None
        path = Path(text)
        if not path.exists():
            messagebox.showerror("路径无效", f"{display_name}不存在:\n{text}", parent=self.window)
            return None
        return path

    def _set_details_text(self, text: str) -> None:
        self.details_text.configure(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert(tk.END, text)
        self.details_text.configure(state=tk.DISABLED)

    def save_selected_method_metadata(self) -> None:
        method_name = self._get_active_method_name()
        if not method_name:
            messagebox.showinfo("提示", "请先在方法预览中选择一个方法。", parent=self.window)
            return
        self.manual_metadata_overrides[method_name] = self._collect_metadata_from_editor()
        wrote_registry = False
        if self.loaded_methods_registry:
            entry = next((item for item in self.loaded_methods_registry.entries if item.name == method_name), None)
            if entry is not None:
                self._apply_manual_metadata_to_registry([entry])
                try:
                    self._write_loaded_methods_registry()
                except Exception as exc:
                    messagebox.showerror("写回失败", str(exc), parent=self.window)
                    return
                wrote_registry = True
        if wrote_registry:
            self.status_var.set(f"已保存方法元数据并写回 registry: {method_name}")
        else:
            self.status_var.set(f"已保存方法元数据: {method_name}")
        self.show_selected_method_details(None)

    def clear_selected_method_metadata(self) -> None:
        method_name = self._get_active_method_name()
        if not method_name:
            messagebox.showinfo("提示", "请先在方法预览中选择一个方法。", parent=self.window)
            return
        self.manual_metadata_overrides.pop(method_name, None)
        self._load_selected_method_metadata(method_name)
        self.status_var.set(f"已清空方法元数据: {method_name}")
        self.show_selected_method_details(None)

    def save_selected_method_metadata_to_loaded_registry(self) -> None:
        method_name = self._get_active_method_name()
        if not method_name:
            messagebox.showinfo("提示", "请先选择一个方法或现有 entry。", parent=self.window)
            return
        if not self.loaded_methods_registry:
            messagebox.showinfo("提示", "请先加载现有 methods registry。", parent=self.window)
            return
        entry = next((item for item in self.loaded_methods_registry.entries if item.name == method_name), None)
        if not entry:
            messagebox.showinfo("提示", f"现有 registry 中未找到方法: {method_name}", parent=self.window)
            return
        self.manual_metadata_overrides[method_name] = self._collect_metadata_from_editor()
        self._apply_manual_metadata_to_registry([entry])
        try:
            self._write_loaded_methods_registry()
        except Exception as exc:
            messagebox.showerror("写回失败", str(exc), parent=self.window)
            return
        self.status_var.set(f"已写回元数据到现有 registry: {method_name}")
        self.show_selected_method_details(None)

    def add_selected_method_to_loaded_registry(self) -> None:
        if not self.loaded_methods_registry:
            messagebox.showinfo("提示", "请先加载现有 methods registry。", parent=self.window)
            return
        source_path = self._get_source_path()
        class_name = self.class_name_var.get().strip()
        selection = self.preview_tree.selection()
        if not source_path or not class_name or not selection:
            messagebox.showinfo("提示", "请先选择 Python 文件、类名，并在方法预览中选中一个方法。", parent=self.window)
            return
        method_name = selection[0]
        existing_entry = next((item for item in self.loaded_methods_registry.entries if item.name == method_name), None)
        if existing_entry:
            self.selected_registry_entry_var.set(method_name)
            self.on_selected_registry_entry_changed(None)
            messagebox.showinfo("提示", f"当前 registry 已存在方法: {method_name}\n请使用“更新当前 Entry”。", parent=self.window)
            return
        self.manual_metadata_overrides[method_name] = self._collect_metadata_from_editor()
        try:
            new_entry = self._extract_method_entry_from_source(source_path, class_name, method_name)
        except Exception as exc:
            messagebox.showerror("新增失败", str(exc), parent=self.window)
            return
        self._apply_manual_metadata_to_registry([new_entry])
        self.loaded_methods_registry.entries.append(new_entry)
        try:
            self._write_loaded_methods_registry()
        except Exception as exc:
            self.loaded_methods_registry.entries.pop()
            messagebox.showerror("新增失败", str(exc), parent=self.window)
            return
        self._refresh_loaded_registry_entry_selector(selected_name=method_name)
        self._show_registry_entry_details(method_name)
        self.status_var.set(f"已新增方法到现有 registry: {method_name}")

    def _set_retrieval_details_text(self, text: str) -> None:
        self.retrieval_details_text.configure(state=tk.NORMAL)
        self.retrieval_details_text.delete("1.0", tk.END)
        self.retrieval_details_text.insert(tk.END, text)
        self.retrieval_details_text.configure(state=tk.DISABLED)

    def _reload_retrieval_tree(self) -> None:
        for item_id in self.retrieval_tree.get_children():
            self.retrieval_tree.delete(item_id)
        for result in self.retrieval_results:
            step_id = result.get("step_id")
            method_candidates = result.get("top_method_candidates", []) if isinstance(result.get("top_method_candidates"), list) else []
            top_method = method_candidates[0] if method_candidates else {}
            self.retrieval_tree.insert(
                "",
                tk.END,
                iid=str(step_id),
                values=(
                    step_id,
                    result.get("description", ""),
                    top_method.get("name", ""),
                    top_method.get("score", ""),
                    top_method.get("reason", ""),
                ),
            )
        if self.retrieval_results:
            first_step = str(self.retrieval_results[0].get("step_id", ""))
            if first_step and self.retrieval_tree.exists(first_step):
                self.retrieval_tree.selection_set(first_step)
                self.show_selected_retrieval_details(None)

    def _default_registry_path(self, filename: str) -> str:
        path = Path(__file__).resolve().parents[3] / "converter_assets" / "registry" / filename
        return str(path) if path.exists() else ""

    def _collect_metadata_from_editor(self) -> dict[str, object]:
        return {
            "tags": _split_csv_values(self.method_tags_var.get()),
            "aliases": _split_csv_values(self.method_aliases_var.get()),
            "domain": self.method_domain_var.get().strip(),
            "stability": self.method_stability_var.get().strip(),
            "when_to_use": _read_multiline_text(self.when_to_use_text),
            "when_not_to_use": _read_multiline_text(self.when_not_to_use_text),
        }

    def _load_selected_method_metadata(self, method_name: str) -> None:
        metadata = self._get_method_metadata(method_name)
        self.method_tags_var.set(", ".join(metadata["tags"]))
        self.method_aliases_var.set(", ".join(metadata["aliases"]))
        self.method_domain_var.set(str(metadata["domain"]))
        self.method_stability_var.set(str(metadata["stability"]))
        self._set_text_widget_lines(self.when_to_use_text, metadata["when_to_use"])
        self._set_text_widget_lines(self.when_not_to_use_text, metadata["when_not_to_use"])

    def _get_method_metadata(self, method_name: str) -> dict[str, object]:
        raw = self.manual_metadata_overrides.get(method_name, {})
        return {
            "tags": list(raw.get("tags", [])) if isinstance(raw.get("tags", []), list) else [],
            "aliases": list(raw.get("aliases", [])) if isinstance(raw.get("aliases", []), list) else [],
            "domain": str(raw.get("domain", "")),
            "stability": str(raw.get("stability", "")),
            "when_to_use": list(raw.get("when_to_use", [])) if isinstance(raw.get("when_to_use", []), list) else [],
            "when_not_to_use": list(raw.get("when_not_to_use", [])) if isinstance(raw.get("when_not_to_use", []), list) else [],
        }

    def _get_active_method_name(self) -> str:
        selection = self.preview_tree.selection()
        if selection:
            return selection[0]
        return self.selected_registry_entry_var.get().strip()

    def _set_text_widget_lines(self, widget: tk.Text, values: list[object]) -> None:
        widget.delete("1.0", tk.END)
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if cleaned:
            widget.insert("1.0", "\n".join(cleaned))

    def _apply_manual_metadata_to_registry(self, entries: list[MethodRegistryEntry]) -> None:
        for entry in entries:
            metadata = self.manual_metadata_overrides.get(entry.name)
            if not metadata:
                continue
            entry.tags = [str(item).strip() for item in metadata.get("tags", []) if str(item).strip()]
            entry.aliases = [str(item).strip() for item in metadata.get("aliases", []) if str(item).strip()]
            entry.when_to_use = [str(item).strip() for item in metadata.get("when_to_use", []) if str(item).strip()]
            entry.when_not_to_use = [str(item).strip() for item in metadata.get("when_not_to_use", []) if str(item).strip()]
            entry.domain = str(metadata.get("domain", "")).strip()
            entry.stability = str(metadata.get("stability", "")).strip()

    def _refresh_loaded_registry_entry_selector(self, selected_name: str | None = None) -> None:
        if not self.loaded_methods_registry:
            self.registry_entry_combo["values"] = []
            self.selected_registry_entry_var.set("")
            return
        entry_names = [entry.name for entry in self.loaded_methods_registry.entries]
        self.registry_entry_combo["values"] = entry_names
        if selected_name and selected_name in entry_names:
            self.selected_registry_entry_var.set(selected_name)
            return
        if entry_names:
            current_name = self.selected_registry_entry_var.get().strip()
            self.selected_registry_entry_var.set(current_name if current_name in entry_names else entry_names[0])
            return
        self.selected_registry_entry_var.set("")

    def on_selected_registry_entry_changed(self, _event: tk.Event | None) -> None:
        method_name = self.selected_registry_entry_var.get().strip()
        if not method_name:
            return
        if self.preview_tree.exists(method_name):
            self.preview_tree.selection_set(method_name)
            self.preview_tree.see(method_name)
            self.show_selected_method_details(None)
            return
        self._load_selected_method_metadata(method_name)
        self._show_registry_entry_details(method_name)

    def update_selected_registry_entry(self) -> None:
        if not self.loaded_methods_registry:
            messagebox.showinfo("提示", "请先加载现有 methods registry。", parent=self.window)
            return
        source_path = self._get_source_path()
        class_name = self.class_name_var.get().strip()
        target_entry_name = self.selected_registry_entry_var.get().strip()
        source_method_name = self._get_active_method_name() or target_entry_name
        if not source_path or not class_name or not target_entry_name:
            messagebox.showinfo("提示", "请先选择 Python 文件、类名和现有 entry 名称。", parent=self.window)
            return
        if source_method_name:
            self.manual_metadata_overrides[source_method_name] = self._collect_metadata_from_editor()
        existing_entry = next((item for item in self.loaded_methods_registry.entries if item.name == target_entry_name), None)
        if not existing_entry:
            messagebox.showerror("更新失败", f"现有 registry 中未找到 entry: {target_entry_name}", parent=self.window)
            return
        try:
            updated_entry = self._extract_method_entry_from_source(source_path, class_name, source_method_name)
        except Exception as exc:
            messagebox.showerror("更新失败", str(exc), parent=self.window)
            return
        preserved_metadata = self._entry_to_metadata_dict(existing_entry)
        if source_method_name in self.manual_metadata_overrides:
            preserved_metadata = self._get_method_metadata(source_method_name)
        self.manual_metadata_overrides[target_entry_name] = preserved_metadata
        updated_entry.name = target_entry_name
        updated_entry.exposed_keyword = existing_entry.exposed_keyword or updated_entry.exposed_keyword
        if target_entry_name != source_method_name:
            updated_entry.source = {
                **updated_entry.source,
                "method": source_method_name,
            }
        updated_entry.parameters = self._merge_method_parameters(existing_entry, updated_entry)
        self._apply_manual_metadata_to_registry([updated_entry])
        for index, entry in enumerate(self.loaded_methods_registry.entries):
            if entry.name == target_entry_name:
                self.loaded_methods_registry.entries[index] = updated_entry
                break
        try:
            self._write_loaded_methods_registry()
        except Exception as exc:
            messagebox.showerror("更新失败", str(exc), parent=self.window)
            return
        self._load_selected_method_metadata(target_entry_name)
        self._show_registry_entry_details(target_entry_name)
        if target_entry_name == source_method_name:
            self.status_var.set(f"已更新现有 registry entry: {target_entry_name}")
        else:
            self.status_var.set(f"已更新现有 registry entry: {target_entry_name} <- {source_method_name}")

    def delete_selected_registry_entry(self) -> None:
        if not self.loaded_methods_registry:
            messagebox.showinfo("提示", "请先加载现有 methods registry。", parent=self.window)
            return
        method_name = self.selected_registry_entry_var.get().strip()
        if not method_name:
            messagebox.showinfo("提示", "请先选择要删除的 entry。", parent=self.window)
            return
        existing_index = next((index for index, item in enumerate(self.loaded_methods_registry.entries) if item.name == method_name), None)
        if existing_index is None:
            messagebox.showerror("删除失败", f"现有 registry 中未找到 entry: {method_name}", parent=self.window)
            return
        should_delete = messagebox.askyesno(
            "确认删除",
            f"确定删除当前 entry 吗？\n\n{method_name}\n\n删除后会直接写回现有 registry 文件。",
            parent=self.window,
        )
        if not should_delete:
            return
        removed_entry = self.loaded_methods_registry.entries.pop(existing_index)
        next_selected_name = ""
        if self.loaded_methods_registry.entries:
            next_index = min(existing_index, len(self.loaded_methods_registry.entries) - 1)
            next_selected_name = self.loaded_methods_registry.entries[next_index].name
        try:
            self._write_loaded_methods_registry()
        except Exception as exc:
            self.loaded_methods_registry.entries.insert(existing_index, removed_entry)
            messagebox.showerror("删除失败", str(exc), parent=self.window)
            return
        self._refresh_loaded_registry_entry_selector(selected_name=next_selected_name or None)
        if next_selected_name:
            self.on_selected_registry_entry_changed(None)
        else:
            self.show_selected_method_details(None)
        self.status_var.set(f"已删除现有 registry entry: {method_name}")

    def _build_metadata_overrides_from_loaded_registry(self) -> dict[str, dict[str, object]]:
        if not self.loaded_methods_registry:
            return {}
        return {entry.name: self._entry_to_metadata_dict(entry) for entry in self.loaded_methods_registry.entries}

    def _entry_to_metadata_dict(self, entry: MethodRegistryEntry) -> dict[str, object]:
        return {
            "tags": list(entry.tags),
            "aliases": list(entry.aliases),
            "domain": entry.domain,
            "stability": entry.stability,
            "when_to_use": list(entry.when_to_use),
            "when_not_to_use": list(entry.when_not_to_use),
        }

    def _merge_method_parameters(self, existing_entry: MethodRegistryEntry, updated_entry: MethodRegistryEntry) -> list[MethodParameter]:
        existing_by_name = {item.name: item for item in existing_entry.parameters}
        merged: list[MethodParameter] = []
        for parameter in updated_entry.parameters:
            existing = existing_by_name.get(parameter.name)
            if existing is None:
                merged.append(parameter)
                continue
            merged.append(
                MethodParameter(
                    name=parameter.name,
                    type=parameter.type or existing.type,
                    required=parameter.required,
                    description=parameter.description or existing.description,
                    default=parameter.default if parameter.default is not None else existing.default,
                    example=parameter.example if parameter.example is not None else existing.example,
                    schema_fields=parameter.schema_fields or list(existing.schema_fields),
                )
            )
        return merged

    def _show_registry_entry_details(self, method_name: str) -> None:
        if not self.loaded_methods_registry:
            self._set_details_text("")
            return
        entry = next((item for item in self.loaded_methods_registry.entries if item.name == method_name), None)
        if not entry:
            self._set_details_text("")
            return
        parameter_names = ", ".join(item.name for item in entry.parameters) if entry.parameters else "(无)"
        lines = [
            f"现有 Registry Entry: {entry.name}",
            f"摘要: {entry.summary}",
            f"source: {entry.source.get('path', '')}::{entry.source.get('class', '')}.{entry.source.get('method', '')}",
            f"参数: {parameter_names}",
            f"domain: {entry.domain or '(无)'}",
            f"stability: {entry.stability or '(无)'}",
            "",
            entry.description or "(无描述)",
        ]
        if entry.when_to_use or entry.when_not_to_use or entry.tags or entry.aliases:
            lines.extend(
                [
                    "",
                    "手工元数据:",
                    f"tags: {', '.join(entry.tags) if entry.tags else '(无)'}",
                    f"aliases: {', '.join(entry.aliases) if entry.aliases else '(无)'}",
                    f"when_to_use: {' | '.join(entry.when_to_use) if entry.when_to_use else '(无)'}",
                    f"when_not_to_use: {' | '.join(entry.when_not_to_use) if entry.when_not_to_use else '(无)'}",
                ]
            )
        self._set_details_text("\n".join(lines))

    def _write_loaded_methods_registry(self) -> None:
        if not self.loaded_methods_registry:
            raise ValueError("未加载现有 methods registry")
        registry_path = self._get_existing_path(self.existing_registry_path_var.get().strip(), "现有 methods registry")
        if not registry_path:
            raise ValueError("未选择现有 methods registry")
        self.extractor.dump_registry_yaml(self.loaded_methods_registry, registry_path)

    def _extract_method_entry_from_source(self, source_path: Path, class_name: str, method_name: str) -> MethodRegistryEntry:
        if method_name not in {preview.name for preview in self.current_previews}:
            raise ValueError(f"当前预览中未找到方法: {method_name}")
        if not self.loaded_methods_registry:
            raise ValueError("未加载现有 methods registry")
        extracted_registry = self.extractor.extract_method_registry(
            source_path=source_path,
            class_name=class_name,
            registry_name=self.loaded_methods_registry.metadata.name,
            description=self.loaded_methods_registry.metadata.description,
            filter_config=self._build_filter_config(),
            manual_overrides={**self.manual_overrides, method_name: True},
        )
        new_entry = next((item for item in extracted_registry.entries if item.name == method_name), None)
        if not new_entry:
            raise ValueError(f"在源码类 {class_name} 中未找到可提取的方法: {method_name}")
        return new_entry

    def _registry_presets(self) -> dict[str, dict[str, str]]:
        root = Path(__file__).resolve().parents[3] / "converter_assets" / "registry"
        return {
            "full": {
                "methods": str(root / "control_action_methods.yaml"),
                "scripts": str(root / "scripts.yaml"),
            },
            "pilot": {
                "methods": str(root / "pilot_methods.yaml"),
                "scripts": str(root / "pilot_scripts.yaml"),
            },
        }

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.window)


def _read_multiline_values(widget: tk.Text) -> list[str]:
    return [line.strip() for line in widget.get("1.0", tk.END).splitlines() if line.strip()]


def _read_multiline_text(widget: tk.Text) -> list[str]:
    return [line.strip() for line in widget.get("1.0", tk.END).splitlines() if line.strip()]


def _split_csv_values(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def launch_converter_registry_window() -> None:
    root = tk.Tk()
    root.withdraw()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    window = ConverterRegistryWindow(root)
    window.window.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()