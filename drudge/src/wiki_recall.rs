//! wiki_recall — retrieve by reading `vault/wiki/*.md` directly (no pgvector·embeddings needed).
//!
//! Cross-reference: design decision D1 (wiki-first, pgvector optional).
//!
//! Karpathy-wiki first-class path: for a personal, small corpus (hundreds of documents), reading markdown directly is simpler, more
//! trustworthy, and easier to debug than RAG (2026 trend + repo `CLAUDE.md` "simplest thing that works"). pgvector (vector+graph) is
//! an optional accelerator turned on when the scale/accuracy trigger is crossed.
//!
//! Scoring: not token *equality* but **substring-match frequency**. Tolerates Korean attached endings (임베딩→"임베딩은") and English
//! partial words (decent without morphological analysis). Title matches are weighted. Separates pure logic (`score_doc`) from I/O (`recall`) (SRP).
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use anyhow::{Context, Result};

use crate::config;
use crate::frontmatter::{is_internal_eval_fixture_path, raw_has_generated_brief_tag};

const SECONDS_PER_HOUR: u64 = 3_600;

/// A single retrieval result (minimal fields compatible with the vector path's hit).
#[derive(Debug, Clone)]
pub struct WikiHit {
    pub id: String,
    pub title: String,
    pub origin: String,
    pub project: String,
    pub source_path: String,
    pub snippet: String,
    pub score: f32,
}

/// Split the query into search terms — whitespace split + 2+ chars + lowercase. Pure.
fn query_terms(query: &str) -> Vec<String> {
    query
        .split_whitespace()
        .map(|w| {
            w.trim_matches(|c: char| !c.is_alphanumeric())
                .to_lowercase()
        })
        .filter(|w| w.chars().count() >= 2)
        .collect()
}

/// Non-overlapping occurrence count of `needle` within `haystack`. Pure.
fn count_occurrences(haystack: &str, needle: &str) -> usize {
    if needle.is_empty() {
        return 0;
    }
    let mut n = 0;
    let mut rest = haystack;
    while let Some(pos) = rest.find(needle) {
        n += 1;
        rest = &rest[pos + needle.len()..];
    }
    n
}

/// title+body score + snippet. None if zero matches. Lowercases then delegates to `score_lower`.
/// Test-only convenience now — the production path caches lowercased forms and calls `score_lower`.
/// score = Σ(body occurrences) + 3·Σ(title occurrences) + coverage (count of distinct matched terms).
#[cfg(test)]
fn score_doc(title: &str, body: &str, terms: &[String]) -> Option<(f32, String)> {
    score_lower(&title.to_lowercase(), &body.to_lowercase(), terms)
}

/// Same scoring on already-lowercased title/body — the `WikiIndex` caches the lowercased forms so the
/// hot recall path skips re-lowercasing every document on every query. Pure.
fn score_lower(tl: &str, bl: &str, terms: &[String]) -> Option<(f32, String)> {
    let mut score = 0_usize;
    let mut coverage = 0_usize;
    let mut first_hit: Option<usize> = None;
    for t in terms {
        let bc = count_occurrences(bl, t);
        let tc = count_occurrences(tl, t);
        if bc + tc > 0 {
            coverage += 1;
        }
        score += bc + 3 * tc;
        if first_hit.is_none()
            && let Some(p) = bl.find(t)
        {
            first_hit = Some(p);
        }
    }
    if score == 0 {
        return None;
    }
    score += coverage; // bonus for documents that match a broader set of search terms
    // Snippet is taken from the same lowercased body `bl` that `first_hit` indexes into,
    // so the byte offset and the slicing share one coordinate system (no case-fold drift).
    Some((
        precise_cast(score),
        snippet_around(bl, first_hit.unwrap_or(0)),
    ))
}

/// usize → f32 (lossless range at score magnitudes). Helper to avoid the clippy cast gate.
#[allow(clippy::cast_precision_loss)]
fn precise_cast(n: usize) -> f32 {
    n as f32
}

