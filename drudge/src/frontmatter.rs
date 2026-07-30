//! Frontmatter entity — parse raw `.md` into a typed form once at the boundary (parse-don't-validate).
//!
//! Cross-reference: ENFORCEMENT.md §A (PDV/boundary) · PHILOSOPHY.md Layer 1.
//! If YAML frontmatter (`--- ... ---`) is present, parse it; otherwise infer origin/kind/project from the path.
//! Parse failure goes on the `Result` rail rather than a silent fallback (ROP) — the caller decides the graceful boundary.
use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::config;

/// Scheduler-produced briefing notes are output artifacts, not source memory.
pub const GENERATED_BRIEF_TAG: &str = "daily-brief";

/// Claim kind vocabulary shared by remember/schema/retrieval filters.
pub const CLAIM_KINDS: &[&str] = &[
    "fact",
    "decision",
    "assumption",
    "risk",
    "blocked",
    "goal",
    "term",
    "next",
];

/// Claim confidence vocabulary shared by remember/schema/data hygiene.
pub const CLAIM_CONFIDENCES: &[&str] = &["certain", "likely", "assumption", "outdated"];

#[must_use]
pub fn has_generated_brief_tag(tags: &[String]) -> bool {
    tags.iter().any(|tag| tag.trim() == GENERATED_BRIEF_TAG)
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct TagFrontmatter {
    tags: Vec<String>,
}

#[must_use]
pub fn raw_has_generated_brief_tag(raw: &str) -> bool {
    yaml_frontmatter(raw).is_some_and(|yaml| {
        serde_yaml::from_str::<TagFrontmatter>(yaml)
            .is_ok_and(|fm| has_generated_brief_tag(&fm.tags))
    })
}

#[must_use]
pub fn is_internal_eval_fixture_path(path: &str) -> bool {
    let Some(name) = path.rsplit('/').next() else {
        return false;
    };
    name.starts_with("eval-")
        && std::path::Path::new(name)
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("md"))
}

fn yaml_frontmatter(raw: &str) -> Option<&str> {
    let raw = raw.strip_prefix('\u{feff}').unwrap_or(raw);
    raw.strip_prefix("---\n")
        .and_then(|rest| rest.find("\n---\n").map(|end| &rest[..end]))
}

/// Structured metadata for an ingested document — the basis (SSOT) for audit · filtering · graph edges.
///
/// Honest disclosure: `origin`/`kind` are `String`, not enums — unlike `vault::{Origin,Kind}`,
/// which ARE enums. This is deliberate, not an oversight. These are ingest *boundary* fields parsed
/// from arbitrary markdown (Claude Code transcripts, freeform notes); their only consumers are
/// audit tally (distribution counts) and a Postgres `text` column bind — nothing re-derives domain
/// meaning from them, so there is no parse-don't-validate smell to close. vault's enums cover a
/// different, curated value set (note/memory/session/decision/code) where exhaustive matching matters.
/// Forcing an enum here would mean code changes for any new ingest kind and a second near-duplicate
/// enum — escalation the rule-of-three doesn't justify (§C "simplest thing that works").
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct FrontMatter {
    pub origin: String, // personal | company
    pub project: String,
    pub date: String,
    pub kind: String, // note | memory | doc  (value produced by enrich; "session" exists only as a reserved word)
    pub source_path: String,
    pub title: Option<String>,
    pub tags: Vec<String>,
    // Agent-curated semantic ontology (kernel A): the deterministic source of the graph.
    // The agent (reasoner) extracts these; drudge (kernel) only stores/links them — no LLM extraction.
    // Absent in legacy/source-walk markdown → default empty (serde default), so those docs simply have no semantic graph.
    pub tools: Vec<String>,
    pub concepts: Vec<String>,
    pub claims: Vec<Claim>,
    /// Source artifacts this distilled note is grounded in. These point at local evidence pointers
    /// such as `raw-witness/...#sha256=...`, not transient host paths or indexed raw transcripts.
    pub sources: Vec<String>,
    /// Ephemeral ingestion queue marker. Not part of the semantic graph; carried only so the
    /// hermes/cron worker can confirm that a specific session was remembered. May be absent.
    pub omb_session_id: Option<String>,
    /// OKF bundle version this note targets. Emitted by the write gate; absent on legacy notes.
    pub okf_version: Option<String>,
    /// One-line OKF `description` (and Obsidian summary). May be absent on legacy notes.
    pub summary: Option<String>,
    /// Skills invoked during the session. Graph keys for skill-usage analytics.
    pub skills: Vec<String>,
    /// Contracts referenced or established during the session (e.g., ollama, lm-studio, graph).
    pub contracts: Vec<String>,
    /// Failures, blockers, or repeated errors observed during the session.
    pub incidents: Vec<String>,
    /// Code symbols this note is grounded in (e.g., `src/lib.rs:parse`). Used by `remember_code`
    /// to link a note to the AST code graph.
    pub code_symbols: Vec<String>,
}

