#!/usr/bin/env python3
"""Tests for scripts/self-verify-cycle.py."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import datetime as dt
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "self_verify_cycle", str(ROOT / "scripts" / "self-verify-cycle.py")
)
cycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cycle)


def test_steps_for_cycle_match_contract_guard_positions():
    assert cycle.steps_for_cycle(1) == [
        "codex-status-strict",
        "readiness",
        "quality",
        "recent-events",
        "guard",
    ]
    assert cycle.steps_for_cycle(2) == [
        "codex-status-strict",
        "readiness",
        "quality",
        "recent-events",
    ]
    assert cycle.steps_for_cycle(6)[-1] == "guard"


def test_run_cycle_writes_all_rows_and_reports_failure():
    calls = []

    def runner(step):
        calls.append(step)
        return 2 if step == "readiness" else 0

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        rc = cycle.run_cycle(1, summary, runner)
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))

    assert rc == 1
    assert calls == cycle.steps_for_cycle(1)
    assert len(rows) == 5
    assert rows[1]["step"] == "readiness"
    assert rows[1]["status"] == "failed"
    assert rows[1]["exit_code"] == "2"


def test_run_cycle_records_runner_exception_and_continues():
    calls = []

    def runner(step):
        calls.append(step)
        if step == "readiness":
            raise RuntimeError("make missing")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = cycle.run_cycle(1, summary, runner)
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))

    assert rc == 1
    assert "step failed before exit code: readiness: make missing" in stderr.getvalue()
    assert calls == cycle.steps_for_cycle(1)
    assert len(rows) == 5
    assert rows[1]["step"] == "readiness"
    assert rows[1]["status"] == "failed"
    assert rows[1]["exit_code"] == str(cycle.STEP_EXECUTION_FAILURE_EXIT_CODE)


def test_run_cycle_normalizes_signal_returncode():
    def runner(step):
        return -15 if step == "readiness" else 0

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        rc = cycle.run_cycle(1, summary, runner)
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))

    assert rc == 1
    assert rows[1]["step"] == "readiness"
    assert rows[1]["status"] == "failed"
    assert rows[1]["exit_code"] == "143"


def test_run_cycle_commits_rows_only_after_all_steps_finish():
    seen_during_steps = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"

        def runner(_step):
            seen_during_steps.append(summary.exists())
            return 0

        rc = cycle.run_cycle(1, summary, runner)

    assert rc == 0
    assert seen_during_steps == [False, False, False, False, False]


def test_run_cycle_fsyncs_summary_parent_directory_after_replace():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        calls = []
        old_fsync_parent_dir = cycle.fsync_parent_dir

        def record(path):
            calls.append(Path(path))

        cycle.fsync_parent_dir = record
        try:
            rc = cycle.run_cycle(1, summary, lambda _step: 0)
        finally:
            cycle.fsync_parent_dir = old_fsync_parent_dir

    assert rc == 0
    assert calls == [summary]


def test_run_cycle_status_blocks_invalid_cycle_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"

        rc, reason = cycle.run_cycle_status(0, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "invalid_cycle"
    assert calls == []
    assert not summary.exists()


def test_run_make_step_writes_combined_step_log_and_echoes_output():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_make = root / "fake-make"
        fake_make.write_text(
            "#!/bin/sh\n"
            "echo out-$1\n"
            "echo err-$1 >&2\n"
            "exit 2\n",
            encoding="utf-8",
        )
        fake_make.chmod(0o755)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = cycle.run_make_step(str(fake_make), "readiness", root / "logs", 3)

        log_text = (root / "logs" / "cycle-0003-readiness.log").read_text(encoding="utf-8")

    assert rc == 2
    assert "out-readiness" in stdout.getvalue()
    assert "err-readiness" in stdout.getvalue()
    assert "self_verify_step cycle=0003 step=readiness" in log_text
    assert f"make_bin={fake_make}" in log_text
    assert f"cwd={cycle.ROOT}" in log_text
    assert "started_at=" in log_text
    assert "out-readiness" in log_text
    assert "err-readiness" in log_text
    assert "self_verify_step_complete cycle=0003 step=readiness exit_code=2 ended_at=" in log_text


def test_run_make_step_writes_header_for_quiet_step():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_make = root / "fake-make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        rc = cycle.run_make_step(str(fake_make), "quality", root / "logs", 2)
        log_text = (root / "logs" / "cycle-0002-quality.log").read_text(encoding="utf-8")

    assert rc == 0
    assert log_text.startswith("self_verify_step cycle=0002 step=quality ")
    assert f"make_bin={fake_make}" in log_text
    assert "started_at=" in log_text
    assert "self_verify_step_complete cycle=0002 step=quality exit_code=0 ended_at=" in log_text


def test_run_make_step_footer_normalizes_signal_returncode():
    old_stream = cycle.run_make_step_stream

    def signal_stream(_make_bin, _step, log_handle, _event_log_path=None, _cycle=None, *, summary_path=None):
        assert summary_path is None
        log_handle.write("before signal\n")
        return -15

    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycle.run_make_step_stream = signal_stream

            rc = cycle.run_make_step("make", "readiness", root / "logs", 3)
            log_text = (root / "logs" / "cycle-0003-readiness.log").read_text(encoding="utf-8")

        assert rc == -15
        assert "before signal" in log_text
        assert "self_verify_step_complete cycle=0003 step=readiness exit_code=143 ended_at=" in log_text
    finally:
        cycle.run_make_step_stream = old_stream


def test_run_make_step_fsyncs_completion_footer():
    old_fsync = cycle.os.fsync
    calls = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_make = root / "fake-make"
            fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_make.chmod(0o755)
            cycle.os.fsync = lambda fd: calls.append(fd)

            rc = cycle.run_make_step(str(fake_make), "quality", root / "logs", 2)

        assert rc == 0
        assert len(calls) == 1
        assert calls[0] >= 0
    finally:
        cycle.os.fsync = old_fsync


def test_run_make_step_writes_failure_footer_when_process_spawn_fails():
    old_fsync = cycle.os.fsync
    calls = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_make = root / "missing-make"
            cycle.os.fsync = lambda fd: calls.append(fd)

            try:
                cycle.run_make_step(str(missing_make), "quality", root / "logs", 2)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("missing make executable should fail")

            log_text = (root / "logs" / "cycle-0002-quality.log").read_text(encoding="utf-8")

        assert len(calls) == 1
        assert log_text.startswith("self_verify_step cycle=0002 step=quality ")
        assert f"make_bin={missing_make}" in log_text
        assert (
            "self_verify_step_complete cycle=0002 step=quality "
            f"exit_code={cycle.STEP_EXECUTION_FAILURE_EXIT_CODE} ended_at="
        ) in log_text
    finally:
        cycle.os.fsync = old_fsync


def test_run_make_step_requires_cycle_when_log_dir_is_set():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = root / "ran"
        log_dir = root / "logs"
        fake_make = root / "fake-make"
        fake_make.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(str(fake_make), "quality", log_dir)
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("log routing without cycle should fail")

        assert error == "cycle is required when log_dir is set"
        assert not marker.exists()
        assert not log_dir.exists()


def test_run_make_step_rejects_non_positive_cycle_when_log_dir_is_set():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = root / "ran"
        log_dir = root / "logs"
        fake_make = root / "fake-make"
        fake_make.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(str(fake_make), "quality", log_dir, 0)
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("log routing with cycle 0 should fail")

        assert error == "cycle must be a positive integer: 0"
        assert not marker.exists()
        assert not log_dir.exists()


def test_run_make_step_routes_events_to_run_local_spool():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_log = root / "events.ndjson"
        summary = root / "custom-summary.tsv"
        fake_make = root / "fake-make"
        fake_make.write_text(
            "#!/bin/sh\n"
            "printf 'event-log=%s\\n' \"$BORING_EVENT_LOG\"\n"
            "printf 'event-sink=%s\\n' \"$BORING_EVENT_SINK\"\n"
            "printf 'summary=%s\\n' \"$BORING_SELF_VERIFY_SUMMARY\"\n"
            "printf 'cycle=%s\\n' \"$BORING_SELF_VERIFY_CYCLE\"\n"
            "printf 'step=%s\\n' \"$BORING_SELF_VERIFY_STEP\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_make.chmod(0o755)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = cycle.run_make_step(
                str(fake_make),
                "recent-events",
                root / "logs",
                4,
                str(event_log),
                summary_path=summary,
            )

        log_text = (root / "logs" / "cycle-0004-recent-events.log").read_text(encoding="utf-8")

    assert rc == 0
    assert f"event-log={event_log}" in stdout.getvalue()
    assert "event-sink=spool" in stdout.getvalue()
    assert f"summary={summary}" in stdout.getvalue()
    assert "cycle=4" in stdout.getvalue()
    assert "step=recent-events" in stdout.getvalue()
    assert f"event-log={event_log}" in log_text
    assert "event-sink=spool" in log_text
    assert f"summary={summary}" in log_text
    assert "cycle=4" in log_text
    assert "step=recent-events" in log_text
    assert f"event_log={event_log}" in log_text


def test_run_make_step_routes_events_to_run_local_spool_without_step_log():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_log = root / "events.ndjson"
        summary = root / "custom-summary.tsv"
        fake_make = root / "fake-make"
        fake_make.write_text(
            "#!/bin/sh\n"
            "printf 'event-log=%s\\n' \"$BORING_EVENT_LOG\"\n"
            "printf 'event-sink=%s\\n' \"$BORING_EVENT_SINK\"\n"
            "printf 'summary=%s\\n' \"$BORING_SELF_VERIFY_SUMMARY\"\n"
            "printf 'cycle=%s\\n' \"$BORING_SELF_VERIFY_CYCLE\"\n"
            "printf 'step=%s\\n' \"$BORING_SELF_VERIFY_STEP\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_make.chmod(0o755)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = cycle.run_make_step(
                str(fake_make),
                "recent-events",
                cycle=4,
                event_log_path=str(event_log),
                summary_path=summary,
            )

    assert rc == 0
    assert f"event-log={event_log}" in stdout.getvalue()
    assert "event-sink=spool" in stdout.getvalue()
    assert f"summary={summary}" in stdout.getvalue()
    assert "cycle=4" in stdout.getvalue()
    assert "step=recent-events" in stdout.getvalue()


def test_run_make_step_requires_summary_path_when_routing_event_spool():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_log = root / "events.ndjson"
        log_path = root / "logs" / "cycle-0004-recent-events.log"
        fake_make = root / "fake-make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(str(fake_make), "recent-events", root / "logs", 4, event_log)
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("event log routing without summary_path should fail")

        assert error == "summary_path is required when event_log_path is set"
        assert not log_path.exists()
        assert not event_log.exists()


def test_run_make_step_requires_cycle_when_routing_event_spool():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_log = root / "events.ndjson"
        summary = root / "summary.tsv"
        fake_make = root / "fake-make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(
                str(fake_make),
                "recent-events",
                event_log_path=event_log,
                summary_path=summary,
            )
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("event log routing without cycle should fail")

        assert error == "cycle is required when event_log_path is set"
        assert not event_log.exists()


def test_run_make_step_rejects_non_positive_cycle_when_routing_event_spool():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_log = root / "events.ndjson"
        summary = root / "summary.tsv"
        fake_make = root / "fake-make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(
                str(fake_make),
                "recent-events",
                cycle=0,
                event_log_path=event_log,
                summary_path=summary,
            )
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("event log routing with cycle 0 should fail")

        assert error == "cycle must be a positive integer: 0"
        assert not event_log.exists()


def test_run_make_step_rejects_blank_summary_path_when_routing_event_spool():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_log = root / "events.ndjson"
        log_path = root / "logs" / "cycle-0004-recent-events.log"
        fake_make = root / "fake-make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(
                str(fake_make),
                "recent-events",
                root / "logs",
                4,
                event_log,
                summary_path="",
            )
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("blank summary_path should fail")

        assert error == "summary_path must name a file: "
        assert not log_path.exists()
        assert not event_log.exists()


def test_run_make_step_rejects_blank_event_log_path_before_writing_log():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = root / "summary.tsv"
        log_path = root / "logs" / "cycle-0004-recent-events.log"
        fake_make = root / "fake-make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(
                str(fake_make),
                "recent-events",
                root / "logs",
                4,
                "",
                summary_path=summary,
            )
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("blank event_log_path should fail")

        assert error == "event_log_path must name a file: "
        assert not log_path.exists()


def test_run_make_step_rejects_unknown_step_before_writing_log():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log_path = root / "logs" / "cycle-0001-not-a-step.log"
        fake_make = root / "fake-make"
        fake_make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_make.chmod(0o755)

        try:
            cycle.run_make_step(str(fake_make), "not-a-step", root / "logs", 1)
        except ValueError as e:
            error = str(e)
        else:
            raise AssertionError("unknown self-verify step should fail")

        assert error == "unknown self-verify step: not-a-step"
        assert not log_path.exists()


def test_run_cycle_status_blocks_duplicate_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        rc, reason = cycle.run_cycle_status(1, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "cycle_already_recorded"
    assert calls == []


def test_run_cycle_status_blocks_missing_summary_after_cycle_one():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"

        rc, reason = cycle.run_cycle_status(2, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "no_existing_summary"
    assert calls == []


def test_run_cycle_status_blocks_skipped_cycle_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        rc, reason = cycle.run_cycle_status(3, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "cycle_not_next"
    assert calls == []


def test_run_cycle_status_blocks_malformed_summary_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text("cycle\tstep\n1\treadiness\n", encoding="utf-8")

        rc, reason = cycle.run_cycle_status(2, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "malformed_summary"
    assert calls == []


def test_run_cycle_status_blocks_empty_summary_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text("\t".join(cycle.FIELDNAMES) + "\n", encoding="utf-8")

        rc, reason = cycle.run_cycle_status(2, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "empty_summary"
    assert calls == []


def test_run_cycle_status_blocks_extra_column_summary_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text(
            "\t".join(cycle.FIELDNAMES) + "\n"
            "1\tcodex-status-strict\tok\t0\t2026-06-30T00:00:00+00:00\t2026-06-30T00:00:01+00:00\t1\textra\n",
            encoding="utf-8",
        )

        rc, reason = cycle.run_cycle_status(2, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "malformed_summary"
    assert calls == []


def test_run_cycle_status_blocks_invalid_existing_row_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        bad = _row(1, "codex-status-strict")
        bad["cycle"] = "x"
        _write_summary(summary, [bad])

        rc, reason = cycle.run_cycle_status(2, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "malformed_summary"
    assert calls == []


def test_run_cycle_status_blocks_non_contiguous_existing_summary():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, [*_cycle_rows(1), *_cycle_rows(3)])

        rc, reason = cycle.run_cycle_status(4, summary, lambda step: calls.append(step) or 0)

    assert rc == 1
    assert reason == "malformed_summary"
    assert calls == []


def test_run_cycle_status_blocks_unreadable_summary_before_running_steps():
    calls = []

    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))
        original = cycle.read_summary_rows
        try:
            cycle.read_summary_rows = lambda _path: (_ for _ in ()).throw(OSError("denied"))

            rc, reason = cycle.run_cycle_status(2, summary, lambda step: calls.append(step) or 0)
        finally:
            cycle.read_summary_rows = original

    assert rc == 1
    assert reason == "unreadable_summary"
    assert calls == []


def test_run_cycle_appends_existing_summary_atomically():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        rc = cycle.run_cycle(2, summary, lambda _step: 0)
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))
        leftovers = list(Path(tmp).glob(".summary.tsv.tmp-*"))

    assert rc == 0
    assert [row["cycle"] for row in rows] == ["1", "1", "1", "1", "1", "2", "2", "2", "2"]
    assert leftovers == [], leftovers


def test_append_cycle_rows_preserves_summary_on_replace_failure():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))
        old_replace = cycle.Path.replace

        def boom(_self, _target):
            raise OSError("replace failed")

        cycle.Path.replace = boom
        try:
            try:
                cycle.append_cycle_rows(summary, _cycle_rows(2))
                raise AssertionError("expected OSError")
            except OSError as e:
                assert "replace failed" in str(e)
        finally:
            cycle.Path.replace = old_replace

        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))
        leftovers = list(Path(tmp).glob(".summary.tsv.tmp-*"))

    assert [row["cycle"] for row in rows] == ["1", "1", "1", "1", "1"]
    assert leftovers == [], leftovers


def test_append_cycle_rows_rechecks_duplicate_cycle_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, [*_cycle_rows(1), *_cycle_rows(2)])

        status = cycle.append_cycle_rows(summary, _cycle_rows(2))
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))

    assert status == "recorded"
    assert len(rows) == 9
    assert rows[0]["step"] == "codex-status-strict"


def test_append_cycle_rows_rejects_missing_prior_summary_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"

        status = cycle.append_cycle_rows(summary, _cycle_rows(2))

    assert status == "missing"


def test_append_cycle_rows_rejects_skipped_cycle_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        status = cycle.append_cycle_rows(summary, _cycle_rows(3))

    assert status == "cycle_not_next"


def test_append_cycle_rows_rejects_partial_cycle_rows_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        status = cycle.append_cycle_rows(summary, [_row(2, "readiness")])
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))

    assert status == "partial_cycle_rows"
    assert [row["cycle"] for row in rows] == ["1", "1", "1", "1", "1"]


def test_append_cycle_rows_rejects_duplicate_step_rows_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        status = cycle.append_cycle_rows(summary, [
            _row(2, "codex-status-strict"),
            _row(2, "codex-status-strict"),
        ])
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))

    assert status == "duplicate_step_rows"
    assert [row["cycle"] for row in rows] == ["1", "1", "1", "1", "1"]


def test_append_cycle_rows_rejects_empty_new_rows_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        status = cycle.append_cycle_rows(summary, [])
        rows = list(csv.DictReader(summary.open(encoding="utf-8"), delimiter="\t"))

    assert status == "empty_rows"
    assert [row["cycle"] for row in rows] == ["1", "1", "1", "1", "1"]


def test_append_cycle_rows_rejects_malformed_summary_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text("cycle\tstep\n1\treadiness\n", encoding="utf-8")

        status = cycle.append_cycle_rows(summary, _cycle_rows(2))

    assert status == "malformed"


def test_append_cycle_rows_rejects_empty_summary_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text("\t".join(cycle.FIELDNAMES) + "\n", encoding="utf-8")

        status = cycle.append_cycle_rows(summary, _cycle_rows(2))

    assert status == "empty"


def test_append_cycle_rows_rejects_extra_columns_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text(
            "\t".join(cycle.FIELDNAMES) + "\n"
            "1\tcodex-status-strict\tok\t0\t2026-06-30T00:00:00+00:00\t2026-06-30T00:00:01+00:00\t1\textra\n",
            encoding="utf-8",
        )

        status = cycle.append_cycle_rows(summary, _cycle_rows(2))

    assert status == "malformed"


def test_append_cycle_rows_reports_unreadable_summary_under_lock():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))
        original = cycle.read_summary_rows
        try:
            cycle.read_summary_rows = lambda _path: (_ for _ in ()).throw(OSError("denied"))

            status = cycle.append_cycle_rows(summary, _cycle_rows(2))
        finally:
            cycle.read_summary_rows = original

    assert status == "unreadable"


def test_cycle_rows_state_rejects_malformed_or_mixed_batches():
    assert cycle.cycle_rows_state([]) == ("empty_rows", "")
    malformed = dict(_row(2, "codex-status-strict"))
    malformed["cycle"] = "x"
    assert cycle.cycle_rows_state([malformed]) == ("malformed_rows", "")
    assert cycle.cycle_rows_state([_row(2, "codex-status-strict"), _row(3, "readiness")]) == (
        "mixed_cycle_rows",
        "",
    )
    assert cycle.cycle_rows_state([
        _row(2, "codex-status-strict"),
        _row(2, "codex-status-strict"),
    ]) == ("duplicate_step_rows", "")
    assert cycle.cycle_rows_state([_row(2, "readiness")]) == ("partial_cycle_rows", "")
    assert cycle.cycle_rows_state(_cycle_rows(2)) == ("valid", "2")


def test_existing_cycle_state_blocks_duplicate_cycle_writes():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        assert cycle.existing_cycle_state(summary, 1) == "recorded"
        assert cycle.existing_cycle_state(summary, 2) == "clear"


def test_existing_cycle_state_blocks_missing_and_skipped_cycles():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing.tsv"
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        assert cycle.existing_cycle_state(missing, 1) == "clear"
        assert cycle.existing_cycle_state(missing, 2) == "missing"
        assert cycle.existing_cycle_state(summary, 3) == "cycle_not_next"


def test_existing_cycle_state_rejects_malformed_summary_header():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text("cycle\tstep\n1\treadiness\n", encoding="utf-8")

        assert cycle.existing_cycle_state(summary, 1) == "malformed"


def test_existing_cycle_state_rejects_empty_summary():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text("\t".join(cycle.FIELDNAMES) + "\n", encoding="utf-8")

        assert cycle.existing_cycle_state(summary, 1) == "empty"


def test_auto_cycle_resolution_starts_new_run_when_no_summary_exists():
    with tempfile.TemporaryDirectory() as tmp:
        selected_cycle, summary = cycle.resolve_cycle_and_summary_path(root=Path(tmp))

    assert selected_cycle == 1
    assert summary.name == "summary.tsv"


def test_auto_cycle_resolution_continues_newest_summary():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_summary = root / "old" / "summary.tsv"
        new_summary = root / "new" / "summary.tsv"
        old_summary.parent.mkdir()
        new_summary.parent.mkdir()
        _write_summary(old_summary, _cycle_rows(1))
        _write_summary(new_summary, [*_cycle_rows(1), *_cycle_rows(2)])
        os.utime(old_summary, (1, 1))
        os.utime(new_summary, (2, 2))

        selected_cycle, summary = cycle.resolve_cycle_and_summary_path(root=root)

    assert selected_cycle == 3
    assert summary == new_summary


def test_auto_cycle_resolution_uses_explicit_summary():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, _cycle_rows(1))

        selected_cycle, selected_summary = cycle.resolve_cycle_and_summary_path(
            explicit_summary=str(summary)
        )

    assert selected_cycle == 2
    assert selected_summary == summary


def test_explicit_cycle_one_starts_new_summary_selection():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_summary = root / "old" / "summary.tsv"
        old_summary.parent.mkdir()
        _write_summary(old_summary, _cycle_rows(1))
        os.utime(old_summary, (1, 1))

        selected_cycle, summary = cycle.resolve_cycle_and_summary_path(1, root=root)

    assert selected_cycle == 1
    assert summary.parent != old_summary.parent


def test_default_summary_resolution_continues_existing_run_after_cycle_one():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_summary = root / "old" / "summary.tsv"
        new_summary = root / "new" / "summary.tsv"
        old_summary.parent.mkdir()
        new_summary.parent.mkdir()
        old_summary.write_text("cycle\tstep\tstatus\texit_code\n", encoding="utf-8")
        new_summary.write_text("cycle\tstep\tstatus\texit_code\n", encoding="utf-8")
        os.utime(old_summary, (1, 1))
        os.utime(new_summary, (2, 2))

        assert cycle.resolve_summary_path(1, root=root).parent != new_summary.parent
        assert cycle.resolve_summary_path(2, root=root) == new_summary


def test_default_summary_resolution_requires_existing_run_after_cycle_one():
    with tempfile.TemporaryDirectory() as tmp:
        assert cycle.resolve_summary_path(2, root=Path(tmp)) is None


def test_new_summary_path_keeps_same_instant_runs_distinct_by_nonce():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        instant = dt.datetime(2026, 7, 3, 1, 2, 3, 123456, tzinfo=dt.timezone.utc)
        first = cycle.new_summary_path(
            root=root,
            now=instant,
            nonce="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        second = cycle.new_summary_path(
            root=root,
            now=instant,
            nonce="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )

    assert first != second
    assert first.parent.name == "20260703T010203123456Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert second.parent.name == "20260703T010203123456Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _row(cycle_num, step):
    return {
        "cycle": str(cycle_num),
        "step": step,
        "status": "ok",
        "exit_code": "0",
        "started_at": "2026-06-30T00:00:00+00:00",
        "ended_at": "2026-06-30T00:00:01+00:00",
        "duration_s": "1",
    }


def _cycle_rows(cycle_num):
    return [_row(cycle_num, step) for step in cycle.steps_for_cycle(cycle_num)]


def _write_summary(path, rows):
    lines = ["\t".join(cycle.FIELDNAMES)]
    for row in rows:
        lines.append("\t".join(row[field] for field in cycle.FIELDNAMES))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - self verify cycle")
