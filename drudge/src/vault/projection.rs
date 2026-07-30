//! Graph → Obsidian `relates_to` projection.
//!
//! Cross-reference: design decision D7 (vault/wiki SSOT, DB rebuildable).
use std::path::Path;

use anyhow::{Context, Result};

use crate::frontmatter::raw_has_generated_brief_tag;
use crate::store::Store;
use crate::vault::{is_seed_note, set_relates_to, split_frontmatter, wiki_stem, write_atomic};

const CLAIM_RELATED_LIMIT: i64 = 4;
const SEMANTIC_RELATED_LIMIT: i64 = 4;
const PROJECT_RECENCY_LINK_MIN: usize = 2;
const PROJECT_RECENCY_LIMIT: i64 = 2;
const PROJECT_RELATED_LINK_CAP: usize = 8;

/// Project the Postgres graph (`related_docs`) into each wiki note's `relates_to` wikilinks.
/// Makes the Obsidian graph view draw the GraphRAG connections directly. Idempotent (recomputed and rewritten every time).
/// Among related documents, only wiki notes in the same vault become `[[wiki-NNNN]]` (so Obsidian can resolve them).
/// The shipped seed note (`wiki-0000`) is skipped so private note ids never leak into the public sample.
pub async fn project_links(store: &Store, vault_root: &Path, limit: i64) -> Result<usize> {
    let wiki_dir = vault_root.join("wiki");
    let mut updated = 0;
    for entry in std::fs::read_dir(&wiki_dir)
        .with_context(|| format!("failed to read wiki dir: {}", wiki_dir.display()))?
    {
        if project_note(store, &entry?.path(), limit).await? {
            updated += 1;
        }
    }
    Ok(updated)
}

/// Project ONE wiki note's `relates_to` from the graph + claim-axis + semantic + project-recency fallbacks.
/// `Ok(true)` when the file was rewritten. The single-note unit shared by the full pass
/// (`project_links`) and the `remember` fast path — so the logic (incl. the seed-note id-leak guard
/// and the "don't wipe to []" rule) lives in exactly one place (SSOT).
///
/// remember projects only the new note with this; the *backlinks* from its neighbors are reconciled by
/// the next periodic full `project_links`. That lag is invisible to retrieval (recall is
/// embedding-based, not relates_to-based) and only delays an Obsidian graph edge — eventually
/// consistent, never a stale lie.
pub async fn project_note(store: &Store, path: &Path, limit: i64) -> Result<bool> {
    let stem = path.file_stem().and_then(|s| s.to_str());
    let stem_ok = stem.is_some_and(|n| n.starts_with("wiki-"));
    let ext_ok = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("md"));
    if !(stem_ok && ext_ok) {
        return Ok(false);
    }
    // Never rewrite the tracked seed note: a graph projection would fill its
    // shipped-empty relates_to with the user's PRIVATE note ids (id leak).
    if stem.is_some_and(is_seed_note) {
        return Ok(false);
    }
    let content = std::fs::read_to_string(path)?;
    let Some((yaml, body)) = projectable_frontmatter(&content) else {
        return Ok(false);
    };
    let src_path = path.to_string_lossy().into_owned();
    // Claim continuity: docs that share a `(subject,predicate)` claim are usually the before/after
    // trail for the same durable fact or decision axis. Add them first so exact temporal authority
    // is not pushed out by broad graph or embedding neighbors when the link cap is reached.
    let claim_paths = store
        .claim_related_docs(&src_path, CLAIM_RELATED_LIMIT)
        .await?;
    // Exact graph relations: docs sharing concrete semantic nodes (tools/concepts only).
    let graph_paths = store.related_docs(&src_path, limit).await?;
    // Semantic blend: the graph above only links docs sharing >=2 EXACT concept/tool slugs, so it
    // misses notes about the same thing in DIFFERENT words. Add meaning-nearest docs only when the
    // store can corroborate them by same-project or shared graph/claim evidence, deduped with above.
    let semantic_paths = store
        .semantic_related_docs(&src_path, SEMANTIC_RELATED_LIMIT, 0.40)
        .await?;
    // isolation prevention: STILL fewer than 2 links → supplement with the same project's latest docs.
    let base_stems = projected_wiki_stems(&claim_paths, &graph_paths, &semantic_paths, &[]);
    let recent_paths = if base_stems.len() < PROJECT_RECENCY_LINK_MIN {
        store
            .recent_project_docs(&src_path, PROJECT_RECENCY_LIMIT)
            .await?
    } else {
        Vec::new()
    };
    let stems = if recent_paths.is_empty() {
        base_stems
    } else {
        projected_wiki_stems(&claim_paths, &graph_paths, &semantic_paths, &recent_paths)
    };
    let links: Vec<String> = stems.iter().map(|s| format!("\"[[{s}]]\"")).collect();
    // Don't wipe: if the graph projection found nothing, preserve whatever relates_to the compile
    // relation-pass (shared tools/concepts) already set — an empty graph must not clobber it to [].
    if links.is_empty() {
        return Ok(false);
    }
    let new_content = format!("---\n{}\n---\n{body}", set_relates_to(yaml, &links));
    if new_content != content {
        write_atomic(path, new_content)?;
        return Ok(true);
    }
    Ok(false)
}

