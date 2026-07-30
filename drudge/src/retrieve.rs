//! Retrieval pipeline — vector + BM25 full-text → RRF merge → optional graph rerank → top-k / budget-aware. origin filter.
//!
//! Cross-reference: design decision D3 (read door open) · ENFORCEMENT.md §B (one-way flow).
//!   - `/search` keeps the raw RRF ranking as the external accuracy contract.
//!   - `/ask` enables a lightweight graph reranker (shared tools/concepts/claims + degree + recency).
use std::collections::HashMap;
use std::fmt::Write;

use anyhow::{Context, Result};

use crate::llm::Llm;
use crate::store::{GraphScore, Hit, Store};

const RRF_K: f64 = 60.0; // RRF denominator constant (de facto standard)

/// Compute an RRF term. rank is 1-based (0 not allowed). Err if usize → f64 conversion fails.
fn rrf_term(rank: usize) -> Result<f64> {
    // pool is at most a few hundred — exceeding u32 range is practically impossible, but the type must be the evidence.
    let r = f64::from(u32::try_from(rank).context("rrf rank to u32")?);
    Ok(1.0 / (RRF_K + r))
}

/// Shared RRF merge. Returns merged + sorted hits, origin-filtered, but not yet truncated.
/// Each hit carries the fused RRF score in `Hit.score` for downstream rerankers.
fn merge_hits(
    vec_hits: Vec<Hit>,
    txt_hits: Vec<Hit>,
    exclude_origins: &[String],
) -> Result<Vec<Hit>> {
    let mut fused: HashMap<String, f64> = HashMap::new();
    let mut byid: HashMap<String, Hit> = HashMap::new();
    for (rank, h) in vec_hits.into_iter().enumerate() {
        *fused.entry(h.id.clone()).or_insert(0.0) += rrf_term(rank + 1)?;
        byid.entry(h.id.clone()).or_insert(h);
    }
    for (rank, h) in txt_hits.into_iter().enumerate() {
        *fused.entry(h.id.clone()).or_insert(0.0) += rrf_term(rank + 1)?;
        byid.entry(h.id.clone()).or_insert(h);
    }

    let mut merged: Vec<Hit> = byid
        .into_values()
        .map(|mut h| {
            h.score = *fused.get(&h.id).unwrap_or(&0.0);
            h
        })
        .filter(|h| !exclude_origins.iter().any(|o| o == &h.origin))
        .collect();
    merged.sort_by(|a, b| b.score.total_cmp(&a.score));
    Ok(merged)
}

/// Vector top-N + BM25 top-N → RRF position-based merge → optional graph rerank → top-k.
/// Optional `project`/`since_hours` narrow the pool before ranking.
/// When `rerank` is true, graph-signal features are fetched for the top merged hit and
/// mixed into the RRF score before truncation. `/search` keeps this off to preserve its
/// external accuracy contract; `/ask` enables it.
#[allow(clippy::too_many_arguments)] // filtering flags grow the surface; a struct is overkill at 2 flags.
pub async fn retrieve(
    store: &Store,
    llm: &Llm,
    query: &str,
    top_k: usize,
    exclude_origins: &[String],
    project: Option<&str>,
    since_hours: Option<i32>,
    rerank: bool,
) -> Result<Vec<Hit>> {
    let pool = (top_k * 4).max(20);
    let qe = llm.embed(query).await?;
    let vec_hits = store
        .vector_search_filtered(&qe, pool, exclude_origins, project, since_hours)
        .await?;
    let txt_hits = store
        .text_search_filtered(query, pool, exclude_origins, project, since_hours)
        .await?;
    let mut merged = merge_hits(vec_hits, txt_hits, exclude_origins)?;
    if rerank {
        merged = rerank_by_graph_store(store, merged, GRAPH_RERANK_ALPHA).await?;
    }
    merged.truncate(top_k);
    Ok(merged)
}

/// Token-/character-budget aware retrieval.
///
/// Returns up to `max_results` hits whose total `content` length does not exceed `max_chars`.
/// Each hit is individually capped to `max_chars / max_results` so a single huge chunk cannot
/// consume the whole budget. This lets agents call `recall` with a safe token ceiling.
#[allow(clippy::too_many_arguments)] // filtering flags grow the surface; a struct is overkill at 2 flags.
pub async fn retrieve_budget(
    store: &Store,
    llm: &Llm,
    query: &str,
    max_results: usize,
    max_chars: usize,
    exclude_origins: &[String],
    project: Option<&str>,
    since_hours: Option<i32>,
    rerank: bool,
) -> Result<Vec<Hit>> {
    if max_results == 0 || max_chars == 0 {
        return Ok(Vec::new());
    }
    let pool = (max_results * 4).max(20);
    let qe = llm.embed(query).await?;
    let vec_hits = store
        .vector_search_filtered(&qe, pool, exclude_origins, project, since_hours)
        .await?;
    let txt_hits = store
        .text_search_filtered(query, pool, exclude_origins, project, since_hours)
        .await?;
    let mut merged = merge_hits(vec_hits, txt_hits, exclude_origins)?;
    if rerank {
        merged = rerank_by_graph_store(store, merged, GRAPH_RERANK_ALPHA).await?;
    }
    Ok(trim_hits_to_budget(merged, max_results, max_chars))
}

