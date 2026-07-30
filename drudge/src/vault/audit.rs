//! Vault lint / audit — integrity checks for the personal Obsidian markdown KB.
//!
//! Cross-reference: ENFORCEMENT.md §A (ADT) · design decision D7 (vault/wiki SSOT, DB rebuildable).
//!
//! # Design principles (PRINCIPLES.md)
//! - **PDV**: parse `schema.yaml` and `.md` frontmatter into a typed form once at the boundary.
//! - **ROP**: `?` propagation + anyhow Context. No unwrap/expect/panic.
//! - **ADT**: `Kind`, `Origin`, `Severity` as enums. Make impossible states unrepresentable.
//! - **SRP**: separate pure logic (parse/graph) from I/O (file reading).
//!   - `lint_*`, `audit_*`: pure — &str/slice input → value
//!   - `run_lint` / `run_audit`: I/O shell — collect files then delegate to pure functions

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Component, Path, PathBuf};

use anyhow::Context;
use sha2::{Digest, Sha256};

use super::AuditSummary;
use crate::vault::{
    Issue, Page, RawFrontMatter, Schema, Severity, extract_wikilinks, find_cross_layer_wikilinks,
    load_schema, normalize_link_id, parse_kind, parse_origin, parse_raw_frontmatter,
    split_frontmatter,
};

const RAW_WITNESS_PREFIX: &str = "raw-witness/";

#[derive(Debug)]
struct SourceRoots {
    raw_witness: PathBuf,
}

impl SourceRoots {
    fn default_for(vault_root: &Path) -> Self {
        Self {
            raw_witness: default_raw_witness_root(vault_root),
        }
    }

    fn from_env(vault_root: &Path) -> Self {
        Self {
            raw_witness: std::env::var_os("BORING_RAW_WITNESS_DIR")
                .map_or_else(|| default_raw_witness_root(vault_root), PathBuf::from),
        }
    }
}

// ─────────────────────────────────────────────────────────────
// Lint — pure sub-functions (SRP: each check as a separate function)
// ─────────────────────────────────────────────────────────────

/// Check OKF compatibility fields. Pure.
fn check_okf_fields(raw_fm: &RawFrontMatter, stem: &str, issues: &mut Vec<Issue>) {
    // OKF v0.1 requires `type`. omb's legacy `kind` maps to OKF `type`; tolerate legacy notes
    // but warn so migration can add the explicit field.
    let has_type = raw_fm
        .okf_type
        .as_ref()
        .is_some_and(|t| !t.trim().is_empty());
    let has_kind = raw_fm.kind.is_some();
    if !has_type {
        if has_kind {
            issues.push(Issue::warn(
                "okf-legacy-map",
                stem,
                "OKF `type` is missing; `kind` will be treated as `type` — run migrate-okf to make it explicit",
            ));
        } else {
            issues.push(Issue::error(
                "okf-type-missing",
                stem,
                "OKF required field `type` is missing and no legacy `kind` exists",
            ));
        }
    }

    let has_description = raw_fm
        .description
        .as_ref()
        .is_some_and(|d| !d.trim().is_empty())
        || raw_fm
            .summary
            .as_ref()
            .is_some_and(|s| !s.trim().is_empty());
    if !has_description {
        issues.push(Issue::warn(
            "okf-description-missing",
            stem,
            "OKF recommended field `description`/`summary` is missing",
        ));
    }

    if raw_fm
        .timestamp
        .as_ref()
        .is_none_or(|t| t.trim().is_empty())
    {
        issues.push(Issue::warn(
            "okf-timestamp-missing",
            stem,
            "OKF recommended field `timestamp` is missing",
        ));
    }
}

/// Check session metadata fields for placeholder/empty values. Pure.
fn check_session_metadata(raw_fm: &RawFrontMatter, stem: &str, issues: &mut Vec<Issue>) {
    for (field, values) in [
        ("skills", &raw_fm.skills),
        ("contracts", &raw_fm.contracts),
        ("incidents", &raw_fm.incidents),
    ] {
        for value in values {
            let v = value.trim();
            if v.is_empty()
                || v.eq_ignore_ascii_case("none")
                || v.eq_ignore_ascii_case("n/a")
                || v.eq_ignore_ascii_case("unknown")
            {
                issues.push(Issue::warn(
                    "session-metadata-placeholder",
                    stem,
                    format!("{field} contains a placeholder/empty value: '{value}'"),
                ));
            }
        }
    }
}

/// Check existence of required_frontmatter keys. Pure.
fn check_required_fields(
    raw_fm: &RawFrontMatter,
    required_keys: &[String],
    stem: &str,
    issues: &mut Vec<Issue>,
) {
    for key in required_keys {
        let present = match key.as_str() {
            "id" => raw_fm.id.is_some(),
            "title" => raw_fm.title.is_some(),
            "kind" => raw_fm.kind.is_some(),
            "origin" => raw_fm.origin.is_some(),
            "date" => raw_fm.date.is_some(),
            other => {
                issues.push(Issue::warn(
                    "schema-unknown-required",
                    stem,
                    format!("unknown key in schema required_frontmatter: '{other}'"),
                ));
                true
            }
        };
        if !present {
            issues.push(Issue::error(
                "required-fm-missing",
                stem,
                format!("required frontmatter key missing: '{key}'"),
            ));
        }
    }
}

