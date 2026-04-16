from __future__ import annotations

from dataclasses import asdict
from typing import Any

import yaml

from ..ir.models import ConversionIR, PlannedStep


class YamlCompiler:
    def compile_to_dict(self, ir: ConversionIR) -> dict[str, Any]:
        return {
            "version": ir.ir_version,
            "source": {
                "type": ir.source,
                "session_id": ir.source_session_id,
            },
            "metadata": ir.metadata,
            "steps": [self._compile_step(step) for step in ir.steps],
            "unresolved_steps": list(ir.unresolved_steps),
        }

    def compile_to_text(self, ir: ConversionIR) -> str:
        return yaml.safe_dump(self.compile_to_dict(ir), allow_unicode=True, sort_keys=False)

    def _compile_step(self, step: PlannedStep) -> dict[str, Any]:
        return {
            "id": step.step_id,
            "type": step.step_type,
            "call": step.target_name,
            "args": dict(step.args),
            "source_step_ids": list(step.source_step_ids),
            "confidence": round(step.confidence, 3),
            "retrieval_reason": step.retrieval_reason,
            "notes": list(step.notes),
        }


def compile_ir_to_yaml_dict(ir: ConversionIR) -> dict[str, Any]:
    return YamlCompiler().compile_to_dict(ir)


def compile_ir_to_yaml_text(ir: ConversionIR) -> str:
    return YamlCompiler().compile_to_text(ir)