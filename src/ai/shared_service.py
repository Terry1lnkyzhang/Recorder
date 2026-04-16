from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from src.ai.service_bundle import decode_bundle_base64
from src.ai.service_contract import REMOTE_SERVICE_API_VERSION
from src.ai.session_analyzer import SessionWorkflowAnalyzer
from src.ai.suggestions.models import MethodSelectionSuggestion
from src.ai.suggestions.service import AISuggestionService
from src.common.app_logging import get_logger
from src.common.runtime_paths import get_settings_path
from src.recorder.settings import SettingsStore


logger = get_logger("shared_service")


class SharedServiceError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def build_health_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "api_version": REMOTE_SERVICE_API_VERSION,
    }


def handle_session_analysis(payload: dict[str, Any], authorization: str | None = None) -> dict[str, object]:
    _validate_api_version(payload)
    _validate_authorization(authorization)
    temp_dir = _extract_bundle_to_temp_dir(payload)
    try:
        session_dir = temp_dir / str(payload.get("session_dir_name", "session"))
        session_data = payload.get("session_data", {}) if isinstance(payload.get("session_data", {}), dict) else {}
        session_path = session_dir / "session.json"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps(session_data, indent=2, ensure_ascii=False), encoding="utf-8")

        settings = _load_server_settings()
        settings.analysis_batch_size = int(payload.get("analysis_batch_size", settings.analysis_batch_size) or settings.analysis_batch_size)
        settings.send_fullscreen_screenshots = bool(payload.get("send_fullscreen_screenshots", settings.send_fullscreen_screenshots))
        analyzer = SessionWorkflowAnalyzer(settings)
        result = analyzer.analyze(session_dir, session_data)
        logger.info("Remote session analysis completed | session_dir=%s", session_dir)
        return {"result": result.to_dict()}
    except SharedServiceError:
        raise
    except Exception as exc:
        logger.exception("Remote session analysis failed")
        raise SharedServiceError(500, str(exc)) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def handle_method_suggestions(payload: dict[str, Any], authorization: str | None = None) -> dict[str, object]:
    _validate_api_version(payload)
    _validate_authorization(authorization)
    temp_dir = _extract_bundle_to_temp_dir(payload)
    try:
        session_dir = temp_dir / str(payload.get("session_dir_name", "session"))
        service = AISuggestionService()
        methods_registry_path = _require_registry_path(session_dir, "methods_registry")
        result = service.build_method_selection_from_files(
            session_id=str(payload.get("session_id", session_dir.name)),
            ai_analysis_path=session_dir / "ai_analysis.json",
            methods_registry_path=methods_registry_path,
            session_path=(session_dir / "session.json") if (session_dir / "session.json").exists() else None,
            scripts_registry_path=_resolve_registry_path(session_dir, "scripts_registry"),
            top_k_methods=int(payload.get("top_k_methods", 3) or 3),
            top_k_scripts=int(payload.get("top_k_scripts", 2) or 2),
        )
        logger.info("Remote method suggestion completed | session_dir=%s", session_dir)
        return {"result": result.to_dict()}
    except SharedServiceError:
        raise
    except Exception as exc:
        logger.exception("Remote method suggestion failed")
        raise SharedServiceError(500, str(exc)) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def handle_parameter_recommendation(payload: dict[str, Any], authorization: str | None = None) -> dict[str, object]:
    _validate_api_version(payload)
    _validate_authorization(authorization)
    temp_dir = _extract_bundle_to_temp_dir(payload)
    try:
        session_dir = temp_dir / str(payload.get("session_dir_name", "session"))
        settings = _load_server_settings()
        suggestion_payload = payload.get("suggestion", {}) if isinstance(payload.get("suggestion", {}), dict) else {}
        suggestion = MethodSelectionSuggestion.from_dict(suggestion_payload)
        service = AISuggestionService()
        from src.ai.client import OpenAICompatibleAIClient

        methods_registry_path = _require_registry_path(session_dir, "methods_registry")
        notes, preview, prompt_text, response_text = service.recommend_parameters_for_suggestion(
            client=OpenAICompatibleAIClient(settings),
            suggestion=suggestion,
            ai_analysis_path=session_dir / "ai_analysis.json",
            methods_registry_path=methods_registry_path,
            session_path=(session_dir / "session.json") if (session_dir / "session.json").exists() else None,
            scripts_registry_path=_resolve_registry_path(session_dir, "scripts_registry"),
            top_k_methods=int(payload.get("top_k_methods", 3) or 3),
            top_k_scripts=int(payload.get("top_k_scripts", 2) or 2),
        )
        logger.info("Remote parameter recommendation completed | step_id=%s", suggestion.step_id)
        return {
            "suggestion": suggestion.to_dict(),
            "notes": notes,
            "retrieval_preview": preview,
            "prompt_text": prompt_text,
            "response_text": response_text,
        }
    except SharedServiceError:
        raise
    except Exception as exc:
        logger.exception("Remote parameter recommendation failed")
        raise SharedServiceError(500, str(exc)) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _load_server_settings():
    return SettingsStore(get_settings_path()).load()


def _validate_api_version(payload: dict[str, Any]) -> None:
    version = str(payload.get("api_version", ""))
    if version != REMOTE_SERVICE_API_VERSION:
        raise SharedServiceError(400, f"Unsupported api_version: {version}")


def _validate_authorization(authorization: str | None) -> None:
    settings = _load_server_settings()
    expected = settings.remote_ai_service_api_key.strip()
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise SharedServiceError(401, "Unauthorized")


def _extract_bundle_to_temp_dir(payload: dict[str, Any]) -> Path:
    bundle_base64 = str(payload.get("bundle_base64", ""))
    if not bundle_base64:
        raise SharedServiceError(400, "Missing bundle_base64")
    temp_dir = Path(tempfile.mkdtemp(prefix="recorder_ai_service_"))
    bundle_bytes = decode_bundle_base64(bundle_base64)
    bundle_path = temp_dir / "bundle.zip"
    bundle_path.write_bytes(bundle_bytes)
    with zipfile.ZipFile(bundle_path, mode="r") as archive:
        archive.extractall(temp_dir)
    return temp_dir


def _resolve_registry_path(session_dir: Path, stem: str) -> Path | None:
    registry_dir = session_dir / "registry"
    if not registry_dir.exists():
        return None
    for candidate in registry_dir.iterdir():
        if candidate.is_file() and candidate.stem == stem:
            return candidate
    return None


def _require_registry_path(session_dir: Path, stem: str) -> Path:
    path = _resolve_registry_path(session_dir, stem)
    if path is None:
        raise SharedServiceError(400, f"Missing {stem} in bundle")
    return path
