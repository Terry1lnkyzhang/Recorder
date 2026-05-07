from __future__ import annotations

import io
import importlib
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from src.common.app_logging import get_logger
from src.common.runtime_paths import get_resource_root

try:
    import uiautomator2 as u2
except ImportError:
    u2 = None


class AndroidRecorderError(RuntimeError):
    pass


@dataclass(slots=True)
class AndroidDevice:
    serial: str
    status: str
    model: str = ""
    product: str = ""
    device_name: str = ""
    transport_id: str = ""

    @property
    def label(self) -> str:
        parts = [self.serial]
        if self.model:
            parts.append(self.model)
        elif self.device_name:
            parts.append(self.device_name)
        parts.append(self.status)
        return " | ".join(part for part in parts if part)


@dataclass(slots=True)
class AndroidHierarchySnapshot:
    serial: str
    hierarchy_path: Path
    metadata_path: Path
    package_name: str = ""
    activity: str = ""
    node_count: int = 0


@dataclass(slots=True)
class AndroidDeviceCapture:
    serial: str
    screenshot: Image.Image
    hierarchy_xml: str
    package_name: str = ""
    activity: str = ""


@dataclass(slots=True)
class AndroidExecutableStatus:
    name: str
    requested_value: str
    resolved_value: str
    found: bool
    source: str


@dataclass(slots=True)
class _ActiveRecording:
    serial: str
    remote_path: str
    local_path: Path
    process_id: str
    started_at: float


