#!/usr/bin/env python3
"""Network-free regression tests for code_recall_core.py (AST code-recall lane)."""
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("BORING_CONFIG", None)
os.environ.pop("BORING_HOME", None)

import code_recall_core

CODE_RECALL_ENV_KEYS = (
    "CODE_RECALL_MAX_SYMBOLS",
    "CODE_RECALL_TIMEOUT",
    "CODE_RECALL_RETRIES",
)


def _clear_env():
    old = {name: os.environ.get(name) for name in CODE_RECALL_ENV_KEYS}
    for name in CODE_RECALL_ENV_KEYS:
        os.environ.pop(name, None)
    return old


def _restore_env(old):
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_is_code_query_gate():
    assert code_recall_core.is_code_query("how does parse_file work") is True
    assert code_recall_core.is_code_query("where is AppState defined") is True
    assert code_recall_core.is_code_query("fix the def in foo.py") is True
    assert code_recall_core.is_code_query("what did I do yesterday") is False
    assert code_recall_core.is_code_query("The meeting went well") is False


def test_code_tokens_extracts_identifiers():
    tokens = code_recall_core.code_tokens("how does `parse_file` call DrudgeClient.search?")
    assert "parse_file" in tokens
    assert "DrudgeClient" in tokens  # prefix stops at `.` — ILIKE matches the class
    assert "how" not in tokens
    # cap: at most MAX_QUERY_TOKENS tokens
    many = code_recall_core.code_tokens("a_b c_d e_f g_h i_j k_l")
    assert len(many) == code_recall_core.MAX_QUERY_TOKENS


def test_code_tokens_strips_korean_particles():
    tokens = code_recall_core.code_tokens("EvalFixtureService의 eval_fixture_route를 찾아줘")
    assert tokens == ["EvalFixtureService", "eval_fixture_route"]


def test_run_code_recall_skips_non_code_prompt():
    with mock.patch.object(code_recall_core.DrudgeClient, "code_search_full") as search:
        code_recall_core.run_code_recall({"prompt": "what did I do yesterday at work"})
    search.assert_not_called()


def test_run_code_recall_skips_injection():
    with mock.patch.object(code_recall_core.DrudgeClient, "code_search_full") as search:
        code_recall_core.run_code_recall(
            {"prompt": "how does parse_file work"},
            is_injection=lambda d: True,
        )
    search.assert_not_called()


def test_run_code_recall_formats_context():
    old = _clear_env()
    captured = io.StringIO()
    hits = [
        {
            "kind": "function",
            "name": "parse_file",
            "source_path": "drudge/src/codegraph/parser.rs",
            "signature": "pub fn parse_file(path: &Path) -> Result<Vec<CodeSymbol>>",
        }
    ]
    try:
        with mock.patch.object(code_recall_core.sys, "stdout", captured), \
             mock.patch.object(
                 code_recall_core.DrudgeClient, "code_search_full",
                 return_value={"hits": hits, "notes": []},
             ):
            code_recall_core.run_code_recall({"prompt": "how does parse_file work"})
    finally:
        _restore_env(old)

    payload = json.loads(captured.getvalue())
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "«UNTRUSTED-DATA " in ctx
    assert "«/UNTRUSTED-DATA " in ctx
    assert "function `parse_file` (parser.rs)" in ctx
    assert "pub fn parse_file" in ctx
    assert "📝 Code notes" not in ctx  # no linked notes → section omitted