/// Check id value integrity. Pure.
fn check_id_value(
    raw_fm: &RawFrontMatter,
    id_re: &regex::Regex,
    stem: &str,
    issues: &mut Vec<Issue>,
) {
    if let Some(fm_id) = &raw_fm.id {
        if !id_re.is_match(fm_id) {
            issues.push(Issue::error(
                "id-pattern",
                stem,
                format!("frontmatter id '{fm_id}' does not match schema pattern"),
            ));
        }
        if fm_id != stem {
            issues.push(Issue::error(
                "id-mismatch",
                stem,
                format!("frontmatter id '{fm_id}' ≠ filename stem '{stem}'"),
            ));
        }
    }
}

/// Check sources (prefix + file existence). Pure — includes vault_root filesystem access.
fn check_sources(
    raw_fm: &RawFrontMatter,
    allowed_prefixes: &[String],
    vault_root: &Path,
    source_roots: &SourceRoots,
    stem: &str,
    issues: &mut Vec<Issue>,
) {
    for src in &raw_fm.sources {
        let has_valid_prefix = allowed_prefixes.iter().any(|p| src.starts_with(p.as_str()));
        if has_valid_prefix {
            if source_path_escapes_root(src) {
                issues.push(Issue::error(
                    "source-path-escape",
                    stem,
                    format!("sources path must stay under its allowed root: {src}"),
                ));
                continue;
            }
            let full_path = resolve_source_path(src, vault_root, source_roots);
            let source_exists = full_path.exists();
            check_source_hash(
                src,
                source_exists.then_some(full_path.as_path()),
                stem,
                issues,
            );
            if !source_exists {
                issues.push(Issue::warn(
                    "source-missing",
                    stem,
                    format!("sources file does not exist: {src}"),
                ));
            }
        } else {
            issues.push(Issue::error(
                "source-prefix-violation",
                stem,
                format!(
                    "sources path '{src}' has a disallowed prefix (allowed: {allowed_prefixes:?})"
                ),
            ));
        }
    }
}

fn check_source_hash(src: &str, full_path: Option<&Path>, stem: &str, issues: &mut Vec<Issue>) {
    let file_part = source_file_part(src);
    let expected = source_sha256(src);
    if file_part.starts_with(RAW_WITNESS_PREFIX) && expected.is_none() {
        issues.push(Issue::error(
            "source-sha-missing",
            stem,
            format!("raw witness source must include #sha256=: {src}"),
        ));
        return;
    }

    let Some(expected) = expected else {
        return;
    };
    if !is_sha256_hex(expected) {
        issues.push(Issue::error(
            "source-sha-invalid",
            stem,
            format!("source sha256 fragment is invalid: {src}"),
        ));
        return;
    }

    let Some(full_path) = full_path else {
        return;
    };

    let data = match std::fs::read(full_path) {
        Ok(data) => data,
        Err(e) => {
            issues.push(Issue::warn(
                "source-unreadable",
                stem,
                format!("sources file could not be read for sha256 verification: {src}: {e}"),
            ));
            return;
        }
    };
    let actual = hex::encode(Sha256::digest(&data));
    if actual != expected {
        issues.push(Issue::error(
            "source-sha-mismatch",
            stem,
            format!("source sha256 does not match local evidence bytes: {src}"),
        ));
    }
}

fn resolve_source_path(src: &str, vault_root: &Path, source_roots: &SourceRoots) -> PathBuf {
    let file_part = source_file_part(src);
    if let Some(raw_witness) = file_part.strip_prefix(RAW_WITNESS_PREFIX) {
        return source_roots.raw_witness.join(raw_witness);
    }
    vault_root.join(file_part)
}

fn source_file_part(src: &str) -> &str {
    src.split('#').next().map_or(src, |part| part)
}

fn source_path_escapes_root(src: &str) -> bool {
    Path::new(source_file_part(src))
        .components()
        .any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
}

fn source_sha256(src: &str) -> Option<&str> {
    let mut parts = src.split('#');
    let file_part = parts.next().unwrap_or(src);
    let first_fragment = parts.next()?;
    if file_part.starts_with(RAW_WITNESS_PREFIX) {
        if parts.next().is_some() {
            return Some("");
        }
        return first_fragment.strip_prefix("sha256=");
    }
    std::iter::once(first_fragment)
        .chain(parts)
        .find_map(|fragment| fragment.strip_prefix("sha256="))
}

