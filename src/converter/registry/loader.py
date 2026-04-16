from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import MethodRegistry, MethodRegistryEntry, RegistryBundle, RegistryMetadata, ScriptRegistry, ScriptRegistryEntry


def load_method_registry(path: Path) -> MethodRegistry:
    payload = _load_yaml_dict(path)
    metadata = RegistryMetadata.from_dict(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {})
    entries = [MethodRegistryEntry.from_dict(item) for item in payload.get("entries", []) if isinstance(item, dict)]
    return MethodRegistry(metadata=metadata, entries=entries)


def load_script_registry(path: Path) -> ScriptRegistry:
    payload = _load_yaml_dict(path)
    metadata = RegistryMetadata.from_dict(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {})
    entries = [ScriptRegistryEntry.from_dict(item) for item in payload.get("entries", []) if isinstance(item, dict)]
    return ScriptRegistry(metadata=metadata, entries=entries)


def load_registry_bundle(methods_path: Path, scripts_path: Path) -> RegistryBundle:
    return RegistryBundle(
        methods=load_method_registry(methods_path),
        scripts=load_script_registry(scripts_path),
    )


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Registry file must contain a mapping: {path}")
    return data