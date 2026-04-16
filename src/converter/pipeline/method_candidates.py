from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..registry.loader import load_method_registry, load_script_registry
from ..registry.models import RegistryBundle, RegistryMetadata, ScriptRegistry
from ..retrieval.retriever import RegistryRetriever
from ..retrieval.models import CandidateMatch, SemanticStep
from .semantic_steps import SemanticStepExtractor


def build_retrieval_preview_from_files(
    ai_analysis_path: Path,
    methods_registry_path: Path,
    session_path: Path | None = None,
    scripts_registry_path: Path | None = None,
    top_k_methods: int = 5,
    top_k_scripts: int = 3,
) -> dict[str, Any]:
    ai_analysis = _load_json_dict(ai_analysis_path)
    session_data = _load_json_dict(session_path) if session_path else None
    return build_retrieval_preview(
        ai_analysis=ai_analysis,
        methods_registry_path=methods_registry_path,
        session_data=session_data,
        scripts_registry_path=scripts_registry_path,
        top_k_methods=top_k_methods,
        top_k_scripts=top_k_scripts,
        ai_analysis_path=ai_analysis_path,
        session_path=session_path,
    )


def build_retrieval_preview(
    ai_analysis: dict[str, Any],
    methods_registry_path: Path,
    session_data: dict[str, Any] | None = None,
    scripts_registry_path: Path | None = None,
    top_k_methods: int = 5,
    top_k_scripts: int = 3,
    ai_analysis_path: Path | None = None,
    session_path: Path | None = None,
) -> dict[str, Any]:
    steps = SemanticStepExtractor.from_ai_analysis(ai_analysis, session_data=session_data)
    registry_bundle = RegistryBundle(
        methods=load_method_registry(methods_registry_path),
        scripts=load_script_registry(scripts_registry_path) if scripts_registry_path else _empty_script_registry(),
    )
    retriever = RegistryRetriever(registry_bundle)
    matches_by_step = retriever.retrieve_for_steps(steps, top_k_methods=top_k_methods, top_k_scripts=top_k_scripts)

    return {
        "metadata": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ai_analysis_path": str(ai_analysis_path) if ai_analysis_path else "",
            "session_path": str(session_path) if session_path else "",
            "methods_registry_path": str(methods_registry_path),
            "scripts_registry_path": str(scripts_registry_path) if scripts_registry_path else "",
            "top_k_methods": top_k_methods,
            "top_k_scripts": top_k_scripts,
            "step_count": len(steps),
        },
        "steps": [_serialize_step_result(step, matches_by_step.get(step.step_id, {})) for step in steps],
    }


def _serialize_step_result(step: SemanticStep, matches: dict[str, list[CandidateMatch]]) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "description": step.description,
        "conclusion": step.conclusion,
        "raw_text": step.raw_text,
        "window_title": step.window_title,
        "control_type": step.control_type,
        "event_type": step.event_type,
        "tags": list(step.tags),
        "context": dict(step.context),
        "top_method_candidates": [_serialize_candidate(item) for item in matches.get("methods", [])],
        "top_script_candidates": [_serialize_candidate(item) for item in matches.get("scripts", [])],
    }


def _serialize_candidate(candidate: CandidateMatch) -> dict[str, Any]:
    payload = dict(candidate.payload)
    payload.setdefault("exposed_keyword", payload.get("name", ""))
    return {
        "candidate_type": candidate.candidate_type,
        "name": candidate.name,
        "score": round(float(candidate.score), 3),
        "summary": candidate.summary,
        "reason": candidate.reason,
        "payload": payload,
    }


def _load_json_dict(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _empty_script_registry() -> ScriptRegistry:
    return ScriptRegistry(
        metadata=RegistryMetadata(
            name="empty-script-registry",
            version="1.0",
            description="No script registry configured for retrieval preview.",
            owner="Recorder Converter",
        ),
        entries=[],
    )