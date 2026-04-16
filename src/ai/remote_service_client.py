from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from src.ai.models import AnalysisBatchRecord, SessionAnalysisResult
from src.ai.service_bundle import create_bundle, encode_bundle_base64
from src.ai.service_contract import REMOTE_SERVICE_API_VERSION
from src.ai.suggestions.models import MethodParameterSuggestion, MethodSelectionSuggestion, SuggestionGenerationResult
from src.recorder.settings import AISettings

from .errors import AIClientError


class RemoteAIServiceClient:
    def __init__(self, settings: AISettings) -> None:
        self.settings = settings

    def check_connection(self) -> tuple[bool, str]:
        try:
            response = requests.get(
                self._build_url("/health"),
                headers=self._build_headers(),
                timeout=min(20, self.settings.remote_ai_service_timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            return False, f"远端 AI 服务连接失败: {exc}"
        except Exception as exc:
            return False, f"远端 AI 服务返回无效: {exc}"
        version = str(payload.get("api_version", "")) if isinstance(payload, dict) else ""
        return True, f"远端 AI 服务可用: HTTP {response.status_code} {version}".strip()

    def analyze_session(self, session_dir: Path, session_data: dict[str, object]) -> SessionAnalysisResult:
        files = _collect_session_analysis_files(session_dir, session_data)
        bundle = create_bundle(session_dir.name, "session-analysis", files)
        payload = {
            "api_version": REMOTE_SERVICE_API_VERSION,
            "session_dir_name": session_dir.name,
            "session_data": session_data,
            "bundle_name": bundle.bundle_name,
            "bundle_base64": encode_bundle_base64(bundle),
            "analysis_batch_size": int(self.settings.analysis_batch_size),
            "send_fullscreen_screenshots": bool(self.settings.send_fullscreen_screenshots),
        }
        response_payload = self._post_json("/api/session-analysis", payload)
        result_payload = response_payload.get("result") if isinstance(response_payload, dict) else None
        if not isinstance(result_payload, dict):
            raise AIClientError("远端 AI 服务未返回有效的分析结果。")
        return _session_analysis_result_from_dict(result_payload)

    def build_method_suggestions(
        self,
        session_dir: Path,
        session_data: dict[str, object],
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 3,
        top_k_scripts: int = 2,
    ) -> SuggestionGenerationResult:
        files = _collect_suggestion_files(
            session_dir=session_dir,
            session_data=session_data,
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
        )
        bundle = create_bundle(session_dir.name, "method-suggestions", files)
        payload = {
            "api_version": REMOTE_SERVICE_API_VERSION,
            "session_dir_name": session_dir.name,
            "session_id": str(session_data.get("session_id", session_dir.name)),
            "bundle_name": bundle.bundle_name,
            "bundle_base64": encode_bundle_base64(bundle),
            "top_k_methods": int(top_k_methods),
            "top_k_scripts": int(top_k_scripts),
        }
        response_payload = self._post_json("/api/method-suggestions", payload)
        result_payload = response_payload.get("result") if isinstance(response_payload, dict) else None
        if not isinstance(result_payload, dict):
            raise AIClientError("远端 AI 服务未返回有效的方法建议结果。")
        return SuggestionGenerationResult.from_dict(result_payload)

    def recommend_parameters(
        self,
        session_dir: Path,
        session_data: dict[str, object],
        suggestion: MethodSelectionSuggestion,
        ai_analysis_path: Path,
        methods_registry_path: Path,
        session_path: Path | None = None,
        scripts_registry_path: Path | None = None,
        top_k_methods: int = 3,
        top_k_scripts: int = 2,
    ) -> dict[str, object]:
        files = _collect_suggestion_files(
            session_dir=session_dir,
            session_data=session_data,
            ai_analysis_path=ai_analysis_path,
            methods_registry_path=methods_registry_path,
            session_path=session_path,
            scripts_registry_path=scripts_registry_path,
        )
        bundle = create_bundle(session_dir.name, "parameter-recommendation", files)
        payload = {
            "api_version": REMOTE_SERVICE_API_VERSION,
            "session_dir_name": session_dir.name,
            "bundle_name": bundle.bundle_name,
            "bundle_base64": encode_bundle_base64(bundle),
            "suggestion": suggestion.to_dict(),
            "top_k_methods": int(top_k_methods),
            "top_k_scripts": int(top_k_scripts),
        }
        response_payload = self._post_json("/api/parameter-recommendation", payload)
        if not isinstance(response_payload, dict):
            raise AIClientError("远端 AI 服务未返回有效的参数推荐结果。")
        return response_payload

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        try:
            response = requests.post(
                self._build_url(path),
                headers=self._build_headers(),
                json=payload,
                timeout=self.settings.remote_ai_service_timeout_seconds,
            )
            response.raise_for_status()
            parsed = response.json()
        except requests.RequestException as exc:
            raise AIClientError(f"远端 AI 服务请求失败: {exc}") from exc
        except Exception as exc:
            raise AIClientError(f"远端 AI 服务返回无效 JSON: {exc}") from exc
        if isinstance(parsed, dict) and parsed.get("error"):
            raise AIClientError(str(parsed.get("error")))
        return parsed if isinstance(parsed, dict) else {}

    def _build_url(self, path: str) -> str:
        base_url = self.settings.remote_ai_service_url.strip().rstrip("/")
        if not base_url:
            raise AIClientError("未配置远端 AI 服务地址。")
        return f"{base_url}{path}"

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.remote_ai_service_api_key.strip():
            headers["Authorization"] = f"Bearer {self.settings.remote_ai_service_api_key.strip()}"
        return headers


def _collect_session_analysis_files(session_dir: Path, session_data: dict[str, object]) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    session_path = session_dir / "session.json"
    if session_path.exists():
        files.append((session_path, "session.json"))
    memory_path = session_dir / "ai_batch_memory.json"
    if memory_path.exists():
        files.append((memory_path, "ai_batch_memory.json"))
    added: set[Path] = set()
    events = session_data.get("events", []) if isinstance(session_data.get("events", []), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        candidate_paths: list[str] = []
        screenshot = event.get("screenshot")
        if screenshot:
            candidate_paths.append(str(screenshot))
        media = event.get("media", [])
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict) and item.get("path"):
                    candidate_paths.append(str(item.get("path")))
        for relative in candidate_paths:
            source_path = session_dir / relative
            if source_path.exists() and source_path not in added:
                files.append((source_path, relative))
                added.add(source_path)
    return files


def _collect_suggestion_files(
    session_dir: Path,
    session_data: dict[str, object],
    ai_analysis_path: Path,
    methods_registry_path: Path,
    session_path: Path | None = None,
    scripts_registry_path: Path | None = None,
) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    effective_session_path = session_path or (session_dir / "session.json")
    if effective_session_path.exists():
        files.append((effective_session_path, "session.json"))
    elif session_data:
        temp_session_path = session_dir / "session.json"
        if temp_session_path.exists():
            files.append((temp_session_path, "session.json"))
    if ai_analysis_path.exists():
        files.append((ai_analysis_path, "ai_analysis.json"))
    if methods_registry_path.exists():
        files.append((methods_registry_path, "registry/methods_registry" + methods_registry_path.suffix))
    if scripts_registry_path and scripts_registry_path.exists():
        files.append((scripts_registry_path, "registry/scripts_registry" + scripts_registry_path.suffix))
    return files


def _session_analysis_result_from_dict(payload: dict[str, Any]) -> SessionAnalysisResult:
    batches = []
    for item in payload.get("batches", []):
        if not isinstance(item, dict):
            continue
        batches.append(
            AnalysisBatchRecord(
                batch_id=str(item.get("batch_id", "")),
                start_step=int(item.get("start_step", 0) or 0),
                end_step=int(item.get("end_step", 0) or 0),
                event_indexes=[int(value) for value in item.get("event_indexes", []) if isinstance(value, int)],
                image_paths=[str(value) for value in item.get("image_paths", []) if str(value).strip()],
                prompt_preview=str(item.get("prompt_preview", "")),
                response_text=str(item.get("response_text", "")),
                parsed_result=item.get("parsed_result", {}) if isinstance(item.get("parsed_result", {}), dict) else {},
            )
        )
    return SessionAnalysisResult(
        session_id=str(payload.get("session_id", "")),
        batch_size=int(payload.get("batch_size", 1) or 1),
        status=str(payload.get("status", "completed")),
        failure_message=str(payload.get("failure_message", "")),
        carry_memory=[item for item in payload.get("carry_memory", []) if isinstance(item, dict)],
        batches=batches,
        step_insights=[item for item in payload.get("step_insights", []) if isinstance(item, dict)],
        invalid_steps=[item for item in payload.get("invalid_steps", []) if isinstance(item, dict)],
        reusable_modules=[item for item in payload.get("reusable_modules", []) if isinstance(item, dict)],
        wait_suggestions=[item for item in payload.get("wait_suggestions", []) if isinstance(item, dict)],
        analysis_notes=[str(item) for item in payload.get("analysis_notes", []) if isinstance(item, str)],
        workflow_report_markdown=str(payload.get("workflow_report_markdown", "")),
    )
