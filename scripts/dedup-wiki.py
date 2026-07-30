#!/usr/bin/env python3
"""One-time duplicate-note cleanup for vault/wiki.

Clusters notes only when embedding similarity is corroborated by title/body/claim evidence,
then archives older duplicates so the newest note per cluster remains. Defaults to --dry-run;
pass --apply to confirm interactively, or --apply --yes for unattended execution.
"""
import argparse
import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_THRESHOLD = 0.93
DEFAULT_ARCHIVE_DIR = "data/archive/dup"
TITLE_DUP_MIN = (1, 2)
TITLE_DUP_SHARED_MIN = 2
BODY_DUP_MIN = (4, 5)
CLAIM_VALUE_DUP_MIN = (4, 5)
GENERATED_BRIEF_TAG = "daily-brief"
INTERNAL_EVAL_FIXTURE_PREFIX = "eval-"


def parse_similarity_threshold(raw: str) -> float:
    try:
        threshold = float(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"threshold must be a number in 0..1, got {raw!r}"
        ) from e
    if not 0.0 <= threshold <= 1.0:
        raise argparse.ArgumentTypeError(
            f"threshold must be in 0..1, got {raw!r}"
        )
    return threshold


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LlmEmbedder:
    """Minimal OpenAI-compatible embeddings client."""

    def __init__(self) -> None:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
        import omb_env

        self.base_url = omb_env.llm_base_url().rstrip("/")
        self.api_key = omb_env.llm_api_key()
        self.model = omb_env.embed_model()

    def embed(self, text: str) -> list[float]:
        payload = {"input": text, "model": self.model}
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=data,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp["data"][0]["embedding"]


# Pull in urllib only where we need it to keep the top clean.
import urllib.request  # noqa: E402
import yaml  # noqa: E402


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def parse_note(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return note_record(path, "", text, [], project=derive_project(path))
    end = text.find("\n---\n")
    if end == -1:
        return note_record(path, "", text, [], project=derive_project(path))
    yaml_text = text[4:end]
    body = text[end + 5 :]
    fm = parse_frontmatter_map(yaml_text, path)
    title = fm.get("title") or ""
    if not isinstance(title, str):
        raise ValueError(f"frontmatter title must be a string: {path}")
    project = parse_project(fm, path)
    return note_record(
        path,
        title,
        body,
        parse_claims(fm, path),
        parse_session_id(fm, path),
        project,
        parse_origin(fm, path),
        parse_tags(fm, path),
    )


def note_record(
    path: Path,
    title: str,
    body: str,
    claims: list[dict[str, str]],
    session_id: str = "",
    project: str = "",
    origin: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "path": path,
        "title": title.strip(),
        "body": body.strip(),
        "claims": claims,
        "omb_session_id": session_id.strip(),
        "project": project.strip(),
        "origin": origin.strip(),
        "tags": tags or [],
        "mtime": path.stat().st_mtime,
    }


def parse_frontmatter_map(yaml_text: str, path: Path) -> dict[str, Any]:
    try:
        fm = json.loads(yaml_text) if yaml_text.strip().startswith("{") else yaml.safe_load(yaml_text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"malformed frontmatter in {path}: {e}") from e
    if fm is None:
        return {}
    if not isinstance(fm, dict):
        raise ValueError(f"frontmatter must be a mapping: {path}")
    return fm


def parse_claims(fm: dict[str, Any], path: Path) -> list[dict[str, str]]:
    raw = fm.get("claims", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"frontmatter claims must be a list: {path}")
    claims = []
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"frontmatter claim #{idx} must be a mapping: {path}")
        claim = {}
        for key in ("subject", "predicate", "value"):
            value = item.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"frontmatter claim #{idx} field {key} must be a string: {path}")
            claim[key] = value.strip()
        if claim["subject"] and claim["predicate"] and claim["value"]:
            claims.append(claim)
    return claims


def parse_session_id(fm: dict[str, Any], path: Path) -> str:
    raw = fm.get("omb_session_id", "")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"frontmatter omb_session_id must be a string: {path}")
    return raw.strip()


def parse_project(fm: dict[str, Any], path: Path) -> str:
    raw = fm.get("project", "")
    if raw is None:
        return derive_project(path)
    if not isinstance(raw, str):
        raise ValueError(f"frontmatter project must be a string: {path}")
    project = raw.strip()
    return project or derive_project(path)


