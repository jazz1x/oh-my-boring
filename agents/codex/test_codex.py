#!/usr/bin/env python3
"""Network-free regression tests for the Codex adapter."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))

for _var in (
    "BORING_CONFIG",
    "BORING_HOME",
    "BORING_URL",
    "BORING_EVENT_SINK",
    "BORING_EVENT_SPOOL",
    "BORING_EVENT_DB_MIRROR",
    "BORING_LLM_BASE_URL",
    "BORING_LLM_MODEL",
):
    os.environ.pop(_var, None)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = _load("codex_distill_session", "distill-session.py")
collect = _load("codex_collect_sessions", "collect-sessions.py")


def _witness(path: str) -> dict:
    return {
        "path": path,
        "source": "raw-witness/codex/20260703/codex-abc.jsonl#sha256=abc123",
        "sha256": "abc123",
        "bytes": 100,
    }


def _value_error_from(call):
    try:
        call()
    except ValueError as e:
        return str(e)
    raise AssertionError("expected ValueError")


def _run_main(payload: dict, extracted: str):
    stderr = io.StringIO()
    with (
        mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
        mock.patch.object(distill.sys, "stderr", stderr),
        mock.patch.object(distill, "write_raw_witness", return_value=_witness(payload["transcript_path"])),
        mock.patch.object(distill, "extract", return_value=extracted),
        mock.patch.object(distill, "git_remote_url", return_value=""),
        mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
        mock.patch.object(distill, "_mark") as mark,
    ):
        rc = distill.main()
    return rc, stderr.getvalue(), mark


def test_large_raw_parse_short_marks_retry():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "last_agent_message": "assistant result " * 40,
                    },
                }
            )
            + "\n"
        )
        path = f.name
    try:
        payload = {
            "transcript_path": path,
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "raw_bytes": 100,
            "min_raw_bytes_for_retry": 10,
        }
        rc, err, mark = _run_main(payload, "too short")
        assert rc == 1
        assert "marked for retry" in err
        mark.assert_called_once_with("codex-abc", retry=True)
    finally:
        os.unlink(path)


def test_large_raw_with_only_tiny_assistant_parse_short_marks_done():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"base_instructions": {"text": "x" * 9000}},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": "ok",
                    },
                }
            )
            + "\n"
        )
        path = f.name
    try:
        payload = {
            "transcript_path": path,
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "raw_bytes": 9000,
            "min_raw_bytes_for_retry": 10,
        }
        rc, err, mark = _run_main(payload, "too short")
        assert rc == 0
        assert "transcript too short; skipping" in err
        mark.assert_called_once_with("codex-abc")
    finally:
        os.unlink(path)


def test_large_raw_import_only_parse_short_marks_done():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"base_instructions": {"text": "x" * 9000}},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": distill.EXTERNAL_IMPORT_MESSAGE,
                    },
                }
            )
            + "\n"
        )
        path = f.name
    try:
        payload = {
            "transcript_path": path,
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "raw_bytes": 9000,
            "min_raw_bytes_for_retry": 10,
        }
        rc, err, mark = _run_main(payload, "too short")
        assert rc == 0
        assert "transcript too short; skipping" in err
        mark.assert_called_once_with("codex-abc")
    finally:
        os.unlink(path)


def test_small_raw_parse_short_marks_done():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("{}\n")
        path = f.name
    try:
        with tempfile.TemporaryDirectory() as d:
            event_path = Path(d) / "events.ndjson"
            payload = {
                "transcript_path": path,
                "session_id": "abc",
                "hook_event_name": "SessionEnd",
                "raw_bytes": 5,
                "min_raw_bytes_for_retry": 10,
            }
            with mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}):
                rc, err, mark = _run_main(payload, "too short")
            assert rc == 0
            assert "transcript too short" in err
            mark.assert_called_once_with("codex-abc")
            event = _read_last_event(event_path)
            assert event["reason"] == "too_short"
            assert event["workflow_node"] == "skipped"
            assert event["workflow_outcome"] == "skip"
    finally:
        os.unlink(path)


def test_success_passes_session_id_to_shared_core():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("{}\n")
        path = f.name
    try:
        payload = {
            "transcript_path": path,
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "cwd": "/work/oh-my-boring",
        }
        stderr = io.StringIO()
        extracted = "x" * 600
        with (
            mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(distill.sys, "stderr", stderr),
            mock.patch.object(distill, "write_raw_witness", return_value=_witness(path)),
            mock.patch.object(distill, "extract", return_value=extracted),
            mock.patch.object(distill, "git_remote_url", return_value=""),
            mock.patch.object(distill, "repo_slug", return_value="oh-my-boring"),
            mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
            mock.patch.object(distill, "_mark") as mark,
            mock.patch.object(distill, "distill_and_remember", return_value=True) as remember,
        ):
            rc = distill.main()

        assert rc == 0
        remember.assert_called_once_with(
            extracted,
            "personal",
            "oh-my-boring",
            "codex-abc",
            sources=["raw-witness/codex/20260703/codex-abc.jsonl#sha256=abc123"],
        )
        mark.assert_called_once_with("codex-abc")
    finally:
        os.unlink(path)


def test_run_reports_invalid_distill_resolution_without_traceback():
    stderr = io.StringIO()

    with (
        mock.patch.object(distill, "main", side_effect=ValueError("invalid BORING_DISTILL_RESOLUTION='typo'")),
        mock.patch.object(distill.sys, "stderr", stderr),
    ):
        rc = distill.run()

    assert rc == 1
    assert "invalid BORING_DISTILL_RESOLUTION" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_distill_extracts_from_raw_witness_snapshot():
    with tempfile.TemporaryDirectory() as d:
        original = Path(d) / "original.jsonl"
        witness_path = Path(d) / "raw-witness-copy.jsonl"
        original.write_text("{}\n", encoding="utf-8")
        witness_path.write_text("{}\n", encoding="utf-8")
        payload = {
            "transcript_path": str(original),
            "session_id": "abc",
            "hook_event_name": "SessionEnd",
            "cwd": "/work/oh-my-boring",
        }
        stderr = io.StringIO()
        extracted = "x" * 600
        with (
            mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(distill.sys, "stderr", stderr),
            mock.patch.object(distill, "write_raw_witness", return_value=_witness(str(witness_path))),
            mock.patch.object(distill, "extract", return_value=extracted) as extract,
            mock.patch.object(distill, "git_remote_url", return_value=""),
            mock.patch.object(distill, "repo_slug", return_value="oh-my-boring"),
            mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
            mock.patch.object(distill, "_mark"),
            mock.patch.object(distill, "distill_and_remember", return_value=True),
        ):
            rc = distill.main()

        assert rc == 0
        extract.assert_called_once_with(str(witness_path))


def test_codex_distill_clamps_with_ingest_budget():
    old_clamp = distill.CLAMP
    try:
        distill.CLAMP = 80
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("{}\n")
            path = f.name
        try:
            with tempfile.TemporaryDirectory() as d:
                payload = {
                    "transcript_path": path,
                    "session_id": "abc",
                    "hook_event_name": "SessionEnd",
                    "cwd": "/work/oh-my-boring",
                    "raw_bytes": 9000,
                    "distill_clamp": 80,
                }
                event_path = Path(d) / "events.ndjson"
                extracted = "START-" + ("x" * 700) + "-END"
                stderr = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
                    mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(distill.sys, "stderr", stderr),
                    mock.patch.object(distill, "write_raw_witness", return_value=_witness(path)),
                    mock.patch.object(distill, "extract", return_value=extracted),
                    mock.patch.object(distill, "git_remote_url", return_value=""),
                    mock.patch.object(distill, "repo_slug", return_value="oh-my-boring"),
                    mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
                    mock.patch.object(distill, "_mark") as mark,
                    mock.patch.object(distill, "distill_and_remember", return_value=True) as remember,
                ):
                    rc = distill.main()

                assert rc == 0
                remembered_text = remember.call_args.args[0]
                assert remembered_text.startswith("START-")
                assert remembered_text.endswith("-END")
                assert len(remembered_text) < len(extracted)
                assert "transcript clamped" in stderr.getvalue()
                event = _read_last_event(event_path)
                assert event["event"] == "input_budget"
                assert event["raw_bytes"] == 9000
                assert event["source_chars"] == len(extracted)
                assert event["emitted_chars"] == len(remembered_text)
                assert event["distill_clamp"] == 80
                assert event["clamped"] is True
                mark.assert_called_once_with("codex-abc")
        finally:
            os.unlink(path)
    finally:
        distill.CLAMP = old_clamp


def test_codex_distill_respects_zero_payload_clamp_override():
    old_clamp = distill.CLAMP
    try:
        distill.CLAMP = 80
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("{}\n")
            path = f.name
        try:
            with tempfile.TemporaryDirectory() as d:
                payload = {
                    "transcript_path": path,
                    "session_id": "abc",
                    "hook_event_name": "SessionEnd",
                    "cwd": "/work/oh-my-boring",
                    "raw_bytes": 9000,
                    "distill_clamp": 0,
                }
                event_path = Path(d) / "events.ndjson"
                extracted = "START-" + ("x" * 700) + "-END"
                stderr = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
                    mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(distill.sys, "stderr", stderr),
                    mock.patch.object(distill, "write_raw_witness", return_value=_witness(path)),
                    mock.patch.object(distill, "extract", return_value=extracted),
                    mock.patch.object(distill, "git_remote_url", return_value=""),
                    mock.patch.object(distill, "repo_slug", return_value="oh-my-boring"),
                    mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)),
                    mock.patch.object(distill, "_mark"),
                    mock.patch.object(distill, "distill_and_remember", return_value=True) as remember,
                ):
                    rc = distill.main()

                assert rc == 0
                assert remember.call_args.args[0] == extracted
                assert "transcript clamped" not in stderr.getvalue()
                event = _read_last_event(event_path)
                assert event["distill_clamp"] == 0
                assert event["emitted_chars"] == len(extracted)
                assert event["clamped"] is False
        finally:
            os.unlink(path)
    finally:
        distill.CLAMP = old_clamp


def test_codex_distill_rejects_invalid_payload_clamp():
    for raw in (-1, "soon", True, 1.5):
        msg = _value_error_from(lambda raw=raw: distill._clamp_limit({"distill_clamp": raw}))
        assert "distill_clamp must be a non-negative integer" in msg


def test_codex_distill_rejects_invalid_env_clamp():
    old_clamp = distill.CLAMP
    try:
        distill.CLAMP = None
        with mock.patch.dict(os.environ, {"CODEX_DISTILL_CLAMP": "-1"}, clear=True):
            msg = _value_error_from(lambda: distill._clamp_limit({}))
            assert "CODEX_DISTILL_CLAMP must be a non-negative integer" in msg
        with mock.patch.dict(os.environ, {"INGEST_CLAMP": "soon"}, clear=True):
            msg = _value_error_from(lambda: distill._clamp_limit({}))
            assert "INGEST_CLAMP must be a non-negative integer" in msg
    finally:
        distill.CLAMP = old_clamp


def test_codex_collect_rejects_invalid_distill_clamp():
    old_clamp = collect.DISTILL_CLAMP
    try:
        collect.DISTILL_CLAMP = None
        with mock.patch.dict(os.environ, {"CODEX_DISTILL_CLAMP": "-1"}, clear=True):
            msg = _value_error_from(collect._effective_distill_clamp)
            assert "CODEX_DISTILL_CLAMP must be a non-negative integer" in msg
        with mock.patch.dict(os.environ, {"INGEST_CLAMP": "soon"}, clear=True):
            msg = _value_error_from(collect._effective_distill_clamp)
            assert "INGEST_CLAMP must be a non-negative integer" in msg
        collect.DISTILL_CLAMP = 0
        assert collect._effective_distill_clamp() == 0
    finally:
        collect.DISTILL_CLAMP = old_clamp


def _write_codex_session(path: Path, subagent: bool = False) -> None:
    payload = {"thread_source": "subagent"} if subagent else {}
    path.write_text(json.dumps({"payload": payload}) + "\nbody\n", encoding="utf-8")


def _read_last_event(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def test_collect_scan_classifies_queue_marked_rollout_and_subagent():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_include = collect.INCLUDE_SUBAGENTS
    old_include_rollouts = collect.INCLUDE_ROLLOUTS
    old_stable_age = collect.STABLE_AGE_S
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.INCLUDE_SUBAGENTS = False
            collect.INCLUDE_ROLLOUTS = False
            collect.STABLE_AGE_S = 0

            _write_codex_session(source / "todo.jsonl")
            _write_codex_session(source / "done.jsonl")
            _write_codex_session(source / "rollout-2026-06-29T19-29-28-demo.jsonl")
            _write_codex_session(source / "subagent.jsonl", subagent=True)
            collect.markers.mark_done("codex-done")

            scan = collect._scan_sessions(str(source), cutoff=0)

            assert scan["total"] == 4
            assert scan["already_marked"] == 1
            assert scan["rollout"] == 1
            assert scan["subagent"] == 1
            assert [Path(p).name for p in scan["todo"]] == ["todo.jsonl"]
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.INCLUDE_SUBAGENTS = old_include
        collect.INCLUDE_ROLLOUTS = old_include_rollouts
        collect.STABLE_AGE_S = old_stable_age


def test_collect_scan_can_include_rollouts_without_subagents():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_include = collect.INCLUDE_SUBAGENTS
    old_include_rollouts = collect.INCLUDE_ROLLOUTS
    old_stable_age = collect.STABLE_AGE_S
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.INCLUDE_SUBAGENTS = False
            collect.INCLUDE_ROLLOUTS = True
            collect.STABLE_AGE_S = 0

            _write_codex_session(source / "rollout-2026-06-29T19-29-28-demo.jsonl")
            _write_codex_session(source / "subagent.jsonl", subagent=True)

            scan = collect._scan_sessions(str(source), cutoff=0)

            assert scan["rollout"] == 0
            assert scan["subagent"] == 1
            assert [Path(p).name for p in scan["todo"]] == ["rollout-2026-06-29T19-29-28-demo.jsonl"]
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.INCLUDE_SUBAGENTS = old_include
        collect.INCLUDE_ROLLOUTS = old_include_rollouts
        collect.STABLE_AGE_S = old_stable_age


def test_collect_scan_skips_unstable_recent_sessions():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_stable_age = collect.STABLE_AGE_S
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.STABLE_AGE_S = 60

            _write_codex_session(source / "recent.jsonl")
            _write_codex_session(source / "stable.jsonl")
            old = time.time() - 120
            os.utime(source / "stable.jsonl", (old, old))

            scan = collect._scan_sessions(str(source), cutoff=0)

            assert scan["too_new"] == 1
            assert [Path(p).name for p in scan["todo"]] == ["stable.jsonl"]
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.STABLE_AGE_S = old_stable_age


def test_collect_scan_reoffers_stale_pending_but_skips_fresh_pending():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_pending_ttl = collect.PENDING_TTL
    old_stable_age = collect.STABLE_AGE_S
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.PENDING_TTL = 60
            collect.STABLE_AGE_S = 0

            _write_codex_session(source / "fresh.jsonl")
            _write_codex_session(source / "stale.jsonl")
            collect.markers.mark_pending("codex-fresh")
            collect.markers.mark_pending("codex-stale")
            stale_path = mark_dir / "codex-stale.pending"
            old = time.time() - 3600
            os.utime(stale_path, (old, old))

            scan = collect._scan_sessions(str(source), cutoff=0)

            assert scan["already_marked"] == 1
            assert [Path(p).name for p in scan["todo"]] == ["stale.jsonl"]
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.PENDING_TTL = old_pending_ttl
        collect.STABLE_AGE_S = old_stable_age


def test_collect_noop_run_logs_workflow_fields():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            event_path = root / "events.ndjson"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0

            stdout = io.StringIO()
            with (
                mock.patch.object(collect, "_source_dir", return_value=str(source)),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
                mock.patch.object(collect.sys, "stdout", stdout),
                mock.patch.object(collect.subprocess, "run") as run,
                mock.patch.object(collect, "DrudgeClient") as client,
            ):
                rc = collect.main([])

            assert rc == 0
            run.assert_not_called()
            client.assert_not_called()
            event = _read_last_event(event_path)
            assert event["event"] == "collector_run"
            assert event["workflow"] == "memory_ingest"
            assert event["workflow_node"] == "readiness_projected"
            assert event["workflow_outcome"] == "pass"
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb


def test_collect_success_with_sync_failure_is_degraded_success():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_stable_age = collect.STABLE_AGE_S
    old_limit = collect.LIMIT
    old_distill_clamp = collect.DISTILL_CLAMP
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            event_path = root / "events.ndjson"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.STABLE_AGE_S = 0
            collect.LIMIT = 1
            collect.DISTILL_CLAMP = 123
            _write_codex_session(source / "todo.jsonl")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(collect, "_source_dir", return_value=str(source)),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
                mock.patch.object(collect.sys, "stdout", stdout),
                mock.patch.object(collect.sys, "stderr", stderr),
                mock.patch.object(collect.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                mock.patch.object(collect, "DrudgeClient") as client,
            ):
                client.return_value.sync.side_effect = TimeoutError("timed out")
                rc = collect.main([])

            assert rc == 0
            payload = json.loads(run.call_args.kwargs["input"])
            assert payload["distill_clamp"] == 123
            assert "sync failed: timed out" in stderr.getvalue()
            assert "sync=failed" in stdout.getvalue()
            event = _read_last_event(event_path)
            assert event["status"] == "ok"
            assert event["processed"] == 1
            assert event["failed"] == 0
            assert event["sync_status"] == "failed"
            assert event["sync_degraded"] is True
            assert event["workflow_node"] == "done_marked"
            assert event["workflow_outcome"] == "continue"
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.STABLE_AGE_S = old_stable_age
        collect.LIMIT = old_limit
        collect.DISTILL_CLAMP = old_distill_clamp


def test_status_mode_reports_queue_worker_and_note_without_mutation():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_include = collect.INCLUDE_SUBAGENTS
    old_include_rollouts = collect.INCLUDE_ROLLOUTS
    old_stable_age = collect.STABLE_AGE_S
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            vault = root / "vault"
            wiki = vault / "wiki"
            wiki.mkdir(parents=True)
            jobs = root / "jobs.json"
            event_path = root / "events.ndjson"

            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.INCLUDE_SUBAGENTS = False
            collect.INCLUDE_ROLLOUTS = False
            collect.STABLE_AGE_S = 0
            _write_codex_session(source / "todo.jsonl")
            collect.markers.mark_done("codex-done")
            collect.markers.mark_done("codex-rollout-2026-06-29T19-29-28-demo")
            jobs.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "name": "codex-memory-ingest-worker",
                                "enabled": True,
                                "state": "scheduled",
                                "last_status": "success",
                                "last_run_at": "2026-06-29T09:00:00+09:00",
                                "next_run_at": "2999-06-29T09:20:00+09:00",
                                "script": "/host/oh-my-boring/agents/codex/collect-sessions.py",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (wiki / "wiki-0001.md").write_text(
                "---\ntitle: codex note\nomb_session_id: codex-note\n---\nbody\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with (
                mock.patch.object(collect, "_source_dir", return_value=str(source)),
                mock.patch.object(collect, "_hermes_jobs_path", return_value=str(jobs)),
                mock.patch.object(
                    collect,
                    "_host_worker_status",
                    return_value={
                        "kind": "launchd",
                        "found": True,
                        "loaded": True,
                        "path": "/tmp/com.ohmyboring.codex-ingest.plist",
                    },
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "BORING_VAULT_DIR": str(vault),
                        "BORING_EVENT_LOG": str(event_path),
                        "BORING_EVENT_SINK": "spool",
                    },
                ),
                mock.patch.object(collect.sys, "stdout", stdout),
                mock.patch.object(collect.subprocess, "run") as run,
                mock.patch.object(collect, "DrudgeClient") as client,
            ):
                rc = collect.main(["--status"])

            assert rc == 0
            run.assert_not_called()
            client.assert_not_called()
            out = stdout.getvalue()
            assert "queue_pending=1" in out
            assert "next_extract classification=short_skip" in out
            assert "distill_clamp=" in out
            assert "skipped_new=0 skipped_small=0 skipped_rollout=0 skipped_marked=0 skipped_subagent=0" in out
            assert "markers done=1 pending=0 retry=0 dead_letter=0 stale_pending=0 stale_retry=0" in out
            assert "codex-rollout-" not in out
            assert "worker found=true enabled=true state=scheduled last_status=success" in out
            assert "host_worker found=true loaded=true kind=launchd" in out
            assert "newest_note session_id=codex-note" in out
            event = _read_last_event(event_path)
            assert event["event"] == "collector_status"
            assert event["workflow_node"] == "readiness_projected"
            assert event["workflow_outcome"] == "pass"
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.INCLUDE_SUBAGENTS = old_include
        collect.INCLUDE_ROLLOUTS = old_include_rollouts
        collect.STABLE_AGE_S = old_stable_age


def test_newest_codex_note_includes_harvested_rollout():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vault = root / "vault"
        wiki = vault / "wiki"
        wiki.mkdir(parents=True)
        normal = wiki / "wiki-0001.md"
        rollout = wiki / "wiki-0002.md"
        normal.write_text("---\ntitle: codex\nomb_session_id: codex-note\n---\nbody\n", encoding="utf-8")
        rollout.write_text(
            "---\ntitle: rollout\nomb_session_id: codex-rollout-note\n---\nbody\n",
            encoding="utf-8",
        )
        old = time.time() - 60
        new = time.time()
        os.utime(normal, (old, old))
        os.utime(rollout, (new, new))

        with mock.patch.dict(os.environ, {"BORING_VAULT_DIR": str(vault)}):
            note = collect._newest_codex_note()

        assert note["session_id"] == "codex-rollout-note"
        assert note["path"] == str(rollout)


def test_next_extract_probe_classifies_metadata_heavy_short_rollout():
    old_min_kb = collect.MIN_KB
    try:
        collect.MIN_KB = 1
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            path.write_text(
                json.dumps({"type": "session_meta", "payload": {"base_instructions": {"text": "x" * 9000}}})
                + "\n"
                + json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}})
                + "\n",
                encoding="utf-8",
            )

            probe = collect._next_extract_probe(str(path))

        assert probe["raw_bytes"] >= 1024
        assert probe["extracted_chars"] < 500
        assert probe["assistant_chars"] == 2
        assert probe["short_retry"] is False
        assert probe["classification"] == "short_skip"
    finally:
        collect.MIN_KB = old_min_kb


def test_next_extract_probe_classifies_substantive_session():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "session.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "substantive " * 80}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        probe = collect._next_extract_probe(str(path))

    assert probe["extracted_chars"] >= 500
    assert probe["assistant_chars"] >= 500
    assert probe["short_retry"] is False
    assert probe["classification"] == "distillable"


def test_codex_note_session_id_uses_yaml_frontmatter():
    with tempfile.TemporaryDirectory() as d:
        note = Path(d) / "wiki-0001.md"
        note.write_text(
            "---\ntitle: codex\nomb_session_id: 'codex-note' # collected\n---\nbody\n",
            encoding="utf-8",
        )

        assert collect._frontmatter_session_id(str(note)) == "codex-note"


def test_codex_note_session_id_rejects_non_string_yaml_value():
    with tempfile.TemporaryDirectory() as d:
        note = Path(d) / "wiki-0001.md"
        note.write_text("---\ntitle: codex\nomb_session_id: [bad]\n---\nbody\n", encoding="utf-8")

        assert collect._frontmatter_session_id(str(note)) == ""


def test_status_strict_fails_when_host_worker_missing():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            event_path = root / "events.ndjson"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            _write_codex_session(source / "todo.jsonl")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(collect, "_source_dir", return_value=str(source)),
                mock.patch.object(collect, "_hermes_worker_status", return_value={"found": False}),
                mock.patch.object(
                    collect,
                    "_host_worker_status",
                    return_value={
                        "kind": "launchd",
                        "found": False,
                        "loaded": False,
                        "path": "/tmp/com.ohmyboring.codex-ingest.plist",
                    },
                ),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
                mock.patch.object(collect.sys, "stdout", stdout),
                mock.patch.object(collect.sys, "stderr", stderr),
            ):
                rc = collect.main(["--status", "--strict"])

            assert rc == 1
            assert "host_worker found=false loaded=false kind=launchd" in stdout.getvalue()
            assert "readiness failed: worker/marker state is not ready" in stderr.getvalue()
            event = _read_last_event(event_path)
            assert event["event"] == "collector_status"
            assert event["workflow_node"] == "readiness_projected"
            assert event["workflow_outcome"] == "fail"
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb


def test_status_strict_reports_hermes_worker_failure_as_notice_when_host_worker_loaded():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            _write_codex_session(source / "todo.jsonl")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(collect, "_source_dir", return_value=str(source)),
                mock.patch.object(
                    collect,
                    "_hermes_worker_status",
                    return_value={
                        "found": True,
                        "enabled": True,
                        "state": "scheduled",
                        "last_status": "failed",
                        "last_error": "Script exited with code 1\nstderr:\nTraceback boom\nstdout:\ndone",
                        "last_run_at": "2026-06-29T09:00:00+09:00",
                        "next_run_at": "2999-06-29T09:20:00+09:00",
                        "script": "codex-collect-sessions.py",
                        "path": "/tmp/jobs.json",
                    },
                ),
                mock.patch.object(
                    collect,
                    "_host_worker_status",
                    return_value={
                        "kind": "launchd",
                        "found": True,
                        "loaded": True,
                        "path": "/tmp/com.ohmyboring.codex-ingest.plist",
                    },
                ),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(root / "events.ndjson"), "BORING_EVENT_SINK": "spool"}),
                mock.patch.object(collect.sys, "stdout", stdout),
                mock.patch.object(collect.sys, "stderr", stderr),
            ):
                rc = collect.main(["--status", "--strict"])

            assert rc == 0
            out = stdout.getvalue()
            worker_lines = [line for line in out.splitlines() if line.startswith("[codex-status] worker ")]
            assert len(worker_lines) == 1
            assert "last_error=Script exited with code 1 stderr: Traceback boom stdout: done" in worker_lines[0]
            assert "stderr:\nTraceback" not in out
            assert "readiness_notice hermes codex worker last_status=failed" in out
            assert "readiness_notice hermes codex worker last_error set" in out
            assert "readiness failed: worker/marker state is not ready" not in stderr.getvalue()
            event = _read_last_event(root / "events.ndjson")
            assert event["workflow_node"] == "readiness_projected"
            assert event["workflow_outcome"] == "pass"
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb


def test_status_strict_fails_on_stale_codex_markers():
    old_mark_dir = collect.markers.MARK_DIR
    old_min_kb = collect.MIN_KB
    old_pending_ttl = collect.PENDING_TTL
    old_retry_ttl = collect.RETRY_TTL
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "sessions"
            source.mkdir()
            mark_dir = root / "markers"
            collect.markers.set_mark_dir(str(mark_dir))
            collect.MIN_KB = 0
            collect.PENDING_TTL = 60
            collect.RETRY_TTL = 60
            _write_codex_session(source / "todo.jsonl")
            collect.markers.mark_pending("codex-pending")
            collect.markers.mark_retry("codex-retry")
            collect.markers.mark_retry("codex-rollout-retry")
            old = time.time() - 3600
            os.utime(mark_dir / "codex-pending.pending", (old, old))
            os.utime(mark_dir / "codex-retry.retry", (old, old))
            os.utime(mark_dir / "codex-rollout-retry.retry", (old, old))
            (mark_dir / "codex-dead.dead").write_text("dead", encoding="utf-8")
            (mark_dir / "codex-dead-letter.dead-letter").write_text("dead", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(collect, "_source_dir", return_value=str(source)),
                mock.patch.object(
                    collect,
                    "_hermes_worker_status",
                    return_value={
                        "found": True,
                        "enabled": True,
                        "state": "scheduled",
                        "last_status": "success",
                        "last_error": "",
                        "last_run_at": "2026-06-29T09:00:00+09:00",
                        "next_run_at": "2999-06-29T09:20:00+09:00",
                        "script": "codex-collect-sessions.py",
                        "path": "/tmp/jobs.json",
                    },
                ),
                mock.patch.object(
                    collect,
                    "_host_worker_status",
                    return_value={
                        "kind": "launchd",
                        "found": True,
                        "loaded": True,
                        "path": "/tmp/com.ohmyboring.codex-ingest.plist",
                    },
                ),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(root / "events.ndjson"), "BORING_EVENT_SINK": "spool"}),
                mock.patch.object(collect.sys, "stdout", stdout),
                mock.patch.object(collect.sys, "stderr", stderr),
            ):
                rc = collect.main(["--status", "--strict"])

            assert rc == 1
            out = stdout.getvalue()
            assert "dead_letter=2 stale_pending=1 stale_retry=2" in out
            assert "readiness_issue stale codex pending markers=1" in out
            assert "readiness_issue stale codex retry markers=2" in out
            assert "readiness_issue codex dead-letter markers=2" in out
    finally:
        collect.markers.set_mark_dir(old_mark_dir)
        collect.MIN_KB = old_min_kb
        collect.PENDING_TTL = old_pending_ttl
        collect.RETRY_TTL = old_retry_ttl


if __name__ == "__main__":
    test_large_raw_parse_short_marks_retry()
    test_large_raw_with_only_tiny_assistant_parse_short_marks_done()
    test_large_raw_import_only_parse_short_marks_done()
    test_small_raw_parse_short_marks_done()
    test_success_passes_session_id_to_shared_core()
    test_run_reports_invalid_distill_resolution_without_traceback()
    test_distill_extracts_from_raw_witness_snapshot()
    test_codex_distill_clamps_with_ingest_budget()
    test_codex_distill_respects_zero_payload_clamp_override()
    test_codex_distill_rejects_invalid_payload_clamp()
    test_codex_distill_rejects_invalid_env_clamp()
    test_codex_collect_rejects_invalid_distill_clamp()
    test_collect_scan_classifies_queue_marked_rollout_and_subagent()
    test_collect_scan_can_include_rollouts_without_subagents()
    test_collect_scan_skips_unstable_recent_sessions()
    test_collect_scan_reoffers_stale_pending_but_skips_fresh_pending()
    test_collect_noop_run_logs_workflow_fields()
    test_collect_success_with_sync_failure_is_degraded_success()
    test_status_mode_reports_queue_worker_and_note_without_mutation()
    test_newest_codex_note_includes_harvested_rollout()
    test_next_extract_probe_classifies_metadata_heavy_short_rollout()
    test_next_extract_probe_classifies_substantive_session()
    test_codex_note_session_id_uses_yaml_frontmatter()
    test_codex_note_session_id_rejects_non_string_yaml_value()
    test_status_strict_fails_when_host_worker_missing()
    test_status_strict_reports_hermes_worker_failure_as_notice_when_host_worker_loaded()
    test_status_strict_fails_on_stale_codex_markers()
    print("ok - codex adapter")