fn projectable_frontmatter(content: &str) -> Option<(&str, &str)> {
    if raw_has_generated_brief_tag(content) {
        return None;
    }
    split_frontmatter(content)
}

fn projected_wiki_stems(
    claim_paths: &[String],
    graph_paths: &[String],
    semantic_paths: &[String],
    recent_paths: &[String],
) -> Vec<String> {
    let mut stems = Vec::new();
    push_unique_wiki_stems(&mut stems, claim_paths);
    push_unique_wiki_stems(&mut stems, graph_paths);
    push_unique_wiki_stems(&mut stems, semantic_paths);
    if stems.len() < PROJECT_RECENCY_LINK_MIN {
        push_unique_wiki_stems(&mut stems, recent_paths);
    }
    stems.truncate(PROJECT_RELATED_LINK_CAP); // cap relates_to so a hub note doesn't explode into a mesh
    stems
}

fn push_unique_wiki_stems(stems: &mut Vec<String>, paths: &[String]) {
    for path in paths {
        push_unique_wiki_stem(stems, path);
    }
}

fn push_unique_wiki_stem(stems: &mut Vec<String>, path: &str) {
    if let Some(stem) = wiki_stem(path)
        && !stems.contains(&stem)
    {
        stems.push(stem);
    }
}

#[cfg(test)]
mod tests {
    use super::{projectable_frontmatter, projected_wiki_stems, push_unique_wiki_stem};

    #[test]
    fn projectable_frontmatter_excludes_generated_brief_sources() {
        assert!(projectable_frontmatter("---\ntags: [daily-brief]\n---\nsummary").is_none());
        assert!(projectable_frontmatter("---\ntags: [source-memory]\n---\nbody").is_some());
    }

    #[test]
    fn push_unique_wiki_stem_keeps_wiki_links_deduped() {
        let mut stems = vec!["wiki-0001".to_owned()];

        push_unique_wiki_stem(&mut stems, "/vault/wiki/wiki-0002.md");
        push_unique_wiki_stem(&mut stems, "/vault/wiki/wiki-0002.md");
        push_unique_wiki_stem(&mut stems, "/vault/raw/session.md");

        assert_eq!(stems, vec!["wiki-0001".to_owned(), "wiki-0002".to_owned()]);
    }

    #[test]
    fn projected_wiki_stems_keeps_claim_axis_before_cap() {
        let claim_paths = paths(&["wiki-0101", "wiki-0102"]);
        let graph_paths = paths(&[
            "wiki-0201",
            "wiki-0202",
            "wiki-0203",
            "wiki-0204",
            "wiki-0205",
            "wiki-0206",
            "wiki-0207",
            "wiki-0208",
        ]);
        let semantic_paths = paths(&["wiki-0301"]);
        let stems = projected_wiki_stems(&claim_paths, &graph_paths, &semantic_paths, &[]);

        assert_eq!(
            stems,
            vec![
                "wiki-0101",
                "wiki-0102",
                "wiki-0201",
                "wiki-0202",
                "wiki-0203",
                "wiki-0204",
                "wiki-0205",
                "wiki-0206",
            ]
        );
    }

    #[test]
    fn projected_wiki_stems_uses_recency_only_for_isolated_notes() {
        let recent_paths = paths(&["wiki-0401", "wiki-0402"]);
        assert_eq!(
            projected_wiki_stems(&[], &[], &[], &recent_paths),
            vec!["wiki-0401", "wiki-0402"]
        );
        assert_eq!(
            projected_wiki_stems(
                &paths(&["wiki-0101"]),
                &paths(&["wiki-0201"]),
                &[],
                &recent_paths
            ),
            vec!["wiki-0101", "wiki-0201"]
        );
    }

    fn paths(stems: &[&str]) -> Vec<String> {
        stems
            .iter()
            .map(|stem| format!("/vault/wiki/{stem}.md"))
            .collect()
    }
}
