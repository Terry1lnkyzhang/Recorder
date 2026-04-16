from __future__ import annotations

import base64
import io
import json
import mimetypes
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import imageio
import requests
from PIL import Image

from src.recorder.settings import AISettings, SettingsStore

from .errors import AIClientError


class OpenAICompatibleAIClient:
    def __init__(self, settings: AISettings) -> None:
        self.settings = settings
        self._active_session: requests.Session | None = None
        self._session_lock = threading.Lock()

    def query(
        self,
        user_prompt: str,
        image_paths: list[Path] | None = None,
        video_path: Path | None = None,
        system_prompt: str | None = None,
        inline_images: list[Image.Image] | None = None,
        extra_body: dict[str, Any] | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        if not self.settings.endpoint.strip():
            raise AIClientError("未配置 AI endpoint。")
        if not self.settings.model.strip():
            raise AIClientError("未配置 AI model。")

        content: list[dict[str, object]] = [{"type": "text", "text": user_prompt}]
        if cancel_callback and cancel_callback():
            raise AIClientError("AI 分析已取消。")
        if progress_callback:
            progress_callback(
                "prepare_media",
                {
                    "image_count": len(image_paths or []),
                    "inline_image_count": len(inline_images or []),
                    "has_video": bool(video_path),
                },
            )
        for image_path in image_paths or []:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._build_data_url(image_path)},
                }
            )
        for image in inline_images or []:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._build_image_data_url(image)},
                }
            )

        sampled_frames = []
        if video_path:
            if self.settings.send_video_directly:
                content.append(
                    {
                        "type": "text",
                        "text": f"附带 1 段原始视频，文件名: {video_path.name}",
                    }
                )
                content.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": self._build_data_url(video_path)},
                    }
                )
            else:
                sampled_frames = self._sample_video_frames(video_path, max_frames=self.settings.video_frame_count)
            if sampled_frames:
                content.append(
                    {
                        "type": "text",
                        "text": f"附带 1 段视频，已抽取 {len(sampled_frames)} 帧供分析，原视频文件名: {video_path.name}",
                    }
                )
                for frame in sampled_frames:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": self._build_image_data_url(frame)},
                        }
                    )

        body = {
            "model": self.settings.model,
            "temperature": self.settings.temperature,
            "chat_template_kwargs": {"enable_thinking": self.settings.enable_thinking},
            "messages": [
                {"role": "system", "content": system_prompt or self.settings.default_system_prompt},
                {"role": "user", "content": content},
            ],
        }
        if extra_body:
            body.update(extra_body)

        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        headers.update(SettingsStore.parse_extra_headers(self.settings.extra_headers_json))

        if progress_callback:
            progress_callback(
                "send_request",
                {
                    "image_count": len(image_paths or []),
                    "inline_image_count": len(inline_images or []),
                    "sampled_video_frames": len(sampled_frames),
                    "direct_video_upload": bool(video_path and self.settings.send_video_directly),
                    "timeout_seconds": self.settings.timeout_seconds,
                },
            )
        if cancel_callback and cancel_callback():
            raise AIClientError("AI 分析已取消。")

        session = requests.Session()
        with self._session_lock:
            self._active_session = session
        try:
            response = session.post(
                self.settings.endpoint,
                headers=headers,
                json=body,
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            if cancel_callback and cancel_callback():
                raise AIClientError("AI 分析已取消。") from exc
            raise
        finally:
            with self._session_lock:
                if self._active_session is session:
                    self._active_session = None
            session.close()
        if progress_callback:
            progress_callback(
                "response_received",
                {
                    "status_code": response.status_code,
                },
            )
        payload = response.json()

        if progress_callback:
            progress_callback(
                "parse_response",
                {
                    "status_code": response.status_code,
                },
            )
        content_text = self._extract_content_text(payload)
        return {
            "response_text": content_text,
            "raw_response": payload,
            "sampled_video_frames": len(sampled_frames),
            "direct_video_upload": bool(video_path and self.settings.send_video_directly),
        }

    def cancel(self) -> None:
        with self._session_lock:
            session = self._active_session
            self._active_session = None
        if session is not None:
            session.close()

    def check_connection(self) -> tuple[bool, str]:
        if not self.settings.endpoint.strip():
            return False, "未配置 endpoint"
        if not self.settings.model.strip():
            return False, "未配置 model"

        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        try:
            headers.update(SettingsStore.parse_extra_headers(self.settings.extra_headers_json))
        except Exception as exc:
            return False, f"extra headers 配置无效: {exc}"

        body = {
            "model": self.settings.model,
            "temperature": self.settings.temperature,
            "chat_template_kwargs": {"enable_thinking": self.settings.enable_thinking},
            "messages": [
                {"role": "system", "content": "Connection check"},
                {"role": "user", "content": [{"type": "text", "text": "ping"}]},
            ],
            "max_tokens": 1,
        }

        try:
            response = requests.post(
                self.settings.endpoint,
                headers=headers,
                json=body,
                timeout=min(20, self.settings.timeout_seconds),
            )
        except requests.RequestException as exc:
            return False, f"连接失败: {exc}"

        if response.ok:
            return True, f"连接正常: HTTP {response.status_code}"

        preview = response.text[:240].replace("\n", " ")
        return False, f"连接失败: HTTP {response.status_code} {preview}"

    def _build_data_url(self, path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _build_image_data_url(self, image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _sample_video_frames(self, path: Path, max_frames: int) -> list[Image.Image]:
        if max_frames <= 0:
            return []
        reader = imageio.get_reader(str(path))
        try:
            frame_total = reader.count_frames()
            if frame_total <= 0:
                return []
            step = max(1, frame_total // max_frames)
            sampled: list[Image.Image] = []
            for frame_index in range(0, frame_total, step):
                frame = reader.get_data(frame_index)
                sampled.append(Image.fromarray(frame))
                if len(sampled) >= max_frames:
                    break
            return sampled
        finally:
            reader.close()

    def _extract_content_text(self, payload: dict[str, Any]) -> str:
        try:
            choices = payload.get("choices", [])
            first_choice = choices[0]
            message = first_choice.get("message", {})
            content = message.get("content", "")
        except Exception as exc:
            raise AIClientError(f"无法解析模型返回: {json.dumps(payload, ensure_ascii=False)[:800]}") from exc

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
            return "\n".join(texts).strip()
        return str(content)