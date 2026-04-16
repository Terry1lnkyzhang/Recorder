from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ai.client import OpenAICompatibleAIClient
from src.ai.errors import AIClientError
from src.converter.pipeline.method_candidates import build_retrieval_preview_from_files

from .method_selection import build_method_selection_result
from .models import MethodSelectionSuggestion, SuggestionGenerationResult
from .parameter_recommendation import parse_parameter_recommendation_payload, parse_parameter_recommendation_response_text
from .prompt_builder import build_parameter_recommendation_prompt, build_parameter_recommendation_system_prompt


class AISuggestionService:
    def build_retrieval_preview_from_files(
        self,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 5,
        top_k_scripts: int = 3,
    ) -> dict[str, Any]:
        return build_retrieval_preview_from_files(
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )

    def build_method_selection_from_files(
        self,
        session_id: str,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 5,
        top_k_scripts: int = 3,
    ) -> SuggestionGenerationResult:
        preview = self.build_retrieval_preview_from_files(
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )
        return build_method_selection_result(session_id=session_id, retrieval_preview=preview)

    def build_parameter_prompt_for_step_from_files(
        self,
        suggestion: MethodSelectionSuggestion,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 3,
        top_k_scripts: int = 2,
    ) -> tuple[str, dict[str, Any]]:
        preview = self.build_retrieval_preview_from_files(
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )
        return self.build_parameter_prompt_for_step(suggestion, preview), preview

    def write_result_file(self, output_path: Path, result: SuggestionGenerationResult) -> None:
        output_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_result_file(self, path: Path) -> SuggestionGenerationResult:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Suggestion result file must contain an object: {path}")
        return SuggestionGenerationResult.from_dict(payload)

    def build_parameter_prompt_for_step(
        self,
        suggestion: MethodSelectionSuggestion,
        retrieval_preview: dict[str, Any],
    ) -> str:
        step_result = next(
            (
                item
                for item in retrieval_preview.get("steps", [])
                if isinstance(item, dict) and int(item.get("step_id", 0) or 0) == suggestion.step_id
            ),
            {},
        )
        top_candidates = step_result.get("top_method_candidates", []) if isinstance(step_result.get("top_method_candidates", []), list) else []
        return build_parameter_recommendation_prompt(suggestion.to_dict(), top_candidates)

    def apply_parameter_recommendation(
        self,
        suggestion: MethodSelectionSuggestion,
        payload: dict[str, Any],
    ) -> list[str]:
        selected_method, reason, parameters, notes = parse_parameter_recommendation_payload(payload)
        if selected_method:
            suggestion.method_name = selected_method
        if reason:
            suggestion.reason = reason
        if parameters:
            suggestion.parameters = parameters
        return notes

    def recommend_parameters_for_suggestion(
        self,
        client: OpenAICompatibleAIClient,
        suggestion: MethodSelectionSuggestion,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 3,
        top_k_scripts: int = 2,
        system_prompt: str | None = None,
    ) -> tuple[list[str], dict[str, Any], str, str]:
        prompt, preview = self.build_parameter_prompt_for_step_from_files(
            suggestion=suggestion,
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
            top_k_methods=top_k_methods,
            top_k_scripts=top_k_scripts,
        )
        try:
            response = client.query(
                user_prompt=prompt,
                system_prompt=system_prompt or build_parameter_recommendation_system_prompt(),
            )
        except Exception as exc:
            if isinstance(exc, AIClientError):
                raise
            raise AIClientError(str(exc)) from exc
        response_text = str(response.get("response_text", ""))
        payload = parse_parameter_recommendation_response_text(response_text)
        notes = self.apply_parameter_recommendation(suggestion, payload)
        return notes, preview, prompt, response_text