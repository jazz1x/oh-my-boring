#!/usr/bin/env python3
"""GitHub Codex session distillation hook.

Parses a Codex JSONL session transcript, extracts user/assistant text, and
stores a curated note via ohmyboring's remember tool. Designed to be called by
both the host-side backfill collector and a hermes-agent cron worker.
"""
import json
import os
import sys

# Allow import of shared agent policy library regardless of how this script is invoked.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import boring_config
import event_log
import transcript
from distill_core import (  # noqa: F401
    _extract_json,
    _mark,
    _strip_trailing_metadata,
    _build_prompt,
    _call_llm,
    _call_remember,
    _distill_resolution,
    _throttled,
    distill_and_remember,
    git_remote_url,
    log_skip_event,
    repo_slug,
    write_raw_witness,
)

TRANSCRIPT_FORMAT = "codex-jsonl"
DEFAULT_CODEX_DISTILL_CLAMP = 4000
CLAMP = None
EXTERNAL_IMPORT_MESSAGE = "<EXTERNAL SESSION IMPORTED>"
SHORT_EXTRACT_RETRY_MIN_ASSISTANT_CHARS = transcript.CODEX_SHORT_EXTRACT_RETRY_MIN_ASSISTANT_CHARS


def extract(path):
    """Extract user/assistant text from a Codex JSONL session transcript."""
    return transcript.extract(path, TRANSCRIPT_FORMAT)


def should_retry_short_extract(data: dict, transcript_path: str) -> bool:
    """True when a parse-short result came from a raw transcript large enough to require review."""
    min_raw = data.get("min_raw_bytes_for_retry")
    if min_raw is None:
        return False
    raw_bytes = data.get("raw_bytes")
    if raw_bytes is None:
        raw_bytes = os.path.getsize(transcript_path)
    return (
        int(raw_bytes) >= int(min_raw)
        and transcript.codex_extractable_assistant_chars(transcript_path)
        >= SHORT_EXTRACT_RETRY_MIN_ASSISTANT_CHARS
    )


def _env_clamp_limit() -> int:
    for name in ("CODEX_DISTILL_CLAMP", "INGEST_CLAMP"):
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return transcript.parse_clamp_limit(raw, name)
    return DEFAULT_CODEX_DISTILL_CLAMP


def _default_clamp_limit() -> int:
    if CLAMP is not None:
        return transcript.parse_clamp_limit(CLAMP, "CLAMP")
    return _env_clamp_limit()


def _clamp_limit(data: dict) -> int:
    if "distill_clamp" in data and data["distill_clamp"] is not None:
        return transcript.parse_clamp_limit(data["distill_clamp"], "distill_clamp")
    return _default_clamp_limit()


def _raw_bytes(data: dict, transcript_path: str) -> int:
    raw = data.get("raw_bytes")
    if raw is None:
        return os.path.getsize(transcript_path)
    return int(raw)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"[omb-distill-codex] invalid stdin JSON: {e}", file=sys.stderr)
        return 2

    transcript_path = data.get("transcript_path") or ""
    if not transcript_path or not os.path.exists(transcript_path):
        print(f"[omb-distill-codex] transcript not found: {transcript_path!r}", file=sys.stderr)
        return 2

    raw_session_id = data.get("session_id") or ""
    # Prefix the marker id so Codex session ids never collide with Claude/Kimi ids.
    session_id = f"codex-{raw_session_id}" if raw_session_id else ""
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return 0

    cwd = data.get("cwd") or ""
    remote_url = git_remote_url(cwd)
    origin, _rule = boring_config.classify(cwd, remote_url or None)
    repo = repo_slug(cwd)
    witness = write_raw_witness(transcript_path, "codex", session_id)
    text = extract(witness["path"])
    if len(text) < 500:
        if should_retry_short_extract(data, witness["path"]):
            print(
                "[omb-distill-codex] extracted text too short for large transcript; marked for retry",
                file=sys.stderr,
            )
            if session_id:
                _mark(session_id, retry=True)
            return 1
        print("[omb-distill-codex] transcript too short; skipping", file=sys.stderr)
        log_skip_event(session_id, origin, repo, _distill_resolution(), "too_short")
        if session_id:
            _mark(session_id)
        return 0
    source_chars = len(text)
    clamp_limit = _clamp_limit(data)
    text, was_clamped = transcript.clamp_text(text, clamp_limit)
    if was_clamped:
        print(f"[omb-distill-codex] transcript clamped to {len(text)} chars", file=sys.stderr)
    event_log.try_append_event(
        "codex-distill",
        "input_budget",
        "ok",
        session_id=session_id,
        raw_bytes=_raw_bytes(data, witness["path"]),
        source_chars=source_chars,
        emitted_chars=len(text),
        distill_clamp=clamp_limit,
        clamped=was_clamped,
    )

    if distill_and_remember(text, origin, repo, session_id, sources=[witness["source"]]):
        _mark(session_id)
        print("[omb-distill-codex] remembered", file=sys.stderr)
        return 0
    else:
        _mark(session_id, retry=True)
        print("[omb-distill-codex] remember failed; marked for retry", file=sys.stderr)
        return 1


def run() -> int:
    try:
        return main()
    except Exception as e:
        print(f"[omb-distill-codex] crashed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