/// ~200-char snippet around `pos`, a byte offset INTO `text`. Caller must pass the same
/// string `pos` was found in (we pass the lowercased body) so byte→char conversion is exact.
/// Char-boundary safe. Pure.
fn snippet_around(text: &str, pos: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    // pos indexes `text` itself → char count of the prefix is exact (no cross-string drift).
    let char_pos = text.get(..pos).map_or(0, |s| s.chars().count());
    let start = char_pos.saturating_sub(40);
    let end = (start + 200).min(chars.len());
    let s: String = chars[start..end].iter().collect();
    let s = s.replace('\n', " ");
    if start > 0 {
        format!("…{}", s.trim())
    } else {
        s.trim().to_owned()
    }
}

fn split_frontmatter(content: &str) -> Option<(&str, &str)> {
    content.strip_prefix("---\n").and_then(|rest| {
        rest.find("\n---\n")
            .map(|end| (&rest[..end], &rest[end + 5..]))
    })
}

/// Split `--- yaml ---\nbody` + extract the title from frontmatter. If absent, the first `# ` heading, and failing that the stem. Pure.
fn extract_title_body<'a>(content: &'a str, stem: &str) -> (String, &'a str) {
    let (yaml, body) = split_frontmatter(content).unwrap_or(("", content));
    if let Some(line) = yaml.lines().find(|l| l.trim_start().starts_with("title:")) {
        let t = line
            .split_once(':')
            .map_or("", |(_, v)| v)
            .trim()
            .trim_matches('"');
        if !t.is_empty() {
            return (t.to_owned(), body);
        }
    }
    if let Some(h) = body.lines().find(|l| l.starts_with("# ")) {
        return (h[2..].trim().to_owned(), body);
    }
    (stem.to_owned(), body)
}

/// Extract a scalar frontmatter field by key, if present. Pure.
fn extract_frontmatter_scalar(content: &str, key: &str) -> String {
    split_frontmatter(content)
        .map(|(yaml, _body)| yaml)
        .and_then(|yaml| {
            yaml.lines()
                .find(|l| l.trim_start().starts_with(key))
                .and_then(|l| l.split_once(':'))
                .map(|(_, v)| v.trim().trim_matches('"').to_owned())
        })
        .unwrap_or_default()
}

fn extract_origin(content: &str) -> Result<config::Origin> {
    let origin = extract_frontmatter_scalar(content, "origin:");
    if origin.is_empty() {
        return Ok(config::Origin::Personal);
    }
    origin.parse::<config::Origin>().map_err(anyhow::Error::msg)
}

fn extract_project(content: &str) -> String {
    extract_frontmatter_scalar(content, "project:")
}

fn file_modified_time(path: &Path) -> Result<SystemTime> {
    std::fs::metadata(path)
        .with_context(|| format!("stat wiki mtime: {}", path.display()))?
        .modified()
        .with_context(|| format!("read wiki mtime: {}", path.display()))
}

/// One parsed wiki note, cached in lowercased form for scoring. `mtime` keys incremental refresh.
struct CachedDoc {
    id: String,
    title: String, // raw (for display); lowercased forms below are what scoring reads
    source_path: String,
    origin: config::Origin,
    project: String,
    title_lower: String,
    body_lower: String,
    mtime: SystemTime,
}

/// In-memory wiki index — parses `vault/wiki/*.md` once and keeps the lowercased title/body cached so
/// the per-query recall path scores in memory instead of re-reading + re-lowercasing every file. The
/// index is **honest, not stale**: `refresh()` re-stats the dir each call and re-reads only files whose
/// mtime changed (catching out-of-band edits, e.g. Obsidian), and drops vanished files. Reading bodies
/// — the expensive part — happens only on first sight or change. (Layer 1 honesty kept; Layer 3 cost cut.)
#[derive(Default)]
pub struct WikiIndex {
    docs: HashMap<PathBuf, CachedDoc>,
}

