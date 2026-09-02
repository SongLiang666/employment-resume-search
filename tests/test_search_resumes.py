import base64
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "clickhouse-resume-search" / "scripts" / "search_resumes.py"
SCHEMA = ROOT / "skills" / "clickhouse-resume-search" / "references" / "schema.md"
SKILL = ROOT / "skills" / "clickhouse-resume-search" / "SKILL.md"

VALID_SQL = """
SELECT
    toString(r.ResumeGuid) AS ResumeGuid,
    r.ResumeID,
    r.JobSeekerID,
    r.JobSeekerName
FROM RCW_RC_Voodoo_Jobseeker.JobSeekerResume AS r
WHERE r.DelFlag = 0
  AND r.ResumeState = 2
  AND r.ResumeGuid IS NOT NULL
QUALIFY row_number() OVER
(
    PARTITION BY r.ResumeGuid
    ORDER BY r.LastRefreshDate DESC, r.LastEditTime DESC, r.ResumeID DESC
) = 1
ORDER BY r.LastRefreshDate DESC, r.LastEditTime DESC
LIMIT {limit:UInt32}
""".strip()


def run_cli(request=None, config=None, *extra_args):
    args = [sys.executable, str(SCRIPT)]
    if config is not None:
        args.extend(["--config", str(config)])
    args.extend(extra_args)
    payload = "" if request is None else json.dumps(request, ensure_ascii=False)
    return subprocess.run(args, input=payload, capture_output=True, text=True, check=False)


def write_private_config(path, value, mode=0o600):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


class FakeClickHouseHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body = ""
    captured = []

    def do_GET(self):
        type(self).captured.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        body = type(self).response_body.encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class ExecutorPresenceTests(unittest.TestCase):
    def test_help_is_available(self):
        result = run_cli(None, None, "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ClickHouse resume search", result.stdout)