def test_run_code_recall_renders_linked_notes():
    old = _clear_env()
    captured = io.StringIO()
    payload = {
        "hits": [
            {
                "kind": "function",
                "name": "parse_file",
                "source_path": "drudge/src/codegraph/parser.rs",
                "signature": "pub fn parse_file(path: &Path)",
            }
        ],
        "notes": [
            {
                "source_path": "vault/wiki/wiki-0999.md",
                "title": "parse_file는 max_symbols_per_file 상한을 넘기면 안 된다",
                "snippet": "상한 초과분은 버려진다. 운영에서 재인덱싱 후에도 이 노트는 유지된다.",
                "symbol_name": "parse_file",
                "symbol_path": "drudge/src/codegraph/parser.rs",
            }
        ],
    }
    try:
        with mock.patch.object(code_recall_core.sys, "stdout", captured), \
             mock.patch.object(
                 code_recall_core.DrudgeClient, "code_search_full", return_value=payload
             ):
            code_recall_core.run_code_recall({"prompt": "how does parse_file work"})
    finally:
        _restore_env(old)

    ctx = json.loads(captured.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "📝 Code notes" in ctx
    assert "parse_file는 max_symbols_per_file 상한을 넘기면 안 된다" in ctx
    assert "(about `parse_file`)" in ctx
    assert "상한 초과분은 버려진다" in ctx


def test_run_code_recall_note_without_title_uses_filename():
    old = _clear_env()
    captured = io.StringIO()
    payload = {
        "hits": [
            {
                "kind": "function",
                "name": "parse_file",
                "source_path": "drudge/src/codegraph/parser.rs",
                "signature": "",
            }
        ],
        "notes": [
            {
                "source_path": "vault/wiki/wiki-0999.md",
                "title": "",
                "snippet": "",
                "symbol_name": "parse_file",
                "symbol_path": "drudge/src/codegraph/parser.rs",
            }
        ],
    }
    try:
        with mock.patch.object(code_recall_core.sys, "stdout", captured), \
             mock.patch.object(
                 code_recall_core.DrudgeClient, "code_search_full", return_value=payload
             ):
            code_recall_core.run_code_recall({"prompt": "how does parse_file work"})
    finally:
        _restore_env(old)

    ctx = json.loads(captured.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "- wiki-0999.md (about `parse_file`)" in ctx


def test_run_code_recall_dedupes_across_tokens():
    old = _clear_env()
    captured = io.StringIO()
    hit = {
        "kind": "method",
        "name": "search",
        "source_path": "agents/shared/drudge_client.py",
        "signature": "def search(self, query)",
    }
    note = {
        "source_path": "vault/wiki/wiki-0998.md",
        "title": "search 호출 전 timeout 계약 확인",
        "snippet": "재시도는 5xx만.",
        "symbol_name": "search",
        "symbol_path": "agents/shared/drudge_client.py",
    }
    try:
        with mock.patch.object(code_recall_core.sys, "stdout", captured), \
             mock.patch.object(
                 code_recall_core.DrudgeClient, "code_search_full",
                 return_value={"hits": [hit], "notes": [note]},
             ) as search:
            code_recall_core.run_code_recall({"prompt": "DrudgeClient search_query method"})
    finally:
        _restore_env(old)

    assert search.call_count == 2  # one lookup per identifier token
    ctx = json.loads(captured.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert ctx.count("method `search`") == 1
    assert ctx.count("search 호출 전 timeout 계약 확인") == 1  # note deduped across tokens too


def test_run_code_recall_engine_down_is_silent():
    old = _clear_env()
    captured = io.StringIO()
    try:
        with mock.patch.object(code_recall_core.sys, "stdout", captured), \
             mock.patch.object(
                 code_recall_core.DrudgeClient, "code_search_full", side_effect=OSError("down")
             ):
            code_recall_core.run_code_recall({"prompt": "how does parse_file work"})
    finally:
        _restore_env(old)
    assert captured.getvalue() == ""


def test_run_code_recall_rejects_invalid_config():
    old = _clear_env()
    stderr = io.StringIO()
    try:
        os.environ["CODE_RECALL_MAX_SYMBOLS"] = "0"
        with mock.patch.object(code_recall_core.sys, "stderr", stderr), \
             mock.patch.object(code_recall_core.DrudgeClient, "code_search_full") as search:
            code_recall_core.run_code_recall({"prompt": "how does parse_file work"})
        search.assert_not_called()
        assert "CODE_RECALL_MAX_SYMBOLS must be a positive integer" in stderr.getvalue()
    finally:
        _restore_env(old)


if __name__ == "__main__":
    test_is_code_query_gate()
    test_code_tokens_extracts_identifiers()
    test_code_tokens_strips_korean_particles()
    test_run_code_recall_skips_non_code_prompt()
    test_run_code_recall_skips_injection()
    test_run_code_recall_formats_context()
    test_run_code_recall_renders_linked_notes()
    test_run_code_recall_note_without_title_uses_filename()
    test_run_code_recall_dedupes_across_tokens()
    test_run_code_recall_engine_down_is_silent()
    test_run_code_recall_rejects_invalid_config()
    print("ok - code_recall_core")