fn is_sha256_hex(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn default_raw_witness_root(vault_root: &Path) -> PathBuf {
    vault_root.parent().map_or_else(
        || PathBuf::from("data").join("raw-witness"),
        |home| home.join("data").join("raw-witness"),
    )
}

/// Check wikilinks (cross-layer + dangling). Pure.
fn check_wikilinks(body: &str, stem: &str, known_ids: &HashSet<String>, issues: &mut Vec<Issue>) {
    for bad_link in find_cross_layer_wikilinks(body) {
        issues.push(Issue::error(
            "cross-layer-wikilink",
            stem,
            format!("cross-layer wikilink [[{bad_link}]] — reference it via the sources: field"),
        ));
    }
    for link in extract_wikilinks(body) {
        if link.starts_with("wiki-") && !known_ids.contains(&link) {
            issues.push(Issue::error(
                "wikilink-dangling",
                stem,
                format!("body [[{link}]] target page does not exist"),
            ));
        }
    }
}

// ─────────────────────────────────────────────────────────────
// Lint — public entry point (pure)
// ─────────────────────────────────────────────────────────────

/// Take one file's content + path and return the list of issues. Pure function (the only I/O is checking sources existence).
///
/// # Arguments
/// - `abs_path`: absolute file path
/// - `content`: full file content
/// - `schema`: typed schema
/// - `vault_root`: vault root absolute path (for source file existence checks)
/// - `known_ids`: set of all page IDs that exist in vault/wiki
#[allow(clippy::too_many_lines)] // many integrity checks per page — a procedural list within a single responsibility (lint)
pub fn lint_page(
    abs_path: &Path,
    content: &str,
    schema: &Schema,
    vault_root: &Path,
    known_ids: &HashSet<String>,
) -> (Option<Page>, Vec<Issue>) {
    lint_page_with_source_roots(
        abs_path,
        content,
        schema,
        vault_root,
        &SourceRoots::default_for(vault_root),
        known_ids,
    )
}

#[allow(clippy::too_many_lines)] // many integrity checks per page — a procedural list within a single responsibility (lint)
fn lint_page_with_source_roots(
    abs_path: &Path,
    content: &str,
    schema: &Schema,
    vault_root: &Path,
    source_roots: &SourceRoots,
    known_ids: &HashSet<String>,
) -> (Option<Page>, Vec<Issue>) {
    let stem = abs_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_owned();

    // OKF reserves index.md / log.md as directory/listing files; they are not concept documents.
    // daily-brief-* files are generated output artifacts, not source memory, and do not follow
    // the wiki-NNNN schema.
    if stem == "index" || stem == "log" || stem.starts_with("daily-brief-") {
        return (None, Vec::new());
    }

    let mut issues = Vec::new();

    // ── id-format: filename pattern check ──
    let id_re = match regex::Regex::new(&schema.page_id.pattern) {
        Ok(r) => r,
        Err(e) => {
            issues.push(Issue::error(
                "schema-invalid",
                &stem,
                format!("failed to compile page_id.pattern: {e}"),
            ));
            return (None, issues);
        }
    };
    if !id_re.is_match(&stem) {
        issues.push(Issue::error(
            "id-format",
            &stem,
            format!(
                "filename stem '{stem}' does not match schema pattern ({})",
                schema.page_id.pattern
            ),
        ));
        return (None, issues);
    }

    // ── frontmatter split ──
    let Some((yaml, body)) = split_frontmatter(content) else {
        issues.push(Issue::error(
            "fm-parse",
            &stem,
            "no YAML frontmatter (--- ... ---)",
        ));
        return (None, issues);
    };

    // ── frontmatter YAML parsing ──
    let raw_fm = match parse_raw_frontmatter(yaml) {
        Ok(fm) => fm,
        Err(e) => {
            issues.push(Issue::error(
                "fm-parse",
                &stem,
                format!("failed to parse YAML: {e}"),
            ));
            return (None, issues);
        }
    };

    // ── checks (UX boundary — accumulated) ──
    check_required_fields(&raw_fm, &schema.required_frontmatter, &stem, &mut issues);
    check_id_value(&raw_fm, &id_re, &stem, &mut issues);
    check_okf_fields(&raw_fm, &stem, &mut issues);
    check_session_metadata(&raw_fm, &stem, &mut issues);

    // ── kind parsing ──
    let kind = raw_fm.kind.as_ref().and_then(parse_kind);
    if raw_fm.kind.is_some() && kind.is_none() {
        let raw_str = raw_fm.kind.as_ref().and_then(|v| v.as_str()).unwrap_or("?");
        issues.push(Issue::error(
            "kind-invalid",
            &stem,
            format!("kind '{raw_str}' is not an allowed value (note/memory/session/decision/code)"),
        ));
    }

    // ── origin parsing ──
    let origin = raw_fm.origin.as_ref().and_then(parse_origin);
    if raw_fm.origin.is_some() && origin.is_none() {
        let raw_str = raw_fm
            .origin
            .as_ref()
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        issues.push(Issue::error(
            "origin-invalid",
            &stem,
            format!("origin '{raw_str}' is not an allowed value (personal/company)"),
        ));
    }

    check_wikilinks(body, &stem, known_ids, &mut issues);
    check_sources(
        &raw_fm,
        &schema.sources.allowed_prefixes,
        vault_root,
        source_roots,
        &stem,
        &mut issues,
    );

    // ── Page construction ──
    let page = if let (Some(id), Some(title), Some(kind), Some(origin), Some(date)) = (
        raw_fm.id.clone(),
        raw_fm.title.clone(),
        kind,
        origin,
        raw_fm.date.clone(),
    ) {
        Some(Page {
            id,
            title,
            kind,
            origin,
            date,
            sources: raw_fm.sources.clone(),
            // Normalize Obsidian-style "[[wiki-NNNN]]" (from project_links) to bare ids so audit's
            // superseded/adjacency checks recognize them.
            relates_to: raw_fm
                .relates_to
                .iter()
                .map(|r| normalize_link_id(r))
                .collect(),
            tags: raw_fm.tags.clone(),
            superseded_by: raw_fm.superseded_by,
            body: body.to_owned(),
            path: abs_path.to_owned(),
        })
    } else {
        None
    };

    (page, issues)
}

// ─────────────────────────────────────────────────────────────
// Audit — pure graph sub-functions (SRP)
// ─────────────────────────────────────────────────────────────

/// Check superseded_by dangling + superseded-referenced. Pure.
fn check_superseded(pages: &[Page], page_ids: &HashSet<&str>, issues: &mut Vec<Issue>) {
    let superseded_page_ids: HashSet<&str> = pages
        .iter()
        .filter(|p| p.superseded_by.is_some())
        .map(|p| p.id.as_str())
        .collect();

    for page in pages {
        if let Some(ref sup) = page.superseded_by
            && !page_ids.contains(sup.as_str())
        {
            issues.push(Issue::error(
                "superseded-dangling",
                &page.id,
                format!("superseded_by '{sup}' target page does not exist"),
            ));
        }

        // warn if a live page's relates_to points to a superseded page
        if page.superseded_by.is_none() {
            for rel in &page.relates_to {
                if superseded_page_ids.contains(rel.as_str()) {
                    issues.push(Issue::warn(
                        "superseded-referenced",
                        &page.id,
                        format!("relates_to '{rel}' points to an already-superseded page — update to the successor page"),
                    ));
                }
            }
        }
    }
}

/// Build an undirected adjacency list + edge_count. Pure (uses owned String — avoids lifetime complexity).
fn build_adjacency(
    pages: &[Page],
    page_ids: &HashSet<&str>,
) -> (HashMap<String, HashSet<String>>, usize) {
    let mut adj: HashMap<String, HashSet<String>> = HashMap::new();
    let mut edge_set: HashSet<(String, String)> = HashSet::new();

    for page in pages {
        let pid = &page.id;

        let body_links: Vec<String> = extract_wikilinks(&page.body)
            .into_iter()
            .filter(|l| l.starts_with("wiki-") && page_ids.contains(l.as_str()))
            .collect();

        let all_neighbors: HashSet<String> = page
            .relates_to
            .iter()
            .filter(|id| page_ids.contains(id.as_str()))
            .cloned()
            .chain(body_links)
            .collect();

        for nbr in &all_neighbors {
            // undirected — normalize: (min, max)
            let (a, b) = if pid <= nbr {
                (pid.clone(), nbr.clone())
            } else {
                (nbr.clone(), pid.clone())
            };
            if a != b {
                edge_set.insert((a, b));
            }
            adj.entry(pid.clone()).or_default().insert(nbr.clone());
            adj.entry(nbr.clone()).or_default().insert(pid.clone());
        }
    }

    (adj, edge_set.len())
}

/// Return the list of connected-component sizes via BFS. Pure.
fn connected_components(nodes: &[&str], adj: &HashMap<String, HashSet<String>>) -> Vec<usize> {
    let mut visited: HashSet<&str> = HashSet::new();
    let mut sizes: Vec<usize> = Vec::new();

    for &start in nodes {
        if visited.contains(start) {
            continue;
        }
        let mut queue: VecDeque<&str> = VecDeque::new();
        queue.push_back(start);
        visited.insert(start);
        let mut size = 0_usize;
        while let Some(node) = queue.pop_front() {
            size += 1;
            if let Some(neighbors) = adj.get(node) {
                for nbr in neighbors {
                    if visited.insert(nbr.as_str()) {
                        queue.push_back(nbr.as_str());
                    }
                }
            }
        }
        sizes.push(size);
    }

    sizes.sort_unstable_by(|a, b| b.cmp(a));
    sizes
}

// ─────────────────────────────────────────────────────────────
// Audit — public entry point (pure)
// ─────────────────────────────────────────────────────────────

/// Build the graph from the pages list and return the audit result. Pure function (no I/O).
pub fn audit_pages(pages: &[Page]) -> AuditSummary {
    let mut issues = Vec::new();

    let page_ids: HashSet<&str> = pages.iter().map(|p| p.id.as_str()).collect();
    let superseded_count = pages.iter().filter(|p| p.superseded_by.is_some()).count();

    check_superseded(pages, &page_ids, &mut issues);

    let (adj, edge_count) = build_adjacency(pages, &page_ids);

    // ── orphan check ──
    let mut orphan_count = 0_usize;
    for page in pages {
        let has_edges = adj.get(&page.id).is_some_and(|s| !s.is_empty());
        if !has_edges {
            issues.push(Issue::warn(
                "orphan",
                &page.id,
                "inbound·outbound edges both 0 — orphan page",
            ));
            orphan_count += 1;
        }
    }

    // ── connected components (BFS) ──
    let all_nodes: Vec<&str> = pages.iter().map(|p| p.id.as_str()).collect();
    let component_sizes = connected_components(&all_nodes, &adj);
    let component_count = component_sizes.len();

    if component_count > 1 {
        issues.push(Issue::warn(
            "graph-fragmented",
            "graph",
            format!(
                "{component_count} connected components (sizes: {component_sizes:?}) — add [[wiki-NNNN]] bridges between components"
            ),
        ));
    }

    AuditSummary {
        page_count: pages.len(),
        edge_count,
        component_count,
        component_sizes,
        orphan_count,
        superseded_count,
        issues,
    }
}

// ─────────────────────────────────────────────────────────────
// I/O shell — run_lint / run_audit
// ─────────────────────────────────────────────────────────────

/// Collect the list of vault wiki pages from the filesystem. (I/O)
pub(crate) fn collect_wiki_pages(
    wiki_dir: &Path,
) -> anyhow::Result<(HashSet<String>, Vec<PathBuf>)> {
    let mut known_ids: HashSet<String> = HashSet::new();
    let mut entries: Vec<PathBuf> = Vec::new();

    let read_dir = std::fs::read_dir(wiki_dir)
        .with_context(|| format!("failed to read wiki directory: {}", wiki_dir.display()))?;

    for entry in read_dir {
        let entry = entry.context("failed to read wiki directory entry")?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) == Some("md") {
            if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                known_ids.insert(stem.to_owned());
            }
            entries.push(path);
        }
    }
    entries.sort();
    Ok((known_ids, entries))
}