const GRAPH_RERANK_ALPHA: f64 = 0.5;

/// Fetch graph-signal features for the top merged hit and rerank the merged list.
async fn rerank_by_graph_store(store: &Store, merged: Vec<Hit>, alpha: f64) -> Result<Vec<Hit>> {
    if merged.len() < 2 {
        return Ok(merged);
    }
    let top = &merged[0];
    let features = store.graph_rerank_features(top, &merged).await?;
    Ok(rerank_by_graph(merged, &features, alpha))
}

/// Graph-signal reranking: boost candidates that share tools/concepts/claims with the
/// top vector hit, have high graph degree, and are recent. Uses min-max normalization
/// so the four signals are on the same scale and the mix stays deterministic.
/// The top vector hit is kept as the anchor and only the remaining candidates are reordered.
pub fn rerank_by_graph(merged: Vec<Hit>, features: &[GraphScore], alpha: f64) -> Vec<Hit> {
    if merged.len() != features.len() || merged.len() < 2 {
        return merged;
    }
    let tools: Vec<f64> = features.iter().map(|f| f64::from(f.shared_tools)).collect();
    let claims: Vec<f64> = features
        .iter()
        .map(|f| f64::from(f.shared_claims))
        .collect();
    let degrees: Vec<f64> = features.iter().map(|f| f64::from(f.degree)).collect();
    let recency: Vec<f64> = features
        .iter()
        .map(|f| 1.0 / (1.0 + f.recency_hours / 24.0))
        .collect();
    let norm_tools = min_max_normalize(&tools);
    let norm_claims = min_max_normalize(&claims);
    let norm_degrees = min_max_normalize(&degrees);

    let mut extracted: Vec<Option<Hit>> = merged.into_iter().map(Some).collect();
    let mut order: Vec<usize> = (1..extracted.len()).collect();
    order.sort_by(|&a, &b| {
        let score_a = extracted[a].as_ref().map_or(0.0, |h| {
            h.score + alpha * (norm_tools[a] + norm_claims[a] + norm_degrees[a] + recency[a])
        });
        let score_b = extracted[b].as_ref().map_or(0.0, |h| {
            h.score + alpha * (norm_tools[b] + norm_claims[b] + norm_degrees[b] + recency[b])
        });
        score_b.total_cmp(&score_a).then(a.cmp(&b))
    });

    let mut out = Vec::with_capacity(extracted.len());
    if let Some(mut h) = extracted[0].take() {
        h.score += alpha * (norm_tools[0] + norm_claims[0] + norm_degrees[0] + recency[0]);
        out.push(h);
    }
    for i in order {
        if let Some(mut h) = extracted[i].take() {
            h.score += alpha * (norm_tools[i] + norm_claims[i] + norm_degrees[i] + recency[i]);
            out.push(h);
        }
    }
    out
}

fn min_max_normalize(values: &[f64]) -> Vec<f64> {
    let min = values.iter().copied().fold(f64::INFINITY, f64::min);
    let max = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let range = max - min;
    if range <= 0.0 {
        return values.iter().map(|_| 0.0).collect();
    }
    values.iter().map(|v| (v - min) / range).collect()
}

fn trim_hits_to_budget(merged: Vec<Hit>, max_results: usize, max_chars: usize) -> Vec<Hit> {
    let per_hit_cap = max_chars / max_results;
    let mut budget = max_chars;
    let mut out = Vec::new();
    for mut h in merged {
        if out.len() >= max_results {
            break;
        }
        let take = per_hit_cap.min(budget);
        if take == 0 {
            break;
        }
        let cut = h.content.chars().take(take).collect::<String>();
        if cut.is_empty() {
            continue;
        }
        budget -= cut.chars().count();
        h.content = cut;
        out.push(h);
    }
    out
}

