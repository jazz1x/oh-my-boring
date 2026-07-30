#!/usr/bin/env python3
"""Kimi Code CLI UserPromptSubmit hook — recalls AST-indexed code symbols
relevant to the current coding prompt from ohmyboring (code graph).

Thin agent-specific entry point; all shared logic lives in
`agents/shared/code_recall_core.py`.
Requires BORING_VECTOR=on and a populated code graph (`make code-index`).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import code_recall_core  # noqa: E402


def _is_injection(data: dict) -> bool:
    """Skip recall for system/injection prompts that are not real user input."""
    origin = data.get("origin") or {}
    if isinstance(origin, dict) and origin.get("kind") != "user":
        return True
    prompt = (data.get("prompt") or "").strip()
    return prompt.startswith("<system-reminder>") or prompt.startswith("<kimi-skill-loaded")


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(f"[omb-code-recall] invalid stdin JSON: {e}", file=sys.stderr)
        return
    code_recall_core.run_code_recall(data, is_injection=_is_injection)


if __name__ == "__main__":
    main()
