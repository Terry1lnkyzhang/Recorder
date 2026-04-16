from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class ExposureFilterConfig:
    exclude_names: list[str] = field(default_factory=list)
    include_names: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    include_only_documented: bool = False
    exclude_decorator_like: bool = False
    exclude_single_callable_parameter: bool = False


@dataclass(slots=True)
class ExtractedMethodPreview:
    name: str
    summary: str
    description: str
    source_line: int
    param_names: list[str] = field(default_factory=list)
    has_docstring: bool = False
    decorator_names: list[str] = field(default_factory=list)
    is_public: bool = True
    is_decorator_like: bool = False
    manual_exposed: bool | None = None
    exposed: bool = True
    filter_reason: str = ""


def apply_exposure_filters(previews: list[ExtractedMethodPreview], config: ExposureFilterConfig) -> None:
    include_names = {name.strip() for name in config.include_names if name.strip()}
    exclude_names = {name.strip() for name in config.exclude_names if name.strip()}
    patterns = [re.compile(pattern) for pattern in config.exclude_patterns if pattern.strip()]

    for preview in previews:
        if preview.manual_exposed is not None:
            preview.exposed = preview.manual_exposed
            preview.filter_reason = "手工覆盖"
            continue

        exposed = preview.is_public
        reasons: list[str] = []

        if preview.name in include_names:
            preview.exposed = True
            preview.filter_reason = "命中 include_names"
            continue

        if preview.name in exclude_names:
            exposed = False
            reasons.append("命中 exclude_names")

        if config.include_only_documented and not preview.has_docstring:
            exposed = False
            reasons.append("缺少 docstring")

        if config.exclude_decorator_like and preview.is_decorator_like:
            exposed = False
            reasons.append("疑似装饰器/包装器")

        if config.exclude_single_callable_parameter and preview.param_names == ["func"]:
            exposed = False
            reasons.append("单 callable 参数")

        for pattern in patterns:
            if pattern.search(preview.name):
                exposed = False
                reasons.append(f"命中正则 {pattern.pattern}")
                break

        preview.exposed = exposed
        preview.filter_reason = "；".join(reasons) if reasons else "默认暴露" if exposed else "被筛除"