@unittest.skipUnless(SCRIPT.is_file(), "executor not implemented yet")
class ExecutorCliTests(unittest.TestCase):
    def setUp(self):
        FakeClickHouseHandler.response_status = 200
        FakeClickHouseHandler.response_body = ""
        FakeClickHouseHandler.captured = []

    def test_validate_only_applies_default_limit_without_exposing_sql(self):
        result = run_cli(
            {"sql": VALID_SQL, "params": {}},
            None,
            "--validate-only",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["requested_limit"], 100)
        self.assertNotIn("sql", output)

    def test_validate_only_honors_explicit_positive_limit(self):
        result = run_cli(
            {"sql": VALID_SQL, "params": {}, "limit": 237},
            None,
            "--validate-only",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["requested_limit"], 237)

    def test_rejects_limit_larger_than_uint32(self):
        result = run_cli(
            {"sql": VALID_SQL, "params": {}, "limit": 2**32},
            None,
            "--validate-only",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["code"], "INVALID_REQUEST")

    def test_reference_summary_template_passes_executor_validation(self):
        reference = SCHEMA.read_text(encoding="utf-8")
        match = re.search(
            r"## Summary Query Template.*?```sql\n(.*?)\n```",
            reference,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        result = run_cli(
            {"sql": match.group(1), "params": {}, "limit": 1},
            None,
            "--validate-only",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reference_summary_template_uses_stable_identity_aliases(self):
        reference = SCHEMA.read_text(encoding="utf-8")
        self.assertIn("r.ResumeID AS ResumeID", reference)
        self.assertIn("r.JobSeekerID AS JobSeekerID", reference)

    def test_reference_summary_keeps_latest_employment_from_one_row(self):
        reference = SCHEMA.read_text(encoding="utf-8")
        self.assertIn(
            "tuple(ExperienceText1, ExperienceText2, ExperienceStartTime, ExperienceFinishTime, IsToThisDay)",
            reference,
        )
        self.assertNotIn("max(ExperienceStartTime) AS LatestWorkStartTime", reference)

    def test_reference_documents_numeric_types_and_inclusive_ranges(self):
        reference = SCHEMA.read_text(encoding="utf-8")
        self.assertIn("`JobSeekerAge Nullable(UInt8)`", reference)
        self.assertIn("`JobSeekerWorkYear Nullable(UInt8)`", reference)
        self.assertIn("inclusive", reference.lower())

    def test_skill_treats_keyword_phrases_as_intact_substrings(self):
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("intact substring", skill.lower())

    def test_skill_documents_executor_structure_constraints(self):
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("after the mandatory predicates", skill.lower())
        self.assertIn("sql comments", skill.lower())
        self.assertIn("4294967295", skill)

    def test_reference_summary_deduplicates_resume_guid_in_sql(self):
        reference = SCHEMA.read_text(encoding="utf-8")
        self.assertRegex(
            reference,
            r"QUALIFY\s+row_number\(\)\s+OVER\s*\(\s*"
            r"PARTITION BY r\.ResumeGuid\s+ORDER BY r\.LastRefreshDate DESC,\s*"
            r"r\.LastEditTime DESC,\s*r\.ResumeID DESC\s*\)\s*=\s*1",
        )

    def test_rejects_mutating_or_multi_statement_sql(self):
        for sql in (
            "DROP TABLE x",
            VALID_SQL + "; SELECT 1",
            VALID_SQL.replace("SELECT", "INSERT INTO x SELECT", 1),
        ):
            with self.subTest(sql=sql[:30]):
                result = run_cli(
                    {"sql": sql, "params": {}},
                    None,
                    "--validate-only",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["code"], "INVALID_SQL")

    def test_rejects_unknown_table_and_missing_mandatory_predicates(self):
        cases = (
            VALID_SQL.replace(
                "RCW_RC_Voodoo_Jobseeker.JobSeekerResume",
                "other.SecretTable",
            ),
            VALID_SQL.replace("r.DelFlag = 0", "1 = 1"),
            VALID_SQL.replace("r.ResumeState = 2", "1 = 1"),
            VALID_SQL.replace("r.ResumeGuid IS NOT NULL", "1 = 1"),
            re.sub(r"QUALIFY row_number\(\).*?\) = 1\n", "", VALID_SQL, flags=re.DOTALL),
        )
        for sql in cases:
            with self.subTest(sql=sql[:80]):
                result = run_cli(
                    {"sql": sql, "params": {}},
                    None,
                    "--validate-only",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["code"], "INVALID_SQL")

    def test_rejects_union_outer_alias_and_top_level_or_bypasses(self):
        select_list = """SELECT
    toString(r.ResumeGuid) AS ResumeGuid,
    r.ResumeID,
    r.JobSeekerID,
    r.JobSeekerName
FROM RCW_RC_Voodoo_Jobseeker.JobSeekerResume AS r"""
        union_bypass = VALID_SQL.rsplit("ORDER BY", 1)[0] + f"""UNION ALL
{select_list}
WHERE 1 = 1
LIMIT {{limit:UInt32}}"""
        outer_alias_bypass = f"""WITH safe AS
(
    {select_list}
    WHERE r.DelFlag = 0
      AND r.ResumeState = 2
      AND r.ResumeGuid IS NOT NULL
)
SELECT
    toString(exposed.ResumeGuid) AS ResumeGuid
FROM RCW_RC_Voodoo_Jobseeker.JobSeekerResume AS exposed
WHERE 1 = 1
LIMIT {{limit:UInt32}}"""
        top_level_or_bypass = VALID_SQL.replace(
            "  AND r.ResumeGuid IS NOT NULL",
            "  AND r.ResumeGuid IS NOT NULL\n  OR 1 = 1",
        )

        for sql in (union_bypass, outer_alias_bypass, top_level_or_bypass):
            with self.subTest(sql=sql):
                result = run_cli(
                    {"sql": sql, "params": {}},
                    None,
                    "--validate-only",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["code"], "INVALID_SQL")

    def test_rejects_url_userinfo_and_group_readable_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "connection.json"
            cases = (
                ("http://embedded:secret@127.0.0.1:1", 0o600),
                ("http://127.0.0.1:1", 0o640),
            )
            for url, mode in cases:
                with self.subTest(url=url, mode=oct(mode)):
                    write_private_config(
                        config,
                        {
                            "url": url,
                            "database": "RCW_RC_Voodoo_Jobseeker",
                            "username": "u",
                            "password": "p",
                        },
                        mode,
                    )
                    result = run_cli({"sql": VALID_SQL, "params": {}}, config)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(json.loads(result.stderr)["code"], "CONFIG_ERROR")

    def test_rejects_password_column_and_mandatory_predicates_in_comments(self):
        password_sql = VALID_SQL.replace(
            "r.JobSeekerName\nFROM",
            "r.JobSeekerName,\n    base.JobSeekerPassword\nFROM",
        ).replace(
            "WHERE r.DelFlag = 0",
            "LEFT JOIN RCW_RC_Voodoo_Jobseeker.JobSeekerBaseInfo AS base\n"
            "    ON base.JobSeekerID = r.JobSeekerID\n"
            "WHERE r.DelFlag = 0",
        )
        comment_bypass_sql = VALID_SQL.replace(
            "WHERE r.DelFlag = 0\n"
            "  AND r.ResumeState = 2\n"
            "  AND r.ResumeGuid IS NOT NULL",
            "WHERE 1 = 1\n"
            "  /* r.DelFlag = 0\n"
            "     AND r.ResumeState = 2\n"
            "     AND r.ResumeGuid IS NOT NULL */",
        )

        for sql in (password_sql, comment_bypass_sql):
            with self.subTest(sql=sql):
                result = run_cli(
                    {"sql": sql, "params": {}},
                    None,
                    "--validate-only",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["code"], "INVALID_SQL")

    def test_executes_http_get_with_readonly_settings_and_typed_params(self):
        rows = [
            {
                "ResumeGuid": "264081e3-053a-4a54-9868-b9fd8d5ca4b2",
                "ResumeID": 7,
                "JobSeekerID": 9,
                "JobSeekerName": "测试候选人",
            }
        ]
        FakeClickHouseHandler.response_body = "\n".join(
            json.dumps(row, ensure_ascii=False) for row in rows
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClickHouseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory() as tmp:
            password = "not-for-logs"
            config = Path(tmp) / "connection.json"
            write_private_config(
                config,
                {
                    "url": f"http://127.0.0.1:{server.server_port}",
                    "database": "RCW_RC_Voodoo_Jobseeker",
                    "username": "readonly_test",
                    "password": password,
                    "request_timeout_seconds": 10,
                },
            )
            sql = VALID_SQL.replace(
                "  AND r.ResumeGuid IS NOT NULL",
                "  AND r.ResumeGuid IS NOT NULL\n"
                "  AND positionCaseInsensitiveUTF8(ifNull(r.ResumeName, ''), {keyword:String}) > 0",
            )
            result = run_cli(
                {
                    "sql": sql,
                    "params": {"keyword": "C++/支付 & 风控"},
                    "limit": 1,
                },
                config,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["count"], 1)
        self.assertEqual(output["requested_limit"], 1)
        self.assertEqual(output["results"], rows)
        self.assertNotIn("sql", output)
        self.assertNotIn(password, result.stdout + result.stderr)

        self.assertEqual(len(FakeClickHouseHandler.captured), 1)
        request = FakeClickHouseHandler.captured[0]
        parsed = urlparse(request["path"])
        query = parse_qs(parsed.query)
        self.assertEqual(query["readonly"], ["1"])
        self.assertNotIn("max_execution_time", query)
        self.assertEqual(query["database"], ["RCW_RC_Voodoo_Jobseeker"])
        self.assertEqual(query["param_keyword"], ["C++/支付 & 风控"])
        self.assertEqual(query["param_limit"], ["1"])
        self.assertTrue(query["query"][0].endswith("FORMAT JSONEachRow"))
        expected_auth = base64.b64encode(b"readonly_test:not-for-logs").decode("ascii")
        self.assertEqual(request["authorization"], f"Basic {expected_auth}")

    def test_rejects_null_or_duplicate_resume_guid(self):
        bad_responses = (
            '{"ResumeGuid":null,"ResumeID":1}\n',
            (
                '{"ResumeGuid":"264081e3-053a-4a54-9868-b9fd8d5ca4b2","ResumeID":1}\n'
                '{"ResumeGuid":"264081e3-053a-4a54-9868-b9fd8d5ca4b2","ResumeID":2}\n'
            ),
        )
        for response in bad_responses:
            with self.subTest(response=response):
                FakeClickHouseHandler.response_body = response
                server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClickHouseHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        config = Path(tmp) / "connection.json"
                        write_private_config(
                            config,
                            {
                                "url": f"http://127.0.0.1:{server.server_port}",
                                "database": "RCW_RC_Voodoo_Jobseeker",
                                "username": "u",
                                "password": "p",
                            },
                        )
                        result = run_cli({"sql": VALID_SQL, "params": {}}, config)
                finally:
                    server.shutdown()
                    server.server_close()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["code"], "INVALID_RESULT")

    def test_missing_config_and_http_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = run_cli(
                {"sql": VALID_SQL, "params": {}},
                Path(tmp) / "missing-connection.json",
            )
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(json.loads(missing.stderr)["code"], "CONFIG_ERROR")

        FakeClickHouseHandler.response_status = 401
        FakeClickHouseHandler.response_body = "Authentication failed"
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClickHouseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                password = "must-not-leak"
                config = Path(tmp) / "connection.json"
                write_private_config(
                    config,
                    {
                        "url": f"http://127.0.0.1:{server.server_port}",
                        "database": "RCW_RC_Voodoo_Jobseeker",
                        "username": "u",
                        "password": password,
                    },
                )
                failed = run_cli({"sql": VALID_SQL, "params": {}}, config)
        finally:
            server.shutdown()
            server.server_close()
        self.assertNotEqual(failed.returncode, 0)
        error = json.loads(failed.stderr)
        self.assertEqual(error["code"], "HTTP_ERROR")
        self.assertNotIn(password, failed.stdout + failed.stderr)

    def test_clickhouse_500_error_returns_sanitized_database_message(self):
        FakeClickHouseHandler.response_status = 500
        FakeClickHouseHandler.response_body = (
            "Code: 62. DB::Exception: Syntax error near secret query text\n"
            "Stack trace omitted"
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClickHouseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                password = "must-not-leak"
                config = Path(tmp) / "connection.json"
                write_private_config(
                    config,
                    {
                        "url": f"http://127.0.0.1:{server.server_port}",
                        "database": "RCW_RC_Voodoo_Jobseeker",
                        "username": "u",
                        "password": password,
                    },
                )
                failed = run_cli({"sql": VALID_SQL, "params": {}}, config)
        finally:
            server.shutdown()
            server.server_close()
        self.assertNotEqual(failed.returncode, 0)
        error = json.loads(failed.stderr)
        self.assertEqual(error["code"], "CLICKHOUSE_ERROR")
        self.assertIn("Code: 62", error["message"])
        self.assertIn("Syntax error", error["message"])
        self.assertNotIn("secret query text", error["message"])
        self.assertNotIn(password, failed.stdout + failed.stderr)


if __name__ == "__main__":
    unittest.main()
