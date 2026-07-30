#!/usr/bin/env python3
"""Network-free regression tests for session collector status semantics."""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SHARED_DIR = HERE.parent / "shared"
sys.path.insert(0, str(SHARED_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claude_collect = _load("claude_collect_sessions", "collect-sessions.py")
kimi_collect = _load("kimi_collect_sessions", "collect-kimi-sessions.py")


def _last_event(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def test_claude_collector_fails_when_sync_fails():
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "claude" / "project"
            source.mkdir(parents=True)
            session = source / "s1.jsonl"
            session.write_text(json.dumps({"cwd": "/work/repo"}) + "\nbody\n", encoding="utf-8")
            event_path = root / "events.ndjson"

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 1

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py"]),
                mock.patch.object(claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(claude_collect.subprocess, "run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(claude_collect, "DrudgeClient") as client,
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
            ):
                client.return_value.sync.side_effect = OSError("sync down")
                rc = claude_collect.main()

            assert rc == 1
            event = _last_event(event_path)
            assert event["component"] == "claude-collector"
            assert event["status"] == "failed"
            assert event["sync_status"] == "failed"
            assert event["workflow"] == "memory_ingest"
            assert event["workflow_node"] == "retry_marked"
            assert event["workflow_outcome"] == "fail"
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)
        claude_collect.MIN_KB = old_min_kb
        claude_collect.LIMIT = old_limit


def test_claude_collector_skips_unstatable_session_candidate():
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "claude" / "project"
            source.mkdir(parents=True)
            missing = source / "missing.jsonl"
            session = source / "s1.jsonl"
            session.write_text(json.dumps({"cwd": "/work/repo"}) + "\nbody\n", encoding="utf-8")
            event_path = root / "events.ndjson"

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 1

            real_glob = claude_collect.glob.glob
            real_getmtime = claude_collect.os.path.getmtime

            def fake_glob(pattern):
                if pattern.endswith("*.jsonl"):
                    return [str(missing), str(session)]
                return real_glob(pattern)

            def fake_getmtime(path):
                if path == str(missing):
                    raise OSError("gone")
                return real_getmtime(path)

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py"]),
                mock.patch.object(claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]),
                mock.patch.object(claude_collect.glob, "glob", side_effect=fake_glob),
                mock.patch.object(claude_collect.os.path, "getmtime", side_effect=fake_getmtime),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(claude_collect.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                mock.patch.object(claude_collect, "DrudgeClient"),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
            ):
                rc = claude_collect.main()

            assert rc == 0
            assert run.call_count == 1
            event = _last_event(event_path)
            assert event["status"] == "ok"
            assert event["pending"] == 1
            assert event["processed"] == 1
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)
        claude_collect.MIN_KB = old_min_kb
        claude_collect.LIMIT = old_limit


def test_claude_collector_skips_done_markers_and_batches_newest_first():
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "claude" / "project"
            source.mkdir(parents=True)
            sessions = [
                ("s-old", "/work/old", 1_000_000),
                ("s-middle", "/work/middle", 1_000_100),
                ("s-newest", "/work/newest", 1_000_200),
                ("s-done", "/work/done", 1_000_300),
            ]
            for sid, cwd, stamp in sessions:
                path = source / f"{sid}.jsonl"
                path.write_text(json.dumps({"cwd": cwd}) + "\nbody\n", encoding="utf-8")
                os.utime(path, (stamp, stamp))
            event_path = root / "events.ndjson"

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.markers.mark_done("s-done")
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 2

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py"]),
                mock.patch.object(claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(claude_collect.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                mock.patch.object(claude_collect, "DrudgeClient"),
                mock.patch.object(claude_collect.time, "time", return_value=1_000_400),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
            ):
                rc = claude_collect.main()

            assert rc == 0
            payloads = [json.loads(call.kwargs["input"]) for call in run.call_args_list]
            assert [payload["session_id"] for payload in payloads] == ["s-newest", "s-middle"]
            assert [payload["cwd"] for payload in payloads] == ["/work/newest", "/work/middle"]
            for call in run.call_args_list:
                assert "BORING_DISTILL_NO_MARK" not in call.kwargs["env"]
            event = _last_event(event_path)
            assert event["mode"] == "collect"
            assert event["pending"] == 3
            assert event["batch"] == 2
            assert event["processed"] == 2
            assert event["remaining"] == 1
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)
        claude_collect.MIN_KB = old_min_kb
        claude_collect.LIMIT = old_limit


def test_claude_distill_now_ignores_done_marker_and_leaves_no_mark_env():
    old_mark_dir = claude_collect.markers.MARK_DIR
    old_min_kb = claude_collect.MIN_KB
    old_limit = claude_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "claude" / "project"
            source.mkdir(parents=True)
            older = source / "s-old.jsonl"
            newer = source / "s-new.jsonl"
            older.write_text(json.dumps({"cwd": "/work/old"}) + "\nbody\n", encoding="utf-8")
            newer.write_text(json.dumps({"cwd": "/work/new"}) + "\nbody\n", encoding="utf-8")
            os.utime(older, (1_000_000, 1_000_000))
            os.utime(newer, (1_000_100, 1_000_100))
            event_path = root / "events.ndjson"

            claude_collect.markers.set_mark_dir(str(root / "markers"))
            claude_collect.markers.mark_done("s-new")
            claude_collect.MIN_KB = 0
            claude_collect.LIMIT = 9

            with (
                mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py", "--now"]),
                mock.patch.object(claude_collect.boring_config, "source_dirs", return_value=[str(root / "claude")]),
                mock.patch.object(claude_collect, "_warm_llm"),
                mock.patch.object(claude_collect.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
                mock.patch.object(claude_collect, "DrudgeClient"),
                mock.patch.object(claude_collect.time, "time", return_value=1_000_200),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
            ):
                rc = claude_collect.main()

            assert rc == 0
            assert run.call_count == 1
            payload = json.loads(run.call_args.kwargs["input"])
            assert payload["session_id"] == "s-new"
            assert payload["cwd"] == "/work/new"
            assert run.call_args.kwargs["env"]["BORING_DISTILL_NO_MARK"] == "1"
            assert claude_collect.markers.done_time("s-new") is not None
            event = _last_event(event_path)
            assert event["mode"] == "distill-now"
            assert event["pending"] == 2
            assert event["batch"] == 1
            assert event["processed"] == 1
    finally:
        claude_collect.markers.set_mark_dir(old_mark_dir)
        claude_collect.MIN_KB = old_min_kb
        claude_collect.LIMIT = old_limit


def test_kimi_collector_fails_when_distill_fails():
    old_home = kimi_collect.KIMI_HOME
    old_hook = kimi_collect.HOOK
    old_limit = kimi_collect.LIMIT
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_dir = root / "session"
            session_dir.mkdir()
            second_session_dir = root / "session-2"
            second_session_dir.mkdir()
            index = root / "session_index.jsonl"
            index.write_text(
                json.dumps({"sessionId": "k1", "sessionDir": str(session_dir), "workDir": "/work/repo"})
                + "\n"
                + json.dumps({"sessionId": "k2", "sessionDir": str(second_session_dir), "workDir": "/work/repo"})
                + "\n",
                encoding="utf-8",
            )
            hook = root / "distill-session.py"
            hook.write_text("# stub\n", encoding="utf-8")
            event_path = root / "events.ndjson"

            kimi_collect.KIMI_HOME = str(root)
            kimi_collect.HOOK = str(hook)
            kimi_collect.LIMIT = 1

            with (
                mock.patch.object(kimi_collect, "_distill", return_value=False) as distill,
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
            ):
                rc = kimi_collect.main()

            assert rc == 1
            assert distill.call_count == 1
            event = _last_event(event_path)
            assert event["component"] == "kimi-collector"
            assert event["status"] == "failed"
            assert event["attempted"] == 1
            assert event["failed"] == 1
            assert event["workflow"] == "memory_ingest"
            assert event["workflow_node"] == "retry_marked"
            assert event["workflow_outcome"] == "fail"
    finally:
        kimi_collect.KIMI_HOME = old_home
        kimi_collect.HOOK = old_hook
        kimi_collect.LIMIT = old_limit


def test_kimi_collector_skips_unstatable_session_candidate():
    old_home = kimi_collect.KIMI_HOME
    old_hook = kimi_collect.HOOK
    old_limit = kimi_collect.LIMIT
    old_mark_dir = kimi_collect.markers.MARK_DIR
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bad_session_dir = root / "session-gone"
            good_session_dir = root / "session-ok"
            bad_session_dir.mkdir()
            good_session_dir.mkdir()
            index = root / "session_index.jsonl"
            index.write_text(
                json.dumps({"sessionId": "k-gone", "sessionDir": str(bad_session_dir), "workDir": "/work/gone"})
                + "\n"
                + json.dumps({"sessionId": "k-ok", "sessionDir": str(good_session_dir), "workDir": "/work/ok"})
                + "\n",
                encoding="utf-8",
            )
            hook = root / "distill-session.py"
            hook.write_text("# stub\n", encoding="utf-8")
            event_path = root / "events.ndjson"

            kimi_collect.KIMI_HOME = str(root)
            kimi_collect.HOOK = str(hook)
            kimi_collect.LIMIT = 1
            kimi_collect.markers.set_mark_dir(str(root / "markers"))
            real_getmtime = kimi_collect.os.path.getmtime

            def fake_getmtime(path):
                if path == str(bad_session_dir):
                    raise OSError("gone")
                return real_getmtime(path)

            with (
                mock.patch.object(kimi_collect.os.path, "getmtime", side_effect=fake_getmtime),
                mock.patch.object(kimi_collect, "_distill", return_value=True) as distill,
                mock.patch.object(kimi_collect, "_sync", return_value=True),
                mock.patch.dict(os.environ, {"BORING_EVENT_LOG": str(event_path), "BORING_EVENT_SINK": "spool"}),
            ):
                rc = kimi_collect.main()

            assert rc == 0
            distill.assert_called_once_with("k-ok", "/work/ok")
            event = _last_event(event_path)
            assert event["status"] == "ok"
            assert event["eligible"] == 1
            assert event["attempted"] == 1
            assert event["processed"] == 1
    finally:
        kimi_collect.KIMI_HOME = old_home
        kimi_collect.HOOK = old_hook
        kimi_collect.LIMIT = old_limit
        kimi_collect.markers.set_mark_dir(old_mark_dir)


if __name__ == "__main__":
    test_claude_collector_fails_when_sync_fails()
    test_claude_collector_skips_unstatable_session_candidate()
    test_claude_collector_skips_done_markers_and_batches_newest_first()
    test_claude_distill_now_ignores_done_marker_and_leaves_no_mark_env()
    test_kimi_collector_fails_when_distill_fails()
    test_kimi_collector_skips_unstatable_session_candidate()
    print("ok - scheduler collectors")
