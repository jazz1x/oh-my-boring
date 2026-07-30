#!/usr/bin/env python3
"""Network-free regression tests for the Kimi Code CLI adapters."""
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))

# Neutralize ambient env so module-load + assertions are deterministic.
for _var in ("BORING_CONFIG", "BORING_HOME", "BORING_URL", "BORING_EVENT_SINK",
             "BORING_EVENT_SPOOL", "BORING_EVENT_DB_MIRROR", "BORING_LLM_BASE_URL",
             "BORING_LLM_MODEL", "KIMI_CODE_HOME"):
    os.environ.pop(_var, None)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


distill = _load("kimi_distill_session", "distill-session.py")
recall = _load("kimi_recall", "recall.py")
import recall_core  # noqa: E402


def _write_wire(session_dir: str, text: str = "hello") -> str:
    wire = Path(session_dir) / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text(
        json.dumps({"type": "turn.prompt", "input": [{"type": "text", "text": text}]}) + "\n",
        encoding="utf-8",
    )
    return str(wire)


def _witness(path: str) -> dict:
    return {
        "path": path,
        "source": "raw-witness/kimi/20260703/session-abc.jsonl#sha256=abc123",
        "sha256": "abc123",
        "bytes": 100,
    }


def test_work_dir_key_format():
    key = distill._work_dir_key("/home/user/my-project")
    assert key.startswith("wd_my-project_")
    assert len(key.split("_")[-1]) == 12


def test_find_session_dir_uses_index():
    with tempfile.TemporaryDirectory() as home:
        os.environ["KIMI_CODE_HOME"] = home
        session_dir = Path(home) / "sessions" / "wd_x_1234567890ab" / "session_abc"
        session_dir.mkdir(parents=True)
        index = Path(home) / "session_index.jsonl"
        index.write_text(
            json.dumps({"sessionId": "session_abc", "sessionDir": str(session_dir), "workDir": "/x"})
            + "\n",
            encoding="utf-8",
        )
        # Load a fresh module copy so the new KIMI_HOME constant is picked up.
        fresh = _load("kimi_distill_session_fresh", "distill-session.py")
        found = fresh._find_session_dir("session_abc", "/x")
        assert found == str(session_dir)


