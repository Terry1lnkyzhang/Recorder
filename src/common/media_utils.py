from __future__ import annotations

import hashlib
from pathlib import Path

import imageio
from PIL import Image


def load_video_preview_frame(video_path: Path) -> Image.Image | None:
    try:
        reader = imageio.get_reader(video_path)
        try:
            frame = reader.get_data(0)
            return Image.fromarray(frame)
        finally:
            reader.close()
    except Exception:
        return None


def file_md5(path: Path) -> str | None:
    try:
        digest = hashlib.md5()
        with path.open("rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None