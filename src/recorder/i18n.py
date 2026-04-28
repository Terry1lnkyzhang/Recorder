from __future__ import annotations

UI_LANGUAGE_ZH_CN = "zh-CN"
UI_LANGUAGE_EN_US = "en-US"

UI_LANGUAGE_OPTIONS: list[tuple[str, str]] = [
    ("简体中文", UI_LANGUAGE_ZH_CN),
    ("English", UI_LANGUAGE_EN_US),
]


def normalize_ui_language(language: str | None) -> str:
    value = str(language or "").strip().lower()
    if value in {"en", "en-us", "english"}:
        return UI_LANGUAGE_EN_US
    return UI_LANGUAGE_ZH_CN


def pick_text(language: str | None, zh_cn_text: str, en_us_text: str) -> str:
    if normalize_ui_language(language) == UI_LANGUAGE_EN_US:
        return en_us_text
    return zh_cn_text


def ui_language_labels() -> list[str]:
    return [label for label, _value in UI_LANGUAGE_OPTIONS]


def ui_language_to_label(language: str | None) -> str:
    normalized = normalize_ui_language(language)
    for label, value in UI_LANGUAGE_OPTIONS:
        if value == normalized:
            return label
    return UI_LANGUAGE_OPTIONS[0][0]


def ui_language_from_label(label: str | None) -> str:
    text = str(label or "").strip()
    for item_label, value in UI_LANGUAGE_OPTIONS:
        if item_label == text:
            return value
    return normalize_ui_language(text)