def test_extract_session_filters_injection():
    with tempfile.TemporaryDirectory() as d:
        wire = Path(d) / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.write_text(
            json.dumps({"type": "turn.prompt", "input": [{"type": "text", "text": "hello"}]})
            + "\n"
            + json.dumps(
                {
                    "type": "context.append_message",
                    "message": {
                        "role": "user",
                        "origin": {"kind": "injection"},
                        "content": [{"type": "text", "text": "system reminder"}],
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "context.append_loop_event",
                    "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}},
                }
            )
            + "\n"
        )
        out = distill.extract_session(str(d))
        assert "[user] hello" in out
        assert "[assistant] hi" in out
        assert "system reminder" not in out


def test_recall_skips_short_and_injection():
    assert recall._is_injection({"origin": {"kind": "injection"}}) is True
    assert recall._is_injection({"origin": {"kind": "user"}, "prompt": "hi"}) is False


def test_recall_formats_context():
    captured = io.StringIO()
    hits = [{"source_path": "vault/wiki/wiki-0007.md", "snippet": "fixed   the\ncache"}]
    with mock.patch.object(recall.sys, "stdin", io.StringIO(json.dumps({
        "prompt": "how did I fix the docker cache issue",
        "origin": {"kind": "user"},
    }))), \
         mock.patch.object(recall_core.DrudgeClient, "search", return_value=hits), \
         mock.patch.object(recall.sys, "stdout", captured):
        recall.main()
    payload = json.loads(captured.getvalue())
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "- [wiki-0007.md] fixed the cache" in ctx


def test_recall_failed_search_logs_to_stderr():
    recall_core.RETRIES = 0
    try:
        captured = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(recall.sys, "stdin", io.StringIO(json.dumps({
            "prompt": "how did I fix the docker cache issue",
            "origin": {"kind": "user"},
        }))), \
             mock.patch.object(recall_core.DrudgeClient, "search", side_effect=OSError("down")), \
             mock.patch.object(recall.sys, "stdout", captured), \
             mock.patch.object(recall.sys, "stderr", stderr):
            recall.main()
        assert captured.getvalue() == ""
        assert "[omb-recall] search failed" in stderr.getvalue()
    finally:
        recall_core.RETRIES = 1


def test_distill_invalid_stdin_logs_error():
    captured = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(distill.sys, "stdin", io.StringIO("not json")), \
         mock.patch.object(distill.sys, "stdout", captured), \
         mock.patch.object(distill.sys, "stderr", stderr), \
         mock.patch.object(distill, "_throttled", return_value=False):
        rc = distill.main()
    assert captured.getvalue() == ""
    assert rc == 2
    assert "[omb-distill] invalid stdin JSON" in stderr.getvalue()


def test_distill_short_transcript_logs_skip_and_marks_done():
    with tempfile.TemporaryDirectory() as session_dir:
        root = Path(session_dir)
        wire_path = _write_wire(session_dir)
        event_path = root / "events.ndjson"
        captured = io.StringIO()
        stderr = io.StringIO()
        payload = {"session_id": "session_abc", "cwd": "/x", "hook_event_name": "SessionEnd"}
        with mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(distill.sys, "stdout", captured), \
             mock.patch.object(distill.sys, "stderr", stderr), \
             mock.patch.object(distill, "_find_session_dir", return_value=session_dir), \
             mock.patch.object(distill, "write_raw_witness", return_value=_witness(wire_path)), \
             mock.patch.object(distill, "extract_wire", return_value="too short"), \
             mock.patch.object(distill, "git_remote_url", return_value=""), \
             mock.patch.object(distill, "repo_slug", return_value="repo"), \
             mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)), \
             mock.patch.object(distill, "_mark") as mark, \
             mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}):
            rc = distill.main()

        assert captured.getvalue() == ""
        assert rc == 0
        mark.assert_called_once_with("session_abc")
        assert "transcript too short" in stderr.getvalue()
        event = _read_last_event(event_path)
        assert event["reason"] == "too_short"
        assert event["workflow_node"] == "skipped"
        assert event["workflow_outcome"] == "skip"


def test_distill_remember_failure_returns_nonzero_and_marks_retry():
    with tempfile.TemporaryDirectory() as session_dir:
        wire_path = _write_wire(session_dir)
        captured = io.StringIO()
        stderr = io.StringIO()
        payload = {"session_id": "session_abc", "cwd": "/x", "hook_event_name": "SessionEnd"}
        with mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(distill.sys, "stdout", captured), \
             mock.patch.object(distill.sys, "stderr", stderr), \
             mock.patch.object(distill, "_find_session_dir", return_value=session_dir), \
             mock.patch.object(distill, "write_raw_witness", return_value=_witness(wire_path)), \
             mock.patch.object(distill, "extract_wire", return_value="x" * 600), \
             mock.patch.object(distill, "git_remote_url", return_value=""), \
             mock.patch.object(distill, "repo_slug", return_value="repo"), \
             mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)), \
             mock.patch.object(distill, "distill_and_remember", return_value=False) as remember, \
             mock.patch.object(distill, "_mark") as mark:
            rc = distill.main()

    assert captured.getvalue() == ""
    assert rc == 1
    remember.assert_called_once_with(
        "x" * 600,
        "personal",
        "repo",
        "session_abc",
        sources=["raw-witness/kimi/20260703/session-abc.jsonl#sha256=abc123"],
    )
    mark.assert_called_once_with("session_abc", retry=True)
    assert "remember failed" in stderr.getvalue()