/// Detect whether a query smells like a coding question.
/// Code tokens trigger the code-context lane in `/ask`.
/// Identifier shapes (snake_case, camelCase, `::` paths) must be checked on the
/// original case — lowercasing would erase them. The Python hook gate
/// (`code_recall_core.is_code_query`) mirrors this; keep the two in sync.
pub fn is_code_query(query: &str) -> bool {
    /// Whole-word code keywords — matched on word boundaries so `use` inside
    /// `because` does not fire the code lane.
    const CODE_KEYWORDS: &[&str] = &[
        "fn", "def", "class", "import", "use", "struct", "enum", "trait", "function", "method",
        "variable", "constant",
    ];
    let q = query.to_lowercase();
    q.contains("::")
        || q.contains(".rs")
        || q.contains(".py")
        || q.contains(".ts")
        || q.contains(".tsx")
        || q.contains(".kt")
        || q.split_whitespace().any(|w| {
            let w = w.trim_matches(|c: char| !c.is_alphanumeric());
            CODE_KEYWORDS.contains(&w)
        })
        || query.split_whitespace().any(|w| {
            (w.contains('_') && w.chars().any(char::is_lowercase))
                || w.chars().skip(1).any(char::is_uppercase)
        })
}

/// Fetch code symbols relevant to a query. Returns at most `max_symbols` symbols,
/// each capped to `max_chars_per_symbol` characters of signature/context.
pub async fn code_context(
    store: &Store,
    query: &str,
    max_symbols: usize,
    max_chars_per_symbol: usize,
) -> Result<Vec<String>> {
    let symbols = store
        .search_code_symbols(query, i64::try_from(max_symbols).unwrap_or(10))
        .await?;
    let mut out = Vec::new();
    for sym in symbols.into_iter().take(max_symbols) {
        let mut s = format!("{}: {}", sym.kind.as_str(), sym.name);
        if !sym.source_path.is_empty() {
            let _ = write!(s, " ({})", sym.source_path);
        }
        if !sym.signature.is_empty() {
            let sig = sym
                .signature
                .chars()
                .take(max_chars_per_symbol)
                .collect::<String>();
            let _ = write!(s, " — {sig}");
        }
        out.push(s);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]

    use super::{min_max_normalize, rerank_by_graph, trim_hits_to_budget};
    use crate::store::{GraphScore, Hit};

    fn hit(id: &str, content: &str, score: f64) -> Hit {
        Hit {
            id: id.to_owned(),
            content: content.to_owned(),
            origin: "personal".to_owned(),
            project: "omb".to_owned(),
            source_path: format!("vault/wiki/{id}.md"),
            dist: 0.0,
            score,
        }
    }

    #[test]
    fn retrieve_budget_trims_each_hit_and_total_results() {
        let out = trim_hits_to_budget(
            vec![
                hit("a", "abcdefghijk", 1.0),
                hit("b", "1234567890", 0.8),
                hit("c", "extra", 0.6),
            ],
            2,
            10,
        );

        assert_eq!(out.len(), 2);
        assert_eq!(out[0].content, "abcde");
        assert_eq!(out[1].content, "12345");
        assert_eq!(
            out.iter().map(|h| h.content.chars().count()).sum::<usize>(),
            10
        );
    }

    #[test]
    fn retrieve_budget_uses_character_counts_not_bytes() {
        let out = trim_hits_to_budget(vec![hit("ko", "가나다라마바", 1.0)], 1, 4);

        assert_eq!(out[0].content, "가나다라");
        assert_eq!(out[0].content.len(), 12);
        assert_eq!(out[0].content.chars().count(), 4);
    }

    #[test]
    fn rerank_by_graph_boosts_linked_candidates() {
        let merged = vec![
            hit("top", "top content", 1.0),
            hit("linked", "linked content", 0.5),
            hit("unlinked", "unlinked content", 0.5),
        ];
        let features = vec![
            GraphScore {
                shared_tools: 0,
                shared_claims: 0,
                degree: 0,
                recency_hours: 100.0,
            },
            GraphScore {
                shared_tools: 5,
                shared_claims: 0,
                degree: 10,
                recency_hours: 1.0,
            },
            GraphScore {
                shared_tools: 0,
                shared_claims: 0,
                degree: 1,
                recency_hours: 100.0,
            },
        ];
        let out = rerank_by_graph(merged, &features, 1.0);
        assert_eq!(out[0].id, "top");
        assert_eq!(out[1].id, "linked");
        assert_eq!(out[2].id, "unlinked");
    }

    #[test]
    fn min_max_normalize_handles_constant_values() {
        assert_eq!(min_max_normalize(&[5.0, 5.0, 5.0]), vec![0.0, 0.0, 0.0]);
        assert_eq!(min_max_normalize(&[0.0, 5.0, 10.0]), vec![0.0, 0.5, 1.0]);
    }

    #[test]
    fn is_code_query_detects_code_signals() {
        use super::is_code_query;
        assert!(is_code_query("how does parse_file work"));
        assert!(is_code_query("where is AppState defined"));
        assert!(is_code_query("fix the def in foo.py"));
        assert!(is_code_query("store::open connection retry"));
        assert!(!is_code_query("yesterday meeting notes"));
    }
}
