# Recorder MVP

这是一个用于自动化脚本开发前置采集的 Windows 录制器原型，目标是把人工操作过程沉淀成结构化步骤数据，后续再转换成自动化测试框架可消费的 YAML。

当前版本能力：

- 录制键盘按键、鼠标点击、滚轮事件
- 在关键事件发生时自动截图
- 采集活动窗口信息
- 尝试采集点击位置对应的 UI 元素信息
- 支持人工添加 Comment
- 支持人工添加 AI Checkpoint
- 停止录制后输出会话文件和“重复步骤提取建议”
- 提供本地 Session Viewer，把事件、截图和 JSON 明细联动查看
- Comment 支持框选区域截图和大文本输入
- AI Checkpoint 支持两张截图、区域视频录制、Query 和模型返回结果保存
- 提供 AI Settings，可配置 endpoint、api key、model、默认提示词和请求头
- Viewer 支持更大预览区域，且可在鼠标指向位置用滚轮缩放图片
- AI Checkpoint 截图 1 和截图 2 会分开展示并支持滚轮缩放；视频模式会显示视频预览
- AI Checkpoint 关闭窗口后会保留草稿，只有点击保存 Checkpoint 后才清空

## 目录

- `recorder_app.py`：程序入口
- `session_viewer_app.py`：录制内容查看器入口
- `src/common/`：recorder 和 viewer 共用组件
- `src/ai/`：批量 AI 分析、carry-over memory 与建议输出
- `src/recorder/`：录制器核心实现
- `src/viewer/`：查看器与数据清洗实现
- `recordings/`：录制输出目录，首次运行后自动创建

## Viewer 能力

- 左侧表格支持显示精简后的时间格式：`YYYY-MM-DD HH:MM:SS`
- 左侧新增 `Comment` 列，双击可直接编辑并回写到 session 文件
- 左侧新增 `AI建议` 列，会显示每一步的中文动作描述，以及 AI 对该步骤的结论或等待建议
- 支持“数据清洗”预览，会高亮疑似无效点击和可合并的连续 key press
- 支持“应用清洗”，会删除无意义步骤并把连续按键合并为 `type_input`
- AI Checkpoint 事件支持多媒体分页查看：截图 1、截图 2、视频预览
- 支持 Viewer 里按批次调用 VL/多模态 AI 分析 session，并保存 `ai_analysis.json` / `ai_analysis.yaml`
- AI 分析会输出三类建议：无效步骤候选、可复用模块候选、等待条件候选
- Viewer 内置 AI 建议面板，可查看无效步骤、模块建议和等待建议，并可跳转定位到对应步骤
- 支持应用 AI 的 delete 建议，应用后会提示重新执行 AI 分析

## 安装

```powershell
pip install -r requirements.txt
```

## 运行

```powershell
python recorder_app.py
```

查看录制内容：

```powershell
python session_viewer_app.py
```

## 打包成 EXE

推荐使用 `PyInstaller --onedir` 打包，而不是 `--onefile`。这个项目依赖 UIAutomation、视频抽帧和外部资源目录，`onedir` 更稳定，也更容易定位问题。

### 打包前准备

```powershell
pip install -r requirements.txt
```

### 执行打包

项目根目录提供了一个现成脚本：

```powershell
.\build_windows_exe.ps1 -Clean
```

打包完成后会生成：

```text
dist/
  Recorder/
    Recorder.exe
    ...
  SessionViewer/
    SessionViewer.exe
    ...
```

### 分发给其他电脑时怎么用

最简单的方式是直接复制整个目录：

- 把 `dist/Recorder/` 整个目录复制给对方电脑
- 如果需要单独查看历史录制，再把 `dist/SessionViewer/` 整个目录一并复制过去
- 不要只复制 `exe`，要复制整个目录

### 打包后的数据存放位置

开发模式下：

- `recordings/`
- `recorder_settings.json`

打包后的 EXE 模式下，会自动改为写到当前用户目录：

- `%LOCALAPPDATA%\Recorder\recordings`
- `%LOCALAPPDATA%\Recorder\recorder_settings.json`

这样别人把程序放在 `Program Files`、桌面或共享目录下时，也不会因为目录不可写导致保存失败。

### 其他电脑需要满足的条件

- Windows 系统
- 与打包环境相近的系统架构（通常是 64 位 Windows）
- 目标机器允许 UIAutomation / 桌面录制
- 如果要使用 AI 功能，目标机器需要能访问配置好的 AI endpoint

AI 参数设置会保存到项目根目录的 `recorder_settings.json`。

默认 AI 设置：

- Endpoint: `http://130.147.129.154:8001/v1/chat/completions`
- Model: `Qwen/Qwen3-VL-8B-Instruct-FP8`
- Temperature: `0`
- enable_thinking: `false`

AI Settings 里还提供：

- 连接状态检测
- 双屏整图场景下默认仅发送当前操作所在屏幕，也可切换为发送全屏截图
- Playground 对话窗口
- 多图片上传
- 从剪切板粘贴图片
- 视频上传
- 模型回答展示

## 输出结构

每次录制会生成一个独立目录，例如：

```text
recordings/
  session_20260404_101530/
    session.json
    session.yaml
    suggestions.yaml
    screenshots/
      step_0001.png
      comment_0008.png
```

其中：

- `session.json`：完整录制数据，适合作为后续转换器输入
- `session.yaml`：便于人工审阅的 YAML 版本
- `suggestions.yaml`：基于规则的复用建议，后续可替换为 AI 分析结果

## 性能优化

- 鼠标点击和滚轮事件的截图、UI 元素解析改为后台线程处理，避免监听回调阻塞
- 键盘事件复用短时窗口缓存，减少高频按键时的窗口查询开销
- 停止录制改为后台收尾，界面不会再因为 `join` 阻塞而表现成假死
- 停止时仍会等待后台任务落盘完成，保证数据完整

## 后续扩展建议

- 接入你自己的 YAML 固定结构转换器
- 接入 LLM 对步骤进行语义归并、控件定位建议、公共流程抽取建议
- 增加文本输入聚合，把逐键记录聚合成 `input_text` 类步骤
- 增加全局快捷键，支持无焦点添加 comment / checkpoint