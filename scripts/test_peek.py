#!/usr/bin/env python3
"""Tests for peek.py — the invariants that keep a local view from becoming a leak.

Run: python3 scripts/test_peek.py   (no pytest dependency)

This page renders note prose. 1154 of 1541 documents in this corpus are company-origin, so the
question is not "did we remember to redact" but "can prose reach the response at all". These tests
pin the structural answer: a note's own `origin:` decides, unknown counts as company, and the
absence of a phrase window is reported as withheld rather than as nothing.
"""
import importlib.util
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("peek", HERE / "peek.py")
peek = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(peek)


def _note(dirpath, name, origin):
    (dirpath / name).write_text(
        f'---\ntitle: "t"\norigin: {origin}\nkind: note\n---\n\n## 배경\n본문\n',
        encoding="utf-8",
    )


class PayloadShape(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wiki = Path(self._tmp.name)
        self._saved = peek._VAULT_WIKI
        peek._VAULT_WIKI = self.wiki
        peek._ORIGIN_CACHE.clear()
        _note(self.wiki, "wiki-personal.md", "personal")
        _note(self.wiki, "wiki-company.md", "company")

    def tearDown(self):
        peek._VAULT_WIKI = self._saved
        peek._ORIGIN_CACHE.clear()
        self._tmp.cleanup()

    def _rows(self):
        return [
            {
                "session_id": "abcdefgh-1234-5678-9abc-def012345678",
                "ts": 1_788_000_000.0,
                "prompt_words": ["회사", "내부", "얘기", "절대", "나가면", "안", "됨"],
                "hits": [
                    {"src": "/vault/wiki/wiki-personal.md", "phrases": ["개인 노트 구문창 하나"]},
                    {"src": "/vault/wiki/wiki-company.md", "phrases": ["회사 노트 산문이다"]},
                ],
                "controls": [{"src": "wiki-x.md", "phrases": []}],
            }
        ]

    def test_a_note_we_cannot_read_counts_as_company(self):
        """The failure direction has to be silence — the other one cannot be taken back."""
        self.assertFalse(peek._note_is_personal("wiki-does-not-exist.md"))

    def test_the_raw_prompt_never_reaches_the_payload(self):
        """`prompt_words` is the user's prompt, unredacted, stored beside every injection."""
        blob = json.dumps(peek.prompt_rows(self._rows(), {}, []), ensure_ascii=False)
        self.assertNotIn("prompt_words", blob)
        self.assertNotIn("내부", blob, "prompt token leaked into the payload")
        rows = peek.prompt_rows(self._rows(), {}, [])
        self.assertEqual(len(rows[0]["session"]), 8)
        self.assertNotIn("def012345678", blob)

    def test_company_prose_is_withheld_and_says_so(self):
        rows = peek.prompt_rows(self._rows(), {}, [])
        by_src = {h["src"]: h for h in rows[0]["hits"]}

        personal = by_src["wiki-personal.md"]
        self.assertFalse(personal["origin_withheld"])
        self.assertTrue(personal["phrases"], "a personal note keeps its phrase windows")

        company = by_src["wiki-company.md"]
        self.assertTrue(company["origin_withheld"], "a company note must be flagged, not silent")
        self.assertEqual(company["phrases"], [])
        # Withheld and empty are different claims; the flag is what lets the page say which.
        self.assertNotIn("회사 노트 산문", json.dumps(rows, ensure_ascii=False))


class WhyBlock(unittest.TestCase):
    def test_query_log_text_never_reaches_the_payload(self):
        """`why` reads `query_log`, whose rows carry the raw query AND the raw answer snippet.

        Only distances and `dist_kind` may cross — the same rule as the prompt rows, on a second
        source that happens to hold the same secrets.
        """
        rows = [
            {
                "id": 7,
                "endpoint": "search",
                "created_at": "2026-09-01T00:00:00Z",
                "query": "회사 내부 배포 절차 알려줘",
                "answer_snippet": "사내 파이프라인은 …",
                "hit_paths": ["/vault/wiki/wiki-0001.md"],
                "hit_dists": [0.42],
                "hit_dist_kinds": ["vector_cosine"],
            }
        ]
        blob = json.dumps(peek.why_block(rows), ensure_ascii=False)
        self.assertNotIn("내부 배포", blob, "the raw query leaked into the why block")
        self.assertNotIn("사내 파이프라인", blob, "the raw answer snippet leaked into the why block")
        self.assertIn("vector_cosine", blob, "the band summary must still carry dist_kind")

    def test_a_non_search_endpoint_is_not_counted(self):
        """`/ask` and `/brief` log their own retrievals; pooling them would mix populations."""
        rows = [{"endpoint": "ask", "hit_dists": [0.1], "hit_dist_kinds": ["vector_cosine"]}]
        self.assertIsNone(peek.why_block(rows))


class BindAddress(unittest.TestCase):
    def test_the_bind_address_is_loopback_and_there_is_no_flag_to_change_it(self):
        """The loopback bind is the whole access control; nothing may hand it away.

        Asserted by behaviour, not by grepping the source — the source discusses `--host` and
        `0.0.0.0` in the very comments explaining why neither exists, so a text search reports a
        violation that is actually the guard.
        """
        self.assertEqual(peek.BIND_HOST, "127.0.0.1")
        proc = subprocess.run(
            [sys.executable, str(HERE / "peek.py"), "--host", "0.0.0.0", "--port", "0"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0, "a --host flag must not exist")
        self.assertIn("unrecognized arguments", (proc.stderr or "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