/// Take the vault root, run lint, and return the exit code.
///
/// # Exit code semantics
/// - `0`: no errors (warnings may exist)
/// - `1`: errors present
/// - `2`: only warnings, in strict mode
pub fn run_lint(vault_root: &Path, strict: bool) -> anyhow::Result<i32> {
    let schema = load_schema(&vault_root.join(".rules/schema.yaml"))?;

    let wiki_dir = vault_root.join("wiki");
    if !wiki_dir.exists() {
        anyhow::bail!(
            "vault/wiki directory does not exist: {}",
            wiki_dir.display()
        );
    }

    let (known_ids, entries) = collect_wiki_pages(&wiki_dir)?;
    let source_roots = SourceRoots::from_env(vault_root);
    let mut all_issues: Vec<Issue> = Vec::new();

    for path in &entries {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read file: {}", path.display()))?;
        let (_page, issues) = lint_page_with_source_roots(
            path,
            &content,
            &schema,
            vault_root,
            &source_roots,
            &known_ids,
        );
        all_issues.extend(issues);
    }

    print_issues(&all_issues);
    Ok(exit_code(&all_issues, strict))
}

/// Take the vault root, run audit, and return the exit code.
pub fn run_audit(vault_root: &Path, strict: bool) -> anyhow::Result<i32> {
    let schema = load_schema(&vault_root.join(".rules/schema.yaml"))?;

    let wiki_dir = vault_root.join("wiki");
    if !wiki_dir.exists() {
        anyhow::bail!(
            "vault/wiki directory does not exist: {}",
            wiki_dir.display()
        );
    }

    let (known_ids, entries) = collect_wiki_pages(&wiki_dir)?;
    let source_roots = SourceRoots::from_env(vault_root);
    let mut pages: Vec<Page> = Vec::new();
    let mut parse_issues: Vec<Issue> = Vec::new();

    for path in &entries {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read file: {}", path.display()))?;
        let (page, issues) = lint_page_with_source_roots(
            path,
            &content,
            &schema,
            vault_root,
            &source_roots,
            &known_ids,
        );
        parse_issues.extend(issues);
        if let Some(p) = page {
            pages.push(p);
        }
    }

    let summary = audit_pages(&pages);

    println!("Vault Audit Summary");
    println!("  pages      : {}", summary.page_count);
    println!("  edges      : {}", summary.edge_count);
    println!(
        "  components : {} (sizes: {:?})",
        summary.component_count, summary.component_sizes
    );
    println!("  orphans    : {}", summary.orphan_count);
    println!("  superseded : {}", summary.superseded_count);
    println!();

    let mut all_issues = parse_issues;
    all_issues.extend(summary.issues);
    print_issues(&all_issues);
    Ok(exit_code(&all_issues, strict))
}