impl WikiIndex {
    /// Reconcile the cache with `vault/wiki/`: re-read changed/new `.md` (by mtime), drop removed ones.
    /// Missing wiki dir is graceful (cache cleared → empty recall). I/O shell; scoring stays pure.
    pub fn refresh(&mut self, wiki_dir: &Path) -> Result<()> {
        let Ok(read_dir) = std::fs::read_dir(wiki_dir) else {
            self.docs.clear(); // wiki doesn't exist yet — normal (empty recall), not a stale lie
            return Ok(());
        };
        let mut seen: HashSet<PathBuf> = HashSet::new();
        for entry in read_dir {
            let path = entry.context("reading wiki dir entry")?.path();
            if path.extension().and_then(|e| e.to_str()) != Some("md") {
                continue;
            }
            let source_path = path.to_string_lossy().into_owned();
            if is_internal_eval_fixture_path(&source_path) {
                self.docs.remove(&path);
                continue;
            }
            seen.insert(path.clone());
            let mtime = file_modified_time(&path)?;
            // Up-to-date cache entry → skip the expensive read+parse.
            if self.docs.get(&path).is_some_and(|c| c.mtime == mtime) {
                continue;
            }
            let Ok(content) = std::fs::read_to_string(&path) else {
                self.docs.remove(&path);
                continue; // skip on read failure (graceful)
            };
            if raw_has_generated_brief_tag(&content) {
                self.docs.remove(&path);
                continue;
            }
            let stem = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned();
            let (title, body) = extract_title_body(&content, &stem);
            let origin = extract_origin(&content)
                .with_context(|| format!("parse wiki origin: {}", path.display()))?;
            let project = extract_project(&content);
            self.docs.insert(
                path.clone(),
                CachedDoc {
                    id: stem,
                    title_lower: title.to_lowercase(),
                    body_lower: body.to_lowercase(),
                    title,
                    source_path,
                    origin,
                    project,
                    mtime,
                },
            );
        }
        self.docs.retain(|p, _| seen.contains(p)); // drop vanished notes
        Ok(())
    }

    /// Top-K notes closest to `query`, scored over the cached (lowercased) corpus. Pure — no I/O.
    /// Optional `project`/`since_hours` filters are applied before scoring.
    #[must_use]
    pub fn search(
        &self,
        query: &str,
        k: usize,
        project: Option<&str>,
        exclude_origins: &[String],
        since_hours: Option<i32>,
    ) -> Vec<WikiHit> {
        let terms = query_terms(query);
        if terms.is_empty() {
            return Vec::new();
        }
        let cutoff = since_hours.map(|h| {
            let hours = u64::from(h.max(0).unsigned_abs());
            let secs = hours * SECONDS_PER_HOUR;
            SystemTime::now() - std::time::Duration::from_secs(secs)
        });
        let mut hits: Vec<WikiHit> = self
            .docs
            .values()
            .filter(|d| {
                project.is_none_or(|project| d.project == project)
                    && !exclude_origins
                        .iter()
                        .any(|origin| origin == d.origin.as_str())
                    && cutoff.is_none_or(|cutoff| d.mtime >= cutoff)
            })
            .filter_map(|d| {
                score_lower(&d.title_lower, &d.body_lower, &terms).map(|(score, snippet)| WikiHit {
                    id: d.id.clone(),
                    title: d.title.clone(),
                    origin: d.origin.as_str().to_owned(),
                    project: d.project.clone(),
                    source_path: d.source_path.clone(),
                    snippet,
                    score,
                })
            })
            .collect();
        hits.sort_by(|a, b| {
            b.score
                .total_cmp(&a.score)
                .then_with(|| a.source_path.cmp(&b.source_path))
        });
        hits.truncate(k);
        hits
    }
}

