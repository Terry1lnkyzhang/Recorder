from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any

from src.recorder.settings import Settings

from .database import connect_mysql, validate_sql_identifier


_LABEL_COLUMN_CANDIDATES = (
    "PromptName",
    "TemplateName",
    "Name",
    "Title",
    "PromptTitle",
    "PromptTemplate",
)
_CONTENT_COLUMN_CANDIDATES = (
    "PromptContent",
    "PromptText",
    "TemplateBody",
    "Template",
    "Prompt",
    "Content",
    "SystemPrompt",
    "PromptTemplate",
)
_KEY_COLUMN_CANDIDATES = (
    "PromptKey",
    "TemplateKey",
    "Key",
    "ID",
    "Id",
    "id",
)


@dataclass(slots=True)
class PromptTemplateRecord:
    key: str
    label: str
    content: str


def load_checkpoint_prompt_templates(settings: Settings) -> list[PromptTemplateRecord]:
    connection_string = settings.prompt_db_connection_string.strip()
    if not connection_string:
        return []

    table_name = validate_sql_identifier(settings.checkpoint_prompt_table, "checkpoint_prompt_table")

    with closing(connect_mysql(connection_string)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{table_name}`")
            rows = cursor.fetchall() or []

    records: list[PromptTemplateRecord] = []
    seen_keys: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue

        content = _pick_first_non_empty_value(
            row,
            settings.checkpoint_prompt_content_column,
            _CONTENT_COLUMN_CANDIDATES,
        )
        if not content:
            continue

        label = _pick_first_non_empty_value(
            row,
            settings.checkpoint_prompt_label_column,
            _LABEL_COLUMN_CANDIDATES,
        )
        if not label:
            label = _summarize_template_content(content)

        key = _pick_first_non_empty_value(
            row,
            settings.checkpoint_prompt_key_column,
            _KEY_COLUMN_CANDIDATES,
        )
        if not key:
            key = label or f"template_{index}"

        normalized_key = key.casefold()
        if normalized_key in seen_keys:
            continue
        seen_keys.add(normalized_key)
        records.append(PromptTemplateRecord(key=key, label=label, content=content))

    return records


def _pick_first_non_empty_value(
    row: dict[str, Any],
    configured_column: str,
    candidate_columns: tuple[str, ...],
) -> str:
    column_names: list[str] = []
    if configured_column.strip():
        column_names.append(validate_sql_identifier(configured_column, "prompt column"))
    column_names.extend(candidate_columns)

    seen_names: set[str] = set()
    for column_name in column_names:
        lowered_name = column_name.casefold()
        if lowered_name in seen_names:
            continue
        seen_names.add(lowered_name)
        for actual_name, raw_value in row.items():
            if str(actual_name).casefold() != lowered_name:
                continue
            value = str(raw_value or "").strip()
            if value:
                return value
            break
    return ""


def _summarize_template_content(content: str) -> str:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    summary = first_line or content.strip()
    if len(summary) <= 48:
        return summary
    return summary[:45].rstrip() + "..."