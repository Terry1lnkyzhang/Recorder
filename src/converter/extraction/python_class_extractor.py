from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .filtering import ExtractedMethodPreview, ExposureFilterConfig, apply_exposure_filters
from ..registry.models import MethodParameter, MethodRegistry, MethodRegistryEntry, ParameterSchemaField, RegistryMetadata


@dataclass(slots=True)
class _ParsedDocArg:
    name: str
    description: str = ""
    declared_type: str = ""
    extra_lines: list[str] = field(default_factory=list)


class PythonClassMethodExtractor:
    def list_classes(self, source_path: Path) -> list[str]:
        module = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
        return [node.name for node in module.body if isinstance(node, ast.ClassDef)]

    def build_method_previews(self, source_path: Path, class_name: str) -> list[ExtractedMethodPreview]:
        module = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
        class_node = self._find_class_node(module, class_name)
        previews: list[ExtractedMethodPreview] = []
        for node in class_node.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            docstring = ast.get_docstring(node, clean=True) or ""
            summary, _, _, _, _ = _parse_google_style_docstring(docstring)
            previews.append(
                ExtractedMethodPreview(
                    name=node.name,
                    summary=summary or node.name,
                    description=docstring,
                    source_line=node.lineno,
                    param_names=self._build_preview_param_names(node),
                    has_docstring=bool(docstring.strip()),
                    decorator_names=[_safe_unparse(item) for item in node.decorator_list if _safe_unparse(item)],
                    is_public=not node.name.startswith("_"),
                    is_decorator_like=_is_decorator_like(node),
                )
            )
        return previews

    def extract_method_registry(
        self,
        source_path: Path,
        class_name: str,
        registry_name: str | None = None,
        description: str = "",
        filter_config: ExposureFilterConfig | None = None,
        manual_overrides: dict[str, bool] | None = None,
    ) -> MethodRegistry:
        module = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
        class_node = self._find_class_node(module, class_name)
        previews = self.build_method_previews(source_path, class_name)
        if manual_overrides:
            for preview in previews:
                if preview.name in manual_overrides:
                    preview.manual_exposed = manual_overrides[preview.name]
        apply_exposure_filters(previews, filter_config or ExposureFilterConfig())
        preview_map = {preview.name: preview for preview in previews}

        entries = []
        for node in class_node.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            preview = preview_map.get(node.name)
            if not preview or not preview.exposed:
                continue
            entries.append(self._build_entry(source_path, class_name, node))

        metadata = RegistryMetadata(
            name=registry_name or f"{class_name}-method-registry",
            version="1.0",
            description=description or f"Extracted public methods from {class_name} in {source_path.name}",
            owner="Recorder Converter",
        )
        return MethodRegistry(metadata=metadata, entries=entries)

    def dump_registry_yaml(self, registry: MethodRegistry, output_path: Path) -> None:
        payload = {
            "metadata": asdict(registry.metadata),
            "entries": [_serialize_method_entry(entry) for entry in registry.entries],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _find_class_node(self, module: ast.Module, class_name: str) -> ast.ClassDef:
        for node in module.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node
        raise ValueError(f"Class not found: {class_name}")

    def _build_entry(self, source_path: Path, class_name: str, node: ast.FunctionDef) -> MethodRegistryEntry:
        docstring = ast.get_docstring(node, clean=True) or ""
        summary, description, args_doc, returns_doc, examples = _parse_google_style_docstring(docstring)
        parameters = self._extract_parameters(node, args_doc)
        return MethodRegistryEntry(
            name=node.name,
            exposed_keyword=node.name,
            summary=summary or node.name,
            description=description or docstring,
            source={
                "path": str(source_path),
                "class": class_name,
                "method": node.name,
                "line": str(node.lineno),
            },
            tags=[],
            aliases=[],
            parameters=parameters,
            returns=returns_doc,
            examples=examples,
            when_to_use=[],
            when_not_to_use=[],
            stability="",
            domain="",
        )

    def _extract_parameters(self, node: ast.FunctionDef, args_doc: dict[str, _ParsedDocArg]) -> list[MethodParameter]:
        positional_args = list(node.args.posonlyargs) + list(node.args.args)
        filtered_positional_args = [arg for arg in positional_args if arg.arg != "self"]
        defaults = list(node.args.defaults)
        default_offset = len(filtered_positional_args) - len(defaults)
        default_map: dict[str, Any] = {}
        for index, arg in enumerate(filtered_positional_args):
            if index >= default_offset:
                default_node = defaults[index - default_offset]
                default_map[arg.arg] = _safe_literal(default_node)

        kw_defaults = list(node.args.kw_defaults)
        kw_default_map: dict[str, Any] = {}
        kw_required_map: dict[str, bool] = {}
        for arg, default_node in zip(node.args.kwonlyargs, kw_defaults):
            if arg.arg == "self":
                continue
            if default_node is None:
                kw_required_map[arg.arg] = True
            else:
                kw_required_map[arg.arg] = False
                kw_default_map[arg.arg] = _safe_literal(default_node)

        parameters: list[MethodParameter] = []
        for arg in filtered_positional_args:
            annotation = ast.unparse(arg.annotation) if arg.annotation is not None else "Any"
            parsed_arg = args_doc.get(arg.arg, _ParsedDocArg(name=arg.arg))
            schema_fields = _parse_nested_schema_fields(parsed_arg.extra_lines)
            parameters.append(
                MethodParameter(
                    name=arg.arg,
                    type=parsed_arg.declared_type or annotation,
                    required=arg.arg not in default_map,
                    description=parsed_arg.description,
                    default=default_map.get(arg.arg),
                    schema_fields=schema_fields,
                )
            )

        for arg in node.args.kwonlyargs:
            if arg.arg == "self":
                continue
            annotation = ast.unparse(arg.annotation) if arg.annotation is not None else "Any"
            parsed_arg = args_doc.get(arg.arg, _ParsedDocArg(name=arg.arg))
            schema_fields = _parse_nested_schema_fields(parsed_arg.extra_lines)
            parameters.append(
                MethodParameter(
                    name=arg.arg,
                    type=parsed_arg.declared_type or annotation,
                    required=kw_required_map.get(arg.arg, True),
                    description=parsed_arg.description,
                    default=kw_default_map.get(arg.arg),
                    schema_fields=schema_fields,
                )
            )

        if node.args.vararg is not None and node.args.vararg.arg != "self":
            arg = node.args.vararg
            annotation = ast.unparse(arg.annotation) if arg.annotation is not None else "Any"
            parsed_arg = args_doc.get(arg.arg, _ParsedDocArg(name=arg.arg))
            schema_fields = _parse_nested_schema_fields(parsed_arg.extra_lines)
            parameters.append(
                MethodParameter(
                    name=f"*{arg.arg}",
                    type=parsed_arg.declared_type or f"*{annotation}",
                    required=False,
                    description=parsed_arg.description,
                    schema_fields=schema_fields,
                )
            )

        if node.args.kwarg is not None and node.args.kwarg.arg != "self":
            arg = node.args.kwarg
            annotation = ast.unparse(arg.annotation) if arg.annotation is not None else "Any"
            parsed_arg = args_doc.get(arg.arg, _ParsedDocArg(name=arg.arg))
            schema_fields = _parse_nested_schema_fields(parsed_arg.extra_lines)
            parameters.append(
                MethodParameter(
                    name=f"**{arg.arg}",
                    type=parsed_arg.declared_type or f"**{annotation}",
                    required=False,
                    description=parsed_arg.description,
                    schema_fields=schema_fields,
                )
            )
        return parameters

    def _build_preview_param_names(self, node: ast.FunctionDef) -> list[str]:
        names: list[str] = []
        for arg in [*node.args.posonlyargs, *node.args.args]:
            if arg.arg != "self":
                names.append(arg.arg)
        if node.args.vararg is not None and node.args.vararg.arg != "self":
            names.append(f"*{node.args.vararg.arg}")
        for arg in node.args.kwonlyargs:
            if arg.arg != "self":
                names.append(arg.arg)
        if node.args.kwarg is not None and node.args.kwarg.arg != "self":
            names.append(f"**{node.args.kwarg.arg}")
        return names


def extract_method_registry_from_python_class(
    source_path: Path,
    class_name: str,
    output_path: Path,
    registry_name: str | None = None,
    description: str = "",
    filter_config: ExposureFilterConfig | None = None,
    manual_overrides: dict[str, bool] | None = None,
) -> MethodRegistry:
    extractor = PythonClassMethodExtractor()
    registry = extractor.extract_method_registry(
        source_path=source_path,
        class_name=class_name,
        registry_name=registry_name,
        description=description,
        filter_config=filter_config,
        manual_overrides=manual_overrides,
    )
    extractor.dump_registry_yaml(registry, output_path)
    return registry


def _serialize_method_entry(entry: MethodRegistryEntry) -> dict[str, Any]:
    payload = asdict(entry)
    for key in ("tags", "when_to_use", "when_not_to_use", "stability", "domain"):
        value = payload.get(key)
        if value in ([], ""):
            payload.pop(key, None)
    return payload


def _parse_google_style_docstring(docstring: str) -> tuple[str, str, dict[str, _ParsedDocArg], str, list[str]]:
    if not docstring.strip():
        return "", "", {}, "", []

    lines = [line.rstrip() for line in docstring.splitlines()]
    summary = lines[0].strip() if lines else ""
    sections: dict[str, list[str]] = {"description": [], "args": [], "returns": [], "examples": []}
    current = "description"
    for line in lines[1:]:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered in {"args:", "arguments:"}:
            current = "args"
            continue
        if lowered in {"returns:", "return:"}:
            current = "returns"
            continue
        if lowered in {"examples:", "example:"}:
            current = "examples"
            continue
        sections[current].append(line)

    description = "\n".join(line.strip() for line in sections["description"] if line.strip()).strip()
    args_doc = _parse_args_section(sections["args"])
    returns_doc = " ".join(line.strip() for line in sections["returns"] if line.strip()).strip()
    examples = [line.strip().lstrip("- ") for line in sections["examples"] if line.strip()]
    return summary, description, args_doc, returns_doc, examples


def _parse_args_section(lines: list[str]) -> dict[str, _ParsedDocArg]:
    result: dict[str, _ParsedDocArg] = {}
    current_name = ""
    current_description = ""
    current_declared_type = ""
    current_extra_lines: list[str] = []
    base_indent: int | None = None
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        match = re.match(r"^(\*\*?\w+|\w+)(?:\s*\(([^)]+)\))?\s*:\s*(.*)$", stripped)
        is_top_level_arg = bool(match) and (base_indent is None or indent <= base_indent)
        if is_top_level_arg and match:
            if base_indent is None:
                base_indent = indent
            if current_name:
                result[current_name] = _ParsedDocArg(
                    name=current_name,
                    description=current_description.strip(),
                    declared_type=current_declared_type.strip(),
                    extra_lines=list(current_extra_lines),
                )
            current_name = match.group(1).lstrip("*")
            current_declared_type = (match.group(2) or "").strip()
            current_description = match.group(3).strip()
            current_extra_lines = []
            continue
        if current_name:
            current_extra_lines.append(raw_line)
    if current_name:
        result[current_name] = _ParsedDocArg(
            name=current_name,
            description=current_description.strip(),
            declared_type=current_declared_type.strip(),
            extra_lines=list(current_extra_lines),
        )
    return result


def _parse_nested_schema_fields(lines: list[str]) -> list[ParameterSchemaField]:
    fields: list[ParameterSchemaField] = []
    current_fields: list[ParameterSchemaField] = []

    def flush_current() -> None:
        nonlocal current_fields
        if current_fields:
            fields.extend(current_fields)
            current_fields = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(("supported keys:", "common keys include:", "common keys:", "options:")):
            continue
        match = re.match(r"^[-*]\s*(.+?)\s*(?:\(([^)]+)\))?\s*:\s*(.*)$", stripped)
        if match:
            flush_current()
            parsed_type, required = _parse_field_type(match.group(2) or "")
            field_names = _split_schema_field_names(match.group(1))
            if not field_names:
                continue
            current_fields = [
                ParameterSchemaField(
                    name=field_name,
                    type=parsed_type,
                    description=match.group(3).strip(),
                    required=required,
                )
                for field_name in field_names
            ]
            continue
        if current_fields and stripped.lower().startswith("example:"):
            example_text = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            for current in current_fields:
                current.example = example_text
                current.description = f"{current.description} Example: {example_text}".strip()
            continue
        if stripped.endswith(":"):
            flush_current()
            continue
        if current_fields:
            for current in current_fields:
                current.description = f"{current.description} {stripped}".strip()
    flush_current()
    return fields


def _split_schema_field_names(raw_name: str) -> list[str]:
    cleaned = raw_name.replace("`", " ").replace("'", " ").replace('"', " ")
    parts = re.split(r"\s*/\s*|\s*,\s*", cleaned)
    names = [re.sub(r"\s+", " ", part).strip() for part in parts]
    return [name for name in names if name]


def _parse_field_type(raw_type: str) -> tuple[str, bool]:
    cleaned = raw_type.strip()
    if not cleaned:
        return "string", False
    lowered = cleaned.lower()
    required = "optional" not in lowered
    normalized = re.sub(r"\boptional\b", "", cleaned, flags=re.IGNORECASE)
    normalized = normalized.replace(", ,", ",")
    normalized = normalized.strip(" ,") or "string"
    return normalized, required


def _is_decorator_like(node: ast.FunctionDef) -> bool:
    public_args = [arg.arg for arg in node.args.args if arg.arg != "self"]
    if node.name.lower().startswith("wrapper"):
        return True
    if public_args == ["func"]:
        return True
    for child in node.body:
        if isinstance(child, ast.FunctionDef):
            return True
    return False


def _safe_literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        try:
            return ast.unparse(node)
        except Exception:
            return None


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""