def test_distill_extracts_from_raw_witness_snapshot():
    with tempfile.TemporaryDirectory() as session_dir:
        wire_path = _write_wire(session_dir)
        witness_path = Path(session_dir) / "raw-witness-copy.jsonl"
        witness_path.write_text(Path(wire_path).read_text(encoding="utf-8"), encoding="utf-8")
        captured = io.StringIO()
        stderr = io.StringIO()
        payload = {"session_id": "session_abc", "cwd": "/x", "hook_event_name": "SessionEnd"}
        with mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(distill.sys, "stdout", captured), \
             mock.patch.object(distill.sys, "stderr", stderr), \
             mock.patch.object(distill, "_find_session_dir", return_value=session_dir), \
             mock.patch.object(distill, "write_raw_witness", return_value=_witness(str(witness_path))), \
             mock.patch.object(distill, "extract_wire", return_value="x" * 600) as extract_wire, \
             mock.patch.object(distill, "git_remote_url", return_value=""), \
             mock.patch.object(distill, "repo_slug", return_value="repo"), \
             mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)), \
             mock.patch.object(distill, "distill_and_remember", return_value=True), \
             mock.patch.object(distill, "_mark"):
            rc = distill.main()

    assert captured.getvalue() == ""
    assert rc == 0
    extract_wire.assert_called_once_with(str(witness_path))


def test_distill_clamps_with_direct_hook_budget():
    old_clamp = distill.CLAMP
    try:
        with tempfile.TemporaryDirectory() as session_dir:
            wire_path = _write_wire(session_dir)
            captured = io.StringIO()
            stderr = io.StringIO()
            payload = {"session_id": "session_abc", "cwd": "/x", "hook_event_name": "SessionEnd"}
            long_text = "0123456789" * 80
            distill.CLAMP = 120
            with mock.patch.object(distill.sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(distill.sys, "stdout", captured), \
                 mock.patch.object(distill.sys, "stderr", stderr), \
                 mock.patch.object(distill, "_find_session_dir", return_value=session_dir), \
                 mock.patch.object(distill, "write_raw_witness", return_value=_witness(wire_path)), \
                 mock.patch.object(distill, "extract_wire", return_value=long_text), \
                 mock.patch.object(distill, "git_remote_url", return_value=""), \
                 mock.patch.object(distill, "repo_slug", return_value="repo"), \
                 mock.patch.object(distill.boring_config, "classify", return_value=("personal", None)), \
                 mock.patch.object(distill, "distill_and_remember", return_value=True) as remember, \
                 mock.patch.object(distill, "_mark"):
                rc = distill.main()

        assert captured.getvalue() == ""
        assert rc == 0
        remembered_text = remember.call_args.args[0]
        assert len(remembered_text) < len(long_text)
        assert "truncated" in remembered_text
        assert "transcript clamped" in stderr.getvalue()
    finally:
        distill.CLAMP = old_clamp


def test_distill_rejects_invalid_clamp_env_at_boundary():
    old_clamp = distill.CLAMP
    try:
        distill.CLAMP = None
        with mock.patch.dict(os.environ, {"DISTILL_CLAMP": "-1"}, clear=True):
            try:
                distill._clamp_limit()
            except ValueError as e:
                assert "DISTILL_CLAMP must be a non-negative integer" in str(e)
            else:
                raise AssertionError("expected invalid DISTILL_CLAMP to fail")
    finally:
        distill.CLAMP = old_clamp


def test_distill_run_returns_nonzero_on_crash():
    stderr = io.StringIO()
    with mock.patch.object(distill, "main", side_effect=RuntimeError("boom")), \
         mock.patch.object(distill.sys, "stderr", stderr):
        rc = distill.run()
    assert rc == 1
    assert "[omb-distill] crashed: boom" in stderr.getvalue()


def _read_last_event(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


if __name__ == "__main__":
    test_work_dir_key_format()
    test_find_session_dir_uses_index()
    test_extract_session_filters_injection()
    test_recall_skips_short_and_injection()
    test_recall_formats_context()
    test_recall_failed_search_logs_to_stderr()
    test_distill_invalid_stdin_logs_error()
    test_distill_short_transcript_logs_skip_and_marks_done()
    test_distill_remember_failure_returns_nonzero_and_marks_retry()
    test_distill_extracts_from_raw_witness_snapshot()
    test_distill_clamps_with_direct_hook_budget()
    test_distill_rejects_invalid_clamp_env_at_boundary()
    test_distill_run_returns_nonzero_on_crash()
    print("ok - kimi agent adapters")
