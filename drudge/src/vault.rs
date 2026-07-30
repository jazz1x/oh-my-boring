//! Vault — the personal Obsidian markdown KB and its integrity/ingest helpers.
//!
//! Cross-reference: ENFORCEMENT.md §A (ADT) · design decision D7 (vault/wiki SSOT, DB rebuildable).
//!
//! # Module layout
//! - `mod.rs`: shared ADTs, schema parsing, frontmatter helpers, wikilink utilities.
//! - `remember.rs`: note rendering, id allocation, body normalization.
//! - `audit.rs`: lint + graph audit.
//! - `projection.rs`: graph → Obsidian `relates_to` projection.
//!
//! # Design principles
//! - **PDV**: parse `schema.yaml` and `.md` frontmatter into a typed form once at the boundary.
//! - **ROP**: `?` propagation + anyhow Context. No unwrap/expect/panic.
//! - **ADT**: `Kind`, `Origin`, `Severity` as enums. Make impossible states unrepresentable.
//! - **SRP**: separate pure logic (parse/graph) from I/O (file reading).

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use anyhow::{Context, Result};
use serde::Deserialize;

pub mod audit;
pub mod projection;
pub mod remember;

pub use audit::{audit_pages, lint_page, run_audit, run_lint};
pub use projection::{project_links, project_note};
pub use remember::{
    allocate_wiki_path, normalize_body, persist_new_wiki_note, render_wiki_note, sanitize_tag,
    today_utc,
};

static ATOMIC_WRITE_COUNTER: AtomicU64 = AtomicU64::new(0);

fn atomic_temp_path(path: &Path) -> std::io::Result<PathBuf> {
    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("atomic write target has no parent: {}", path.display()),
        )
    })?;
    let name = path.file_name().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("atomic write target has no file name: {}", path.display()),
        )
    })?;
    let seq = ATOMIC_WRITE_COUNTER.fetch_add(1, Ordering::Relaxed);
    Ok(parent.join(format!(
        ".{}.omb-write-{}-{seq}.tmp",
        name.to_string_lossy(),
        std::process::id()
    )))
}

fn write_atomic_temp(path: &Path, content: &[u8]) -> std::io::Result<PathBuf> {
    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("atomic write target has no parent: {}", path.display()),
        )
    })?;
    std::fs::create_dir_all(parent)?;
    loop {
        let tmp = atomic_temp_path(path)?;
        match std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&tmp)
        {
            Ok(mut file) => {
                file.write_all(content)?;
                file.sync_all()?;
                return Ok(tmp);
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e),
        }
    }
}

/// Replace a file through a same-directory temp file + rename so readers never observe a
/// truncate-then-write half state.
pub(crate) fn write_atomic(path: &Path, content: impl AsRef<[u8]>) -> Result<()> {
    let tmp = write_atomic_temp(path, content.as_ref())
        .with_context(|| format!("atomic write temp: {}", path.display()))?;
    if let Err(e) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(anyhow::Error::new(e).context(format!("atomic rename: {}", path.display())));
    }
    Ok(())
}

/// Publish a new vault file only if the final path is still absent. The final path appears only
/// after the complete temp file is linked into place; concurrent allocators get `AlreadyExists`.
pub(crate) fn write_new_atomic(path: &Path, content: impl AsRef<[u8]>) -> std::io::Result<()> {
    let tmp = write_atomic_temp(path, content.as_ref())?;
    match std::fs::hard_link(&tmp, path) {
        Ok(()) => {
            let _ = std::fs::remove_file(tmp);
            Ok(())
        }
        Err(e) => {
            let _ = std::fs::remove_file(tmp);
            Err(e)
        }
    }
}

// ─────────────────────────────────────────────────────────────
// ADT — make impossible states unrepresentable
// ─────────────────────────────────────────────────────────────

/// Allowed values for page kind.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Note,
    Memory,
    Session,
    Decision,
    /// `remember_code` code-context note linked to an AST symbol (`code_symbols` frontmatter).
    Code,
}

/// Allowed values for page origin.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Origin {
    Personal,
    Company,
    Mirror,
    Community,
}

/// Issue severity.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Error,
    Warn,
}

// ─────────────────────────────────────────────────────────────
// Domain types (typed evidence — PDV)
// ─────────────────────────────────────────────────────────────

