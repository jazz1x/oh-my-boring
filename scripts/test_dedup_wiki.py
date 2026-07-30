#!/usr/bin/env python3
"""Tests for scripts/dedup-wiki.py."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dedup_wiki", str(ROOT / "scripts" / "dedup-wiki.py")
)
dedup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dedup)


class StaticEmbedder:
    model = "test-embedder"

    def embed(self, _text):
        return [1.0, 0.0]


class FailingEmbedder:
    model = "test-embedder"

    def embed(self, _text):
        raise RuntimeError("down")


def test_embed_notes_writes_embeddings_for_all_notes():
    notes = [_note("wiki-0001.md"), _note("wiki-0002.md")]

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        ok = dedup.embed_notes(notes, StaticEmbedder())

    assert ok
    assert [note["embedding"] for note in notes] == [[1.0, 0.0], [1.0, 0.0]]


def test_embed_notes_aborts_without_zero_vector_on_failure():
    note = _note("wiki-0001.md")
    stderr = io.StringIO()

    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
        ok = dedup.embed_notes([note], FailingEmbedder())

    assert not ok
    assert "embedding" not in note
    assert "aborting duplicate clustering" in stderr.getvalue()


def test_parse_similarity_threshold_accepts_closed_unit_interval():
    assert dedup.parse_similarity_threshold("0") == 0.0
    assert dedup.parse_similarity_threshold("0.93") == 0.93
    assert dedup.parse_similarity_threshold("1") == 1.0


def test_parse_similarity_threshold_rejects_invalid_policy_values():
    for raw in ("-0.01", "1.01", "nan", "soon"):
        error = _argparse_error_from(lambda raw=raw: dedup.parse_similarity_threshold(raw))
        assert "threshold must" in str(error)


def test_parse_note_rejects_malformed_frontmatter():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\ntitle: [broken\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "malformed frontmatter" in str(error)


def test_parse_note_rejects_non_mapping_frontmatter():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\n- title\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "frontmatter must be a mapping" in str(error)


def test_parse_note_rejects_non_list_claims():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\ntitle: x\nclaims: bad\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "frontmatter claims must be a list" in str(error)


def test_parse_note_rejects_non_string_session_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\ntitle: x\nomb_session_id: [bad]\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "frontmatter omb_session_id must be a string" in str(error)


def test_parse_note_rejects_non_string_project():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\ntitle: x\nproject: [bad]\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "frontmatter project must be a string" in str(error)


def test_parse_note_rejects_non_string_origin():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\ntitle: x\norigin: [bad]\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "frontmatter origin must be a string" in str(error)


def test_parse_note_rejects_non_list_tags():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\ntitle: x\ntags: daily-brief\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "frontmatter tags must be a list" in str(error)


def test_parse_note_rejects_non_string_tag():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text("---\ntitle: x\ntags: [daily-brief, 7]\n---\nbody\n", encoding="utf-8")

        error = _value_error_from(lambda: dedup.parse_note(path))

    assert "frontmatter tag #2 must be a string" in str(error)


def test_parse_note_without_frontmatter_derives_project_from_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "projects" / "legacy-app" / "wiki-0001.md"
        path.parent.mkdir(parents=True)
        path.write_text("plain body\n", encoding="utf-8")

        note = dedup.parse_note(path)

    assert note["title"] == ""
    assert note["body"] == "plain body"
    assert note["claims"] == []
    assert note["omb_session_id"] == ""
    assert note["project"] == "legacy-app"
    assert note["origin"] == ""
    assert note["tags"] == []


def test_parse_note_derives_blank_project_from_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "projects" / "oh-my-boring" / "wiki-0001.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ntitle: x\norigin: ' company '\nproject: '   '\nomb_session_id: ' session-a '\n---\nbody\n",
            encoding="utf-8",
        )

        note = dedup.parse_note(path)

    assert note["project"] == "oh-my-boring"
    assert note["origin"] == "company"
    assert note["omb_session_id"] == "session-a"


def test_parse_note_keeps_session_id_as_duplicate_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wiki-0001.md"
        path.write_text(
            "---\ntitle: x\norigin: company\nproject: app-alpha\nomb_session_id: session-a\n---\nbody\n",
            encoding="utf-8",
        )

        note = dedup.parse_note(path)

    assert note["omb_session_id"] == "session-a"
    assert note["project"] == "app-alpha"
    assert note["origin"] == "company"


def test_parse_note_keeps_tags_for_source_memory_filter():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "daily-brief-2026-07-03.md"
        path.write_text(
            "---\ntitle: brief\ntags: [daily-brief, generated]\n---\nbody\n",
            encoding="utf-8",
        )

        note = dedup.parse_note(path)

    assert note["tags"] == ["daily-brief", "generated"]
    assert dedup.is_generated_brief_note(note)


def test_source_memory_candidates_exclude_generated_and_eval_notes():
    notes = [
        _note("daily-brief-2026-07-03.md", tags=["daily-brief"]),
        _note("eval-docker-layer-cache.md"),
        _note("wiki-0001.md", tags=["repo/omb"]),
    ]

    assert dedup.source_memory_candidates(notes) == [notes[2]]


def test_newest_source_note_path_uses_source_memory_filter():
    with tempfile.TemporaryDirectory() as tmp:
        wiki_dir = Path(tmp)
        source = wiki_dir / "wiki-0001.md"
        generated = wiki_dir / "wiki-0002.md"
        source.write_text("---\ntitle: source\ntags: [repo/omb]\n---\nsource\n", encoding="utf-8")
        generated.write_text(
            "---\ntitle: brief\ntags: [daily-brief]\n---\ngenerated\n",
            encoding="utf-8",
        )
        os.utime(source, (100, 100))
        os.utime(generated, (200, 200))

        assert dedup.newest_source_note_path(wiki_dir) == source


def test_cluster_notes_requires_duplicate_evidence_beyond_embedding():
    notes = [
        _note("wiki-0001.md", title="Release status", body="Tests are pending.", embedding=[1.0, 0.0]),
        _note(
            "wiki-0002.md",
            title="Design note",
            body="Architecture decision recorded.",
            embedding=[1.0, 0.0],
        ),
    ]

    assert dedup.cluster_notes(notes, 0.93) == []


def test_cluster_notes_rejects_single_shared_title_token():
    notes = [
        _note("wiki-0001.md", title="release alpha", body="note one", embedding=[1.0, 0.0]),
        _note("wiki-0002.md", title="release beta", body="note two", embedding=[1.0, 0.0]),
    ]

    assert not dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.cluster_notes(notes, 0.93) == []


def test_cluster_notes_accepts_same_session_id_as_evidence():
    notes = [
        _note(
            "wiki-0001.md",
            title="rich note",
            body="Detailed evidence for the harvested session.",
            embedding=[1.0, 0.0],
            mtime=2.0,
            session_id="codex-session-a",
        ),
        _note(
            "wiki-0002.md",
            title="short memo",
            body="Brief summary.",
            embedding=[1.0, 0.0],
            mtime=1.0,
            session_id="codex-session-a",
        ),
    ]

    assert dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.cluster_notes(notes, 0.93) == [[0, 1]]


def test_cluster_notes_keeps_cross_project_duplicates_apart():
    notes = [
        _note(
            "wiki-0001.md",
            title="release train status",
            body="Release train status updated after cargo test and guard passed.",
            embedding=[1.0, 0.0],
            mtime=2.0,
            project="app-alpha",
            claims=[
                {
                    "subject": "release train",
                    "predicate": "status",
                    "value": "guard passed",
                }
            ],
        ),
        _note(
            "wiki-0002.md",
            title="release train status",
            body="Release train status updated after cargo test and guard passed.",
            embedding=[1.0, 0.0],
            mtime=1.0,
            project="app-beta",
            claims=[
                {
                    "subject": "release-train",
                    "predicate": "status",
                    "value": "guard passed",
                }
            ],
        ),
    ]

    assert not dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.cluster_notes(notes, 0.93) == []


def test_cluster_notes_keeps_cross_origin_duplicates_apart():
    notes = [
        _note(
            "wiki-0001.md",
            title="release train status",
            body="Release train status updated after cargo test and guard passed.",
            embedding=[1.0, 0.0],
            mtime=2.0,
            project="shared-release",
            origin="company",
            claims=[
                {
                    "subject": "release train",
                    "predicate": "status",
                    "value": "guard passed",
                }
            ],
        ),
        _note(
            "wiki-0002.md",
            title="release train status",
            body="Release train status updated after cargo test and guard passed.",
            embedding=[1.0, 0.0],
            mtime=1.0,
            project="shared-release",
            origin="personal",
            claims=[
                {
                    "subject": "release-train",
                    "predicate": "status",
                    "value": "guard passed",
                }
            ],
        ),
    ]

    assert not dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.cluster_notes(notes, 0.93) == []


def test_cluster_notes_same_session_overrides_project_mismatch():
    notes = [
        _note(
            "wiki-0001.md",
            title="session rewrite",
            body="Richer corrected version.",
            embedding=[1.0, 0.0],
            mtime=2.0,
            project="app-alpha",
            origin="company",
            session_id="session-a",
        ),
        _note(
            "wiki-0002.md",
            title="session rewrite draft",
            body="Earlier draft.",
            embedding=[1.0, 0.0],
            mtime=1.0,
            project="app-beta",
            origin="personal",
            session_id="session-a",
        ),
    ]

    assert dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.cluster_notes(notes, 0.93) == [[0, 1]]


def test_cluster_notes_keeps_conflicting_claim_axis_state_change():
    notes = [
        _note(
            "wiki-0001.md",
            title="release train status",
            body="Updated release train status after follow-up tests.",
            embedding=[1.0, 0.0],
            mtime=2.0,
            claims=[
                {
                    "subject": "release train",
                    "predicate": "status",
                    "value": "follow-up tests done",
                }
            ],
        ),
        _note(
            "wiki-0002.md",
            title="release train status",
            body="Updated release train status after follow-up tests were pending.",
            embedding=[1.0, 0.0],
            mtime=1.0,
            claims=[
                {
                    "subject": "release-train",
                    "predicate": "status",
                    "value": "follow-up tests pending",
                }
            ],
        ),
    ]

    assert dedup.claim_axis_value_conflict(notes[0]["claims"], notes[1]["claims"])
    assert not dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.cluster_notes(notes, 0.93) == []


def test_cluster_notes_same_session_overrides_claim_value_conflict():
    notes = [
        _note(
            "wiki-0001.md",
            title="session rewrite",
            body="Richer corrected version.",
            embedding=[1.0, 0.0],
            mtime=2.0,
            session_id="session-a",
            claims=[
                {
                    "subject": "release train",
                    "predicate": "status",
                    "value": "follow-up tests done",
                }
            ],
        ),
        _note(
            "wiki-0002.md",
            title="session rewrite draft",
            body="Earlier draft.",
            embedding=[1.0, 0.0],
            mtime=1.0,
            session_id="session-a",
            claims=[
                {
                    "subject": "release-train",
                    "predicate": "status",
                    "value": "follow-up tests pending",
                }
            ],
        ),
    ]

    assert dedup.claim_axis_value_conflict(notes[0]["claims"], notes[1]["claims"])
    assert dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.cluster_notes(notes, 0.93) == [[0, 1]]


def test_cluster_notes_accepts_same_claim_axis_and_value():
    notes = [
        _note(
            "wiki-0001.md",
            title="Provider note",
            body="Local model route selected.",
            embedding=[1.0, 0.0],
            mtime=2.0,
            claims=[
                {
                    "subject": "oh-my-boring",
                    "predicate": "llm provider",
                    "value": "LM Studio local server",
                }
            ],
        ),
        _note(
            "wiki-0002.md",
            title="Routing memo",
            body="Embedding setup verified.",
            embedding=[1.0, 0.0],
            mtime=1.0,
            claims=[
                {
                    "subject": "OH my boring",
                    "predicate": "LLM-provider",
                    "value": "lmstudio local server",
                }
            ],
        ),
    ]

    assert dedup.cluster_notes(notes, 0.93) == [[0, 1]]


def test_cluster_notes_does_not_archive_through_transitive_chain():
    notes = [
        _note(
            "wiki-0001.md",
            title="release alpha",
            body="note one",
            embedding=[1.0, 0.0],
            mtime=3.0,
        ),
        _note(
            "wiki-0002.md",
            title="release alpha beta",
            body="note two",
            embedding=[1.0, 0.0],
            mtime=2.0,
        ),
        _note(
            "wiki-0003.md",
            title="release beta",
            body="note three",
            embedding=[1.0, 0.0],
            mtime=1.0,
        ),
    ]

    assert dedup.has_duplicate_evidence(notes[0], notes[1])
    assert dedup.has_duplicate_evidence(notes[1], notes[2])
    assert not dedup.has_duplicate_evidence(notes[0], notes[2])
    assert dedup.cluster_notes(notes, 0.93) == [[0, 1]]


def test_archive_destination_conflicts_rejects_existing_destination():
    with tempfile.TemporaryDirectory() as tmp:
        archive_dir = Path(tmp) / "archive"
        archive_dir.mkdir()
        existing = archive_dir / "wiki-0001.md"
        existing.write_text("archived", encoding="utf-8")

        conflicts = dedup.archive_destination_conflicts(
            [(Path("wiki-0001.md"), Path("wiki-0002.md"))],
            archive_dir,
        )

    assert conflicts == [existing]


def test_archive_destination_conflicts_rejects_repeated_destination():
    archive_dir = Path("archive")

    conflicts = dedup.archive_destination_conflicts(
        [
            (Path("a/wiki-0001.md"), Path("a/wiki-0002.md")),
            (Path("b/wiki-0001.md"), Path("b/wiki-0003.md")),
        ],
        archive_dir,
    )

    assert conflicts == [archive_dir / "wiki-0001.md"]


def test_confirm_apply_accepts_yes_flag_without_prompt():
    called = []

    assert dedup.confirm_apply(True, lambda _prompt: called.append("prompt") or "n")
    assert called == []


def test_confirm_apply_accepts_prompt_yes():
    with redirect_stdout(io.StringIO()):
        ok = dedup.confirm_apply(False, lambda _prompt: "yes")

    assert ok


def test_confirm_apply_rejects_prompt_default():
    stdout = io.StringIO()

    with redirect_stdout(stdout):
        ok = dedup.confirm_apply(False, lambda _prompt: "")

    assert not ok
    assert "aborted" in stdout.getvalue()


def _note(
    name,
    title="Duplicate gate",
    body="Evidence for duplicate cleanup behavior.",
    claims=None,
    embedding=None,
    mtime=0.0,
    session_id="",
    project="",
    origin="",
    tags=None,
):
    note = {
        "path": Path(name),
        "title": title,
        "body": body,
        "claims": claims or [],
        "omb_session_id": session_id,
        "project": project,
        "origin": origin,
        "tags": tags or [],
        "mtime": mtime,
    }
    if embedding is not None:
        note["embedding"] = embedding
    return note


def _value_error_from(fn):
    try:
        fn()
    except ValueError as e:
        return e
    raise AssertionError("expected ValueError")


def _argparse_error_from(fn):
    try:
        fn()
    except dedup.argparse.ArgumentTypeError as e:
        return e
    raise AssertionError("expected ArgumentTypeError")


if __name__ == "__main__":
    test_embed_notes_writes_embeddings_for_all_notes()
    test_embed_notes_aborts_without_zero_vector_on_failure()
    test_parse_similarity_threshold_accepts_closed_unit_interval()
    test_parse_similarity_threshold_rejects_invalid_policy_values()
    test_parse_note_rejects_malformed_frontmatter()
    test_parse_note_rejects_non_mapping_frontmatter()
    test_parse_note_rejects_non_list_claims()
    test_parse_note_rejects_non_string_session_id()
    test_parse_note_rejects_non_string_project()
    test_parse_note_rejects_non_string_origin()
    test_parse_note_rejects_non_list_tags()
    test_parse_note_rejects_non_string_tag()
    test_parse_note_without_frontmatter_derives_project_from_path()
    test_parse_note_derives_blank_project_from_path()
    test_parse_note_keeps_session_id_as_duplicate_evidence()
    test_parse_note_keeps_tags_for_source_memory_filter()
    test_source_memory_candidates_exclude_generated_and_eval_notes()
    test_newest_source_note_path_uses_source_memory_filter()
    test_cluster_notes_requires_duplicate_evidence_beyond_embedding()
    test_cluster_notes_rejects_single_shared_title_token()
    test_cluster_notes_accepts_same_session_id_as_evidence()
    test_cluster_notes_keeps_cross_project_duplicates_apart()
    test_cluster_notes_keeps_cross_origin_duplicates_apart()
    test_cluster_notes_same_session_overrides_project_mismatch()
    test_cluster_notes_keeps_conflicting_claim_axis_state_change()
    test_cluster_notes_same_session_overrides_claim_value_conflict()
    test_cluster_notes_accepts_same_claim_axis_and_value()
    test_cluster_notes_does_not_archive_through_transitive_chain()
    test_archive_destination_conflicts_rejects_existing_destination()
    test_archive_destination_conflicts_rejects_repeated_destination()
    test_confirm_apply_accepts_yes_flag_without_prompt()
    test_confirm_apply_accepts_prompt_yes()
    test_confirm_apply_rejects_prompt_default()
    print("ok - dedup wiki")
