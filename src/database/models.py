from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MySQLConnectionInfo:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"


@dataclass(slots=True)
class PromptTemplateModel:
    key: str
    label: str
    content: str


@dataclass(slots=True)
class TestcaseManagementRecord:
    testcase_id: str
    status: str
    designer: str
    script_version: str
    update_time: str