/// A lint/audit result issue. A pure value decoupled from I/O.
#[derive(Debug, Clone)]
pub struct Issue {
    pub rule: &'static str,
    pub severity: Severity,
    pub target: String,
    pub message: String,
}

impl Issue {
    pub(crate) fn error(
        rule: &'static str,
        target: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            rule,
            severity: Severity::Error,
            target: target.into(),
            message: message.into(),
        }
    }

    pub(crate) fn warn(
        rule: &'static str,
        target: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            rule,
            severity: Severity::Warn,
            target: target.into(),
            message: message.into(),
        }
    }
}

// ─────────────────────────────────────────────────────────────
// Schema parsing (PDV: typed parse once at the boundary)
// ─────────────────────────────────────────────────────────────

/// Typed representation of `.rules/schema.yaml`.
#[derive(Debug, Deserialize)]
pub struct Schema {
    pub page_id: PageIdSchema,
    pub sources: SourcesSchema,
    pub required_frontmatter: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct PageIdSchema {
    pub pattern: String,
}

#[derive(Debug, Deserialize)]
pub struct SourcesSchema {
    pub allowed_prefixes: Vec<String>,
}

/// Read the schema from a file and parse it into a typed value. (I/O boundary)
pub fn load_schema(schema_path: &Path) -> Result<Schema> {
    let raw = std::fs::read_to_string(schema_path)
        .with_context(|| format!("failed to read schema file: {}", schema_path.display()))?;
    serde_yaml::from_str(&raw)
        .with_context(|| format!("failed to parse schema YAML: {}", schema_path.display()))
}

// ─────────────────────────────────────────────────────────────
// Frontmatter parsing (PDV: once at the boundary)
// ─────────────────────────────────────────────────────────────

/// wiki page frontmatter (optional fields consolidated via raw serde_yaml).
#[derive(Debug, Deserialize)]
pub struct RawFrontMatter {
    pub id: Option<String>,
    pub title: Option<String>,
    pub kind: Option<serde_yaml::Value>,
    #[serde(rename = "type")]
    pub okf_type: Option<String>,
    pub origin: Option<serde_yaml::Value>,
    pub date: Option<String>,
    pub timestamp: Option<String>,
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default)]
    pub relates_to: Vec<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    pub superseded_by: Option<String>,
    pub summary: Option<String>,
    pub description: Option<String>,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default)]
    pub contracts: Vec<String>,
    #[serde(default)]
    pub incidents: Vec<String>,
    pub okf_version: Option<String>,
}

/// A wiki page parsed into typed form at the boundary (PDV-complete state — no re-validation needed afterward).
#[derive(Debug, Clone)]
pub struct Page {
    pub id: String,
    #[allow(dead_code)]
    pub title: String,
    #[allow(dead_code)]
    pub kind: Kind,
    #[allow(dead_code)]
    pub origin: Origin,
    #[allow(dead_code)]
    pub date: String,
    #[allow(dead_code)]
    pub sources: Vec<String>,
    pub relates_to: Vec<String>,
    #[allow(dead_code)]
    pub tags: Vec<String>,
    pub superseded_by: Option<String>,
    pub body: String,
    #[allow(dead_code)]
    pub path: PathBuf,
}

/// Summary of the graph audit result.
#[derive(Debug)]
pub struct AuditSummary {
    pub page_count: usize,
    pub edge_count: usize,
    pub component_count: usize,
    pub component_sizes: Vec<usize>,
    pub orphan_count: usize,
    pub superseded_count: usize,
    pub issues: Vec<Issue>,
}

/// Split a `--- yaml ---\nbody` style .md file into (raw frontmatter YAML, body).
/// Pure function — &str input, no I/O.
pub(crate) fn split_frontmatter(content: &str) -> Option<(&str, &str)> {
    let rest = content.strip_prefix("---\n")?;
    let end = rest.find("\n---\n")?;
    let yaml = &rest[..end];
    let body = &rest[end + 5..];
    Some((yaml, body))
}

