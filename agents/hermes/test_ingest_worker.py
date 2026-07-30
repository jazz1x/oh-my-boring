import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Load the module under test (ingest-worker.py) under a Python-valid name.
_ingest_worker_path = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "ingest-worker.py"
)
spec = importlib.util.spec_from_file_location("ingest_worker", _ingest_worker_path)
ingest_worker = importlib.util.module_from_spec(spec)
sys.modules["ingest_worker"] = ingest_worker
spec.loader.exec_module(ingest_worker)


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ingest_worker.MARK_DIR = self.tmp.name
        ingest_worker.markers.set_mark_dir(self.tmp.name)

        # BORING_VAULT_DIR is the vault root; notes live under vault/wiki.
        self.vault_root = Path(self.tmp.name) / "vault"
        self.wiki_dir = self.vault_root / "wiki"
        self.wiki_dir.mkdir(parents=True)
        self._orig_vault_dir = os.environ.get("BORING_VAULT_DIR")
        self._orig_event_log = os.environ.get("BORING_EVENT_LOG")
        self._orig_event_sink = os.environ.get("BORING_EVENT_SINK")
        os.environ["BORING_VAULT_DIR"] = str(self.vault_root)
        os.environ["BORING_EVENT_LOG"] = str(Path(self.tmp.name) / "events.ndjson")
        os.environ["BORING_EVENT_SINK"] = "spool"

        self.addCleanup(self._restore_vault_dir)

        self.vector = False
        self.total_chunks = 0
        self.engine_down = False
        self._orig_is_vector_mode = ingest_worker._is_vector_mode
        self._orig_chunk_count = ingest_worker._chunk_count
        ingest_worker._is_vector_mode = self._is_vector_mode
        ingest_worker._chunk_count = self._chunk_count
        self.addCleanup(self._restore_engine_probe)

    def _restore_vault_dir(self):
        if self._orig_vault_dir is None:
            os.environ.pop("BORING_VAULT_DIR", None)
        else:
            os.environ["BORING_VAULT_DIR"] = self._orig_vault_dir
        if self._orig_event_log is None:
            os.environ.pop("BORING_EVENT_LOG", None)
        else:
            os.environ["BORING_EVENT_LOG"] = self._orig_event_log
        if self._orig_event_sink is None:
            os.environ.pop("BORING_EVENT_SINK", None)
        else:
            os.environ["BORING_EVENT_SINK"] = self._orig_event_sink

    def _restore_engine_probe(self):
        ingest_worker._is_vector_mode = self._orig_is_vector_mode
        ingest_worker._chunk_count = self._orig_chunk_count

    def _is_vector_mode(self):
        if self.engine_down:
            return False
        return self.vector

    def _chunk_count(self):
        if self.engine_down:
            return None
        return self.total_chunks

    def _pending(self, sid, before, attempts=0):
        path = Path(self.tmp.name) / f"{sid}.pending"
        before_text = "" if before is None else str(before)
        path.write_text(f"{sid}\n{before_text}\n{attempts}\n")

    def _read_attempts(self, sid):
        path = Path(self.tmp.name) / f"{sid}.pending"
        if not path.exists():
            return None
        parts = path.read_text().strip().split("\n")
        return int(parts[2]) if len(parts) > 2 else 0

    def _last_event(self):
        event_path = Path(os.environ["BORING_EVENT_LOG"])
        return json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])

    def _done_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.ts").exists()

    def _retry_exists(self, sid):
        return (Path(self.tmp.name) / f"{sid}.retry").exists()

    def _write_note(self, sid, wiki_id="wiki-9999"):
        note = self.wiki_dir / f"{wiki_id}.md"
        note.write_text(
            f"---\ntitle: test\nomb_session_id: {sid}\n---\nbody\n"
        )

    def test_frontmatter_session_id_parsing(self):
        self._write_note("s-parse", "wiki-0001")
        self.assertEqual(
            ingest_worker._frontmatter_session_id(self.wiki_dir / "wiki-0001.md"),
            "s-parse",
        )

    def test_frontmatter_session_id_parsing_uses_yaml_not_line_regex(self):
        note = self.wiki_dir / "wiki-0001.md"
        note.write_text(
            "---\ntitle: test\nomb_session_id: 's-quoted' # queue marker\n---\nbody\n"
        )

        self.assertEqual(ingest_worker._frontmatter_session_id(note), "s-quoted")

    def test_frontmatter_session_id_rejects_non_string_yaml_value(self):
        note = self.wiki_dir / "wiki-0001.md"
        note.write_text("---\ntitle: test\nomb_session_id: [bad]\n---\nbody\n")

        self.assertIsNone(ingest_worker._frontmatter_session_id(note))

    def test_find_session_note_finds_marker(self):
        self._write_note("s-marker")
        found = ingest_worker._find_session_note("s-marker")
        self.assertEqual(Path(found), self.wiki_dir / "wiki-9999.md")

    def test_find_session_note_ignores_generated_brief_marker(self):
        note = self.wiki_dir / "wiki-0001.md"
        note.write_text(
            "---\ntitle: generated\ntags: [daily-brief]\nomb_session_id: s-generated\n---\nsummary\n"
        )

        self.assertIsNone(ingest_worker._find_session_note("s-generated"))

    def test_find_session_note_uses_vault_wiki_not_vault_root(self):
        root_note = self.vault_root / "wiki-0001.md"
        root_note.write_text(
            "---\ntitle: wrong\nomb_session_id: s-root\n---\nbody\n"
        )
        self._write_note("s-root", "wiki-0002")

        found = ingest_worker._find_session_note("s-root")

        self.assertEqual(Path(found), self.wiki_dir / "wiki-0002.md")

    def test_find_session_note_none_without_marker(self):
        note = self.wiki_dir / "wiki-0001.md"
        note.write_text("---\ntitle: other\n---\nbody\n")
        self.assertIsNone(ingest_worker._find_session_note("s-other"))

    def test_vector_mode_prefers_session_marker_over_chunk_count(self):
        self.vector = True
        self.total_chunks = 0
        self._pending("s1", 5)
        self._write_note("s1")
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s1"))
        event = self._last_event()
        self.assertEqual(event["event"], "ingest_reconcile")
        self.assertEqual(event["workflow_node"], "done_marked")
        self.assertEqual(event["workflow_outcome"], "continue")

    def test_vector_mode_falls_back_to_chunk_count(self):
        self.vector = True
        self.total_chunks = 10
        self._pending("s2", 5)
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s2"))

    def test_vector_mode_does_not_complete_from_missing_chunk_baseline(self):
        self.vector = True
        self.total_chunks = 10
        self._pending("s2", None)
        ingest_worker._reconcile()
        self.assertFalse(self._done_exists("s2"))
        self.assertEqual(self._read_attempts("s2"), 0)

    def test_vector_mode_rejects_negative_chunk_baseline(self):
        self.vector = True
        self.total_chunks = 0
        self._pending("s2", -1)
        ingest_worker._reconcile()
        self.assertFalse(self._done_exists("s2"))
        self.assertFalse((Path(self.tmp.name) / "s2.pending").exists())
        event = self._last_event()
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["reason"], "pending_marker_unreadable")

    def test_chunk_count_missing_total_chunks_is_absent_witness(self):
        ingest_worker._chunk_count = self._orig_chunk_count
        client = mock.Mock()
        client.audit.return_value = {}

        try:
            with mock.patch.object(ingest_worker, "DrudgeClient", return_value=client):
                self.assertIsNone(ingest_worker._chunk_count())
        finally:
            ingest_worker._chunk_count = self._chunk_count

    def test_wiki_mode_uses_session_marker(self):
        self.vector = False
        self.total_chunks = 0
        self._pending("s3", 0)
        self._write_note("s3")
        ingest_worker._reconcile()
        self.assertTrue(self._done_exists("s3"))

    def test_mismatched_pending_marker_identity_is_corrupt_not_done(self):
        self.vector = False
        self.total_chunks = 0
        (Path(self.tmp.name) / "s1.pending").write_text("s2\n0\n0\n")
        self._write_note("s2")

        ingest_worker._reconcile()

        self.assertFalse(self._done_exists("s1"))
        self.assertFalse(self._done_exists("s2"))
        self.assertFalse((Path(self.tmp.name) / "s1.pending").exists())
        event = self._last_event()
        self.assertEqual(event["event"], "ingest_reconcile")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["session_id"], "s1")
        self.assertEqual(event["reason"], "pending_marker_unreadable")

    def test_corrupt_pending_marker_remove_failure_is_visible(self):
        self.vector = False
        self.total_chunks = 0
        (Path(self.tmp.name) / "s1.pending").write_text("s2\n0\n0\n")

        with mock.patch.object(ingest_worker.markers, "remove_pending", side_effect=OSError("remove failed")):
            with self.assertRaisesRegex(OSError, "remove failed"):
                ingest_worker._reconcile()

        self.assertFalse(self._done_exists("s1"))

    def test_extra_field_pending_marker_is_corrupt_not_done(self):
        self.vector = False
        self.total_chunks = 0
        (Path(self.tmp.name) / "s1.pending").write_text("s1\n0\n0\nextra")
        self._write_note("s1")

        ingest_worker._reconcile()

        self.assertFalse(self._done_exists("s1"))
        self.assertFalse((Path(self.tmp.name) / "s1.pending").exists())
        event = self._last_event()
        self.assertEqual(event["event"], "ingest_reconcile")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["session_id"], "s1")
        self.assertEqual(event["reason"], "pending_marker_unreadable")

    def test_wiki_mode_increments_attempts_without_marker(self):
        self.vector = False
        self.total_chunks = 0
        self._pending("s4", 0, attempts=0)
        ingest_worker._reconcile()
        self.assertFalse(self._done_exists("s4"))
        self.assertEqual(self._read_attempts("s4"), 1)

    def test_wiki_mode_moves_pending_to_retry_after_max_attempts(self):
        self.vector = False
        self.total_chunks = 0
        self._pending("s5", 0, attempts=ingest_worker.MAX_WIKI_ATTEMPTS)
        ingest_worker._reconcile()
        self.assertFalse(self._done_exists("s5"))
        self.assertTrue(self._retry_exists("s5"))
        self.assertIsNone(self._read_attempts("s5"))
        event = self._last_event()
        self.assertEqual(event["event"], "ingest_reconcile")
        self.assertEqual(event["workflow_node"], "retry_marked")
        self.assertEqual(event["workflow_outcome"], "fail")

    def test_unknown_worker_projection_raises(self):
        with self.assertRaises(ValueError):
            ingest_worker._log_worker_event("unknown", "ok")

    def test_fresh_retry_marker_is_not_reoffered(self):
        retry = Path(self.tmp.name) / "s-retry.retry"
        retry.write_text("0")
        session = Path(self.tmp.name) / "s-retry.jsonl"
        session.write_text("{}\n")

        self.assertFalse(ingest_worker._eligible(str(session)))

    def test_stale_retry_marker_is_reoffered(self):
        retry = Path(self.tmp.name) / "s-retry.retry"
        retry.write_text("0")
        stale = time.time() - ingest_worker.RETRY_TTL - 1
        os.utime(retry, (stale, stale))
        session = Path(self.tmp.name) / "s-retry.jsonl"
        session.write_text("{}\n")

        self.assertTrue(ingest_worker._eligible(str(session)))

    def test_health_failure_treated_as_wiki_and_retries(self):
        self.engine_down = True
        self._pending("s6", 0)
        ingest_worker._reconcile()
        # Unreachable engine falls back to wiki-first → attempts incremented, not done yet.
        self.assertFalse(self._done_exists("s6"))
        self.assertEqual(self._read_attempts("s6"), 1)

    def test_ingest_clamp_rejects_invalid_env_value(self):
        old_clamp = ingest_worker.CLAMP
        try:
            ingest_worker.CLAMP = None
            with mock.patch.dict(os.environ, {"INGEST_CLAMP": "-1"}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    ingest_worker._clamp_limit()
        finally:
            ingest_worker.CLAMP = old_clamp

        self.assertIn("INGEST_CLAMP must be a non-negative integer", str(ctx.exception))

    def test_ingest_clamp_accepts_zero_override(self):
        old_clamp = ingest_worker.CLAMP
        try:
            ingest_worker.CLAMP = 0
            self.assertEqual(ingest_worker._clamp_limit(), 0)
        finally:
            ingest_worker.CLAMP = old_clamp


if __name__ == "__main__":
    unittest.main()