def derive_project(path: Path) -> str:
    parts = [part for part in path.as_posix().split("/") if part]
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if len(parts) >= 2:
        return parts[-2]
    return "unknown"


def parse_origin(fm: dict[str, Any], path: Path) -> str:
    raw = fm.get("origin", "")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"frontmatter origin must be a string: {path}")
    return raw.strip()


def parse_tags(fm: dict[str, Any], path: Path) -> list[str]:
    raw = fm.get("tags", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"frontmatter tags must be a list: {path}")
    tags = []
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, str):
            raise ValueError(f"frontmatter tag #{idx} must be a string: {path}")
        tag = item.strip()
        if tag:
            tags.append(tag)
    return tags


def source_memory_candidates(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [note for note in notes if is_source_memory_candidate(note)]


def newest_source_note_path(wiki_dir: Path) -> Path | None:
    paths = sorted(
        wiki_dir.glob("wiki-*.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        if is_source_memory_candidate(parse_note(path)):
            return path
    return None


def is_source_memory_candidate(note: dict[str, Any]) -> bool:
    return not is_generated_brief_note(note) and not is_internal_eval_fixture(note["path"])


def is_generated_brief_note(note: dict[str, Any]) -> bool:
    return any(tag.strip() == GENERATED_BRIEF_TAG for tag in note.get("tags", []))


def is_internal_eval_fixture(path: Path) -> bool:
    name = path.name
    return name.startswith(INTERNAL_EVAL_FIXTURE_PREFIX) and name.endswith(".md")


def cluster_notes(notes: list[dict[str, Any]], threshold: float) -> list[list[int]]:
    n = len(notes)
    emb = [n["embedding"] for n in notes]
    newest_first = sorted(range(n), key=lambda i: notes[i]["mtime"], reverse=True)
    assigned = set()
    clusters = []
    for keeper in newest_first:
        if keeper in assigned:
            continue
        group = [keeper]
        for candidate in newest_first:
            if candidate == keeper or candidate in assigned:
                continue
            sim = cosine_similarity(emb[keeper], emb[candidate])
            if sim >= threshold and has_duplicate_evidence(notes[keeper], notes[candidate]):
                group.append(candidate)
        if len(group) > 1:
            assigned.update(group)
            clusters.append(group)
    return clusters


def has_duplicate_evidence(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if same_session_id(a, b):
        return True
    if not duplicate_boundary_compatible(a, b):
        return False
    if claim_axis_value_conflict(a["claims"], b["claims"]):
        return False
    return (
        title_duplicate_evidence(a["title"], b["title"])
        or token_jaccard_at_least(a["body"], b["body"], BODY_DUP_MIN)
        or claim_identity_value_overlap(a["claims"], b["claims"])
    )


def same_session_id(a: dict[str, Any], b: dict[str, Any]) -> bool:
    left = a.get("omb_session_id", "").strip()
    right = b.get("omb_session_id", "").strip()
    return bool(left and right and left == right)


def project_compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    left = a.get("project", "").strip().lower()
    right = b.get("project", "").strip().lower()
    return not left or not right or left == right


def duplicate_boundary_compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return project_compatible(a, b) and origin_compatible(a, b)


def origin_compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return origin_key(a.get("origin", "")) == origin_key(b.get("origin", ""))


def origin_key(origin: str) -> str:
    origin = origin.strip().lower()
    return origin or "personal"


def title_duplicate_evidence(left: str, right: str) -> bool:
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    shared = len(left_tokens & right_tokens)
    if shared < TITLE_DUP_SHARED_MIN:
        return False
    if claim_key(left) == claim_key(right):
        return True
    return ratio_at_least(shared, len(left_tokens | right_tokens), TITLE_DUP_MIN)


def claim_identity_value_overlap(left: list[dict[str, str]], right: list[dict[str, str]]) -> bool:
    for l_claim in left:
        l_subject = claim_key(l_claim["subject"])
        l_predicate = claim_key(l_claim["predicate"])
        if not l_subject or not l_predicate:
            continue
        for r_claim in right:
            if (
                l_subject == claim_key(r_claim["subject"])
                and l_predicate == claim_key(r_claim["predicate"])
                and claim_value_equivalent(l_claim["value"], r_claim["value"])
            ):
                return True
    return False


def claim_axis_value_conflict(left: list[dict[str, str]], right: list[dict[str, str]]) -> bool:
    for l_claim in left:
        l_subject = claim_key(l_claim["subject"])
        l_predicate = claim_key(l_claim["predicate"])
        if not l_subject or not l_predicate or not l_claim["value"].strip():
            continue
        for r_claim in right:
            if (
                l_subject == claim_key(r_claim["subject"])
                and l_predicate == claim_key(r_claim["predicate"])
                and r_claim["value"].strip()
                and not claim_value_equivalent(l_claim["value"], r_claim["value"])
            ):
                return True
    return False


def claim_value_equivalent(left: str, right: str) -> bool:
    left_key = claim_key(left)
    right_key = claim_key(right)
    if left_key and left_key == right_key:
        return True
    left_semantic_key = semantic_key(left)
    right_semantic_key = semantic_key(right)
    if left_semantic_key and left_semantic_key == right_semantic_key:
        return True
    return token_jaccard_at_least(left, right, CLAIM_VALUE_DUP_MIN)


def token_jaccard_at_least(a: str, b: str, minimum: tuple[int, int]) -> bool:
    a_tokens = token_set(a)
    b_tokens = token_set(b)
    if not a_tokens or not b_tokens:
        return False
    intersection = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    return ratio_at_least(intersection, union, minimum)


def ratio_at_least(numerator: int, denominator: int, minimum: tuple[int, int]) -> bool:
    if denominator == 0:
        return False
    return numerator * minimum[1] >= denominator * minimum[0]


def token_set(text: str) -> set[str]:
    tokens = set()
    buf = []
    for ch in text.lower():
        if ch.isalnum():
            buf.append(ch)
        else:
            flush_token(tokens, buf)
    flush_token(tokens, buf)
    return tokens


def flush_token(tokens: set[str], buf: list[str]) -> None:
    if len(buf) > 1:
        tokens.add("".join(buf))
    buf.clear()


def claim_key(value: str) -> str:
    normalized = value.lower().replace("c++", "cpp").replace("c#", "csharp").replace(".net", "dotnet")
    chars = [ch if ch.isalnum() else " " for ch in normalized]
    return " ".join("".join(chars).split())


def semantic_key(value: str) -> str:
    normalized = value.lower().replace("c++", "cpp").replace("c#", "csharp").replace(".net", "dotnet")
    return "".join(ch for ch in normalized if ch.isascii() and ch.isalnum())


def archive_destination_conflicts(actions: list[tuple[Path, Path]], archive_dir: Path) -> list[Path]:
    seen = set()
    conflicts = []
    for dup_path, _keeper_path in actions:
        dst = archive_dir / dup_path.name
        if dst in seen or dst.exists():
            conflicts.append(dst)
        seen.add(dst)
    return conflicts


def embed_notes(notes: list[dict[str, Any]], embedder: LlmEmbedder) -> bool:
    failed = []
    for i, n in enumerate(notes):
        text = f"{n['title']}\n\n{n['body']}"[:4000]
        try:
            n["embedding"] = embedder.embed(text)
        except Exception as e:
            failed.append(n["path"].name)
            print(f"[{_now()}] embedding failed for {n['path'].name}: {e}", file=sys.stderr)
        if (i + 1) % 10 == 0 or i + 1 == len(notes):
            print(f"  embedded {i + 1}/{len(notes)}")
    if failed:
        print(
            f"[{_now()}] aborting duplicate clustering: {len(failed)} note embedding(s) failed",
            file=sys.stderr,
        )
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Deduplicate vault/wiki notes")
    parser.add_argument("--wiki-dir", default="vault/wiki", help="wiki directory")
    parser.add_argument("--archive-dir", default=DEFAULT_ARCHIVE_DIR, help="archive directory")
    parser.add_argument(
        "--threshold",
        type=parse_similarity_threshold,
        default=DEFAULT_THRESHOLD,
        help="cosine similarity threshold in 0..1",
    )
    parser.add_argument("--apply", action="store_true", help="actually archive and forget duplicates")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    parser.add_argument(
        "--print-newest-source-note",
        action="store_true",
        help="print newest source-memory wiki note path and exit",
    )
    args = parser.parse_args()

    wiki_dir = Path(args.wiki_dir)
    archive_dir = Path(args.archive_dir)
    if not wiki_dir.is_dir():
        print(f"wiki dir not found: {wiki_dir}", file=sys.stderr)
        return 1
    if args.print_newest_source_note:
        try:
            newest_note = newest_source_note_path(wiki_dir)
        except ValueError as e:
            print(f"[{_now()}] {e}", file=sys.stderr)
            return 1
        if newest_note is not None:
            print(newest_note)
        return 0

    paths = sorted(wiki_dir.glob("*.md"))
    if not paths:
        print("no wiki notes found")
        return 0

    print(f"[{_now()}] Loading {len(paths)} notes from {wiki_dir} ...")
    try:
        parsed_notes = [parse_note(p) for p in paths]
    except ValueError as e:
        print(f"[{_now()}] {e}", file=sys.stderr)
        return 1
    notes = source_memory_candidates(parsed_notes)
    skipped = len(parsed_notes) - len(notes)
    if skipped:
        print(f"[{_now()}] Skipping {skipped} generated/eval artifact note(s).")
    if not notes:
        print("no source-memory wiki notes found")
        return 0

    embedder = LlmEmbedder()
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
    from drudge_client import DrudgeClient

    client = DrudgeClient(timeout=30, retries=2)

    print(f"[{_now()}] Embedding {len(notes)} notes (model={embedder.model}) ...")
    if not embed_notes(notes, embedder):
        return 1

    print(f"[{_now()}] Clustering with threshold {args.threshold} ...")
    clusters = cluster_notes(notes, args.threshold)
    total_dup = sum(len(g) - 1 for g in clusters)
    print(f"[{_now()}] Found {len(clusters)} duplicate clusters ({total_dup} duplicates to archive).\n")

    if not clusters:
        print("No duplicates found.")
        return 0

    actions = []
    for cid, group in enumerate(clusters, 1):
        group.sort(key=lambda i: notes[i]["mtime"], reverse=True)
        keeper = group[0]
        dupes = group[1:]
        print(f"Cluster {cid}: keep {notes[keeper]['path'].name} (mtime={datetime.fromtimestamp(notes[keeper]['mtime'], tz=timezone.utc).isoformat()})")
        for d in dupes:
            print(f"  → archive {notes[d]['path'].name} (mtime={datetime.fromtimestamp(notes[d]['mtime'], tz=timezone.utc).isoformat()})")
            actions.append((notes[d]["path"], notes[keeper]["path"]))

    if not args.apply:
        print(
            f"\n[{_now()}] Dry run complete. Pass --apply to confirm archiving "
            f"{len(actions)} files, or --apply --yes for unattended execution."
        )
        return 0

    conflicts = archive_destination_conflicts(actions, archive_dir)
    if conflicts:
        print(f"[{_now()}] refusing to archive: destination already exists or repeats", file=sys.stderr)
        for dst in conflicts:
            print(f"  conflict: {dst}", file=sys.stderr)
        return 1

    if not confirm_apply(args.yes):
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[{_now()}] Archiving {len(actions)} duplicates to {archive_dir} ...")
    for dup_path, keeper_path in actions:
        dst = archive_dir / dup_path.name
        shutil.move(str(dup_path), str(dst))
        print(f"  archived {dup_path.name}")
        # Also tell the engine to purge the vector/graph record.
        wiki_id = dup_path.stem
        try:
            client.mcp_call("forget", {"id": wiki_id})
            print(f"  forgot {wiki_id}")
        except Exception as e:
            print(f"  ⚠️ forget failed for {wiki_id}: {e}", file=sys.stderr)

    print(f"\n[{_now()}] Done. Run 'make sync' to rebuild the vector/graph state.")
    return 0


def confirm_apply(yes: bool, ask=input) -> bool:
    if yes:
        return True
    ans = ask("Archive duplicate notes and call forget? [y/N] ")
    if ans.lower() in ("y", "yes"):
        return True
    print("aborted.")
    return False


if __name__ == "__main__":
    sys.exit(main())
