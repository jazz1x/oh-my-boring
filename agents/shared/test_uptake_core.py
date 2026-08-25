#!/usr/bin/env python3
"""Tests for uptake_core.py — above all, that an injection cannot count as its own uptake."""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uptake_core

SNIPPET = (
    "the connection pool died because deadpool recycled a socket the server had already closed "
    "and the retry loop kept handing the same broken object back to every caller"
)


def _hit(src="wiki-0007.md", snippet=SNIPPET):
    return {"source_path": f"/vault/wiki/{src}", "snippet": snippet}


def test_injection_is_not_its_own_uptake():
    # The injected block lands in the transcript as part of the user turn. Grep the whole
    # transcript and it matches itself; that is the failure this whole module guards.
    record = uptake_core.injection_record("s1", "why did the pool die", [_hit()], 3)
    transcript = (
        f"[user] 📚 My past work experience\n- [wiki-0007.md] {SNIPPET}\nwhy did the pool die\n"
        "[assistant] Let me look at the pool configuration first.\n"
    )
    used, total, used_prompts, prompts = uptake_core.session_uptake([record], transcript)
    assert (used, total) == (0, 1), "the injected text quoting itself must not count"
    assert (used_prompts, prompts) == (0, 1)


def test_assistant_reusing_the_note_name_counts():
    record = uptake_core.injection_record("s1", "why did the pool die", [_hit()], 3)
    transcript = (
        f"[user] 📚 past experience\n- [wiki-0007.md] {SNIPPET}\nwhy did the pool die\n"
        "[assistant] Per wiki-0007.md this is the recycled-socket case again.\n"
    )
    used, total, _, _ = uptake_core.session_uptake([record], transcript)
    assert (used, total) == (1, 1)


def test_assistant_reusing_a_phrase_counts():
    record = uptake_core.injection_record("s1", "why did the pool die", [_hit()], 3)
    transcript = (
        f"[user] 📚 past experience\n- [wiki-0007.md] {SNIPPET}\nwhy did the pool die\n"
        "[assistant] Looks like deadpool recycled a socket the server had already closed.\n"
    )
    used, total, _, _ = uptake_core.session_uptake([record], transcript)
    assert (used, total) == (1, 1), "an assistant echoing the substance must count"


def test_a_phrase_the_user_already_said_is_not_evidence():
    # The agent would have said it anyway; crediting the injection here would turn uptake into
    # a similarity score between the prompt and the answer.
    prompt = "deadpool recycled a socket the server had already closed — why did the pool die"
    record = uptake_core.injection_record("s1", prompt, [_hit()], 3)
    transcript = (
        f"[user] {prompt}\n"
        "[assistant] Right, deadpool recycled a socket the server had already closed.\n"
    )
    used, total, _, _ = uptake_core.session_uptake([record], transcript)
    assert (used, total) == (0, 1)


def test_only_assistant_turns_are_scanned():
    record = uptake_core.injection_record("s1", "unrelated question", [_hit()], 3)
    transcript = (
        "[user] unrelated question\n"
        f"[user] deadpool recycled a socket the server had already closed\n"
        "[assistant] I have no idea.\n"
    )
    used, _, _, _ = uptake_core.session_uptake([record], transcript)
    assert used == 0, "a later user turn is not the agent using the memory"


def test_assistant_text_extracts_only_assistant_bodies():
    text = "[user] alpha\n[assistant] beta\n[user] gamma\n[assistant] delta\n"
    got = uptake_core.assistant_text(text)
    assert "beta" in got and "delta" in got
    assert "alpha" not in got and "gamma" not in got


def test_phrases_spread_across_the_snippet_not_just_the_head():
    # Distilled notes all begin with the same section headers, so head-only fingerprints would
    # match the template rather than the content.
    body = "## 배경 문제 " + " ".join(f"word{i}" for i in range(60))
    got = uptake_core.phrases(body)
    assert len(got) == uptake_core.MAX_PHRASES
    assert "배경" in got[0], "the first window still starts at the head"
    assert all("배경" not in p for p in got[1:]), "later windows must clear the boilerplate"
    assert any("word4" in p for p in got[-1:]), "the last window must reach the snippet's tail"
    assert len(set(got)) == len(got), "windows must not be duplicates"


def test_short_snippets_yield_no_phrases():
    assert uptake_core.phrases("too short") == []


def test_a_record_without_a_session_id_is_never_written():
    # SessionEnd looks records up by session id; one without an id can only inflate the
    # denominator. It is also the guard that keeps test suites out of the live ledger.
    assert uptake_core.injection_record("", "a real prompt", [_hit()], 3) is None
    assert uptake_core.injection_record(None, "a real prompt", [_hit()], 3) is None


def test_record_is_none_when_nothing_injectable():
    assert uptake_core.injection_record("s1", "p", [], 3) is None
    assert uptake_core.injection_record("s1", "p", [{"source_path": "", "snippet": ""}], 3) is None


def test_record_respects_max_results():
    hits = [_hit(f"wiki-000{i}.md") for i in range(5)]
    record = uptake_core.injection_record("s1", "p", hits, 2)
    assert len(record["hits"]) == 2


def test_ledger_roundtrip_and_prune_keep_other_sessions():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        uptake_core.append_record(uptake_core.injection_record("s1", "p", [_hit()], 3), path)
        uptake_core.append_record(uptake_core.injection_record("s2", "p", [_hit()], 3), path)
        assert len(uptake_core.load_records("s1", path)) == 1
        uptake_core.prune_session("s1", path)
        assert uptake_core.load_records("s1", path) == []
        assert len(uptake_core.load_records("s2", path)) == 1, "pruning one session must not touch another"


def test_ledger_failures_are_silent_not_fatal():
    # A ledger write must never cost the user their prompt.
    assert uptake_core.append_record({"session_id": "s"}, "/nonexistent-root-dir/x/y.jsonl") is False
    assert uptake_core.load_records("s1", "/nonexistent-root-dir/x/y.jsonl") == []


def test_malformed_ledger_lines_are_skipped_not_crashed():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "injections.jsonl")
        Path(path).write_text('not json\n{"session_id": "s1", "hits": []}\n', encoding="utf-8")
        assert len(uptake_core.load_records("s1", path)) == 1


def test_ledger_path_is_read_per_call_not_frozen_at_import():
    # A constant captured at import time made the redirect depend on import order, and the hook
    # suite appended seven rows to the owner's live ledger before this was caught.
    import os

    default = uptake_core.ledger_path()
    os.environ["BORING_INJECTION_LEDGER"] = "/tmp/redirected-after-import.jsonl"
    try:
        assert uptake_core.ledger_path() == "/tmp/redirected-after-import.jsonl"
    finally:
        os.environ.pop("BORING_INJECTION_LEDGER", None)
    assert uptake_core.ledger_path() == default


def test_snippet_text_is_not_stored_in_the_ledger():
    # The ledger must not become a second copy of the vault.
    record = uptake_core.injection_record("s1", "p", [_hit()], 3)
    blob = json.dumps(record, ensure_ascii=False)
    assert SNIPPET not in blob
    assert "wiki-0007.md" in blob


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - uptake_core: injections cannot count as their own uptake")