class AndroidRecorderBackend:
    def __init__(
        self,
        output_dir: Path,
        adb_executable: str = "adb",
        scrcpy_executable: str = "scrcpy",
    ) -> None:
        self.output_dir = output_dir
        self._adb_requested = adb_executable.strip() or "adb"
        self._scrcpy_requested = scrcpy_executable.strip() or "scrcpy"
        self.adb_executable = self._adb_requested
        self.scrcpy_executable = self._scrcpy_requested
        self._lock = threading.Lock()
        self._active_recording: _ActiveRecording | None = None
        self._device_cache: dict[str, Any] = {}
        self.logger = get_logger("android-recorder")
        self.refresh_executables()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._active_recording is not None

    @property
    def active_recording(self) -> _ActiveRecording | None:
        with self._lock:
            return self._active_recording

    def refresh_executables(self) -> None:
        adb_status = self._resolve_executable_status("adb", self._adb_requested, self._candidate_paths("adb"))
        scrcpy_status = self._resolve_executable_status("scrcpy", self._scrcpy_requested, self._candidate_paths("scrcpy"))
        self.adb_executable = adb_status.resolved_value
        self.scrcpy_executable = scrcpy_status.resolved_value

    def set_executables(self, adb_executable: str, scrcpy_executable: str) -> None:
        self._adb_requested = adb_executable.strip() or "adb"
        self._scrcpy_requested = scrcpy_executable.strip() or "scrcpy"
        self.refresh_executables()

    def get_effective_executables(self) -> tuple[str, str]:
        return self.adb_executable, self.scrcpy_executable

    def get_executable_statuses(self) -> tuple[AndroidExecutableStatus, AndroidExecutableStatus]:
        return (
            self._resolve_executable_status("adb", self._adb_requested, self._candidate_paths("adb")),
            self._resolve_executable_status("scrcpy", self._scrcpy_requested, self._candidate_paths("scrcpy")),
        )

    def discover_devices(self) -> list[AndroidDevice]:
        self._ensure_command_available(self.adb_executable, "ADB")
        result = self._run_command([self.adb_executable, "devices", "-l"], timeout=20)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        devices: list[AndroidDevice] = []
        for line in lines[1:]:
            columns = line.split()
            if len(columns) < 2:
                continue
            serial = columns[0]
            status = columns[1]
            payload: dict[str, str] = {}
            for column in columns[2:]:
                if ":" not in column:
                    continue
                key, value = column.split(":", 1)
                payload[key] = value
            devices.append(
                AndroidDevice(
                    serial=serial,
                    status=status,
                    model=payload.get("model", ""),
                    product=payload.get("product", ""),
                    device_name=payload.get("device", ""),
                    transport_id=payload.get("transport_id", ""),
                )
            )
        return devices

    def launch_mirror(self, serial: str) -> None:
        self._ensure_command_available(self.scrcpy_executable, "scrcpy")
        command = [self.scrcpy_executable, "-s", serial, "--window-title", f"Recorder Android Mirror [{serial}]"]
        try:
            subprocess.Popen(command)
        except OSError as exc:
            raise AndroidRecorderError(f"启动 scrcpy 失败: {exc}") from exc
        self.logger.info("Android mirror launched | serial=%s", serial)

    def start_recording(self, serial: str) -> Path:
        with self._lock:
            if self._active_recording is not None:
                raise AndroidRecorderError("当前已经有一段 Android 录屏在进行中。")

        self._ensure_command_available(self.adb_executable, "ADB")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_dir = self._get_device_output_dir(serial)
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / f"android_recording_{timestamp}.mp4"
        remote_path = f"/sdcard/RecorderAndroid_{timestamp}.mp4"
        shell_command = f"screenrecord {self._quote_shell_arg(remote_path)} > /dev/null 2>&1 & echo $!"
        result = self._run_adb(["-s", serial, "shell", "sh", "-c", shell_command], timeout=20)
        process_id = self._extract_process_id(result.stdout)
        if not process_id:
            raise AndroidRecorderError("无法启动 Android 录屏，请确认设备支持 screenrecord。")

        with self._lock:
            self._active_recording = _ActiveRecording(
                serial=serial,
                remote_path=remote_path,
                local_path=local_path,
                process_id=process_id,
                started_at=time.monotonic(),
            )

        self.logger.info(
            "Android recording started | serial=%s | pid=%s | remote=%s | local=%s",
            serial,
            process_id,
            remote_path,
            local_path,
        )
        return local_path

    def stop_recording(self) -> Path:
        with self._lock:
            active = self._active_recording
        if active is None:
            raise AndroidRecorderError("当前没有正在进行的 Android 录屏。")

        self._run_adb(["-s", active.serial, "shell", "kill", "-INT", active.process_id], timeout=15, check=False)
        time.sleep(1.2)
        self._run_adb(["-s", active.serial, "pull", active.remote_path, str(active.local_path)], timeout=180)
        self._run_adb(["-s", active.serial, "shell", "rm", "-f", active.remote_path], timeout=15, check=False)

        with self._lock:
            self._active_recording = None

        self.logger.info(
            "Android recording stopped | serial=%s | pid=%s | local=%s | duration=%.2fs",
            active.serial,
            active.process_id,
            active.local_path,
            time.monotonic() - active.started_at,
        )
        return active.local_path

    def capture_hierarchy(self, serial: str) -> AndroidHierarchySnapshot:
        device = self._get_u2_device(serial)
        xml = device.dump_hierarchy(compressed=False)
        app_info = self._safe_app_current(device)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = self._get_device_output_dir(serial)
        output_dir.mkdir(parents=True, exist_ok=True)
        xml_path = output_dir / f"hierarchy_{timestamp}.xml"
        metadata_path = output_dir / f"hierarchy_{timestamp}.json"
        xml_path.write_text(xml, encoding="utf-8")
        metadata = {
            "serial": serial,
            "captured_at": timestamp,
            "package": app_info.get("package", ""),
            "activity": app_info.get("activity", ""),
            "node_count": xml.count("<node "),
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot = AndroidHierarchySnapshot(
            serial=serial,
            hierarchy_path=xml_path,
            metadata_path=metadata_path,
            package_name=str(metadata["package"]),
            activity=str(metadata["activity"]),
            node_count=int(metadata["node_count"]),
        )
        self.logger.info(
            "Android hierarchy captured | serial=%s | xml=%s | package=%s | activity=%s | node_count=%s",
            serial,
            xml_path,
            snapshot.package_name,
            snapshot.activity,
            snapshot.node_count,
        )
        return snapshot

    def capture_device_state(self, serial: str) -> AndroidDeviceCapture:
        device = self._get_u2_device(serial)
        try:
            screenshot = device.screenshot(format="pillow")
        except TypeError:
            screenshot = device.screenshot()
        except Exception as exc:
            raise AndroidRecorderError(f"抓取 Android 截图失败: {exc}") from exc

        if isinstance(screenshot, Image.Image):
            screenshot_image = screenshot
        elif isinstance(screenshot, bytes):
            screenshot_image = Image.open(io.BytesIO(screenshot)).convert("RGB")
        else:
            raise AndroidRecorderError("uiautomator2 返回了无法识别的截图格式。")

        try:
            xml = device.dump_hierarchy(compressed=False)
        except Exception as exc:
            raise AndroidRecorderError(f"抓取 Android 页面树失败: {exc}") from exc

        app_info = self._safe_app_current(device)
        capture = AndroidDeviceCapture(
            serial=serial,
            screenshot=screenshot_image.copy(),
            hierarchy_xml=xml,
            package_name=str(app_info.get("package", "") or ""),
            activity=str(app_info.get("activity", "") or ""),
        )
        self.logger.info(
            "Android device state captured | serial=%s | package=%s | activity=%s | image=%sx%s",
            serial,
            capture.package_name,
            capture.activity,
            capture.screenshot.width,
            capture.screenshot.height,
        )
        return capture

    def query_xpath(self, serial: str, xpath: str, max_results: int = 10) -> list[dict[str, Any]]:
        module = self._get_u2_module()

        query = xpath.strip()
        if not query:
            raise AndroidRecorderError("XPath 不能为空。")

        device = module.connect(serial)
        try:
            matches = device.xpath(query).all()
        except Exception as exc:
            raise AndroidRecorderError(f"执行 XPath 失败: {exc}") from exc

        payload: list[dict[str, Any]] = []
        for item in matches[:max_results]:
            attributes = getattr(item, "attrib", {})
            if not isinstance(attributes, dict):
                attributes = {}
            payload.append(
                {
                    "text": str(attributes.get("text", "") or ""),
                    "resource_id": str(attributes.get("resource-id", "") or ""),
                    "content_desc": str(attributes.get("content-desc", "") or ""),
                    "class_name": str(attributes.get("class", "") or ""),
                    "bounds": str(attributes.get("bounds", "") or ""),
                }
            )
        self.logger.info("Android xpath queried | serial=%s | xpath=%s | matches=%s", serial, query, len(matches))
        return payload

    def get_device_output_dir(self, serial: str) -> Path:
        return self._get_device_output_dir(serial)

    def _get_device_output_dir(self, serial: str) -> Path:
        safe_serial = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in serial)
        return self.output_dir / "android" / safe_serial

    def _run_adb(
        self,
        arguments: list[str],
        timeout: int = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.adb_executable, *arguments]
        return self._run_command(command, timeout=timeout, check=check)

    def _run_command(
        self,
        command: list[str],
        timeout: int = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AndroidRecorderError(f"命令不存在: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AndroidRecorderError(f"命令执行超时: {' '.join(command)}") from exc

        if check and result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise AndroidRecorderError(stderr or f"命令执行失败: {' '.join(command)}")
        return result

    @staticmethod
    def _ensure_command_available(command: str, display_name: str) -> None:
        if shutil.which(command):
            return
        if Path(command).exists():
            return
        raise AndroidRecorderError(f"未找到 {display_name} 可执行文件: {command}")

    @staticmethod
    def _resolve_executable_status(name: str, command: str, fallback_candidates: list[Path]) -> AndroidExecutableStatus:
        normalized = command.strip()
        if not normalized:
            return AndroidExecutableStatus(
                name=name,
                requested_value=command,
                resolved_value="",
                found=False,
                source="missing",
            )

        explicit_path = Path(normalized)
        if explicit_path.exists():
            return AndroidExecutableStatus(
                name=name,
                requested_value=normalized,
                resolved_value=str(explicit_path),
                found=True,
                source="explicit",
            )

        discovered = shutil.which(normalized)
        if discovered:
            return AndroidExecutableStatus(
                name=name,
                requested_value=normalized,
                resolved_value=discovered,
                found=True,
                source="path",
            )

        for candidate in fallback_candidates:
            if candidate.exists():
                return AndroidExecutableStatus(
                    name=name,
                    requested_value=normalized,
                    resolved_value=str(candidate),
                    found=True,
                    source="bundled",
                )

        return AndroidExecutableStatus(
            name=name,
            requested_value=normalized,
            resolved_value=normalized,
            found=False,
            source="missing",
        )

    @staticmethod
    def _candidate_paths(kind: str) -> list[Path]:
        resource_root = get_resource_root()
        executable_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else resource_root
        search_roots = [
            resource_root / "converter_assets" / "android_tools",
            resource_root / "android_tools",
            executable_dir / "converter_assets" / "android_tools",
            executable_dir / "android_tools",
        ]

        candidates: list[Path] = []
        for root in search_roots:
            if kind == "adb":
                candidates.extend([
                    root / "platform-tools" / "adb.exe",
                    root / "adb.exe",
                ])
            elif kind == "scrcpy":
                candidates.extend([
                    root / "scrcpy" / "scrcpy.exe",
                    root / "scrcpy.exe",
                ])

        deduplicated: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(candidate)
        return deduplicated

    @staticmethod
    def _extract_process_id(output: str) -> str:
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if candidate.isdigit():
                return candidate
        return ""

    @staticmethod
    def _quote_shell_arg(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    @staticmethod
    def _safe_app_current(device: Any) -> dict[str, Any]:
        try:
            current = device.app_current()
        except Exception:
            return {}
        return current if isinstance(current, dict) else {}

    @staticmethod
    def _get_u2_module() -> Any:
        global u2
        if u2 is not None:
            return u2
        try:
            u2 = importlib.import_module("uiautomator2")
        except ImportError as exc:
            raise AndroidRecorderError("未安装 uiautomator2，请先执行 pip install uiautomator2。") from exc
        return u2

    def _get_u2_device(self, serial: str) -> Any:
        module = self._get_u2_module()
        cached = self._device_cache.get(serial)
        if cached is not None:
            return cached
        try:
            device = module.connect(serial)
        except Exception as exc:
            raise AndroidRecorderError(f"连接设备失败: {serial} | {exc}") from exc
        self._device_cache[serial] = device
        return device