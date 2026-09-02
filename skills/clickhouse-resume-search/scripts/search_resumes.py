#!/usr/bin/env python3
"""Validate and execute read-only ClickHouse resume searches."""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_LIMIT = 100
DEFAULT_DATABASE = "RCW_RC_Voodoo_Jobseeker"
DEFAULT_REQUEST_TIMEOUT = 30

ALLOWED_TABLES = {
    "RCW_RC_Voodoo_Jobseeker.JobSeekerBaseInfo",
    "RCW_RC_Voodoo_Jobseeker.JobSeekerResume",
    "RCW_RC_Voodoo_Jobseeker.JobSeekerResumeExperience",
    "RCW_RC_Voodoo_Jobseeker.JobSeekerResumeExtension",
    "RCW_RC_Voodoo_Jobseeker.JobSeekerResumeProjectExperience",
    "JobSeekerBaseInfo",
    "JobSeekerResume",
    "JobSeekerResumeExperience",
    "JobSeekerResumeExtension",
    "JobSeekerResumeProjectExperience",
}

FORBIDDEN_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CREATE|RENAME|OPTIMIZE|"
    r"SYSTEM|KILL|ATTACH|DETACH|GRANT|REVOKE|BACKUP|RESTORE)\b",
    re.IGNORECASE,
)
TABLE_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)",
    re.IGNORECASE,
)
CTE_NAME = re.compile(
    r"(?:\bWITH\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(",
    re.IGNORECASE,
)
PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error(code: str, message: str) -> SearchError:
    return SearchError(code, message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ClickHouse resume search validator and read-only executor"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "connection.json",
        help="Private connection JSON file",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the stdin request without connecting",
    )
    return parser.parse_args()


def read_request() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise error("INVALID_REQUEST", "Request must be one JSON object on stdin") from exc
    if not isinstance(value, dict):
        raise error("INVALID_REQUEST", "Request must be a JSON object")
    return value


def validate_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise error("INVALID_REQUEST", "limit must be a positive integer")
    return value


