from __future__ import annotations

import json
from typing import Any


def build_parameter_recommendation_system_prompt() -> str:
    return (
        "你是自动化测试方法参数推荐助手。"
        "你必须基于给定语义步骤、原始上下文和候选方法元数据，推荐最合理的方法参数。"
        "不要臆造不存在的信息；无法确认的参数请保留空值并写 missing_reason。"
        "如果候选方法包含 schema_fields，请优先将信息映射到具体子字段。"
        "仅返回 JSON 对象。"
    )


def build_parameter_recommendation_prompt(suggestion: dict[str, Any], top_candidates: list[dict[str, Any]]) -> str:
    step_payload = dict(suggestion)
    step_payload.pop("step_conclusion", None)
    payload = {
        "task": "基于语义步骤和候选方法，推荐最终方法参数",
        "requirements": [
            "优先复用已知步骤文本、控件信息、键盘输入、窗口信息",
            "step 中只把 step_description 视为步骤语义，不要把结论性说明当作参数事实依据",
            "只在给定候选方法中选择，不要发明新的方法名",
            "无法确认的参数不要臆造，请明确标记 missing_reason",
            "如果参数存在 schema_fields，请尽量映射到具体子字段",
            "仅输出 JSON，不要 markdown 代码块",
        ],
        "json_schema_hint": {
            "selected_method": "string",
            "reason": "string",
            "parameters": [
                {
                    "name": "string",
                    "suggested_value": "any",
                    "confidence": 0.0,
                    "evidence": ["string"],
                    "missing_reason": "string",
                }
            ],
            "notes": ["string"],
        },
        "step": step_payload,
        "top_candidates": top_candidates,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)