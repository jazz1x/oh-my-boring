#!/usr/bin/env python3
"""Network-free regression tests for vault-cleanup-gate.py."""
from __future__ import annotations

import argparse
import importlib.util
import tarfile
import tempfile
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("vault_cleanup_gate", str(HERE / "vault-cleanup-gate.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class _FakeSteward:
    FIXABLE_ISSUE_KINDS = {"placeholder_tag"}

    @staticmethod
    def fixable_note_names(report: dict) -> list[str]:
        return list(report.get("fixable", []))


def _write_note(wiki: Path, name: str, frontmatter: str, body: str = "body.\n") -> Path:
    path = wiki / name
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _base_report(wiki: Path) -> dict:
    return {
        "wiki_dir": str(wiki),
        "note_count": 1,
        "note_issues": {},
        "claim_issues": [],
        "fixable": [],
    }


def _args(root: Path, fix: bool) -> argparse.Namespace:
    return argparse.Namespace(
        check=not fix,
        fix=fix,
        vault=str(root / "vault"),
        backup_dir=str(root / "backups"),
        report=str(root / "report.md"),
    )


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def test_create_backup_publishes_complete_fsynced_archive_without_temp_leftover():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        _write_note(wiki, "wiki-0001.md", "id: wiki-0001\ntitle: t\nkind: note\norigin: personal\n")
        seen_fsyncs = []
        real_fsync = gate.os.fsync
        gate.os.fsync = lambda fd: seen_fsyncs.append(fd)
        try:
            backup = gate._create_backup(wiki, root / "backups")
        finally:
            gate.os.fsync = real_fsync

        assert seen_fsyncs
        assert backup.exists()
        with tarfile.open(backup, "r:gz") as tar:
            names = tar.getnames()
        assert "wiki/wiki-0001.md" in names
        assert "manifest.json" in names
        assert not list((root / "backups").glob(".vault-wiki-*.tmp-*"))


def test_create_backup_preserves_existing_backup_on_publish_failure():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        backup_dir = root / "backups"
        wiki.mkdir(parents=True)
        backup_dir.mkdir()
        _write_note(wiki, "wiki-0001.md", "id: wiki-0001\ntitle: t\nkind: note\norigin: personal\n")
        stamp = "20260102T030405Z"
        backup = backup_dir / f"vault-wiki-{stamp}.tar.gz"
        backup.write_bytes(b"old-backup")
        real_stamp = gate._stamp
        real_replace = gate.os.replace
        gate._stamp = lambda: stamp
        gate.os.replace = lambda src, dst: (_ for _ in ()).throw(OSError("denied"))
        try:
            _assert_raises(OSError, gate._create_backup, wiki, backup_dir)
        finally:
            gate._stamp = real_stamp
            gate.os.replace = real_replace

        assert backup.read_bytes() == b"old-backup"
        assert not list(backup_dir.glob(".vault-wiki-*.tmp-*"))


def test_write_report_uses_atomic_replace_without_temp_leftover():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        report = root / "reports" / "cleanup.md"
        seen_fsyncs = []
        real_fsync = gate.os.fsync
        gate.os.fsync = lambda fd: seen_fsyncs.append(fd)
        try:
            gate._write_report(
                report,
                mode="check",
                status="ok",
                backup=None,
                before=_base_report(wiki),
                after=_base_report(wiki),
                fixed=[],
                issues=[],
                data_steward=_FakeSteward(),
            )
        finally:
            gate.os.fsync = real_fsync

        text = report.read_text(encoding="utf-8")
        assert seen_fsyncs
        assert "status: `ok`" in text
        assert "backup: `not-created`" in text
        assert not list((root / "reports").glob(".cleanup.md.tmp-*"))


def test_write_report_preserves_existing_report_on_publish_failure():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        report = root / "report.md"
        report.write_text("old report\n", encoding="utf-8")
        real_replace = gate.os.replace
        gate.os.replace = lambda src, dst: (_ for _ in ()).throw(OSError("denied"))
        try:
            _assert_raises(
                OSError,
                gate._write_report,
                report,
                mode="check",
                status="failed",
                backup=None,
                before=_base_report(wiki),
                after=_base_report(wiki),
                fixed=[],
                issues=["boom"],
                data_steward=_FakeSteward(),
            )
        finally:
            gate.os.replace = real_replace

        assert report.read_text(encoding="utf-8") == "old report\n"
        assert not list(root.glob(".report.md.tmp-*"))


def test_fix_creates_backup_and_clears_fixable_issues():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        _write_note(
            wiki,
            "wiki-0001.md",
            "id: wiki-0001\ntitle: t\nkind: session\norigin: personal\n"
            "project: marketboro/omb\n"
            "tags:\n- repo/marketboro/omb\n- _\n"
            "omb_session_id: s-1\n"
            "claims:\n"
            "- {subject: omb, predicate: status, value: remembered, kind: fact, confidence: certain}\n"
            "- {subject: omb, predicate: decision, value: cleanup gate, kind: decision, confidence: certain}\n",
        )

        rc = gate.run(_args(root, fix=True))

        assert rc == 0
        backups = sorted((root / "backups").glob("vault-wiki-*.tar.gz"))
        assert len(backups) == 1
        with tarfile.open(backups[0], "r:gz") as tar:
            assert "wiki/wiki-0001.md" in tar.getnames()
            assert "manifest.json" in tar.getnames()
        text = (wiki / "wiki-0001.md").read_text(encoding="utf-8")
        fm = yaml.safe_load(text[4 : text.find("\n---\n")])
        assert fm["project"] == "omb"
        assert "_" not in fm["tags"]
        assert "repo/omb" in fm["tags"]
        assert (wiki / "wiki-0001.md.bak").exists()
        report = (root / "report.md").read_text(encoding="utf-8")
        assert "status: `ok`" in report


def test_check_fails_when_fixable_issues_remain():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        _write_note(
            wiki,
            "wiki-0001.md",
            "id: wiki-0001\ntitle: t\nkind: note\norigin: personal\n"
            "project: marketboro/omb\ntags: [_]\n",
        )

        rc = gate.run(_args(root, fix=False))

        assert rc == 1
        assert not (root / "backups").exists()
        report = (root / "report.md").read_text(encoding="utf-8")
        assert "fixable steward issues remain" in report


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"\nOK: {len(fns)} vault-cleanup gate tests passed.")


if __name__ == "__main__":
    main()
