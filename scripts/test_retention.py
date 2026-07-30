#!/usr/bin/env python3
"""Guardrail tests for retention.plan() — the data-loss-critical decisions.

retention.py --apply archives/deletes raw session transcripts, so the planning pass must NEVER put an
undistilled (unprocessed) session on the delete path, must honor age thresholds, and must not mutate
the filesystem while planning. These tests run fully offline against tmpdirs (no ~/.claude, no docker).

Owned guardrails (what would break in production if these regress):
  1. An UNPROCESSED session is never hard-deleted — only archived — even under delete_only.
  2. delete_only deletes only PROCESSED sessions.
  3. Sessions younger than the threshold are kept (not touched).
  4. PENDING sessions are skipped entirely.
  5. Ancient-archive deletion only targets PROCESSED archives.
  6. Raw witness snapshots older than policy are deleted; young snapshots are kept.
  7. plan() is pure — it mutates nothing on disk.
"""
import io
import gzip
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import retention  # noqa: E402

DAY = 86400


class RetentionPlanGuardrails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "projects"
        self.mark = root / "mark"
        self.archive = self.mark / "archive"
        self.raw_witness = root / "data" / "raw-witness"
        self.src.mkdir()
        self.mark.mkdir()
        self.archive.mkdir()
        self.raw_witness.mkdir(parents=True)
        # Redirect the module's location helpers at the tmp dirs (call-time lookup → monkeypatch works).
        self._orig = (retention._source_dirs, retention._mark_dir, retention._archive_dir, retention._raw_witness_dir)
        retention._source_dirs = lambda: [self.src]
        retention._mark_dir = lambda: self.mark
        retention._archive_dir = lambda: self.archive
        retention._raw_witness_dir = lambda: self.raw_witness
        self.now = time.time()

    def tearDown(self):
        retention._source_dirs, retention._mark_dir, retention._archive_dir, retention._raw_witness_dir = self._orig
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------
    def _session(self, sid: str, age_days: float) -> Path:
        p = self.src / f"{sid}.jsonl"
        p.write_text("{}\n", encoding="utf-8")
        ts = self.now - age_days * DAY
        os.utime(p, (ts, ts))
        return p

    def _marker(self, sid: str, suffix: str, age_days: float = 0):
        p = self.mark / f"{retention._safe(sid)}{suffix}"
        p.write_text("x", encoding="utf-8")
        ts = self.now - age_days * DAY
        os.utime(p, (ts, ts))
        return p

    def _archived(self, sid: str, age_days: float) -> Path:
        p = self.archive / f"{sid}.jsonl.gz"
        p.write_text("gz", encoding="utf-8")
        ts = self.now - age_days * DAY
        os.utime(p, (ts, ts))
        return p

    def _raw_witness(self, agent: str, name: str, age_days: float, body: str = "raw transcript") -> Path:
        p = self.raw_witness / agent / "20260703" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        ts = self.now - age_days * DAY
        os.utime(p, (ts, ts))
        return p

    def _plan(self, delete_only=False):
        return retention.plan(
            self.now,
            processed_days=30,
            unprocessed_days=90,
            archive_days=180,
            raw_witness_days=90,
            delete_only=delete_only,
        )

    # --- guardrails --------------------------------------------------------
    def test_unprocessed_old_is_archived_never_deleted(self):
        # No marker = unprocessed. Older than the unprocessed threshold.
        self._session("u1", age_days=120)
        p = self._plan(delete_only=False)
        arch = [s.name for s, _, _ in p["to_archive"]]
        self.assertIn("u1.jsonl", arch)
        self.assertEqual(p["to_delete"], [])

    def test_unprocessed_never_deleted_even_under_delete_only(self):
        # THE critical guardrail: delete_only must NOT hard-delete an undistilled session
        # (its raw transcript is the only thing it can ever be distilled from).
        self._session("u1", age_days=120)
        p = self._plan(delete_only=True)
        self.assertEqual(p["to_delete"], [], "unprocessed session must never be on the delete path")
        self.assertIn("u1.jsonl", [s.name for s, _, _ in p["to_archive"]])

    def test_processed_old_archived_or_deleted_by_mode(self):
        self._session("p1", age_days=60)
        self._marker("p1", ".ts")  # processed
        # default: archive
        self.assertIn("p1.jsonl", [s.name for s, _, _ in self._plan(False)["to_archive"]])
        # delete_only: delete
        po = self._plan(True)
        self.assertEqual([s.name for s in po["to_delete"]], ["p1.jsonl"])
        self.assertEqual(po["to_archive"], [])

    def test_young_session_is_kept(self):
        self._session("p1", age_days=5)
        self._marker("p1", ".ts")  # processed, but < 30d
        self._session("u1", age_days=10)  # unprocessed, < 90d
        p = self._plan(delete_only=True)
        self.assertEqual(p["to_archive"], [])
        self.assertEqual(p["to_delete"], [])

    def test_pending_session_is_skipped(self):
        self._session("x1", age_days=365)
        self._marker("x1", ".pending")  # in-flight → never touched
        p = self._plan(delete_only=True)
        self.assertEqual(p["to_archive"], [])
        self.assertEqual(p["to_delete"], [])

    def test_ancient_archive_deletes_only_processed(self):
        self._archived("p1", age_days=200)
        self._marker("p1", ".ts")  # processed → ancient archive may be deleted
        self._archived("u1", age_days=200)  # unprocessed → archive is sole re-distill source, keep
        p = self._plan()
        names = [a.name for a in p["ancient_archives"]]
        self.assertIn("p1.jsonl.gz", names)
        self.assertNotIn("u1.jsonl.gz", names)

    def test_old_raw_witness_is_deleted_but_young_witness_is_kept(self):
        old = self._raw_witness("codex", "old.jsonl", age_days=120, body="old raw")
        young = self._raw_witness("codex", "young.jsonl", age_days=10, body="young raw")

        p = self._plan()

        self.assertIn(old, p["raw_witness_delete"])
        self.assertNotIn(young, p["raw_witness_delete"])
        self.assertEqual(p["raw_witness_count"], 2)
        self.assertEqual(p["raw_witness_total_bytes"], len("old raw") + len("young raw"))
        self.assertEqual(p["raw_witness_bytes"], len("old raw"))

    def test_raw_witness_summary_reports_total_and_eligible_bytes(self):
        self.assertEqual(
            retention._raw_witness_summary(2, 1536, 512),
            "raw witness snapshots: 2 (1.5KB total, 512.0B eligible for deletion)",
        )

    def test_env_days_rejects_invalid_policy_value(self):
        old = os.environ.get("BORING_RETENTION_PROCESSED_DAYS")
        os.environ["BORING_RETENTION_PROCESSED_DAYS"] = "soon"
        try:
            with self.assertRaisesRegex(ValueError, "BORING_RETENTION_PROCESSED_DAYS must be a number"):
                retention._env_days("BORING_RETENTION_PROCESSED_DAYS", 30)
        finally:
            if old is None:
                os.environ.pop("BORING_RETENTION_PROCESSED_DAYS", None)
            else:
                os.environ["BORING_RETENTION_PROCESSED_DAYS"] = old

    def test_env_days_rejects_negative_policy_value(self):
        old = os.environ.get("BORING_RETENTION_RAW_WITNESS_DAYS")
        os.environ["BORING_RETENTION_RAW_WITNESS_DAYS"] = "-1"
        try:
            with self.assertRaisesRegex(ValueError, "BORING_RETENTION_RAW_WITNESS_DAYS must be non-negative"):
                retention._env_days("BORING_RETENTION_RAW_WITNESS_DAYS", 90)
        finally:
            if old is None:
                os.environ.pop("BORING_RETENTION_RAW_WITNESS_DAYS", None)
            else:
                os.environ["BORING_RETENTION_RAW_WITNESS_DAYS"] = old

    def test_plan_does_not_mutate_filesystem(self):
        s = self._session("u1", age_days=120)
        self._session("p1", age_days=60)
        self._marker("p1", ".ts")
        a = self._archived("p2", age_days=200)
        self._marker("p2", ".ts")
        raw = self._raw_witness("kimi", "old.jsonl", age_days=120)
        before = {p for p in self.src.rglob("*")} | {p for p in self.mark.rglob("*")} | {p for p in self.raw_witness.rglob("*")}
        self._plan(delete_only=True)
        after = {p for p in self.src.rglob("*")} | {p for p in self.mark.rglob("*")} | {p for p in self.raw_witness.rglob("*")}
        self.assertEqual(before, after, "plan() must be read-only")
        self.assertTrue(s.exists() and a.exists() and raw.exists())

    def test_apply_exits_nonzero_when_action_fails(self):
        s = self._session("u1", age_days=120)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(sys, "argv", ["retention.py", "--apply", "--yes"]):
            with mock.patch.object(sys, "stdout", stdout):
                with mock.patch.object(sys, "stderr", stderr):
                    with mock.patch.object(retention, "_archive", side_effect=OSError("disk full")):
                        with self.assertRaises(SystemExit) as cm:
                            retention.main()

        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[error] failed to archive", stderr.getvalue())
        self.assertTrue(s.exists(), "failed archive must not remove the source transcript")

    def test_archive_writes_fsynced_gzip_before_source_removal_boundary(self):
        src = self._session("u1", age_days=120)
        src.write_text("raw session\n", encoding="utf-8")
        ts = self.now - 120 * DAY
        os.utime(src, (ts, ts))
        dst = self.archive / "u1.jsonl.gz"
        old_fsync = retention.os.fsync
        calls = []
        try:
            retention.os.fsync = lambda fd: calls.append(fd)

            retention._archive(src, dst)

            self.assertTrue(src.exists(), "_archive must not remove the source transcript itself")
            with gzip.open(dst, "rt", encoding="utf-8") as f:
                self.assertEqual(f.read(), "raw session\n")
            self.assertEqual(len(calls), 1)
            self.assertGreaterEqual(calls[0], 0)
            self.assertAlmostEqual(dst.stat().st_mtime, ts, delta=1)
            self.assertFalse(dst.with_suffix(dst.suffix + ".tmp").exists())
        finally:
            retention.os.fsync = old_fsync

    def test_archive_preserves_source_and_existing_archive_on_publish_failure(self):
        src = self._session("u1", age_days=120)
        src.write_text("new raw\n", encoding="utf-8")
        dst = self.archive / "u1.jsonl.gz"
        dst.write_bytes(b"old archive")
        old_replace = retention.os.replace
        try:
            def boom(tmp, archive):
                raise OSError("denied")

            retention.os.replace = boom
            with self.assertRaisesRegex(OSError, "denied"):
                retention._archive(src, dst)

            self.assertEqual(src.read_text(encoding="utf-8"), "new raw\n")
            self.assertEqual(dst.read_bytes(), b"old archive")
            self.assertFalse(dst.with_suffix(dst.suffix + ".tmp").exists())
        finally:
            retention.os.replace = old_replace


if __name__ == "__main__":
    unittest.main(verbosity=2)
