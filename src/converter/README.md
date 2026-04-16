Converter 子系统职责分层：

- `extraction/`: 从外部 Python 源码中提取公开方法，转换为标准化 registry 元数据。
- `registry/`: 维护框架方法注册表和通用脚本注册表的数据结构与加载器。
- `retrieval/`: 基于语义步骤，对方法和脚本做程序化 Top-K 检索。
- `pipeline/`: 把现有 AI 语义步骤整理成统一输入，并先收敛成 IR。
- `ir/`: 中间表示层，不直接绑定最终 YAML 结构。
- `compiler/`: 程序化把 IR 编译成 YAML。

当前已支持：

- 从外部 Python 文件中扫描类列表。
- 从指定类中提取所有公开方法预览。
- 对提取结果应用二次筛选：
	- 指定 include / exclude 方法名
	- 指定排除正则
	- 仅保留有 docstring 的方法
	- 排除疑似装饰器 / 包装器方法
	- 排除单 `func` 参数方法
- 对 `paramDict` 一类复杂参数，从 docstring bullet 中提取嵌套 schema 字段。
- 通过独立 UI 生成筛选后的方法 registry：运行根目录 `converter_registry_app.py`

当前版本刻意不依赖大模型 prompt，也不直接耦合现有 viewer/recorder UI。

推荐工作流：

- 当前功能验证阶段，可以只使用少量 curated registry：
	- `converter_assets/registry/pilot_methods.yaml`
	- `converter_assets/registry/pilot_scripts.yaml`
- 例如先只放 5 个方法、2 个模块，验证 `AI 语义步骤 -> Top-K 检索 -> 后续转换` 是否走通。
- 等功能验证完成后，再切回或扩展到大规模 registry，例如：
	- `converter_assets/registry/control_action_methods.yaml`
	- `converter_assets/registry/scripts.yaml`

设计原则：

- 当前允许小规模 curated registry 做验证，不影响后续扩展。
- retrieval / pipeline / compiler 都基于统一 registry 模型，不关心是 5 条还是 500 条。
- 小规模阶段优先验证链路正确性；大规模阶段再解决召回率、排序稳定性、性能和 AI rerank。