from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RegistryMetadata:
    name: str
    version: str = "1.0"
    description: str = ""
    owner: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RegistryMetadata:
        data = payload or {}
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "1.0")),
            description=str(data.get("description", "")),
            owner=str(data.get("owner", "")),
        )


@dataclass(slots=True)
class ParameterSchemaField:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    example: Any = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParameterSchemaField:
        return cls(
            name=str(payload.get("name", "")),
            type=str(payload.get("type", "string")),
            description=str(payload.get("description", "")),
            required=bool(payload.get("required", False)),
            default=payload.get("default"),
            example=payload.get("example"),
        )


@dataclass(slots=True)
class MethodParameter:
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""
    default: Any = None
    example: Any = None
    schema_fields: list[ParameterSchemaField] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MethodParameter:
        schema_fields = payload.get("schema_fields", [])
        return cls(
            name=str(payload.get("name", "")),
            type=str(payload.get("type", "string")),
            required=bool(payload.get("required", False)),
            description=str(payload.get("description", "")),
            default=payload.get("default"),
            example=payload.get("example"),
            schema_fields=[ParameterSchemaField.from_dict(item) for item in schema_fields if isinstance(item, dict)],
        )


@dataclass(slots=True)
class MethodRegistryEntry:
    name: str
    summary: str
    description: str = ""
    source: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    parameters: list[MethodParameter] = field(default_factory=list)
    returns: str = ""
    examples: list[str] = field(default_factory=list)
    when_to_use: list[str] = field(default_factory=list)
    when_not_to_use: list[str] = field(default_factory=list)
    exposed_keyword: str = ""
    stability: str = ""
    domain: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MethodRegistryEntry:
        params = payload.get("parameters", [])
        return cls(
            name=str(payload.get("name", "")),
            summary=str(payload.get("summary", "")),
            description=str(payload.get("description", "")),
            source=payload.get("source", {}) if isinstance(payload.get("source"), dict) else {},
            tags=[str(item) for item in payload.get("tags", []) if str(item).strip()],
            aliases=[str(item) for item in payload.get("aliases", []) if str(item).strip()],
            parameters=[MethodParameter.from_dict(item) for item in params if isinstance(item, dict)],
            returns=str(payload.get("returns", "")),
            examples=[str(item) for item in payload.get("examples", []) if str(item).strip()],
            when_to_use=[str(item) for item in payload.get("when_to_use", []) if str(item).strip()],
            when_not_to_use=[str(item) for item in payload.get("when_not_to_use", []) if str(item).strip()],
            exposed_keyword=str(payload.get("exposed_keyword", payload.get("name", ""))),
            stability=str(payload.get("stability", "")),
            domain=str(payload.get("domain", "")),
        )


@dataclass(slots=True)
class ScriptParameter:
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""
    example: Any = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScriptParameter:
        return cls(
            name=str(payload.get("name", "")),
            type=str(payload.get("type", "string")),
            required=bool(payload.get("required", False)),
            description=str(payload.get("description", "")),
            example=payload.get("example"),
        )


@dataclass(slots=True)
class ScriptRegistryEntry:
    name: str
    summary: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    parameters: list[ScriptParameter] = field(default_factory=list)
    script_type: str = "reusable_flow"
    priority: int = 100
    domain: str = ""
    covers_steps: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    expected_outcome: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    entry_keyword: str = ""
    stability: str = "stable"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScriptRegistryEntry:
        params = payload.get("parameters", [])
        return cls(
            name=str(payload.get("name", "")),
            summary=str(payload.get("summary", "")),
            description=str(payload.get("description", "")),
            tags=[str(item) for item in payload.get("tags", []) if str(item).strip()],
            aliases=[str(item) for item in payload.get("aliases", []) if str(item).strip()],
            parameters=[ScriptParameter.from_dict(item) for item in params if isinstance(item, dict)],
            script_type=str(payload.get("script_type", "reusable_flow")),
            priority=int(payload.get("priority", 100)),
            domain=str(payload.get("domain", "")),
            covers_steps=[str(item) for item in payload.get("covers_steps", []) if str(item).strip()],
            preconditions=[str(item) for item in payload.get("preconditions", []) if str(item).strip()],
            expected_outcome=[str(item) for item in payload.get("expected_outcome", []) if str(item).strip()],
            examples=[str(item) for item in payload.get("examples", []) if str(item).strip()],
            entry_keyword=str(payload.get("entry_keyword", payload.get("name", ""))),
            stability=str(payload.get("stability", "stable")),
        )


@dataclass(slots=True)
class MethodRegistry:
    metadata: RegistryMetadata
    entries: list[MethodRegistryEntry]


@dataclass(slots=True)
class ScriptRegistry:
    metadata: RegistryMetadata
    entries: list[ScriptRegistryEntry]


@dataclass(slots=True)
class RegistryBundle:
    methods: MethodRegistry
    scripts: ScriptRegistry