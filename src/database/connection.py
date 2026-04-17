from __future__ import annotations

from contextlib import closing
import re
from urllib.parse import parse_qs, unquote, urlparse

from .models import MySQLConnectionInfo, TestcaseManagementRecord


_SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_sql_identifier(identifier: str, field_name: str) -> str:
    value = (identifier or "").strip()
    if not value:
        raise ValueError(f"{field_name} 不能为空。")
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} 包含非法字符: {value}")
    return value


def parse_mysql_connection_string(connection_string: str) -> MySQLConnectionInfo:
    raw_value = (connection_string or "").strip()
    if not raw_value:
        raise ValueError("未配置模板数据库连接字符串。")

    parsed = urlparse(raw_value)
    if parsed.scheme not in {"mysql+pymysql", "mysql"}:
        raise ValueError("数据库连接字符串必须使用 mysql+pymysql:// 或 mysql:// 格式。")
    if not parsed.hostname:
        raise ValueError("数据库连接字符串缺少 host。")
    if not parsed.username:
        raise ValueError("数据库连接字符串缺少 user。")

    database_name = parsed.path.lstrip("/").strip()
    if not database_name:
        raise ValueError("数据库连接字符串缺少 database。")

    query_params = parse_qs(parsed.query or "")
    charset = (query_params.get("charset") or ["utf8mb4"])[0] or "utf8mb4"
    return MySQLConnectionInfo(
        host=parsed.hostname,
        port=int(parsed.port or 3306),
        user=unquote(parsed.username),
        password=unquote(parsed.password or ""),
        database=unquote(database_name),
        charset=charset,
    )


def connect_mysql(connection_string: str):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError("缺少 pymysql 依赖，请先安装 pymysql。") from exc

    connection_info = parse_mysql_connection_string(connection_string)
    return pymysql.connect(
        host=connection_info.host,
        port=connection_info.port,
        user=connection_info.user,
        password=connection_info.password,
        database=connection_info.database,
        charset=connection_info.charset,
        cursorclass=DictCursor,
        autocommit=True,
    )


def fetch_latest_testcase_management_record(
    connection_string: str,
    testcase_id: str,
    *,
    table_name: str = "testcasemanagement",
    testcase_id_column: str = "TestcaseID",
    update_time_column: str = "UpdateTime",
    status_column: str = "Status",
    designer_column: str = "Designer",
    script_version_column: str = "ScriptVersion",
) -> TestcaseManagementRecord | None:
    normalized_testcase_id = (testcase_id or "").strip()
    if not normalized_testcase_id:
        return None

    resolved_table_name = validate_sql_identifier(table_name, "table_name")
    resolved_testcase_id_column = validate_sql_identifier(testcase_id_column, "testcase_id_column")
    resolved_update_time_column = validate_sql_identifier(update_time_column, "update_time_column")
    resolved_status_column = validate_sql_identifier(status_column, "status_column")
    resolved_designer_column = validate_sql_identifier(designer_column, "designer_column")
    resolved_script_version_column = validate_sql_identifier(script_version_column, "script_version_column")

    query = f"""
        SELECT
            `{resolved_testcase_id_column}` AS testcase_id,
            `{resolved_status_column}` AS status,
            `{resolved_designer_column}` AS designer,
            `{resolved_script_version_column}` AS script_version,
            `{resolved_update_time_column}` AS update_time
        FROM `{resolved_table_name}`
        WHERE `{resolved_testcase_id_column}` = %s
        ORDER BY (`{resolved_update_time_column}` IS NULL), `{resolved_update_time_column}` DESC
        LIMIT 1
    """

    with closing(connect_mysql(connection_string)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (normalized_testcase_id,))
            row = cursor.fetchone() or None

    if not isinstance(row, dict):
        return None

    return TestcaseManagementRecord(
        testcase_id=str(row.get("testcase_id") or normalized_testcase_id).strip(),
        status=str(row.get("status") or "").strip(),
        designer=str(row.get("designer") or "").strip(),
        script_version=str(row.get("script_version") or "").strip(),
        update_time=str(row.get("update_time") or "").strip(),
    )


def fetch_distinct_baseline_names(
    connection_string: str,
    *,
    table_name: str = "baselinetable",
    baseline_name_column: str = "BaseLineName",
) -> list[str]:
    resolved_table_name = validate_sql_identifier(table_name, "table_name")
    resolved_baseline_name_column = validate_sql_identifier(baseline_name_column, "baseline_name_column")

    query = f"""
        SELECT DISTINCT `{resolved_baseline_name_column}` AS baseline_name
        FROM `{resolved_table_name}`
        WHERE `{resolved_baseline_name_column}` IS NOT NULL
          AND TRIM(`{resolved_baseline_name_column}`) <> ''
        ORDER BY `{resolved_baseline_name_column}` ASC
    """

    with closing(connect_mysql(connection_string)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall() or []

    results: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(row.get("baseline_name") or "").strip()
        if value:
            results.append(value)
    return results
