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
        self._append_image_attachments(content, image_paths=image_paths, inline_images=inline_images)

        sampled_frames = []
        if video_path:
            sampled_frames = self._sample_video_frames(video_path, max_frames=self.settings.video_frame_count)
            if sampled_frames:
                self._append_sampled_video_frames(content, video_path, sampled_frames)

        body = self._build_request_body(
            content=content,
            system_prompt=system_prompt,
            extra_body=extra_body,
        )

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
                    "timeout_seconds": self.settings.timeout_seconds,
                },
            )
        if cancel_callback and cancel_callback():
            raise AIClientError("AI 分析已取消。")

        session = requests.Session()
        with self._session_lock:
            self._active_session = session
        response: requests.Response | None = None
        try:
            response = self._post_with_fallback(session, headers, body)
        except requests.HTTPError as exc:
            if cancel_callback and cancel_callback():
                raise AIClientError("AI 分析已取消。") from exc
            if response is None:
                if cancel_callback and cancel_callback():
                    raise AIClientError("AI 分析已取消。") from exc
                response = exc.response
                status_code = response.status_code if response is not None else "?"
                preview = ""
                if response is not None:
                    try:
                        preview = response.text[:600].replace("\n", " ").strip()
                    except Exception:
                        preview = ""
                message = f"AI 请求失败: HTTP {status_code}"
                if preview:
                    message = f"{message} | {preview}"
                raise AIClientError(message) from exc
        except requests.RequestException as exc:
            if cancel_callback and cancel_callback():
                raise AIClientError("AI 分析已取消。") from exc
            raise AIClientError(f"AI 请求失败: {exc}") from exc
        finally:
            with self._session_lock:
                if self._active_session is session:
                    self._active_session = None
            session.close()
        if response is None:
            raise AIClientError("AI 请求失败: 未获得有效响应。")
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
            "video_delivery_mode": "sampled_frames" if sampled_frames else "none",
        }

    def _build_request_body(
        self,
        content: list[dict[str, object]],
        system_prompt: str | None,
        extra_body: dict[str, Any] | None,
    ) -> dict[str, object]:
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
        return body

    def _post_with_fallback(self, session: requests.Session, headers: dict[str, str], body: dict[str, object]) -> requests.Response:
        variants = self._build_request_body_variants(body)
        last_error: requests.HTTPError | None = None
        for variant in variants:
            response = session.post(
                self.settings.endpoint,
                headers=headers,
                json=variant,
                timeout=self.settings.timeout_seconds,
            )
            try:
                response.raise_for_status()
                return response
            except requests.HTTPError as exc:
                last_error = exc
                if not self._should_retry_with_minimal_body(response):
                    raise
        if last_error is not None:
            raise last_error
        raise AIClientError("AI 请求失败: 未获得有效响应。")

    def _build_request_body_variants(self, body: dict[str, object]) -> list[dict[str, object]]:
        variants: list[dict[str, object]] = [dict(body)]

        without_chat_template = dict(body)
        without_chat_template.pop("chat_template_kwargs", None)
        if without_chat_template != variants[-1]:
            variants.append(without_chat_template)

        minimal_body = {
            "model": body.get("model", self.settings.model),
            "messages": body.get("messages", []),
        }
        extra_fields = body.get("max_tokens")
        if extra_fields is not None:
            minimal_body["max_tokens"] = extra_fields
        if minimal_body != variants[-1]:
            variants.append(minimal_body)

        return variants

    @staticmethod
    def _should_retry_with_minimal_body(response: requests.Response | None) -> bool:
        if response is None:
            return False
        if response.status_code < 500:
            return False
        try:
            preview = response.text[:600].lower()
        except Exception:
            preview = ""
        return "param null" in preview or "null" in preview or "internal server error" in preview

    def _append_image_attachments(
        self,
        content: list[dict[str, object]],
        image_paths: list[Path] | None,
        inline_images: list[Image.Image] | None,
    ) -> None:
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

    def _append_sampled_video_frames(self, content: list[dict[str, object]], video_path: Path, sampled_frames: list[Image.Image]) -> None:
        content.append(
            {
                "type": "text",
                "text": self._build_sampled_video_attachment_text(video_path, len(sampled_frames)),
            }
        )
        for frame in sampled_frames:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._build_image_data_url(frame)},
                }
            )

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

    def _build_sampled_video_attachment_text(self, video_path: Path, frame_count: int) -> str:
        mode = self._normalized_video_sampling_mode()
        frame_limit = int(self.settings.video_frame_count)
        limit_text = "不设上限" if frame_limit == 0 else f"最多 {frame_limit} 帧"
        if mode == "fixed_interval":
            interval = self._video_frame_interval_seconds()
            return f"附带 1 段视频，已按每 {interval:g} 秒 1 帧抽取 {frame_count} 帧供分析（{limit_text}），原视频文件名: {video_path.name}"
        return f"附带 1 段视频，已按固定帧数均匀抽取 {frame_count} 帧供分析，原视频文件名: {video_path.name}"

    def _sample_video_frames(self, path: Path, max_frames: int) -> list[Image.Image]:
        mode = self._normalized_video_sampling_mode()
        if mode == "fixed_interval":
            return self._sample_video_frames_by_interval(path, self._video_frame_interval_seconds(), max_frames)
        return self._sample_video_frames_evenly(path, max_frames)

    def _normalized_video_sampling_mode(self) -> str:
        mode = str(getattr(self.settings, "video_sampling_mode", "fixed_interval") or "fixed_interval").strip().lower()
        if mode in {"fixed_interval", "fixed_count"}:
            return mode
        return "fixed_interval"

    def _video_frame_interval_seconds(self) -> float:
        try:
            interval = float(getattr(self.settings, "video_frame_interval_seconds", 0.5))
        except Exception:
            interval = 0.5
        return max(0.1, interval)

    def _sample_video_frames_by_interval(self, path: Path, interval_seconds: float, max_frames: int) -> list[Image.Image]:
        reader = imageio.get_reader(str(path))
        try:
            try:
                metadata = reader.get_meta_data()
                fps = float(metadata.get("fps") or 0)
            except Exception:
                fps = 0.0
            frame_step = max(1, int(round(fps * interval_seconds))) if fps > 0 else 1
            sampled: list[Image.Image] = []
            last_frame_index = -1
            last_sampled_index = -1
            last_frame_image: Image.Image | None = None
            for frame_index, frame in enumerate(reader.iter_data()):
                image = Image.fromarray(frame).convert("RGB")
                last_frame_index = frame_index
                last_frame_image = image
                if frame_index == 0 or frame_index % frame_step == 0:
                    sampled.append(image)
                    last_sampled_index = frame_index
                    if max_frames > 0 and len(sampled) >= max_frames:
                        break
            if last_frame_image is not None and last_sampled_index != last_frame_index:
                if max_frames <= 0 or len(sampled) < max_frames:
                    sampled.append(last_frame_image)
                elif sampled:
                    sampled[-1] = last_frame_image
            return sampled
        except Exception:
            return self._sample_video_frames_evenly(path, max_frames if max_frames > 0 else 12)
        finally:
            reader.close()

    def _sample_video_frames_evenly(self, path: Path, max_frames: int) -> list[Image.Image]:
        if max_frames <= 0:
            return []
        reader = imageio.get_reader(str(path))
        try:
            frame_total = reader.count_frames()
            if frame_total <= 0:
                return []
            if max_frames == 1:
                return [Image.fromarray(reader.get_data(frame_total - 1)).convert("RGB")]
            step = max(1, frame_total // max_frames)
            sampled: list[Image.Image] = []
            for frame_index in range(0, frame_total, step):
                frame = reader.get_data(frame_index)
                sampled.append(Image.fromarray(frame).convert("RGB"))
                if len(sampled) >= max_frames - 1:
                    break
            final_frame = Image.fromarray(reader.get_data(frame_total - 1)).convert("RGB")
            sampled.append(final_frame)
            return sampled[:max_frames]
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