/// One temporal fact — `(subject, predicate, value)` plus `kind` and `confidence`.
/// A new value supersedes the old (see `store::upsert_claim`).
/// Agent-provided in note frontmatter; drudge embeds the value (bge-m3) and stores it. No LLM extraction in the kernel.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct Claim {
    pub subject: String,
    pub predicate: String,
    pub value: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub confidence: String,
}

impl Claim {
    /// Normalized kind, or `"fact"` when absent.
    pub fn kind(&self) -> &str {
        let k = self.kind.trim();
        if k.is_empty() { "fact" } else { k }
    }

    /// Normalized confidence, or `"certain"` when absent.
    pub fn confidence(&self) -> &str {
        let c = self.confidence.trim();
        if c.is_empty() { "certain" } else { c }
    }
}

/// Canonical key for tool/concept names used by graph links and duplicate matching.
/// Separators are not meaningful for these labels: `LM Studio`, `lm-studio`, and
/// `lmstudio` should point at the same semantic node.
#[must_use]
pub fn semantic_key(s: &str) -> String {
    let lower = s.to_lowercase();
    let normalized = lower
        .replace("c++", "cpp")
        .replace("c#", "csharp")
        .replace(".net", "dotnet");
    normalized
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

/// Canonical key for claim subjects and predicates. Claims remain human-readable
/// in values/labels, but their identity should not fork on casing or spacing.
#[must_use]
pub fn claim_key(s: &str) -> String {
    let lower = s.to_lowercase();
    let normalized = lower
        .replace("c++", "cpp")
        .replace("c#", "csharp")
        .replace(".net", "dotnet");
    normalized
        .chars()
        .map(|ch| if ch.is_alphanumeric() { ch } else { ' ' })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

impl FrontMatter {
    /// Fill empty fields via path heuristics (part of constructing the typed value).
    fn enrich(&mut self, path: &str, cfg: &config::BoringConfig) {
        path.clone_into(&mut self.source_path);
        self.origin = self.origin.trim().to_owned();
        self.project = self.project.trim().to_owned();
        self.kind = self.kind.trim().to_owned();
        if self.origin.is_empty() {
            let (origin, _rule) = cfg.classify(path, None);
            self.origin.push_str(match origin {
                config::Origin::Personal => "personal",
                config::Origin::Company => "company",
                config::Origin::Mirror => "mirror",
                config::Origin::Community => "community",
            });
        }
        if self.kind.is_empty() {
            self.kind.push_str(if path.contains("/notes/") {
                "note"
            } else if path.contains("/memory") {
                "memory"
            } else {
                "doc"
            });
        }
        if self.project.is_empty() {
            self.project = derive_project(path);
        }
    }
}

/// The `<proj>` in `…/projects/<proj>/…`, or the parent directory name.
fn derive_project(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    if let Some(i) = parts.iter().position(|&p| p == "projects")
        && let Some(proj) = parts.get(i + 1)
    {
        return (*proj).to_owned();
    }
    // fallback: the file's parent directory
    parts
        .iter()
        .rev()
        .nth(1)
        .map_or_else(|| "unknown".to_owned(), |s| (*s).to_owned())
}

/// raw `.md` → (frontmatter, body). Err if frontmatter YAML parsing fails.
pub fn parse(
    raw: &str,
    fallback_path: &str,
    cfg: &config::BoringConfig,
) -> Result<(FrontMatter, String)> {
    let raw = raw.strip_prefix('\u{feff}').unwrap_or(raw); // strip BOM
    let mut front = if let Some(rest) = raw.strip_prefix("---\n") {
        if let Some(end) = rest.find("\n---\n") {
            let yaml = &rest[..end];
            let body = rest[end + 5..].to_owned();
            let front: FrontMatter = serde_yaml::from_str(yaml)?;
            front_enriched(front, fallback_path, &body, cfg)
        } else {
            front_enriched(FrontMatter::default(), fallback_path, raw, cfg)
        }
    } else {
        front_enriched(FrontMatter::default(), fallback_path, raw, cfg)
    };
    let body = std::mem::take(&mut front.1);
    Ok((front.0, body))
}

fn front_enriched(
    mut fm: FrontMatter,
    path: &str,
    body: &str,
    cfg: &config::BoringConfig,
) -> (FrontMatter, String) {
    fm.enrich(path, cfg);
    (fm, body.trim_start().to_owned())
}

/// FrontMatter + body → `.md` text (`--- yaml --- body`).
#[allow(dead_code)] // S8: used when frontmatter-izing the distill hook output
pub fn render(front: &FrontMatter, body: &str) -> Result<String> {
    let yaml = serde_yaml::to_string(front)?;
    Ok(format!("---\n{yaml}---\n{body}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::{
        FrontMatter, GENERATED_BRIEF_TAG, claim_key, is_internal_eval_fixture_path, parse,
        raw_has_generated_brief_tag, render, semantic_key,
    };
    use crate::config::BoringConfig;

    fn test_cfg() -> BoringConfig {
        BoringConfig::default()
    }

    #[test]
    fn parse_with_frontmatter() {
        let raw = "---\norigin: company\nproject: demo\ntags:\n  - rust\n  - rop\n---\n본문 시작\n둘째 줄";
        let (fm, body) = parse(raw, "/x/y.md", &test_cfg()).unwrap();
        assert_eq!(fm.origin, "company");
        assert_eq!(fm.project, "demo");
        assert_eq!(fm.tags, vec!["rust", "rop"]);
        assert_eq!(body, "본문 시작\n둘째 줄");
    }

    #[test]
    fn parse_uses_ingest_path_as_source_path_even_when_yaml_has_stale_value() {
        let raw = "---\nsource_path: stale/wiki-old.md\nproject: demo\n---\n본문";
        let (fm, body) = parse(raw, "/actual/wiki-new.md", &test_cfg()).unwrap();

        assert_eq!(fm.source_path, "/actual/wiki-new.md");
        assert_eq!(body, "본문");
    }

    #[test]
    fn parse_trims_identity_fields_before_storage() {
        let raw = "---\norigin: ' company '\nproject: ' demo '\nkind: ' note '\n---\n본문";
        let (fm, body) = parse(raw, "/actual/wiki-new.md", &test_cfg()).unwrap();

        assert_eq!(fm.origin, "company");
        assert_eq!(fm.project, "demo");
        assert_eq!(fm.kind, "note");
        assert_eq!(body, "본문");
    }

    #[test]
    fn parse_treats_whitespace_project_as_missing_and_derives_from_path() {
        let raw = "---\nproject: '   '\n---\n본문";
        let (fm, body) = parse(
            raw,
            "/Users/x/.claude/projects/oh-my-boring/data/notes/s.md",
            &test_cfg(),
        )
        .unwrap();

        assert_eq!(fm.project, "oh-my-boring");
        assert_eq!(body, "본문");
    }

    #[test]
    fn raw_generated_brief_tag_is_detected_without_full_parse() {
        let raw = format!("---\ntitle: Daily\ntags:\n  - {GENERATED_BRIEF_TAG}\n---\nsummary");

        assert!(raw_has_generated_brief_tag(&raw));
        assert!(!raw_has_generated_brief_tag(
            "---\ntitle: Source\ntags: [memory]\n---\nsource"
        ));
        assert!(
            !raw_has_generated_brief_tag("---\ntags: [unclosed\n---\nbody"),
            "malformed YAML should not be silently classified as generated"
        );
    }

    #[test]
    fn internal_eval_fixture_path_matches_store_boundary() {
        assert!(is_internal_eval_fixture_path(
            "/vault/wiki/eval-docker-layer-cache.md"
        ));
        assert!(is_internal_eval_fixture_path("eval-briefing.md"));
        assert!(!is_internal_eval_fixture_path(
            "/vault/wiki/wiki-eval-docker-layer-cache.md"
        ));
        assert!(!is_internal_eval_fixture_path(
            "/vault/wiki/eval-briefing.txt"
        ));
    }

    #[test]
    fn parse_without_frontmatter_infers_from_path() {
        let (fm, body) = parse(
            "그냥 본문",
            "/Users/x/.claude/projects/oh-my-boring/data/notes/s.md",
            &test_cfg(),
        )
        .unwrap();
        assert_eq!(fm.origin, "personal"); // no company rule → personal
        assert_eq!(fm.kind, "note"); // /notes/ path
        assert_eq!(fm.project, "oh-my-boring"); // projects/<proj>
        assert_eq!(
            fm.source_path,
            "/Users/x/.claude/projects/oh-my-boring/data/notes/s.md"
        );
        assert_eq!(body, "그냥 본문");
    }

    #[test]
    fn round_trip_render_then_parse() {
        let fm = FrontMatter {
            origin: "personal".to_owned(),
            project: "oh-my-boring".to_owned(),
            kind: "note".to_owned(),
            tags: vec!["a".to_owned(), "b".to_owned()],
            ..Default::default()
        };
        let md = render(&fm, "본문").unwrap();
        let (back, body) = parse(&md, "/p.md", &test_cfg()).unwrap();
        assert_eq!(back.origin, "personal");
        assert_eq!(back.project, "oh-my-boring");
        assert_eq!(back.tags, vec!["a", "b"]);
        assert_eq!(body, "본문");
    }

    #[test]
    fn malformed_yaml_is_error_not_silent() {
        // ROP: broken frontmatter goes to Err (not a silent fallback)
        let raw = "---\norigin: [unclosed\n---\n본문";
        assert!(parse(raw, "/p.md", &test_cfg()).is_err());
    }

    #[test]
    fn semantic_key_collapses_tool_and_concept_variants() {
        assert_eq!(semantic_key("LM Studio"), "lmstudio");
        assert_eq!(semantic_key("lm-studio"), "lmstudio");
        assert_eq!(semantic_key("oh-my-boring"), "ohmyboring");
        assert_eq!(semantic_key("c++"), "cpp");
        assert_eq!(semantic_key("C#"), "csharp");
        assert_eq!(semantic_key(".NET"), "dotnet");
    }

    #[test]
    fn claim_key_collapses_casing_and_spacing() {
        assert_eq!(claim_key("  Release   Version "), "release version");
        assert_eq!(claim_key("release-version"), "release version");
        assert_eq!(claim_key("release_version"), "release version");
        assert_eq!(claim_key("OH-my  Boring"), "oh my boring");
        assert_eq!(claim_key("c++/.NET"), "cpp dotnet");
        assert_eq!(claim_key("브리핑 상태"), "브리핑 상태");
    }
}
