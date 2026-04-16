# Recorder 发布说明

## 1. 目标产物

建议发布两个目录：

- `Recorder/`
- `SessionViewer/`

它们分别对应：

- 录制器主程序
- 独立查看器

分发时复制整个目录，不要只复制 `.exe`。

## 2. 打包前提

当前项目使用 Python 3.12 虚拟环境：

- `y:/project/Recorder/.venv/Scripts/python.exe`

需要先安装：

- `requirements.txt` 里的运行依赖
- `PyInstaller`

示例命令：

```powershell
y:/project/Recorder/.venv/Scripts/python.exe -m pip install -r requirements.txt
y:/project/Recorder/.venv/Scripts/python.exe -m pip install pyinstaller
```

如果公司网络无法访问公开镜像，需要：

- 使用内网 PyPI 源
- 或提前下载离线 wheel 包再安装

## 3. 执行打包

项目根目录执行：

```powershell
.\build_windows_exe.ps1 -Clean
```

脚本会生成：

```text
dist/
  Recorder/
  SessionViewer/
```

## 4. 给其他电脑怎么用

把以下目录复制到目标机器：

- `dist/Recorder/`
- `dist/SessionViewer/`（如果需要单独查看 session）

直接运行：

- `Recorder.exe`
- `SessionViewer.exe`

## 5. 打包后数据位置

打包后的 EXE 不会把数据写回程序目录，而是写到当前用户目录：

- `%LOCALAPPDATA%\Recorder\recordings`
- `%LOCALAPPDATA%\Recorder\recorder_settings.json`

这样即使程序放在只读目录，也能正常保存录制结果和设置。

## 6. 目标机器要求

- Windows 64 位
- 允许桌面录制和 UIAutomation
- 如果要用 AI 功能，能访问配置的 AI endpoint

## 7. 当前机器的实际状态

本仓库已经具备打包脚本和运行时路径支持。

但如果当前机器要立即打包，还需要先解决这一项：

- 当前虚拟环境里还没有安装 `PyInstaller`

可先执行：

```powershell
y:/project/Recorder/.venv/Scripts/python.exe -m pip install pyinstaller
```

如果安装时网络超时，需要切换可用镜像源或使用离线安装包。