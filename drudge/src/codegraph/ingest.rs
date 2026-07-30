//! Code-ingest directory walk — finds Rust/Python sources, parses them, aggregates stats.
//!
//! Cross-reference: mod.rs (contract) · crate::ingest (wiki lane — a separate one-way flow).
//! Phase 2 scope is parse-only: the returned stats carry the symbols/relations for the
//! Phase 3 store upsert; nothing is written to Postgres here (ENFORCEMENT.md §B one-way flow).
use std::path::{Path, PathBuf};

use anyhow::Result;
use walkdir::WalkDir;

use super::parser::parse_source;
use super::{CodeIndexConfig, CodeIndexStats, CodeLanguage};

/// Walk `root` for indexable source files (`.rs`, `.py`, `.pyi`), parse each with tree-sitter,
/// and aggregate the outcome. Deterministic: files are processed in sorted-path order.
///
/// Honors `config.exclude_paths` and, when `<root>/.gitignore` exists, its non-glob lines
/// (bare names match any path component; `a/b` paths are root-relative prefixes; glob lines
/// with `*`/`?`/`[` and `!` negations are skipped — full gitignore semantics would need the
/// `ignore` crate, deliberately not pulled in for this).
///
/// Graceful boundaries: entries the walker cannot read count into `files_skipped`, per-file
/// read/parse failures count into `parse_errors` — neither aborts the walk.
pub fn walk_directory(root: &Path, config: &CodeIndexConfig) -> Result<CodeIndexStats> {
    let mut stats = CodeIndexStats::default();
    if !config.enabled {
        return Ok(stats); // master switch off — code indexing skipped entirely
    }
    let excludes = Excludes::load(root, &config.exclude_paths);
    let mut files = collect_files(root, config, &excludes, &mut stats);
    files.sort_by(|a, b| a.0.cmp(&b.0));
    for (path, lang) in files {
        let rel = path.strip_prefix(root).unwrap_or(&path);
        // Symbols are labeled root-relative (`/` separators) so node ids are machine-stable.
        let display = rel.to_string_lossy().replace('\\', "/");
        let Ok(source) = std::fs::read_to_string(&path) else {
            stats.parse_errors += 1;
            continue;
        };
        match parse_source(&source, &display, lang, config.max_symbols_per_file) {
            Ok((symbols, relations)) => {
                stats.files_parsed += 1;
                stats.symbols_upserted += symbols.len();
                stats.relations_upserted += relations.len();
                stats.symbols.extend(symbols);
                stats.relations.extend(relations);
            }
            Err(_) => stats.parse_errors += 1,
        }
    }
    Ok(stats)
}

/// Collect candidate files (extension + language filter, exclude rules applied).
/// `files_skipped` counts extension-matching files whose language is disabled and
/// directory entries the walker could not read.
fn collect_files(
    root: &Path,
    config: &CodeIndexConfig,
    excludes: &Excludes,
    stats: &mut CodeIndexStats,
) -> Vec<(PathBuf, CodeLanguage)> {
    let mut files = Vec::new();
    let mut iter = WalkDir::new(root).into_iter();
    while let Some(entry) = iter.next() {
        let Ok(entry) = entry else {
            stats.files_skipped += 1;
            continue;
        };
        let path = entry.path();
        let rel = path.strip_prefix(root).unwrap_or(path);
        if entry.file_type().is_dir() {
            if rel != Path::new("") && excludes.is_excluded(rel) {
                iter.skip_current_dir();
            }
            continue;
        }
        if !entry.file_type().is_file() || excludes.is_excluded(rel) {
            continue;
        }
        let Some(lang) = path
            .extension()
            .and_then(|e| e.to_str())
            .and_then(CodeLanguage::from_extension)
        else {
            continue;
        };
        if !config.languages.is_empty()
            && !config
                .languages
                .iter()
                .any(|l| l.eq_ignore_ascii_case(lang.as_str()))
        {
            stats.files_skipped += 1;
            continue;
        }
        files.push((path.to_path_buf(), lang));
    }
    files
}

/// Simple path exclusion. Entries without `/` match any single path component (`target`
/// excludes `crates/foo/target/...`); entries with `/` are root-relative prefixes
/// (`src/gen` excludes only `<root>/src/gen/...`). Used for both `exclude_paths` and the
/// non-glob lines of `<root>/.gitignore` (bare gitignore names also match at any depth,
/// so the two rule sets share one matcher).
struct Excludes {
    entries: Vec<String>,
}

