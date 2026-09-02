#!/usr/bin/env python3
"""Tests for uptake-verdict.py's machine-readable mode.

Run: python3 scripts/test_uptake_verdict.py

`--json` exists so every consumer reads the verdict's own numbers instead of recomputing them.
That only holds if the channel stays clean and the three outcomes stay distinguishable.
"""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
os.environ["BORING_EVENT_SINK"] = "spool"
_spec = importlib.util.spec_from_file_location("uptake_verdict", HERE / "uptake-verdict.py")
uv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uv)


def _event(session, used, total, when="2026-09-03T00:00:00+00:00"):
    return {
        "event": "injection_uptake",
        "observed_at": when,
        "session_id": session,
        "attributes": {
            "agent": "claude-code",
            "used_prompts": used,
            "total_prompts": total,
            "used_control_prompts": 0,
            "used_hits": 0,
            "total_hits": total,
            "used_controls": 0,
            "total_controls": total,
        },
    }


class JsonMode(unittest.TestCase):
    def _run(self, rows, argv=("--json",)):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(uv, "fetch_events", return_value=rows):
            with redirect_stdout(out), redirect_stderr(err):
                code = uv.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_stdout_carries_only_the_payload(self):
        """Advisory prose on stdout is how a machine-readable mode stops being one.

        The unreported-sessions line prints on every run that has aged-out sessions, and it went
        to stdout before this — so the first `--json` consumer would have parsed a Korean sentence.
        """
        rows = [
            _event("a", 0, 10),
            {"event": "injection_unreported", "observed_at": "2026-09-03T00:00:00+00:00",
             "attributes": {"aged_sessions": 2, "aged_rows": 9}},
        ]
        code, out, err = self._run(rows)
        self.assertEqual(code, 0)
        payload = json.loads(out)  # must parse with nothing stripped
        self.assertEqual(payload["unreported"], {"sessions": 2, "prompts": 9})
        self.assertIn("측정 안 된 주입", err, "the human line must still be printed, on stderr")

    def test_an_unreachable_engine_is_not_an_empty_one(self):
        """Exit 2 and `reachable: false`, never a zero count.

        A caller that cannot tell these apart reads absence as a measurement — the failure this
        repo has now found in itself three times in one day.
        """
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(uv, "fetch_events", return_value=None):
            with redirect_stdout(out), redirect_stderr(err):
                code = uv.main(["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue()), {"reachable": False})

    def test_the_floors_come_from_the_contract_not_from_here(self):
        """The sample floors are pre-registered in verdict_core; this script may not restate them."""
        code, out, _ = self._run([_event("a", 0, 10)])
        payload = json.loads(out)
        self.assertEqual(payload["floors"]["sessions"], uv.verdict_core.MIN_SESSIONS)
        self.assertEqual(payload["floors"]["prompts"], uv.verdict_core.MIN_INJECTED_PROMPTS)

    def test_pre_and_post_repair_stay_separate(self):
        """§8 D4 chose (A): the verdict reads only post-repair rows, and the rest is reported."""
        rows = [
            _event("old", 5, 50, when="2026-09-01T00:00:00+00:00"),
            _event("new", 0, 10, when="2026-09-03T00:00:00+00:00"),
        ]
        _, out, _ = self._run(rows)
        payload = json.loads(out)
        self.assertEqual(payload["pre_repair"]["claude-code"]["sessions"], 1)
        self.assertEqual(payload["post_repair"]["claude-code"]["sessions"], 1)
        self.assertEqual(payload["post_repair"]["claude-code"]["used_prompts"], 0)


class Midpoint(unittest.TestCase):
    """PRD §2's one-time progress gate. Four different absences must not all read as a shortfall."""

    def _gate(self, per_agent, today, skipped=0):
        err = io.StringIO()
        with redirect_stderr(err):
            code = uv._midpoint(per_agent, skipped, today=today)
        return code, err.getvalue()

    def test_it_is_silent_outside_its_own_window(self):
        """Before the midpoint it is not due; after the close it is spent.

        A gate that keeps firing past the window makes a red doctor the background colour, and a
        signal nobody reads is worse than no signal — the extension changes no threshold, so there
        is nothing for it to gate.
        """
        thin = {"claude-code": {"sessions": 1}}
        self.assertEqual(self._gate(thin, "2026-09-02")[0], uv.MIDPOINT_NOT_DUE)
        self.assertEqual(self._gate(thin, "2026-09-20")[0], uv.MIDPOINT_NOT_DUE)
        self.assertEqual(self._gate(thin, "2026-09-08")[0], uv.MIDPOINT_SHORT)

    def test_the_floor_is_per_adapter(self):
        """Summing adapters would clear a gate neither of them clears.

        Eight Claude Code sessions plus three from Kimi is eleven, and the floor is ten — but the
        adapters run different products (§3 M8), which is why MIN_SESSIONS is per-agent too.
        """
        code, out = self._gate(
            {"claude-code": {"sessions": 8}, "kimi": {"sessions": 3}}, "2026-09-08"
        )
        self.assertEqual(code, uv.MIDPOINT_SHORT, out)
        self.assertIn("claude-code 8", out)
        self.assertIn("kimi 3", out)

    def test_nothing_to_read_is_not_a_shortfall(self):
        """No events, and only pre-counter events, are failures of observation, not of sample.

        Both would count as zero sessions. Calling them a shortfall points whoever reads it at the
        wrong repair — the sample is not behind, the instrument is not answering.
        """
        self.assertEqual(self._gate({}, "2026-09-08")[0], uv.MIDPOINT_UNREADABLE)
        code, out = self._gate({}, "2026-09-08", skipped=9)
        self.assertEqual(code, uv.MIDPOINT_UNREADABLE)
        self.assertIn("관측 불가", out)

    def test_the_dates_are_not_spelled_here(self):
        """The gate reads `verdict_core`, which the PRD-transcription test covers."""
        source = (HERE / "uptake-verdict.py").read_text(encoding="utf-8")
        self.assertNotIn("2026-09-08", source, "the midpoint date is owned by verdict_core")
        self.assertNotIn("2026-09-14", source, "the window close is owned by verdict_core")


if __name__ == "__main__":
    unittest.main(verbosity=2)
