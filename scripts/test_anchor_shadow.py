#!/usr/bin/env python3
"""Tests for anchor-shadow.py — the three ways this number comes out wrong.

Run: python3 scripts/test_anchor_shadow.py
"""
import importlib.util
import json
import os
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["BORING_EVENT_SINK"] = "spool"
_spec = importlib.util.spec_from_file_location("anchor_shadow", HERE / "anchor-shadow.py")
shadow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shadow)


class WhatCounts(unittest.TestCase):
    def test_docs_are_not_code(self):
        """`LOG.md` and `PRD.md` are the most re-edited files in this corpus by a wide margin.

        They are ledgers being appended to, not problems being re-solved — counting them would
        inflate the revisit rate with the one kind of file the north star is not about.
        """
        self.assertTrue(shadow.CODE.search("a/b/nodes.py"))
        self.assertTrue(shadow.CODE.search("Main.kt"))
        self.assertFalse(shadow.CODE.search("docs/LOG.md"))
        self.assertFalse(shadow.CODE.search("PRD.md"))

    def test_a_file_edited_twice_in_one_session_is_not_a_revisit(self):
        """A revisit needs an EARLIER session. Repeats inside one session are the same problem
        still being worked, and counting them turns ordinary iteration into evidence."""
        with self._tmp() as root:
            self._session(root, "s1", ["/r/a.py", "/r/a.py", "/r/a.py"])
            out = self._run(root)
            self.assertEqual(out["revisits"], 0, out)

            self._session(root, "s2", ["/r/a.py"])
            out = self._run(root)
            self.assertEqual(out["revisits"], 1, out)

    def _run(self, root):
        paths = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
        seen, total, revisits = {}, 0, 0
        current, touched = None, set()
        for project, target, session in shadow.edits(paths):
            if current != session:
                for k in touched:
                    seen.setdefault(k, set()).add(current)
                current, touched = session, set()
            total += 1
            key = (project, target)
            touched.add(key)
            if seen.get(key):
                revisits += 1
        return {"total": total, "revisits": revisits}

    def _tmp(self):
        import tempfile

        class Ctx:
            def __enter__(s):
                s.d = tempfile.TemporaryDirectory()
                return Path(s.d.name)

            def __exit__(s, *a):
                s.d.cleanup()

        return Ctx()

    def _session(self, root, name, files):
        import time as _t

        p = root / "proj" / f"{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": f}}
                    ]
                }
            }
            for f in files
        ]
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        os.utime(p, (_t.time(), _t.time()))


class VaultSearch(unittest.TestCase):
    def test_the_vault_search_ignores_gitignore(self):
        """`.gitignore` carries `vault/wiki/*`, and ripgrep honours it when walking a directory.

        Without `--no-ignore` this returns 0 for every file and the coverage reads as a clean 0% —
        the exact reading that was produced and nearly believed on 2026-09-02. Asserted by
        behaviour against the real vault rather than by grepping the source, because the source
        also contains the word in the comment explaining why it is there.
        """
        import subprocess

        sample = next(shadow.VAULT.glob("*.md"), None)
        self.assertIsNotNone(sample, "vault is empty; this test needs the real corpus")
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(sample)], cwd=str(HERE.parent)
        )
        self.assertEqual(ignored.returncode, 0, "vault is no longer gitignored — trap is gone")

        # A note does not contain its own filename, so probe with a string the corpus is known to
        # carry: `markers.py` appears in wiki-0994's body and in its claims frontmatter.
        counts = shadow.vault_mentions(["markers.py", "this-string-is-in-no-note-xyzzy"])
        self.assertGreaterEqual(counts["markers.py"], 1, "the vault is being skipped again")
        self.assertEqual(counts["this-string-is-in-no-note-xyzzy"], 0, "absence must read as 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
