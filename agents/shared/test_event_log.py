#!/usr/bin/env python3
"""Network-free tests for local workflow event sinks.

Run: python3 agents/shared/test_event_log.py
"""
import json
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import event_log


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.old_event_log = os.environ.get("BORING_EVENT_LOG")
        self.old_event_sink = os.environ.get("BORING_EVENT_SINK")
        self.old_event_sink_url = os.environ.get("BORING_EVENT_SINK_URL")
        self.old_event_sink_timeout = os.environ.get("BORING_EVENT_SINK_TIMEOUT")
        self.old_event_recent_hours = os.environ.get("BORING_EVENT_RECENT_HOURS")
        self.old_event_db_mirror = os.environ.get("BORING_EVENT_DB_MIRROR")
        self.old_event_spool = os.environ.get("BORING_EVENT_SPOOL")
        self.old_self_verify_env = {
            name: os.environ.get(name)
            for name in (
                "BORING_SELF_VERIFY_SUMMARY",
                "BORING_SELF_VERIFY_EVENT_LOG",
                "BORING_SELF_VERIFY_CYCLE",
                "BORING_SELF_VERIFY_STEP",
            )
        }
        for name in self.old_self_verify_env:
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["BORING_EVENT_LOG"] = os.path.join(self.tmp.name, "events.ndjson")
        os.environ.pop("BORING_EVENT_SINK", None)
        os.environ.pop("BORING_EVENT_SINK_URL", None)
        os.environ.pop("BORING_EVENT_SINK_TIMEOUT", None)
        os.environ.pop("BORING_EVENT_RECENT_HOURS", None)
        os.environ["BORING_EVENT_DB_MIRROR"] = "0"
        os.environ.pop("BORING_EVENT_SPOOL", None)

    def tearDown(self):
        if self.old_event_log is None:
            os.environ.pop("BORING_EVENT_LOG", None)
        else:
            os.environ["BORING_EVENT_LOG"] = self.old_event_log
        if self.old_event_sink is None:
            os.environ.pop("BORING_EVENT_SINK", None)
        else:
            os.environ["BORING_EVENT_SINK"] = self.old_event_sink
        if self.old_event_sink_url is None:
            os.environ.pop("BORING_EVENT_SINK_URL", None)
        else:
            os.environ["BORING_EVENT_SINK_URL"] = self.old_event_sink_url
        if self.old_event_sink_timeout is None:
            os.environ.pop("BORING_EVENT_SINK_TIMEOUT", None)
        else:
            os.environ["BORING_EVENT_SINK_TIMEOUT"] = self.old_event_sink_timeout
        if self.old_event_recent_hours is None:
            os.environ.pop("BORING_EVENT_RECENT_HOURS", None)
        else:
            os.environ["BORING_EVENT_RECENT_HOURS"] = self.old_event_recent_hours
        if self.old_event_db_mirror is None:
            os.environ.pop("BORING_EVENT_DB_MIRROR", None)
        else:
            os.environ["BORING_EVENT_DB_MIRROR"] = self.old_event_db_mirror
        if self.old_event_spool is None:
            os.environ.pop("BORING_EVENT_SPOOL", None)
        else:
            os.environ["BORING_EVENT_SPOOL"] = self.old_event_spool
        for name, value in self.old_self_verify_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def test_append_event_writes_one_ndjson_line(self):
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "ok",
            session_id="s1",
            verifier_status="pass",
        )

        with open(os.environ["BORING_EVENT_LOG"], encoding="utf-8") as f:
            event = json.loads(f.readline())

        self.assertEqual(event["component"], "distill-session")
        self.assertEqual(event["event"], "distill_resolution")
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["session_id"], "s1")
        self.assertEqual(event["run_id"], "s1")
        self.assertIn("ts", event)
        self.assertEqual(event["otel"]["event_name"], "distill_resolution")
        self.assertEqual(event["otel"]["severity_text"], "INFO")
        self.assertEqual(event["otel"]["severity_number"], 9)
        self.assertEqual(event["otel"]["resource"]["attributes"]["service.name"], "distill-session")
        self.assertEqual(event["otel"]["attributes"]["session_id"], "s1")
        self.assertRegex(event["otel"]["trace_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(event["otel"]["span_id"], r"^[0-9a-f]{16}$")

    def test_append_event_writes_fsynced_complete_ndjson_line(self):
        calls = []

        with mock.patch.object(event_log.os, "fsync", side_effect=lambda fd: calls.append(fd)):
            event_log.append_event("guard", "structural_guard", "ok", run_id="r1")

        with open(os.environ["BORING_EVENT_LOG"], "rb") as f:
            raw = f.read()

        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        event = json.loads(raw.decode("utf-8"))
        self.assertEqual(event["event"], "structural_guard")
        self.assertEqual(event["run_id"], "r1")
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], 0)

    def test_append_event_includes_self_verify_env_provenance(self):
        os.environ["BORING_SELF_VERIFY_SUMMARY"] = "/tmp/run/summary.tsv"
        os.environ["BORING_SELF_VERIFY_EVENT_LOG"] = "/tmp/run/events.ndjson"
        os.environ["BORING_SELF_VERIFY_CYCLE"] = "1"
        os.environ["BORING_SELF_VERIFY_STEP"] = "readiness"

        event_log.append_event("doctor", "readiness", "failed")

        with open(os.environ["BORING_EVENT_LOG"], encoding="utf-8") as f:
            event = json.loads(f.readline())

        self.assertEqual(event["self_verify_summary"], "/tmp/run/summary.tsv")
        self.assertEqual(event["self_verify_event_log"], "/tmp/run/events.ndjson")
        self.assertEqual(event["self_verify_cycle"], "1")
        self.assertEqual(event["self_verify_step"], "readiness")
        self.assertEqual(event["otel"]["attributes"]["self_verify_step"], "readiness")

    def test_append_event_rejects_partial_self_verify_provenance_before_write(self):
        os.environ["BORING_SELF_VERIFY_SUMMARY"] = "/tmp/run/summary.tsv"

        with self.assertRaisesRegex(ValueError, "partial self-verify provenance"):
            event_log.append_event("doctor", "readiness", "failed")

        self.assertFalse(os.path.exists(os.environ["BORING_EVENT_LOG"]))

    def test_append_event_rejects_invalid_self_verify_cycle_before_write(self):
        os.environ["BORING_SELF_VERIFY_SUMMARY"] = "/tmp/run/summary.tsv"
        os.environ["BORING_SELF_VERIFY_EVENT_LOG"] = "/tmp/run/events.ndjson"
        os.environ["BORING_SELF_VERIFY_CYCLE"] = "0"
        os.environ["BORING_SELF_VERIFY_STEP"] = "readiness"

        with self.assertRaisesRegex(ValueError, "BORING_SELF_VERIFY_CYCLE must be a positive integer"):
            event_log.append_event("doctor", "readiness", "failed")

        self.assertFalse(os.path.exists(os.environ["BORING_EVENT_LOG"]))

    def test_record_cli_coerces_fields_and_tail_filters(self):
        with mock.patch.object(
            event_log.sys,
            "argv",
            [
                "event_log.py",
                "--record",
                "guard",
                "guard",
                "ok",
                "--field",
                "duration_s=12",
                "--field",
                "strict=true",
            ],
        ):
            self.assertEqual(event_log.main(), 0)

        stdout = io.StringIO()
        with (
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--tail", "--component", "guard", "--json"]),
            mock.patch.object(event_log.sys, "stdout", stdout),
        ):
            self.assertEqual(event_log.main(), 0)

        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["component"], "guard")
        self.assertEqual(event["duration_s"], 12)
        self.assertEqual(event["strict"], True)

    def test_tail_human_projection_omits_machine_otel_envelope(self):
        event_log.append_event(
            "guard",
            "structural_guard",
            "failed",
            run_id="r1",
            workflow="memory_ingest",
        )

        stdout = io.StringIO()
        with (
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--tail"]),
            mock.patch.object(event_log.sys, "stdout", stdout),
        ):
            self.assertEqual(event_log.main(), 0)

        text = stdout.getvalue()
        self.assertIn("guard structural_guard failed", text)
        self.assertIn("run_id=r1", text)
        self.assertIn("workflow=memory_ingest", text)
        self.assertNotIn("otel=", text)

        json_stdout = io.StringIO()
        with (
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--tail", "--json"]),
            mock.patch.object(event_log.sys, "stdout", json_stdout),
        ):
            self.assertEqual(event_log.main(), 0)

        event = json.loads(json_stdout.getvalue())
        self.assertEqual(event["otel"]["event_name"], "structural_guard")

    def test_try_append_event_returns_false_on_write_failure(self):
        with mock.patch.object(event_log, "append_event", side_effect=OSError("denied")):
            self.assertFalse(event_log.try_append_event("guard", "guard", "failed"))

    def test_try_append_event_reports_invalid_config_without_traceback(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(event_log, "append_event", side_effect=ValueError("bad env")),
            mock.patch.object(event_log.sys, "stderr", stderr),
        ):
            self.assertFalse(event_log.try_append_event("guard", "guard", "failed"))

        text = stderr.getvalue()
        self.assertIn("[event-log] invalid config: bad env", text)
        self.assertNotIn("Traceback", text)

    def test_record_cli_reports_write_failure_without_traceback(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(event_log, "append_event", side_effect=OSError("denied")),
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--record", "doctor", "readiness", "failed"]),
            mock.patch.object(event_log.sys, "stderr", stderr),
        ):
            self.assertEqual(event_log.main(), 1)

        text = stderr.getvalue()
        self.assertIn("[event-log] write failed: denied", text)
        self.assertNotIn("Traceback", text)

    def test_record_cli_reports_invalid_config_without_traceback(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(event_log, "append_event", side_effect=ValueError("bad env")),
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--record", "doctor", "readiness", "failed"]),
            mock.patch.object(event_log.sys, "stderr", stderr),
        ):
            self.assertEqual(event_log.main(), 1)

        text = stderr.getvalue()
        self.assertIn("[event-log] invalid config: bad env", text)
        self.assertNotIn("Traceback", text)

    def test_tail_cli_reports_invalid_config_without_traceback(self):
        os.environ.pop("BORING_EVENT_DB_MIRROR", None)
        os.environ["BORING_EVENT_SINK_TIMEOUT"] = "0"
        stderr = io.StringIO()
        with (
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--tail"]),
            mock.patch.object(event_log.sys, "stderr", stderr),
        ):
            self.assertEqual(event_log.main(), 2)

        text = stderr.getvalue()
        self.assertIn("[event-log] invalid config: BORING_EVENT_SINK_TIMEOUT must be a positive number", text)
        self.assertNotIn("Traceback", text)

    def test_recent_resolution_failures_cli_reports_invalid_config_without_traceback(self):
        os.environ["BORING_EVENT_RECENT_HOURS"] = "0"
        stderr = io.StringIO()
        with (
            mock.patch.object(event_log.sys, "argv", ["event_log.py", "--recent-resolution-failures"]),
            mock.patch.object(event_log.sys, "stderr", stderr),
        ):
            self.assertEqual(event_log.main(), 2)

        text = stderr.getvalue()
        self.assertIn("[event-log] invalid config: BORING_EVENT_RECENT_HOURS must be a positive integer", text)
        self.assertNotIn("Traceback", text)

    def test_recent_resolution_failures_cli_rejects_non_positive_numeric_overrides_before_reads(self):
        cases = (
            ["event_log.py", "--recent-resolution-failures", "--hours", "0"],
            ["event_log.py", "--recent-resolution-failures", "--max", "0"],
        )
        fetch = mock.Mock(side_effect=AssertionError("engine"))
        with mock.patch.object(event_log, "_fetch_engine_events", fetch):
            for argv in cases:
                stderr = io.StringIO()
                with (
                    mock.patch.object(event_log.sys, "argv", argv),
                    mock.patch.object(event_log.sys, "stderr", stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    event_log.main()

                self.assertEqual(raised.exception.code, 2)
                text = stderr.getvalue()
                self.assertIn("must be a positive integer", text)
                self.assertNotIn("Traceback", text)
        fetch.assert_not_called()

    def test_append_event_stores_in_engine_by_default_without_spool(self):
        os.environ.pop("BORING_EVENT_DB_MIRROR", None)
        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"entries":[]}'

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        with mock.patch.object(event_log.urllib.request, "urlopen", fake_urlopen):
            event_log.append_event("guard", "structural_guard", "failed", run_id="r1")

        self.assertEqual(seen["url"], "http://127.0.0.1:7700/events")
        self.assertEqual(seen["timeout"], 0.5)
        self.assertEqual(seen["body"]["event"], "structural_guard")
        self.assertEqual(seen["body"]["otel"]["severity_text"], "ERROR")
        self.assertFalse(os.path.exists(os.environ["BORING_EVENT_LOG"]))

    def test_event_sink_timeout_rejects_invalid_env_before_network(self):
        os.environ.pop("BORING_EVENT_DB_MIRROR", None)
        os.environ["BORING_EVENT_SINK_TIMEOUT"] = "0"

        urlopen = mock.Mock(side_effect=AssertionError("network"))
        with (
            mock.patch.object(event_log.urllib.request, "urlopen", urlopen),
            self.assertRaisesRegex(ValueError, "BORING_EVENT_SINK_TIMEOUT must be a positive number"),
        ):
            event_log.append_event("guard", "structural_guard", "failed", run_id="r1")

        urlopen.assert_not_called()
        self.assertFalse(os.path.exists(os.environ["BORING_EVENT_LOG"]))

    def test_append_event_spools_when_engine_store_fails(self):
        os.environ.pop("BORING_EVENT_DB_MIRROR", None)

        with mock.patch.object(event_log.urllib.request, "urlopen", side_effect=event_log.urllib.error.URLError("down")):
            event_log.append_event("guard", "structural_guard", "failed", run_id="r1")

        with open(os.environ["BORING_EVENT_LOG"], encoding="utf-8") as f:
            event = json.loads(f.readline())
        self.assertEqual(event["run_id"], "r1")
        self.assertEqual(event["event"], "structural_guard")

    def test_append_event_can_use_spool_only_sink(self):
        os.environ["BORING_EVENT_SINK"] = "spool"

        with mock.patch.object(event_log.urllib.request, "urlopen", side_effect=AssertionError("network")):
            event_log.append_event("guard", "structural_guard", "ok", run_id="r1")

    def test_recent_events_prefers_engine_over_spool(self):
        os.environ.pop("BORING_EVENT_DB_MIRROR", None)
        with open(os.environ["BORING_EVENT_LOG"], "w", encoding="utf-8") as f:
            f.write(json.dumps({"component": "guard", "event": "spooled", "status": "ok"}))
            f.write("\n")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "entries": [
                            {
                                "observed_at": "2026-07-01T00:00:00+00:00",
                                "component": "guard",
                                "event": "from_db",
                                "status": "failed",
                                "attributes": {"session_id": "db-session"},
                            }
                        ]
                    }
                ).encode("utf-8")

        with mock.patch.object(event_log.urllib.request, "urlopen", return_value=FakeResponse()):
            events = event_log.recent_events()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "from_db")
        self.assertEqual(events[0]["session_id"], "db-session")

    def test_recent_events_caps_engine_response_to_requested_limit(self):
        os.environ.pop("BORING_EVENT_DB_MIRROR", None)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "entries": [
                            {
                                "observed_at": "2026-07-01T00:02:00+00:00",
                                "component": "guard",
                                "event": "new",
                                "status": "ok",
                            },
                            {
                                "observed_at": "2026-07-01T00:01:00+00:00",
                                "component": "guard",
                                "event": "old",
                                "status": "ok",
                            },
                        ]
                    }
                ).encode("utf-8")

        with mock.patch.object(event_log.urllib.request, "urlopen", return_value=FakeResponse()):
            events = event_log.recent_events(limit=1)

        self.assertEqual([event["event"] for event in events], ["new"])

    def test_recent_events_rejects_non_positive_limit_before_reads(self):
        fetch = mock.Mock(side_effect=AssertionError("engine"))
        with (
            mock.patch.object(event_log, "_fetch_engine_events", fetch),
            mock.patch.object(event_log, "iter_events", side_effect=AssertionError("spool")),
            self.assertRaisesRegex(ValueError, "limit must be a positive integer"),
        ):
            event_log.recent_events(limit=0)

        fetch.assert_not_called()

    def test_recent_resolution_failures_reads_engine(self):
        os.environ.pop("BORING_EVENT_DB_MIRROR", None)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "entries": [
                            {
                                "observed_at": datetime.now(timezone.utc).isoformat(),
                                "component": "distill-session",
                                "event": "distill_resolution",
                                "status": "failed",
                                "attributes": {
                                    "session_id": "db-bad",
                                    "verifier_status": "failed",
                                    "missing_fields": ["section:evidence"],
                                },
                            }
                        ]
                    }
                ).encode("utf-8")

        with mock.patch.object(event_log.urllib.request, "urlopen", return_value=FakeResponse()):
            failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "db-bad")

    def test_recent_resolution_failures_rejects_invalid_recent_hours_before_engine_read(self):
        os.environ["BORING_EVENT_RECENT_HOURS"] = "0"

        fetch = mock.Mock(side_effect=AssertionError("engine"))
        with (
            mock.patch.object(event_log, "_fetch_engine_events", fetch),
            self.assertRaisesRegex(ValueError, "BORING_EVENT_RECENT_HOURS must be a positive integer"),
        ):
            event_log.recent_resolution_failures()

        fetch.assert_not_called()

    def test_recent_resolution_failures_rejects_non_positive_direct_limits_before_reads(self):
        fetch = mock.Mock(side_effect=AssertionError("engine"))
        with mock.patch.object(event_log, "_fetch_engine_events", fetch):
            with self.assertRaisesRegex(ValueError, "limit must be a positive integer"):
                event_log.recent_resolution_failures(limit=0)
            with self.assertRaisesRegex(ValueError, "hours must be a positive integer"):
                event_log.recent_resolution_failures(hours=0)

        fetch.assert_not_called()

    def test_recent_resolution_failures_filters_resolution_failures(self):
        event_log.append_event("distill-session", "distill_resolution", "ok", session_id="pass")
        event_log.append_event("guard", "guard", "failed", session_id="other")
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="bad",
            verifier_status="failed",
            missing_fields=["section:evidence"],
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "bad")

    def test_recent_resolution_failures_ignores_resolved_session(self):
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="resolved",
            resolution="evidence",
            verifier_status="failed",
            missing_fields=["claim-kind:decision"],
        )
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "ok",
            session_id="resolved",
            resolution="evidence",
            verifier_status="pass",
            remember_status="duplicate",
        )
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="bad",
            resolution="evidence",
            verifier_status="failed",
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "bad")

    def test_recent_resolution_failures_keeps_latest_failure_after_success(self):
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "ok",
            session_id="regressed",
            resolution="evidence",
            verifier_status="pass",
        )
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="regressed",
            resolution="evidence",
            verifier_status="failed",
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "regressed")

    def test_recent_resolution_failures_ignores_malformed_lines(self):
        with open(os.environ["BORING_EVENT_LOG"], "w", encoding="utf-8") as f:
            f.write("{bad json\n")
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "failed",
            session_id="bad",
            verifier_status="failed",
        )

        failures = event_log.recent_resolution_failures()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["session_id"], "bad")

    def test_recent_resolution_failures_ignores_stale_failures(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        stale = {
            "ts": old.isoformat(),
            "component": "distill-session",
            "event": "distill_resolution",
            "status": "failed",
            "session_id": "old",
            "verifier_status": "failed",
        }
        with open(os.environ["BORING_EVENT_LOG"], "w", encoding="utf-8") as f:
            f.write(json.dumps(stale))
            f.write("\n")

        failures = event_log.recent_resolution_failures(hours=24)

        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
