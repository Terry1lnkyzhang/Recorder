from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


_VK_CODE_NAME_MAP = {
    96: "0",
    97: "1",
    98: "2",
    99: "3",
    100: "4",
    101: "5",
    102: "6",
    103: "7",
    104: "8",
    105: "9",
    106: "*",
    107: "+",
    109: "-",
    110: ".",
    111: "/",
}


def normalize_recorded_action(value: Any) -> str | list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return str(value or "")


def format_recorded_action(value: Any, separator: str = " + ") -> str:
    normalized = normalize_recorded_action(value)
    if isinstance(normalized, list):
        return separator.join(item for item in normalized if item)
    return normalized.strip()


def normalize_keyboard_key_name(value: Any) -> str:
    key_name = str(value or "").strip()
    if not key_name:
        return ""
    if key_name.startswith("Key."):
        key_name = key_name.split(".", 1)[1]
    match = re.fullmatch(r"<(\d+)>", key_name)
    if match:
        vk_code = int(match.group(1))
        mapped = _VK_CODE_NAME_MAP.get(vk_code)
        if mapped is not None:
            return mapped
    return key_name


def normalize_event_type(value: Any, action: Any = "") -> str:
    event_type = str(value or "").strip()
    action_text = format_recorded_action(action).strip().lower()
    lowered = event_type.lower()

    if lowered == "performscan" or action_text == "performscan":
        return "PerformScan"
    if action_text == "wait_for_image" or lowered == "wait":
        return "wait"
    if lowered == "getscreenshot" or action_text in {"getscreenshot", "manual_screenshot"}:
        return "getScreenshot"
    if lowered in {"key_press", "type_input", "input"}:
        return "input"
    if lowered == "comment" or action_text == "manual_comment":
        return "comment"
    if lowered == "checkpoint" or action_text == "ai_checkpoint":
        return "checkpoint"
    if lowered in {"mouse_drag", "scroll", "mouseaction"}:
        return "mouseAction"
    if lowered == "click":
        return "Click"
    if lowered in {"mouse_click", "controloperation"}:
        return "controlOperation"
    return event_type


@dataclass(slots=True)
class SessionMetadata:
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

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SessionMetadata:
        data = payload or {}
        scope = str(data.get("scope", "All")).strip() or "All"
        if scope not in {"All", "Sub"}:
            scope = "All"
        raw_prs_flag = data.get("is_prs_recording")
        name = str(data.get("name", ""))
        if isinstance(raw_prs_flag, bool):
            is_prs_recording = raw_prs_flag
        else:
            is_prs_recording = not bool(name.strip())
        return cls(
            is_prs_recording=is_prs_recording,
            testcase_id=str(data.get("testcase_id", "")),
            version_number=str(data.get("version_number", "")),
            project=str(data.get("project", "Taichi") or "Taichi"),
            baseline_name=str(data.get("baseline_name", "")),
            name=name,
            recorder_person=str(data.get("recorder_person", "")),
            design_steps=str(data.get("design_steps", "")),
            preconditions=str(data.get("preconditions", "")),
            configuration_requirements=str(data.get("configuration_requirements", "")),
            extra_devices=str(data.get("extra_devices", "")),
            scope=scope,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_prs_recording": self.is_prs_recording,
            "testcase_id": self.testcase_id if self.is_prs_recording else "",
            "version_number": self.version_number if self.is_prs_recording else "",
            "project": self.project,
            "baseline_name": self.baseline_name,
            "name": "" if self.is_prs_recording else self.name,
            "recorder_person": self.recorder_person,
            "design_steps": self.design_steps,
            "preconditions": self.preconditions,
            "configuration_requirements": self.configuration_requirements,
            "extra_devices": self.extra_devices,
            "scope": self.scope,
        }


@dataclass(slots=True)
class WindowInfo:
    title: str = ""
    class_name: str = ""
    handle: str = ""
    process_id: int | None = None
    process_name: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> WindowInfo:
        data = payload or {}
        return cls(
            title=str(data.get("title", "")),
            class_name=str(data.get("class_name", "")),
            handle=str(data.get("handle", "")),
            process_id=data.get("process_id") if isinstance(data.get("process_id"), int) else None,
            process_name=str(data.get("process_name", "")),
        )