/// Best-effort autonomous repair of a malformed wiki note: quote unsafe scalar frontmatter so a note
/// YAML refuses to parse (the one bad note that would otherwise abort the whole sync) becomes valid.
/// Returns the repaired content if anything changed, else None. The caller MUST re-parse before writing
/// — this is a no-op on valid notes and never guarantees the result parses, only that it tried.
pub fn repair_note_frontmatter(content: &str) -> Option<String> {
    let (yaml, body) = split_frontmatter(content)?;
    let fixed = quote_unsafe_scalars(yaml);
    (fixed != yaml).then(|| format!("---\n{fixed}\n---\n{body}"))
}

/// Quote unsafe scalar frontmatter values so a note YAML can't parse becomes valid. The classic case:
/// an unquoted `title: [FEDEV-97] …` — YAML reads the leading `[` as a flow sequence and the whole sync
/// aborts. Touches ONLY the known scalar keys, only when the value is unquoted AND looks unsafe;
/// everything else (lists, body, already-quoted) is preserved verbatim. Pure.
pub(crate) fn quote_unsafe_scalars(yaml: &str) -> String {
    const SCALAR_KEYS: [&str; 6] = ["id", "title", "kind", "origin", "project", "date"];
    // A YAML plain scalar may not start with these indicators; `: ` / ` #` mid-value are also ambiguous.
    const LEAD: &[char] = &[
        '[', ']', '{', '}', ',', '&', '*', '#', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`',
    ];
    let mut out: Vec<String> = Vec::with_capacity(yaml.lines().count());
    for line in yaml.lines() {
        let fixed = SCALAR_KEYS.iter().find_map(|k| {
            let v = line.strip_prefix(&format!("{k}: "))?.trim();
            let unsafe_scalar = !v.is_empty()
                && !v.starts_with('"')
                && !v.starts_with('\'')
                && (v.starts_with(LEAD) || v.contains(": ") || v.contains(" #"));
            unsafe_scalar.then(|| {
                let esc = v.replace('\\', "\\\\").replace('"', "\\\"");
                format!("{k}: \"{esc}\"")
            })
        });
        out.push(fixed.unwrap_or_else(|| line.to_owned()));
    }
    out.join("\n")
}

/// raw frontmatter YAML string → `RawFrontMatter`. Pure function.
pub(crate) fn parse_raw_frontmatter(yaml: &str) -> Result<RawFrontMatter> {
    serde_yaml::from_str(yaml).context("failed to parse frontmatter YAML")
}

/// `/…/wiki-0002.md` → `Some("wiki-0002")`. None if not a wiki note. Pure.
pub(crate) fn wiki_stem(source_path: &str) -> Option<String> {
    let name = source_path.rsplit('/').next()?;
    let stem = name.strip_suffix(".md")?;
    stem.starts_with("wiki-").then(|| stem.to_owned())
}

/// Replace only the `relates_to:` block of the frontmatter YAML with a new link list (other keys preserved). Pure.
/// If the key is absent, append at the end. Empty links → `relates_to: []`.
pub(crate) fn set_relates_to(yaml: &str, links: &[String]) -> String {
    let render = |out: &mut Vec<String>| {
        if links.is_empty() {
            out.push("relates_to: []".to_owned());
        } else {
            out.push("relates_to:".to_owned());
            for l in links {
                out.push(format!("- {l}"));
            }
        }
    };
    let mut out: Vec<String> = Vec::new();
    let mut handled = false;
    let mut skip_list = false;
    for line in yaml.lines() {
        if skip_list {
            if line.trim_start().starts_with('-') {
                continue; // skip old relates_to list items
            }
            skip_list = false;
        }
        if !handled && line.starts_with("relates_to:") {
            render(&mut out);
            handled = true;
            skip_list = true;
            continue;
        }
        out.push(line.to_owned());
    }
    if !handled {
        render(&mut out);
    }
    out.join("\n")
}

/// The shipped seed note. Its `relates_to` is tracked-as-empty so a public clone carries no ids;
/// `project_links` must leave it alone (a graph projection would fill it with the user's PRIVATE note ids).
pub(crate) const SEED_NOTE_STEM: &str = "wiki-0000";

/// Whether a wiki note stem is the tracked seed note that `project_links` must skip. Pure.
pub(crate) fn is_seed_note(stem: &str) -> bool {
    stem == SEED_NOTE_STEM
}

/// `kind` YAML value → `Kind` enum. Pure function.
pub(crate) fn parse_kind(val: &serde_yaml::Value) -> Option<Kind> {
    match val.as_str()? {
        "note" => Some(Kind::Note),
        "memory" => Some(Kind::Memory),
        "session" => Some(Kind::Session),
        "decision" => Some(Kind::Decision),
        "code" => Some(Kind::Code),
        _ => None,
    }
}

/// `origin` YAML value → `Origin` enum. Pure function.
pub(crate) fn parse_origin(val: &serde_yaml::Value) -> Option<Origin> {
    match val.as_str()? {
        "personal" => Some(Origin::Personal),
        "company" => Some(Origin::Company),
        "mirror" => Some(Origin::Mirror),
        "community" => Some(Origin::Community),
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────
// Wikilink extraction (pure)
// ─────────────────────────────────────────────────────────────

/// Extract `[[wiki-NNNN]]` / `[[wiki-NNNN|alias]]` style wikilink target IDs from the body.
/// Pure function.
pub(crate) fn extract_wikilinks(body: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = body;
    while let Some(open) = rest.find("[[") {
        let after_open = &rest[open + 2..];
        let Some(close) = after_open.find("]]") else {
            break;
        };
        let inner = &after_open[..close];
        // strip alias: [[id|alias]] → id
        let target = inner.split('|').next().unwrap_or(inner).trim();
        out.push(target.to_owned());
        rest = &after_open[close + 2..];
    }
    out
}

/// Normalize a `relates_to` entry to a bare page id. `project_links` writes Obsidian-style
/// `"[[wiki-NNNN]]"` (quotes + brackets, optional `|alias`) so the graph view renders links, but
/// audit/lint compare against bare ids — strip the wrapper so both writers agree. Pure.
pub(crate) fn normalize_link_id(raw: &str) -> String {
    let s = raw.trim().trim_matches('"').trim();
    let s = s
        .strip_prefix("[[")
        .map_or(s, |r| r.strip_suffix("]]").unwrap_or(r));
    s.split('|').next().unwrap_or(s).trim().to_owned()
}

/// Check whether the body contains cross-layer links of the form `[[raw/...]]` / `[[meta/...]]` / `[[.rules/...]]`.
/// Pure function.
pub(crate) fn find_cross_layer_wikilinks(body: &str) -> Vec<String> {
    extract_wikilinks(body)
        .into_iter()
        .filter(|t| t.starts_with("raw/") || t.starts_with("meta/") || t.starts_with(".rules/"))
        .collect()
}

/// days since Unix epoch (1970-01-01) → "YYYY-MM-DD" string (pure function, no SystemTime).
pub(crate) fn days_to_date(days: i64) -> String {
    // Proleptic Gregorian calendar conversion (based on the Richards algorithm).
    // 1970-01-01 = JDN 2440588
    let jdn = days + 2_440_588_i64;
    let f = jdn + 1_401 + (((4 * jdn + 274_277) / 146_097) * 3) / 4 - 38;
    let e = 4 * f + 3;
    let g = (e % 1461) / 4;
    let h = 5 * g + 2;
    let day = (h % 153) / 5 + 1;
    let month = (h / 153 + 2) % 12 + 1;
    let year = e / 1461 - 4_716 + (14 - month) / 12;
    format!("{year:04}-{month:02}-{day:02}")
}

/// Best-effort OKF v0.1 field backfill for one wiki note. Returns Some(new_content) if changes
/// are needed, None if the note already conforms. Body is preserved verbatim.
pub fn migrate_okf_note(content: &str) -> Option<String> {
    let (yaml, body) = split_frontmatter(content)?;
    let mut value: serde_yaml::Value = serde_yaml::from_str(yaml).ok()?;
    let mapping = value.as_mapping_mut()?;

    let kind = mapping
        .get(serde_yaml::Value::String("kind".to_owned()))
        .and_then(|v| v.as_str())
        .unwrap_or("note")
        .to_owned();
    let date = mapping
        .get(serde_yaml::Value::String("date".to_owned()))
        .and_then(|v| v.as_str())
        .unwrap_or("1970-01-01")
        .to_owned();

    let mut changed = false;

    if !mapping.contains_key(serde_yaml::Value::String("type".to_owned())) {
        mapping.insert(
            serde_yaml::Value::String("type".to_owned()),
            serde_yaml::Value::String(kind.clone()),
        );
        changed = true;
    }

    let has_description = mapping.contains_key(serde_yaml::Value::String("description".to_owned()))
        || mapping.contains_key(serde_yaml::Value::String("summary".to_owned()));
    if !has_description {
        let first_line = body.trim().lines().next().unwrap_or("").trim();
        if !first_line.is_empty() {
            mapping.insert(
                serde_yaml::Value::String("description".to_owned()),
                serde_yaml::Value::String(first_line.chars().take(200).collect()),
            );
            changed = true;
        }
    }

    if !mapping.contains_key(serde_yaml::Value::String("timestamp".to_owned())) {
        mapping.insert(
            serde_yaml::Value::String("timestamp".to_owned()),
            serde_yaml::Value::String(format!("{date}T00:00:00Z")),
        );
        changed = true;
    }

    for field in ["skills", "contracts", "incidents"] {
        if !mapping.contains_key(serde_yaml::Value::String(field.to_owned())) {
            mapping.insert(
                serde_yaml::Value::String(field.to_owned()),
                serde_yaml::Value::Sequence(Vec::new()),
            );
            changed = true;
        }
    }

    if !mapping.contains_key(serde_yaml::Value::String("okf_version".to_owned())) {
        mapping.insert(
            serde_yaml::Value::String("okf_version".to_owned()),
            serde_yaml::Value::String("0.1".to_owned()),
        );
        changed = true;
    }

    if !changed {
        return None;
    }

    let new_yaml = serde_yaml::to_string(&value).ok()?;
    Some(format!("---\n{new_yaml}---\n{body}"))
}

/// Walk `vault/wiki` and backfill OKF/session-metadata fields on legacy notes.
/// Returns `(examined_count, changed_count)`. Changed files are backed up under
/// `<vault-root>/../data/backups/vault-migrate-okf-<epoch>/` before rewriting.
pub fn run_migrate_okf(vault_root: &Path, dry_run: bool) -> Result<(usize, usize)> {
    let wiki_dir = vault_root.join("wiki");
    if !wiki_dir.exists() {
        anyhow::bail!(
            "vault/wiki directory does not exist: {}",
            wiki_dir.display()
        );
    }

    let backup_dir = vault_root.parent().map_or_else(
        || vault_root.join("backups"),
        |p| p.join("data").join("backups"),
    );
    let backup_subdir = backup_dir.join(format!(
        "vault-migrate-okf-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs())
    ));

    let mut examined = 0_usize;
    let mut changed = 0_usize;

    let entries = std::fs::read_dir(&wiki_dir)
        .with_context(|| format!("failed to read wiki dir: {}", wiki_dir.display()))?;
    for entry in entries {
        let entry = entry.context("failed to read wiki directory entry")?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if name == "index.md" || name == "log.md" || name.starts_with("daily-brief-") {
            continue;
        }
        examined += 1;

        let content = std::fs::read_to_string(&path)
            .with_context(|| format!("failed to read file: {}", path.display()))?;
        let Some(new_content) = migrate_okf_note(&content) else {
            continue;
        };

        println!(
            "{}: {}",
            if dry_run {
                "would migrate"
            } else {
                "migrating"
            },
            path.display()
        );
        changed += 1;

        if dry_run {
            continue;
        }

        std::fs::create_dir_all(&backup_subdir)
            .with_context(|| format!("failed to create backup dir: {}", backup_subdir.display()))?;
        let backup_path = backup_subdir.join(name);
        std::fs::copy(&path, &backup_path).with_context(|| {
            format!(
                "failed to back up {} to {}",
                path.display(),
                backup_path.display()
            )
        })?;
        write_atomic(&path, new_content)
            .with_context(|| format!("failed to write migrated note: {}", path.display()))?;
    }

    Ok((examined, changed))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::{
        days_to_date, is_seed_note, migrate_okf_note, repair_note_frontmatter, split_frontmatter,
        write_atomic, write_new_atomic,
    };

    // ── split_frontmatter ──

    #[test]
    fn split_frontmatter_basic() {
        let content = "---\nid: wiki-0001\n---\n# body\n";
        let (yaml, body) = split_frontmatter(content).unwrap();
        assert_eq!(yaml, "id: wiki-0001");
        assert_eq!(body, "# body\n");
    }

    #[test]
    fn split_frontmatter_none_on_missing_delimiter() {
        assert!(split_frontmatter("no frontmatter").is_none());
    }

    // ── repair_note_frontmatter (autonomous post-correction of malformed scalars) ──

    #[test]
    fn repair_quotes_unsafe_scalar_title() {
        let bad = "---\nid: wiki-0124\ntitle: [FEDEV-97] Hydration 해결\nkind: note\n---\n# body\n";
        let fixed = repair_note_frontmatter(bad).expect("should repair the unsafe title");
        assert!(
            fixed.contains("title: \"[FEDEV-97] Hydration 해결\""),
            "title not quoted: {fixed}"
        );
        assert!(
            fixed.contains("id: wiki-0124"),
            "other lines must be preserved"
        );
        assert!(fixed.contains("# body"), "body must be preserved");
    }

    #[test]
    fn repair_is_noop_on_clean_note() {
        let good =
            "---\nid: wiki-1\ntitle: A normal title\nkind: note\norigin: personal\n---\n# body\n";
        assert!(
            repair_note_frontmatter(good).is_none(),
            "a well-formed note must not be rewritten"
        );
    }

    // ── is_seed_note (project_links must skip the tracked seed so private ids don't leak) ──

    #[test]
    fn seed_note_is_skipped_by_link_projection() {
        assert!(is_seed_note("wiki-0000"));
        assert!(!is_seed_note("wiki-0042"));
        assert!(!is_seed_note("wiki-00000"));
        assert!(!is_seed_note("wiki-0000-draft"));
    }

    // ── atomic vault writes ──

    #[test]
    fn write_atomic_replaces_existing_file_without_temp_leftover() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("wiki-0001.md");
        std::fs::write(&path, "old").unwrap();

        write_atomic(&path, "new").unwrap();

        assert_eq!(std::fs::read_to_string(&path).unwrap(), "new");
        let leftovers: Vec<_> = std::fs::read_dir(tmp.path())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().contains(".omb-write-"))
            .collect();
        assert!(leftovers.is_empty(), "temp leftovers: {leftovers:?}");
    }

    #[test]
    fn write_new_atomic_never_overwrites_existing_file() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("wiki-0001.md");
        std::fs::write(&path, "old").unwrap();

        let err = write_new_atomic(&path, "new").unwrap_err();

        assert_eq!(err.kind(), std::io::ErrorKind::AlreadyExists);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "old");
    }

    // ── days_to_date ──

    #[test]
    fn days_to_date_epoch() {
        assert_eq!(days_to_date(0), "1970-01-01");
    }

    #[test]
    fn days_to_date_known_date() {
        assert_eq!(days_to_date(10_957), "2000-01-01");
    }

    // ── migrate_okf_note ──

    #[test]
    fn migrate_okf_note_adds_okf_and_session_fields() {
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n## Background\nBody line.";
        let migrated = migrate_okf_note(content).expect("should migrate legacy note");
        assert!(migrated.contains("type: note"), "{migrated}");
        assert!(
            migrated.contains("timestamp: 2026-01-01T00:00:00Z"),
            "{migrated}"
        );
        assert!(
            migrated.contains("description: '## Background'"),
            "{migrated}"
        );
        assert!(migrated.contains("skills: []"), "{migrated}");
        assert!(migrated.contains("contracts: []"), "{migrated}");
        assert!(migrated.contains("incidents: []"), "{migrated}");
        assert!(migrated.contains("okf_version: '0.1'"), "{migrated}");
    }

    #[test]
    fn migrate_okf_note_is_noop_when_conformant() {
        let content = "---\nid: wiki-0001\ntitle: Test\ntype: note\nkind: note\norigin: personal\ndate: \"2026-01-01\"\ntimestamp: 2026-01-01T00:00:00Z\ndescription: summary\nskills: []\ncontracts: []\nincidents: []\nokf_version: \"0.1\"\n---\nbody";
        assert!(
            migrate_okf_note(content).is_none(),
            "conformant note should not be rewritten"
        );
    }

    #[test]
    fn migrate_okf_note_preserves_existing_summary() {
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsummary: existing summary\n---\nbody";
        let migrated = migrate_okf_note(content).expect("should migrate");
        assert!(
            !migrated.contains("description:"),
            "summary present → do not add description: {migrated}"
        );
        assert!(migrated.contains("okf_version:"), "{migrated}");
    }
}