def validate_params(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise error("INVALID_REQUEST", "params must be a JSON object")
    output: dict[str, Any] = {}
    for name, param_value in value.items():
        if not isinstance(name, str) or not PARAMETER_NAME.fullmatch(name):
            raise error("INVALID_REQUEST", "parameter names must be SQL identifiers")
        if name == "limit":
            raise error("INVALID_REQUEST", "set limit with the top-level limit field")
        if not isinstance(param_value, (str, int, float, bool)) and param_value is not None:
            raise error("INVALID_REQUEST", f"parameter {name} must be a scalar value")
        output[name] = param_value
    return output


def validate_sql(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error("INVALID_SQL", "sql must be a non-empty string")
    sql = value.strip()
    if ";" in sql:
        raise error("INVALID_SQL", "multiple statements and semicolons are not allowed")
    collapsed = " ".join(sql.split())
    if not re.match(r"^(?:SELECT\b|WITH\b.*\bSELECT\b)", collapsed, re.IGNORECASE):
        raise error("INVALID_SQL", "only SELECT or WITH ... SELECT is allowed")
    if FORBIDDEN_SQL.search(collapsed):
        raise error("INVALID_SQL", "mutating, DDL, system, and privilege SQL is forbidden")

    tables = TABLE_REFERENCE.findall(collapsed)
    cte_names = set(CTE_NAME.findall(collapsed))
    if not tables or any(
        table not in ALLOWED_TABLES and table not in cte_names for table in tables
    ):
        raise error("INVALID_SQL", "query references a table outside the resume allowlist")
    if not any(table.endswith("JobSeekerResume") for table in tables):
        raise error("INVALID_SQL", "query must use JobSeekerResume as the primary source")

    mandatory = (
        r"\br\s*\.\s*DelFlag\s*=\s*0\b",
        r"\br\s*\.\s*ResumeState\s*=\s*2\b",
        r"\br\s*\.\s*ResumeGuid\s+IS\s+NOT\s+NULL\b",
    )
    if any(not re.search(pattern, collapsed, re.IGNORECASE) for pattern in mandatory):
        raise error("INVALID_SQL", "query is missing a mandatory resume predicate")
    if not re.search(
        r"\bLIMIT\s*\{\s*limit\s*:\s*UInt(?:32|64)\s*\}\s*$",
        collapsed,
        re.IGNORECASE,
    ):
        raise error("INVALID_SQL", "query must end with LIMIT {limit:UInt32}")
    return sql


def normalize_request(value: dict[str, Any]) -> tuple[str, dict[str, Any], int]:
    sql = validate_sql(value.get("sql"))
    params = validate_params(value.get("params"))
    limit = validate_limit(value.get("limit"))
    params["limit"] = limit
    return sql, params, limit


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise error("CONFIG_ERROR", "Private connection configuration was not found") from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise error("CONFIG_ERROR", "Private connection configuration is invalid") from exc
    if not isinstance(raw, dict):
        raise error("CONFIG_ERROR", "Private connection configuration must be an object")
    for key in ("url", "username", "password"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise error("CONFIG_ERROR", f"Connection setting {key} is required")

    parsed = urlparse(raw["url"])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise error("CONFIG_ERROR", "Connection URL must use http or https")
    if parsed.query or parsed.fragment:
        raise error("CONFIG_ERROR", "Connection URL cannot contain a query or fragment")

    database = raw.get("database", DEFAULT_DATABASE)
    if not isinstance(database, str) or not database:
        raise error("CONFIG_ERROR", "Connection setting database must be a string")
    raw["database"] = database
    raw["request_timeout_seconds"] = positive_number_setting(
        raw.get(
            "request_timeout_seconds",
            raw.get("max_execution_time_seconds", DEFAULT_REQUEST_TIMEOUT),
        ),
        "request_timeout_seconds",
    )
    return raw


def positive_number_setting(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise error("CONFIG_ERROR", f"Connection setting {name} must be positive")
    return value


def build_request(
    sql: str, params: dict[str, Any], config: dict[str, Any]
) -> tuple[Request, float]:
    query_params: dict[str, str] = {
        "database": config["database"],
        "readonly": "1",
        "query": f"{sql}\nFORMAT JSONEachRow",
    }
    for name, value in params.items():
        if value is None:
            query_params[f"param_{name}"] = "\\N"
        elif isinstance(value, bool):
            query_params[f"param_{name}"] = "1" if value else "0"
        else:
            query_params[f"param_{name}"] = str(value)

    url = f"{config['url'].rstrip('/')}?{urlencode(query_params)}"
    credentials = f"{config['username']}:{config['password']}".encode("utf-8")
    authorization = base64.b64encode(credentials).decode("ascii")
    request = Request(url, method="GET", headers={"Authorization": f"Basic {authorization}"})
    return request, float(config["request_timeout_seconds"])


def execute(request: Request, timeout: float) -> str:
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code not in {401, 403}:
            try:
                body = exc.read(4096).decode("utf-8", errors="replace")
            except OSError:
                body = ""
            raise error("CLICKHOUSE_ERROR", summarize_clickhouse_error(body, exc.code)) from exc
        raise error("HTTP_ERROR", f"ClickHouse HTTP request failed with status {exc.code}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise error("TIMEOUT", "ClickHouse request timed out") from exc
    except (URLError, OSError, UnicodeDecodeError) as exc:
        raise error("CONNECTION_ERROR", "ClickHouse connection failed") from exc


def summarize_clickhouse_error(body: str, status: int) -> str:
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if not first_line:
        return f"ClickHouse rejected the query with status {status}"
    first_line = re.split(
        r"\s+(?:near\b|at position\b|in scope\b|Stack trace\b|\(version\b)",
        first_line,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    first_line = re.sub(r"[\x00-\x1f\x7f]+", " ", first_line).strip()
    return first_line[:240]


def parse_rows(text: str, requested_limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise error("INVALID_RESULT", "ClickHouse returned invalid JSONEachRow") from exc
        if not isinstance(row, dict):
            raise error("INVALID_RESULT", "ClickHouse returned a non-object result row")
        guid = row.get("ResumeGuid")
        if not isinstance(guid, str) or not guid.strip():
            raise error("INVALID_RESULT", "Every result must contain a non-empty ResumeGuid")
        if guid in seen:
            raise error("INVALID_RESULT", "ClickHouse returned duplicate ResumeGuid values")
        seen.add(guid)
        rows.append(row)
    if len(rows) > requested_limit:
        raise error("INVALID_RESULT", "ClickHouse returned more rows than requested")
    return rows


def emit_json(value: dict[str, Any], stream: Any = sys.stdout) -> None:
    json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
    stream.write("\n")


def main() -> int:
    args = parse_args()
    try:
        request_value = read_request()
        sql, params, limit = normalize_request(request_value)
        if args.validate_only:
            emit_json({"ok": True, "requested_limit": limit})
            return 0

        config = load_config(args.config)
        http_request, timeout = build_request(sql, params, config)
        response_text = execute(http_request, timeout)
        rows = parse_rows(response_text, limit)
        emit_json(
            {
                "ok": True,
                "count": len(rows),
                "requested_limit": limit,
                "results": rows,
            }
        )
        return 0
    except SearchError as exc:
        emit_json({"ok": False, "code": exc.code, "message": exc.message}, sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
