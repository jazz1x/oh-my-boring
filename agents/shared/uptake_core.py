"""Did the agent actually use what was injected?

`recall_label` measures whether a hit *should* have helped — a judge's opinion. This module
measures something the judge cannot fake: whether the note the hook pushed in shows up in what
the agent said afterwards. Precision is about the librarian; uptake is about the reader.

It also answers a question precision cannot. The hook fires on every prompt with no opt-in, so
1472 injections a week say the hook is installed, not that anything wanted them — supply, not
demand. Uptake converts those same events into a demand signal.

**The trap this module exists to avoid**: the injected text becomes part of the transcript. Grep
the transcript for the snippet and it matches itself, every time, and the metric reads 100% while
measuring nothing. So uptake is only ever counted over ASSISTANT turns, and only for evidence
that is not already sitting in the user's own prompt.

No I/O beyond a JSONL append the caller hands a path to; the hooks own the rest.
"""

import json
import os
import re

def ledger_path():
    """Where the recall hook leaves its record — resolved per call, never cached at import.

    Same cache dir the distill markers use, so one directory holds everything a session leaves
    behind. `BORING_INJECTION_LEDGER` redirects it, and the redirect has to be readable *now*
    rather than at import: the hook tests drive the real injection path, and a constant frozen
    at import time depends on whether the test set the variable before the first import touched
    this module. It did not, and the suite appended seven rows to the owner's live ledger.
    """
    return os.environ.get("BORING_INJECTION_LEDGER") or os.path.expanduser(
        "~/.cache/boring-distill/injections.jsonl"
    )

#: Words per phrase when fingerprinting a snippet. Long enough that a match is not a coincidence
#: of common words, short enough to survive the agent paraphrasing around it.
PHRASE_WORDS = 8

#: Phrases per injected hit. The snippet is 280 chars ≈ 40 words, so a handful of windows covers
#: it without turning one hit into hundreds of substring scans.
MAX_PHRASES = 4

_WORD = re.compile(r"[\w./:-]+", re.UNICODE)
_TURN = re.compile(r"^\[(user|assistant)\]\s?", re.MULTILINE)


def _words(text):
    return _WORD.findall((text or "").lower())


def phrases(snippet, size=PHRASE_WORDS, limit=MAX_PHRASES):
    """Distinctive word windows from an injected snippet, in order.

    Windows are spread across the snippet rather than taken from its head: distilled notes open
    with boilerplate section headers ("## 배경 / 문제"), so head-only windows would fingerprint
    the template instead of the content.
    """
    words = _words(snippet)
    if len(words) < size:
        return []
    windows = [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]
    if len(windows) <= limit:
        return windows
    step = len(windows) / float(limit)
    return [windows[int(i * step)] for i in range(limit)]


def injection_record(session_id, prompt, hits, max_results):
    """The row the recall hook appends when it injects.

    Stores the source basename and the snippet fingerprints — never the snippet itself, so the
    ledger cannot become a second copy of the vault. `prompt_words` is kept so a later match can
    be discounted when the user had already said the same thing.
    """
    # No session id means SessionEnd can never attribute this row to a transcript, so it could
    # only ever inflate the denominator. Dropping it here also stops any test that drives the
    # real injection path from appending to the owner's live ledger — a guard in code, because
    # the convention "remember to redirect the ledger in your test" already failed twice.
    if not session_id:
        return None
    injected = []
    for hit in (hits or [])[:max_results]:
        src = (hit.get("source_path") or "").rsplit("/", 1)[-1]
        snippet = " ".join((hit.get("snippet") or "").split())[:280]
        if not (src and snippet):
            continue
        injected.append({"src": src, "phrases": phrases(snippet)})
    if not injected:
        return None
    return {
        "session_id": session_id,
        "prompt_words": _words(prompt)[:400],
        "hits": injected,
    }


def append_record(record, path=None):
    """Append one record. Never raises — a failed ledger write must not cost the user a prompt."""
    if not record:
        return False
    target = path or ledger_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def load_records(session_id, path=None):
    """Records for one session, oldest first. Unreadable or malformed lines are skipped."""
    target = path or ledger_path()
    out = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("session_id") == session_id:
                    out.append(row)
    except OSError:
        return []
    return out


def assistant_text(transcript_text):
    """Only what the assistant said, concatenated.

    `transcript.extract` emits `[user] ...` / `[assistant] ...` turns. Uptake counted over user
    turns would count the injection quoting itself — the failure this module is built around.
    """
    if not transcript_text:
        return ""
    parts = _TURN.split(transcript_text)
    # split() yields [pre, role, body, role, body, ...]; keep bodies whose role is assistant.
    kept = [parts[i + 1] for i in range(1, len(parts) - 1, 2) if parts[i] == "assistant"]
    return "\n".join(kept)


def hit_was_used(hit, assistant_words_text, prompt_words):
    """True if the assistant echoed this hit's source name or one of its phrases.

    A phrase the user already used is not evidence: the agent would have said it anyway. That
    subtraction is what keeps this from being a similarity score between prompt and answer.
    """
    src = (hit.get("src") or "").lower()
    if src and src in assistant_words_text:
        return True
    prompt_blob = " ".join(prompt_words or [])
    for phrase in hit.get("phrases") or []:
        if phrase and phrase in assistant_words_text and phrase not in prompt_blob:
            return True
    return False


def session_uptake(records, transcript_text):
    """(used_hits, total_hits, used_prompts, total_prompts) for one session.

    Two rates, because they answer different questions: per-hit uptake says how much of what we
    push gets used, per-prompt uptake says how often an injection mattered at all.
    """
    assistant_blob = " ".join(_words(assistant_text(transcript_text)))
    used_hits = total_hits = used_prompts = 0
    records = records or []
    for record in records:
        hits = record.get("hits") or []
        total_hits += len(hits)
        prompt_words = record.get("prompt_words") or []
        used_here = sum(1 for h in hits if hit_was_used(h, assistant_blob, prompt_words))
        used_hits += used_here
        if used_here:
            used_prompts += 1
    return used_hits, total_hits, used_prompts, len(records)


def prune_session(session_id, path=None):
    """Drop one session's records once its uptake has been recorded. Never raises."""
    target = path or ledger_path()
    try:
        with open(target, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return False
    kept = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            kept.append(line)  # keep what we cannot parse rather than silently deleting it
            continue
        if row.get("session_id") != session_id:
            kept.append(line)
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
        return True
    except OSError:
        return False
