from __future__ import annotations

import json
from pathlib import Path


def load_carry_memory(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def save_carry_memory(path: Path, memory: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")