/// One-shot recall (CLI / non-cached callers): build a fresh index, refresh, search. The resident
/// daemon instead holds a persistent `WikiIndex` (in `AppState`) so repeated `/search` skips re-reads.
/// Missing wiki directory · read failure are graceful: empty result/skip (recall never panics).
pub fn recall(
    wiki_dir: &Path,
    query: &str,
    k: usize,
    project: Option<&str>,
    exclude_origins: &[String],
    since_hours: Option<i32>,
) -> Result<Vec<WikiHit>> {
    let mut index = WikiIndex::default();
    index.refresh(wiki_dir)?;
    Ok(index.search(query, k, project, exclude_origins, since_hours))
}

#[must_use]
pub(crate) fn trim_hits_to_budget(
    hits: Vec<WikiHit>,
    max_results: usize,
    max_chars: usize,
) -> Vec<WikiHit> {
    if max_results == 0 || max_chars == 0 {
        return Vec::new();
    }
    let per_hit_cap = max_chars / max_results;
    let mut budget = max_chars;
    let mut out = Vec::new();
    for mut hit in hits {
        if out.len() >= max_results {
            break;
        }
        let take = per_hit_cap.min(budget);
        if take == 0 {
            break;
        }
        let cut = hit.snippet.chars().take(take).collect::<String>();
        if cut.is_empty() {
            continue;
        }
        budget -= cut.chars().count();
        hit.snippet = cut;
        out.push(hit);
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::float_cmp
    )]
    use super::{
        WikiHit, WikiIndex, count_occurrences, extract_title_body, file_modified_time, query_terms,
        score_doc, trim_hits_to_budget,
    };
    use crate::frontmatter::GENERATED_BRIEF_TAG;

    fn hit(id: &str, snippet: &str) -> WikiHit {
        WikiHit {
            id: id.to_owned(),
            title: id.to_owned(),
            origin: "personal".to_owned(),
            project: "omb".to_owned(),
            source_path: format!("vault/wiki/{id}.md"),
            snippet: snippet.to_owned(),
            score: 1.0,
        }
    }

    #[test]
    fn wiki_recall_budget_trims_each_hit_and_total_results() {
        let out = trim_hits_to_budget(
            vec![
                hit("a", "abcdefghijk"),
                hit("b", "1234567890"),
                hit("c", "extra"),
            ],
            2,
            10,
        );

        assert_eq!(out.len(), 2);
        assert_eq!(out[0].snippet, "abcde");
        assert_eq!(out[1].snippet, "12345");
        assert_eq!(
            out.iter().map(|h| h.snippet.chars().count()).sum::<usize>(),
            10
        );
    }

    #[test]
    fn wiki_recall_budget_uses_character_counts_not_bytes() {
        let out = trim_hits_to_budget(vec![hit("ko", "가나다라마바")], 1, 4);

        assert_eq!(out[0].snippet, "가나다라");
        assert_eq!(out[0].snippet.len(), 12);
        assert_eq!(out[0].snippet.chars().count(), 4);
    }

    #[test]
    fn wiki_index_refresh_is_incremental_and_honest() {
        use std::time::{Duration, SystemTime};
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: docker cache\n---\nlayer caching tips",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: pg pool\n---\ntoo many clients fix",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        // search hits the right note by content
        let hits = idx.search("docker layer", 5, None, &[], None);
        assert_eq!(hits.first().map(|h| h.id.as_str()), Some("wiki-0001"));
        assert!(
            idx.search("clients", 5, None, &[], None)
                .iter()
                .any(|h| h.id == "wiki-0002")
        );

        // OUT-OF-BAND edit: rewrite 0001's body + push mtime forward → refresh must pick it up (honest, not stale).
        let future = SystemTime::now() + Duration::from_secs(5);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: docker cache\n---\nkubernetes oomkilled memory",
        )
        .unwrap();
        filetime_set(&p("wiki-0001.md"), future);
        idx.refresh(dir.path()).unwrap();
        assert!(
            idx.search("kubernetes oomkilled", 5, None, &[], None)
                .iter()
                .any(|h| h.id == "wiki-0001")
        );
        assert!(
            idx.search("layer caching", 5, None, &[], None).is_empty(),
            "stale body must be gone"
        );

        // VANISHED file drops out of the index.
        std::fs::remove_file(p("wiki-0002.md")).unwrap();
        idx.refresh(dir.path()).unwrap();
        assert!(
            idx.search("clients", 5, None, &[], None).is_empty(),
            "removed note must not be recalled"
        );
    }

    #[test]
    fn refresh_drops_cached_note_when_path_becomes_unreadable() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        let note_path = p("wiki-0001.md");
        std::fs::write(&note_path, "---\ntitle: cached\n---\nstale body").unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        assert_eq!(
            idx.search("stale body", 5, None, &[], None)
                .first()
                .map(|h| h.id.as_str()),
            Some("wiki-0001")
        );

        std::fs::remove_file(&note_path).unwrap();
        std::fs::create_dir(&note_path).unwrap();
        idx.refresh(dir.path()).unwrap();

        assert!(
            idx.search("stale body", 5, None, &[], None).is_empty(),
            "unreadable wiki path must drop the stale cached document"
        );
    }

    #[test]
    fn file_modified_time_reports_missing_wiki_path_without_now_fallback() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("missing.md");

        let err = file_modified_time(&missing).unwrap_err();

        assert!(format!("{err:#}").contains("stat wiki mtime"), "{err:#}");
    }

    // Set mtime without an extra dep: write via std then bump using a second write is unreliable, so
    // we just touch through OpenOptions + set_modified (stable since 1.75).
    fn filetime_set(path: &std::path::Path, t: std::time::SystemTime) {
        let f = std::fs::OpenOptions::new().write(true).open(path).unwrap();
        f.set_modified(t).unwrap();
    }

    #[test]
    fn search_filters_by_project() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: docker cache\nproject: omb\n---\nlayer caching tips",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: pg pool\nproject: kb-rag-bot\n---\ntoo many clients fix",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("tips", 5, Some("omb"), &[], None);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "wiki-0001");
        assert!(
            idx.search("tips", 5, Some("kb-rag-bot"), &[], None)
                .is_empty()
        );
    }

    #[test]
    fn search_filters_by_origin() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: personal loop\norigin: personal\nproject: omb\n---\nshared loop content",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: company loop\norigin: company\nproject: omb\n---\nshared loop content",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let all = idx.search("shared loop", 5, Some("omb"), &[], None);
        assert_eq!(all.len(), 2);

        let exclude_company = vec!["company".to_owned()];
        let hits = idx.search("shared loop", 5, Some("omb"), &exclude_company, None);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "wiki-0001");
        assert_eq!(hits[0].origin, "personal");
        assert_eq!(hits[0].project, "omb");
    }

    #[test]
    fn search_tie_breaks_equal_scores_by_source_path() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: tie\nproject: omb\n---\nshared token",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: tie\nproject: omb\n---\nshared token",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("shared token", 5, Some("omb"), &[], None);

        assert_eq!(hits.len(), 2);
        assert!(hits[0].score == hits[1].score);
        assert!(hits[0].source_path < hits[1].source_path);
        assert_eq!(hits[0].id, "wiki-0001");
        assert_eq!(hits[1].id, "wiki-0002");
    }

    #[test]
    fn search_treats_missing_origin_as_personal() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: legacy loop\nproject: omb\n---\nlegacy loop content",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("legacy loop", 5, Some("omb"), &[], None);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].origin, "personal");

        let exclude_personal = vec!["personal".to_owned()];
        assert!(
            idx.search("legacy loop", 5, Some("omb"), &exclude_personal, None)
                .is_empty(),
            "legacy notes without origin must not bypass personal-only exclusion"
        );
    }

    #[test]
    fn refresh_rejects_invalid_origin() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: bad origin\norigin: workplace\nproject: omb\n---\ncontent",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        let err = idx.refresh(dir.path()).unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("parse wiki origin"));
        assert!(msg.contains("invalid origin: workplace"));
    }

    #[test]
    fn search_excludes_generated_briefs_and_eval_fixtures() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: source loop contract\nproject: omb\n---\nloop contract source memory",
        )
        .unwrap();
        std::fs::write(
            p("daily-brief-2026-07-02.md"),
            format!(
                "---\ntitle: generated loop contract\nproject: omb\ntags: [{GENERATED_BRIEF_TAG}]\n---\nloop contract generated summary"
            ),
        )
        .unwrap();
        std::fs::write(
            p("eval-loop-contract.md"),
            "---\ntitle: eval loop contract\nproject: omb\n---\nloop contract eval fixture",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("loop contract", 5, Some("omb"), &[], None);

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "wiki-0001");
    }

    #[test]
    fn search_filters_by_since_hours() {
        use std::time::{Duration, SystemTime};
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: recent\nproject: omb\n---\nrecent content",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: old\nproject: omb\n---\nold content",
        )
        .unwrap();

        // Make the second file 2 days old so a 24-hour window excludes it.
        let old = SystemTime::now() - Duration::from_hours(48);
        filetime_set(&p("wiki-0002.md"), old);

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("content", 5, None, &[], Some(24));
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "wiki-0001");
    }

    #[test]
    fn search_combines_project_and_since_filters() {
        use std::time::{Duration, SystemTime};
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: recent omb\nproject: omb\n---\nshared content",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: old omb\nproject: omb\n---\nshared content",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0003.md"),
            "---\ntitle: recent other\nproject: other\n---\nshared content",
        )
        .unwrap();
        let old = SystemTime::now() - Duration::from_hours(48);
        filetime_set(&p("wiki-0002.md"), old);

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("content", 5, Some("omb"), &[], Some(24));

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "wiki-0001");
    }

    #[test]
    fn query_terms_splits_and_filters() {
        assert_eq!(query_terms("bge-m3 임베딩 a"), vec!["bge-m3", "임베딩"]); // 1-char 'a' excluded
    }

    #[test]
    fn count_occurrences_non_overlapping() {
        assert_eq!(count_occurrences("aaaa", "aa"), 2);
        assert_eq!(count_occurrences("임베딩은 임베딩", "임베딩"), 2);
        assert_eq!(count_occurrences("none", "x"), 0);
    }

    #[test]
    fn score_doc_substring_handles_korean_josa() {
        // the term "임베딩" must catch the body "임베딩은" (with attached particle) via substring match
        let terms = query_terms("임베딩 차원");
        let (score, snip) = score_doc("벡터 노트", "bge-m3 임베딩은 1024차원이다", &terms)
            .expect("부분일치로 점수 나야 함");
        assert!(score > 0.0);
        assert!(snip.contains("임베딩"));
    }

    #[test]
    fn score_doc_title_weighted_and_zero_is_none() {
        let terms = query_terms("docker");
        let in_title = score_doc("docker 캐시", "본문 무관", &terms).unwrap().0;
        let in_body = score_doc("무관", "docker 한 번", &terms).unwrap().0;
        assert!(in_title > in_body, "title 일치가 더 높아야");
        assert!(score_doc("무관", "전혀 다른 내용", &terms).is_none());
    }

    #[test]
    fn extract_title_from_frontmatter_then_heading_then_stem() {
        let fm = "---\nid: wiki-0001\ntitle: 제목A\n---\n본문";
        assert_eq!(extract_title_body(fm, "wiki-0001").0, "제목A");
        let h = "# 헤딩B\n본문";
        assert_eq!(extract_title_body(h, "wiki-0002").0, "헤딩B");
        assert_eq!(
            extract_title_body("프런트매터 없음", "wiki-0003").0,
            "wiki-0003"
        );
    }
}