// ─────────────────────────────────────────────────────────────
// Output helpers (I/O)
// ─────────────────────────────────────────────────────────────

fn print_issues(issues: &[Issue]) {
    if issues.is_empty() {
        println!("PASSED (0 issues)");
        return;
    }

    let errors: Vec<_> = issues
        .iter()
        .filter(|i| i.severity == Severity::Error)
        .collect();
    let warns: Vec<_> = issues
        .iter()
        .filter(|i| i.severity == Severity::Warn)
        .collect();

    for i in &errors {
        println!("✗ [ERROR] {:30}  {:20}  {}", i.rule, i.target, i.message);
    }
    for i in &warns {
        println!("⚠ [WARN]  {:30}  {:20}  {}", i.rule, i.target, i.message);
    }

    println!("──────────────────────────────────────────────────────────");
    if errors.is_empty() {
        println!("WARNINGS: {} warning(s)", warns.len());
    } else {
        println!(
            "FAILED: {} error(s), {} warning(s)",
            errors.len(),
            warns.len()
        );
    }
}

fn exit_code(issues: &[Issue], strict: bool) -> i32 {
    let has_errors = issues.iter().any(|i| i.severity == Severity::Error);
    let has_warns = issues.iter().any(|i| i.severity == Severity::Warn);

    if has_errors {
        1
    } else if has_warns && strict {
        2
    } else {
        0
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use std::collections::HashSet;
    use std::path::{Path, PathBuf};

    use super::{
        SourceRoots, audit_pages, extract_wikilinks, find_cross_layer_wikilinks, lint_page,
        lint_page_with_source_roots, normalize_link_id,
    };
    use crate::vault::{Kind, Origin, Page, PageIdSchema, Schema, Severity, SourcesSchema};
    use sha2::{Digest, Sha256};

    fn test_schema() -> Schema {
        Schema {
            page_id: PageIdSchema {
                pattern: r"^wiki-\d{4,5}$".to_owned(),
            },
            sources: SourcesSchema {
                allowed_prefixes: vec![
                    "raw/".to_owned(),
                    "raw-witness/".to_owned(),
                    "meta/".to_owned(),
                    ".rules/".to_owned(),
                ],
            },
            required_frontmatter: vec![
                "id".to_owned(),
                "title".to_owned(),
                "kind".to_owned(),
                "origin".to_owned(),
                "date".to_owned(),
            ],
        }
    }

    fn known_ids(ids: &[&str]) -> HashSet<String> {
        ids.iter().map(|s| (*s).to_owned()).collect()
    }

    fn make_page(id: &str, relates_to: Vec<&str>, body: &str) -> Page {
        Page {
            id: id.to_owned(),
            title: id.to_owned(),
            kind: Kind::Note,
            origin: Origin::Personal,
            date: "2026-01-01".to_owned(),
            sources: vec![],
            relates_to: relates_to.into_iter().map(str::to_owned).collect(),
            tags: vec![],
            superseded_by: None,
            body: body.to_owned(),
            path: PathBuf::from(format!("/vault/wiki/{id}.md")),
        }
    }

    fn sha256_hex(body: &str) -> String {
        hex::encode(Sha256::digest(body.as_bytes()))
    }

    // ── extract_wikilinks ──

    #[test]
    fn wikilinks_extracted_correctly() {
        let body = "see: [[wiki-0001]] and [[wiki-0002|second]].";
        let links = extract_wikilinks(body);
        assert_eq!(links, vec!["wiki-0001", "wiki-0002"]);
    }

    #[test]
    fn no_wikilinks_returns_empty() {
        assert!(extract_wikilinks("body with no links").is_empty());
    }

    // ── cross-layer wikilinks ──

    #[test]
    fn cross_layer_detected() {
        let body = "[[raw/seed.md]] and [[wiki-0001]]";
        let bad = find_cross_layer_wikilinks(body);
        assert_eq!(bad, vec!["raw/seed.md"]);
    }

    // ── parse_kind / parse_origin live in mod.rs; exercise them via lint_page ──

    #[test]
    fn valid_frontmatter_no_errors() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        let errors: Vec<_> = issues
            .iter()
            .filter(|i| i.severity == Severity::Error)
            .collect();
        assert!(errors.is_empty(), "there should be no errors: {errors:?}");
        assert!(page.is_some(), "Page parse failed");
    }

    #[test]
    fn code_kind_is_allowed() {
        // `remember_code` notes carry `kind: code` + code_symbols frontmatter.
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: code\norigin: personal\ndate: \"2026-01-01\"\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            !issues.iter().any(|i| i.rule == "kind-invalid"),
            "kind: code must lint clean: {issues:?}"
        );
        assert!(page.is_some(), "Page parse failed");
    }

    #[test]
    fn raw_witness_source_resolves_to_local_evidence_root() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");
        let raw_body = "{}\n";
        let witness = tmp
            .path()
            .join("data")
            .join("raw-witness")
            .join("codex")
            .join("20260703")
            .join("session.jsonl");
        std::fs::create_dir_all(witness.parent().unwrap()).unwrap();
        std::fs::write(&witness, raw_body).unwrap();

        let schema = test_schema();
        let content = format!(
            "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/codex/20260703/session.jsonl#sha256={}\n---\nbody",
            sha256_hex(raw_body)
        );
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let (_page, issues) = lint_page(&path, &content, &schema, &vault_root, &ids);

        assert!(
            !issues.iter().any(|i| i.rule == "source-prefix-violation"),
            "raw-witness prefix must be allowed: {issues:?}"
        );
        assert!(
            !issues.iter().any(|i| i.rule == "source-missing"),
            "raw-witness source should resolve outside vault/ to data/raw-witness: {issues:?}"
        );
        assert!(
            !issues.iter().any(|i| i.rule.starts_with("source-sha")),
            "raw-witness source should verify sha256: {issues:?}"
        );
    }

    #[test]
    fn raw_witness_source_resolves_to_configured_evidence_root() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");
        let raw_witness_root = tmp.path().join("custom-witness-root");
        let raw_body = "{}\n";
        let witness = raw_witness_root
            .join("codex")
            .join("20260703")
            .join("session.jsonl");
        std::fs::create_dir_all(witness.parent().unwrap()).unwrap();
        std::fs::write(&witness, raw_body).unwrap();

        let schema = test_schema();
        let content = format!(
            "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/codex/20260703/session.jsonl#sha256={}\n---\nbody",
            sha256_hex(raw_body)
        );
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let source_roots = SourceRoots {
            raw_witness: raw_witness_root,
        };
        let (_page, issues) =
            lint_page_with_source_roots(&path, &content, &schema, &vault_root, &source_roots, &ids);

        assert!(
            !issues.iter().any(|i| i.rule == "source-missing"),
            "raw-witness source should resolve through the injected source root: {issues:?}"
        );
        assert!(
            !issues.iter().any(|i| i.rule.starts_with("source-sha")),
            "raw-witness source should verify sha256 through the injected source root: {issues:?}"
        );
    }

    #[test]
    fn raw_witness_source_rejects_parent_dir_escape() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");
        let escaped_body = "outside evidence\n";
        let escaped = tmp.path().join("data").join("outside.jsonl");
        std::fs::create_dir_all(escaped.parent().unwrap()).unwrap();
        std::fs::write(&escaped, escaped_body).unwrap();

        let schema = test_schema();
        let content = format!(
            "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/../outside.jsonl#sha256={}\n---\nbody",
            sha256_hex(escaped_body)
        );
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let (_page, issues) = lint_page(&path, &content, &schema, &vault_root, &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "source-path-escape" && i.severity == Severity::Error),
            "raw witness parent-dir traversal should be rejected: {issues:?}"
        );
        assert!(
            !issues.iter().any(|i| i.rule == "source-sha-mismatch"),
            "escaped file should not be read for sha verification: {issues:?}"
        );
    }

    #[test]
    fn raw_witness_source_requires_sha256_fragment() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");
        let witness = tmp
            .path()
            .join("data")
            .join("raw-witness")
            .join("codex")
            .join("20260703")
            .join("session.jsonl");
        std::fs::create_dir_all(witness.parent().unwrap()).unwrap();
        std::fs::write(&witness, "{}\n").unwrap();

        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/codex/20260703/session.jsonl\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let (_page, issues) = lint_page(&path, content, &schema, &vault_root, &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "source-sha-missing" && i.severity == Severity::Error),
            "missing raw witness sha should be an error: {issues:?}"
        );
    }

    #[test]
    fn raw_witness_missing_file_still_requires_sha256_fragment() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");

        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/codex/20260703/session.jsonl\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let (_page, issues) = lint_page(&path, content, &schema, &vault_root, &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "source-sha-missing" && i.severity == Severity::Error),
            "missing raw witness sha should remain an error even after retention: {issues:?}"
        );
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "source-missing" && i.severity == Severity::Warn),
            "retained pointer with missing witness bytes should still report missing source: {issues:?}"
        );
    }

    #[test]
    fn raw_witness_source_rejects_invalid_sha256_fragment() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");
        let witness = tmp
            .path()
            .join("data")
            .join("raw-witness")
            .join("codex")
            .join("20260703")
            .join("session.jsonl");
        std::fs::create_dir_all(witness.parent().unwrap()).unwrap();
        std::fs::write(&witness, "{}\n").unwrap();

        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/codex/20260703/session.jsonl#sha256=abc123\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let (_page, issues) = lint_page(&path, content, &schema, &vault_root, &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "source-sha-invalid" && i.severity == Severity::Error),
            "invalid raw witness sha should be an error: {issues:?}"
        );
    }

    #[test]
    fn raw_witness_source_rejects_extra_fragment() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");
        let witness = tmp
            .path()
            .join("data")
            .join("raw-witness")
            .join("codex")
            .join("20260703")
            .join("session.jsonl");
        std::fs::create_dir_all(witness.parent().unwrap()).unwrap();
        std::fs::write(&witness, "{}\n").unwrap();

        let schema = test_schema();
        let digest = sha256_hex("{}\n");
        let content = format!(
            "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/codex/20260703/session.jsonl#note=extra#sha256={digest}\n---\nbody"
        );
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let (_page, issues) = lint_page(&path, &content, &schema, &vault_root, &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "source-sha-invalid" && i.severity == Severity::Error),
            "extra raw witness fragments should be invalid: {issues:?}"
        );
    }

    #[test]
    fn raw_witness_source_detects_sha256_mismatch() {
        let tmp = tempfile::tempdir().unwrap();
        let vault_root = tmp.path().join("vault");
        let witness = tmp
            .path()
            .join("data")
            .join("raw-witness")
            .join("codex")
            .join("20260703")
            .join("session.jsonl");
        std::fs::create_dir_all(witness.parent().unwrap()).unwrap();
        std::fs::write(&witness, "{}\n").unwrap();

        let schema = test_schema();
        let wrong = "0".repeat(64);
        let content = format!(
            "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nsources:\n  - raw-witness/codex/20260703/session.jsonl#sha256={wrong}\n---\nbody"
        );
        let ids = known_ids(&["wiki-0001"]);
        let path = vault_root.join("wiki").join("wiki-0001.md");
        let (_page, issues) = lint_page(&path, &content, &schema, &vault_root, &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "source-sha-mismatch" && i.severity == Severity::Error),
            "mismatched raw witness sha should be an error: {issues:?}"
        );
    }

    #[test]
    fn missing_required_field_is_error() {
        let schema = test_schema();
        let content =
            "---\nid: wiki-0001\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "required-fm-missing" && i.message.contains("title")),
            "no title-missing issue: {issues:?}"
        );
    }

    #[test]
    fn dangling_wikilink_detected() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: T\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n[[wiki-9999]]";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            issues.iter().any(|i| i.rule == "wikilink-dangling"),
            "no dangling wikilink issue: {issues:?}"
        );
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "wikilink-dangling" && i.severity == Severity::Error)
        );
    }

    #[test]
    fn valid_wikilink_no_error() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: T\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n[[wiki-0002]]";
        let ids = known_ids(&["wiki-0001", "wiki-0002"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            !issues.iter().any(|i| i.rule == "wikilink-dangling"),
            "dangling issue on a valid wikilink: {issues:?}"
        );
    }

    #[test]
    fn relates_to_obsidian_alias_normalized() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: T\nkind: note\norigin: personal\ndate: \"2026-01-01\"\nrelates_to:\n  - \"[[wiki-0002|alias]]\"\n---\nbody";
        let ids = known_ids(&["wiki-0001", "wiki-0002"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            !issues.iter().any(|i| i.rule == "wikilink-dangling"),
            "normalized relates_to must not be flagged: {issues:?}"
        );
        let relates = page.unwrap().relates_to;
        assert_eq!(relates, vec!["wiki-0002"]);
    }

    // ── audit_pages: connected components ──

    #[test]
    fn two_connected_pages_single_component() {
        let pages = vec![
            make_page("wiki-0001", vec!["wiki-0002"], "[[wiki-0002]]"),
            make_page("wiki-0002", vec!["wiki-0001"], "[[wiki-0001]]"),
        ];
        let summary = audit_pages(&pages);
        assert_eq!(summary.page_count, 2);
        assert_eq!(summary.component_count, 1);
        assert_eq!(summary.orphan_count, 0);
        assert!(!summary.issues.iter().any(|i| i.rule == "graph-fragmented"));
        assert!(!summary.issues.iter().any(|i| i.rule == "orphan"));
    }

    #[test]
    fn disconnected_pages_multiple_components() {
        let pages = vec![
            make_page("wiki-0001", vec![], ""),
            make_page("wiki-0002", vec![], ""),
        ];
        let summary = audit_pages(&pages);
        assert_eq!(summary.component_count, 2);
        assert_eq!(summary.orphan_count, 2);
        assert!(summary.issues.iter().any(|i| i.rule == "orphan"));
        assert!(summary.issues.iter().any(|i| i.rule == "graph-fragmented"));
    }

    #[test]
    fn superseded_dangling_is_error() {
        let mut p = make_page("wiki-0001", vec![], "");
        p.superseded_by = Some("wiki-9999".to_owned());
        let pages = vec![p];
        let summary = audit_pages(&pages);
        assert!(
            summary
                .issues
                .iter()
                .any(|i| i.rule == "superseded-dangling" && i.severity == Severity::Error)
        );
    }

    #[test]
    fn superseded_referenced_is_warn() {
        let mut old = make_page("wiki-0001", vec![], "");
        old.superseded_by = Some("wiki-0002".to_owned());
        let new_page = make_page("wiki-0002", vec![], "");
        // live wiki-0003 references the superseded wiki-0001 via relates_to
        let live = make_page("wiki-0003", vec!["wiki-0001"], "");
        let pages = vec![old, new_page, live];
        let summary = audit_pages(&pages);
        assert!(
            summary
                .issues
                .iter()
                .any(|i| i.rule == "superseded-referenced" && i.severity == Severity::Warn),
            "no superseded-referenced warn: {:?}",
            summary.issues
        );
    }

    #[test]
    fn normalize_link_id_strips_obsidian_quotes_and_alias() {
        assert_eq!(normalize_link_id("\"[[wiki-0002|alias]]\""), "wiki-0002");
        assert_eq!(normalize_link_id("[[wiki-0002]]"), "wiki-0002");
        assert_eq!(normalize_link_id("wiki-0002"), "wiki-0002");
    }

    // ── OKF compatibility lint ──

    #[test]
    fn okf_type_missing_with_legacy_kind_emits_warn_not_error() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "okf-legacy-map" && i.severity == Severity::Warn),
            "expected okf-legacy-map warn: {issues:?}"
        );
        assert!(
            !issues.iter().any(|i| i.rule == "okf-type-missing"),
            "legacy kind must not produce okf-type-missing error: {issues:?}"
        );
    }

    #[test]
    fn okf_type_missing_without_kind_is_error() {
        let schema = test_schema();
        let content =
            "---\nid: wiki-0001\ntitle: Test\norigin: personal\ndate: \"2026-01-01\"\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);

        assert!(
            issues
                .iter()
                .any(|i| i.rule == "okf-type-missing" && i.severity == Severity::Error),
            "expected okf-type-missing error: {issues:?}"
        );
    }

    #[test]
    fn okf_reserved_files_are_skipped() {
        let schema = test_schema();
        let ids = known_ids(&[]);
        let index_path = Path::new("/vault/wiki/index.md");
        let log_path = Path::new("/vault/wiki/log.md");
        let (_page, index_issues) = lint_page(
            index_path,
            "---\n---\n# Index",
            &schema,
            Path::new("/vault"),
            &ids,
        );
        let (_page, log_issues) = lint_page(
            log_path,
            "---\n---\n# Log",
            &schema,
            Path::new("/vault"),
            &ids,
        );
        assert!(
            index_issues.is_empty(),
            "index.md should be skipped: {index_issues:?}"
        );
        assert!(
            log_issues.is_empty(),
            "log.md should be skipped: {log_issues:?}"
        );
    }
}
