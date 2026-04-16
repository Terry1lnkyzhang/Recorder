from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

from .service_contract import RemoteServiceBundle, build_bundle_member_name


def create_bundle(root_dir_name: str, bundle_name: str, files: list[tuple[Path, str]]) -> RemoteServiceBundle:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_path, relative_path in files:
            if not source_path.exists() or not source_path.is_file():
                continue
            archive.write(source_path, build_bundle_member_name(root_dir_name, relative_path))
    return RemoteServiceBundle(root_dir_name=root_dir_name, bundle_name=bundle_name, zip_bytes=buffer.getvalue())


def encode_bundle_base64(bundle: RemoteServiceBundle) -> str:
    return base64.b64encode(bundle.zip_bytes).decode("ascii")


def decode_bundle_base64(payload: str) -> bytes:
    return base64.b64decode(payload.encode("ascii"))