impl Excludes {
    fn load(root: &Path, config_excludes: &[String]) -> Self {
        let mut entries: Vec<String> = config_excludes
            .iter()
            .map(|e| e.trim_matches('/').to_owned())
            .filter(|e| !e.is_empty())
            .collect();
        if let Ok(raw) = std::fs::read_to_string(root.join(".gitignore")) {
            for line in raw.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') || line.starts_with('!') {
                    continue;
                }
                let pat = line.trim_matches('/');
                if pat.is_empty() || pat.contains(['*', '?', '[']) {
                    continue; // glob semantics are out of scope for the simple matcher
                }
                entries.push(pat.to_owned());
            }
        }
        entries.sort_unstable();
        entries.dedup();
        Self { entries }
    }

    fn is_excluded(&self, rel: &Path) -> bool {
        let rel_str = rel.to_string_lossy();
        self.entries.iter().any(|entry| {
            if entry.contains('/') {
                rel_str == entry.as_str()
                    || (rel_str.starts_with(entry.as_str())
                        && rel_str.as_bytes().get(entry.len()) == Some(&b'/'))
            } else {
                rel.components().any(|c| c.as_os_str() == entry.as_str())
            }
        })
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;
    use crate::codegraph::{CodeRelationKind, CodeSymbolKind};

    fn config() -> CodeIndexConfig {
        CodeIndexConfig {
            enabled: true,
            ..Default::default()
        }
    }

    fn write(dir: &Path, rel: &str, content: &str) {
        let path = dir.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
    }

    #[test]
    fn walks_sources_and_skips_excludes_and_gitignore() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, "src/lib.rs", "fn core() {}\n");
        write(root, "scripts/tool.py", "def run():\n    pass\n");
        write(root, "target/build.rs", "fn generated() {}\n");
        write(root, "ignored/secret.py", "def nope():\n    pass\n");
        write(root, "README.md", "# not code\n");
        std::fs::write(root.join(".gitignore"), "ignored/\n*.log\n").unwrap();

        let stats = walk_directory(root, &config()).unwrap();
        assert_eq!(stats.files_parsed, 2);
        assert_eq!(stats.parse_errors, 0);
        let mut names: Vec<&str> = stats.symbols.iter().map(|s| s.name.as_str()).collect();
        names.sort_unstable();
        assert_eq!(names, ["core", "run"]);
        // source_path is root-relative with `/` separators
        assert!(
            stats
                .symbols
                .iter()
                .any(|s| s.source_path == "src/lib.rs" && s.kind == CodeSymbolKind::Function)
        );
        assert_eq!(stats.symbols_upserted, stats.symbols.len());
        assert_eq!(stats.relations_upserted, stats.relations.len());
    }

    #[test]
    fn disabled_config_is_a_noop() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "a.rs", "fn x() {}\n");
        let stats = walk_directory(dir.path(), &CodeIndexConfig::default()).unwrap();
        assert_eq!(stats, CodeIndexStats::default());
    }

    #[test]
    fn language_filter_skips_other_sources() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "a.rs", "fn x() {}\n");
        write(dir.path(), "b.py", "def y():\n    pass\n");
        let cfg = CodeIndexConfig {
            languages: vec!["python".to_owned()],
            ..config()
        };
        let stats = walk_directory(dir.path(), &cfg).unwrap();
        assert_eq!(stats.files_parsed, 1);
        assert_eq!(stats.files_skipped, 1);
        assert!(
            stats
                .symbols
                .iter()
                .all(|s| s.language == CodeLanguage::Python)
        );
    }

    #[test]
    fn nested_target_dir_is_excluded_by_bare_name() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "crates/foo/target/gen.rs", "fn gen() {}\n");
        write(dir.path(), "crates/foo/src/lib.rs", "fn real() {}\n");
        let stats = walk_directory(dir.path(), &config()).unwrap();
        assert_eq!(stats.files_parsed, 1);
        assert_eq!(stats.symbols[0].name, "real");
    }

    #[test]
    fn relations_are_carried_in_stats_for_phase3() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "a.py", "def caller():\n    helper()\n");
        let stats = walk_directory(dir.path(), &config()).unwrap();
        assert!(
            stats
                .relations
                .iter()
                .any(|r| r.kind == CodeRelationKind::Calls && r.from.name == "caller")
        );
    }

    #[test]
    fn unparseable_extension_files_are_just_not_matched() {
        let dir = tempfile::tempdir().unwrap();
        write(dir.path(), "notes.txt", "fn x() {}\n");
        let stats = walk_directory(dir.path(), &config()).unwrap();
        assert_eq!(stats, CodeIndexStats::default());
    }
}