@dataclass(slots=True)
class UIElementInfo:
    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    help_text: str = ""
    help_text_fallback: str = ""
    name_fallbacks: list[str] = field(default_factory=list)
    rectangle: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> UIElementInfo:
        data = payload or {}
        rectangle = data.get("rectangle", {})
        return cls(
            name=str(data.get("name", "")),
            control_type=str(data.get("control_type", "")),
            automation_id=str(data.get("automation_id", "")),
            class_name=str(data.get("class_name", "")),
            help_text=str(data.get("help_text", "")),
            help_text_fallback=str(data.get("help_text_fallback", "")),
            name_fallbacks=[str(item) for item in data.get("name_fallbacks", []) if str(item).strip()] if isinstance(data.get("name_fallbacks", []), list) else [],
            rectangle=rectangle if isinstance(rectangle, dict) else {},
        )


@dataclass(slots=True)
class RecordedEvent:
    event_id: str
    timestamp: str
    event_type: str
    action: str | list[str]
    screenshot: str | None = None
    mouse: dict[str, Any] = field(default_factory=dict)
    keyboard: dict[str, Any] = field(default_factory=dict)
    scroll: dict[str, Any] = field(default_factory=dict)
    window: WindowInfo = field(default_factory=WindowInfo)
    ui_element: UIElementInfo = field(default_factory=UIElementInfo)
    note: str = ""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    media: list[dict[str, Any]] = field(default_factory=list)
    ai_result: dict[str, Any] = field(default_factory=dict)
    additional_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["window"] = asdict(self.window)
        payload["ui_element"] = asdict(self.ui_element)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RecordedEvent:
        return cls(
            event_id=str(payload.get("event_id", "")),
            timestamp=str(payload.get("timestamp", "")),
            event_type=str(payload.get("event_type", "")),
            action=normalize_recorded_action(payload.get("action", "")),
            screenshot=str(payload.get("screenshot")) if payload.get("screenshot") is not None else None,
            mouse=payload.get("mouse", {}) if isinstance(payload.get("mouse"), dict) else {},
            keyboard=payload.get("keyboard", {}) if isinstance(payload.get("keyboard"), dict) else {},
            scroll=payload.get("scroll", {}) if isinstance(payload.get("scroll"), dict) else {},
            window=WindowInfo.from_dict(payload.get("window") if isinstance(payload.get("window"), dict) else {}),
            ui_element=UIElementInfo.from_dict(payload.get("ui_element") if isinstance(payload.get("ui_element"), dict) else {}),
            note=str(payload.get("note", "")),
            checkpoint=payload.get("checkpoint", {}) if isinstance(payload.get("checkpoint"), dict) else {},
            media=payload.get("media", []) if isinstance(payload.get("media"), list) else [],
            ai_result=payload.get("ai_result", {}) if isinstance(payload.get("ai_result"), dict) else {},
            additional_details=payload.get("additional_details", {}) if isinstance(payload.get("additional_details"), dict) else {},
        )


@dataclass(slots=True)
class RecordingSessionData:
    session_id: str
    created_at: str
    stopped_at: str | None = None
    output_dir: str = ""
    screenshots_dir: str = ""
    media_dir: str = ""
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    environment: dict[str, Any] = field(default_factory=dict)
    events: list[RecordedEvent] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "stopped_at": self.stopped_at,
            "output_dir": self.output_dir,
            "screenshots_dir": self.screenshots_dir,
            "media_dir": self.media_dir,
            "metadata": self.metadata.to_dict(),
            "environment": self.environment,
            "events": [event.to_dict() for event in self.events],
            "comments": self.comments,
            "checkpoints": self.checkpoints,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RecordingSessionData:
        data = payload or {}
        events_payload = data.get("events", [])
        return cls(
            session_id=str(data.get("session_id", "")),
            created_at=str(data.get("created_at", "")),
            stopped_at=str(data.get("stopped_at")) if data.get("stopped_at") is not None else None,
            output_dir=str(data.get("output_dir", "")),
            screenshots_dir=str(data.get("screenshots_dir", "")),
            media_dir=str(data.get("media_dir", "")),
            metadata=SessionMetadata.from_dict(data.get("metadata") if isinstance(data.get("metadata"), dict) else {}),
            environment=data.get("environment", {}) if isinstance(data.get("environment"), dict) else {},
            events=[
                RecordedEvent.from_dict(item)
                for item in events_payload
                if isinstance(item, dict)
            ],
            comments=data.get("comments", []) if isinstance(data.get("comments"), list) else [],
            checkpoints=data.get("checkpoints", []) if isinstance(data.get("checkpoints"), list) else [],
        )