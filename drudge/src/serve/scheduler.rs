//! Background sync/compact scheduler and the deterministic re-ingest cycle.
//!
//! Cross-reference: design decision D7 (vault/wiki SSOT, DB rebuildable).
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::Mutex;

use anyhow::{Context, Result, bail};

use crate::ask;
use crate::audit;
use crate::config;
use crate::frontmatter::GENERATED_BRIEF_TAG;
use crate::ingest;
use crate::llm::Llm;
use crate::store::{CompactSummary, Store};
use crate::vault;

const SECONDS_PER_HOUR: u64 = 3_600;
const SCHEDULER_DEFAULT_SYNC_HOURS: u64 = 4;
const SCHEDULER_DEFAULT_COMPACT_HOURS: u64 = 24;
const SCHEDULER_DEFAULT_BRIEF_HOUR: u32 = 8;

pub(crate) struct SyncOutcome {
    pub(crate) ingest: ingest::Stats,
    /// Full-corpus totals from the post-sync audit. `None` when audit::stats was unavailable (we do
    /// not synthesize a zero-filled AuditStats — that would report "0 files / clean" as if measured).
    pub(crate) total_chunks: Option<usize>,
    pub(crate) total_edges: Option<usize>,
}

/// Deterministic re-ingest: walk notes → embed → pgvector upsert → graph (from frontmatter) → GC →
/// recompute relations. When `store=None` (BORING_VECTOR=off), the wiki files are first-class memory
/// (wiki_recall reads them directly) — nothing to embed/graph. Shared by HTTP `/sync` + the scheduler (SSOT).
pub(crate) async fn do_sync(
    store: Option<&Store>,
    llm: &Llm,
    vault_dir: Option<&PathBuf>,
    cfg: &config::BoringConfig,
) -> Result<SyncOutcome> {
    let Some(store) = store else {
        // vector off → no pgvector store exists, so the totals are a true, measured 0 (not an
        // error fallback): there are no chunks/edges because the backend is intentionally absent.
        return Ok(SyncOutcome {
            ingest: ingest::Stats::default(),
            total_chunks: Some(0),
            total_edges: Some(0),
        });
    };
    // Kernel A corpus = the vault's wiki dir (where remember writes). No vault → nothing to re-ingest
    // (remember ingests each note live; this walk only catches manual edits/deletes).
    let dirs: Vec<String> = vault_dir
        .map(|v| vec![v.join("wiki").to_string_lossy().into_owned()])
        .unwrap_or_default();
    let ingest = ingest::run(store, llm, cfg, &dirs).await?;
    // GC orphan semantic nodes (edges are rebuilt per-doc on re-ingest) — keeps the graph lean.
    match store.gc_orphans().await {
        Ok(g) => eprintln!("[scheduler] gc orphans: {}", g.total()),
        Err(e) => eprintln!("[scheduler] gc warning (ignored): {e:#}"),
    }
    // graph → Obsidian projection: doc↔doc relations as wiki relates_to wikilinks (only when vault exists).
    // Auxiliary stage — on failure it does not break the core ingest, just logs.
    if let Some(vd) = vault_dir {
        match vault::project_links(store, vd, 6).await {
            Ok(n) => eprintln!("[scheduler] graph→obsidian: updated {n} wiki relates_to"),
            Err(e) => eprintln!("[scheduler] project_links warning (ignored): {e:#}"),
        }
    }
    // Post-sync corpus audit + report-only hygiene sweep (every sync surfaces corpus rot in the logs
    // without a manual `make steward`; detection only, never mutates the vault). If audit::stats
    // fails we do NOT fabricate a zero-filled AuditStats — reporting "0 files / clean" as a measured
    // fact would be a lie (Layer 1). Instead we report the corpus totals as unavailable (None) and
    // log the failure with the real this-sync delta for context. Deep checks (placeholder tags,
    // project variants/typos) stay in `scripts/data-steward.py`, which the verdict points operators to.
    let (total_chunks, total_edges) = match audit::stats(store, cfg.allow_company_origin).await {
        Ok(audit) => {
            let generic_projects: usize = audit
                .by_project
                .iter()
                .filter(|(p, _)| matches!(p.as_str(), "Development" | "wiki" | ""))
                .map(|(_, n)| n)
                .sum();
            if audit.clean && generic_projects == 0 {
                eprintln!(
                    "[hygiene] ✅ clean — {} notes, origin/project complete{}",
                    audit.total_files,
                    if audit.company_allowed && audit.company_contamination > 0 {
                        format!(" ({} company-origin, allowed)", audit.company_contamination)
                    } else {
                        String::new()
                    }
                );
            } else {
                eprintln!(
                    "[hygiene] ⚠️ needs review — missing_origin {} · missing_project {} · generic_project {} · company {}{} — run `make steward` to inspect",
                    audit.missing_origin,
                    audit.missing_project,
                    generic_projects,
                    audit.company_contamination,
                    if audit.company_allowed {
                        " (allowed)"
                    } else {
                        ""
                    }
                );
            }
            (Some(audit.total_chunks), Some(audit.graph_edges))
        }
        Err(e) => {
            eprintln!(
                "[hygiene] unavailable — audit::stats failed: {e:#}; this sync added +{} chunks / +{} edges (corpus totals not measured)",
                ingest.chunks, ingest.edges
            );
            (None, None)
        }
    };

    Ok(SyncOutcome {
        ingest,
        total_chunks,
        total_edges,
    })
}

/// One compact cycle. Vector-off is a no-op.
pub(crate) async fn do_compact(store: Option<&Store>) -> Result<CompactSummary> {
    match store {
        Some(store) => store.compact().await,
        None => Ok(CompactSummary::default()),
    }
}

async fn run_sync(
    store: Option<&Store>,
    llm: &Llm,
    vault_dir: Option<&PathBuf>,
    cfg: &config::BoringConfig,
) {
    match do_sync(store, llm, vault_dir, cfg).await {
        Ok(o) => eprintln!(
            "[scheduler] sync done — ingest(new={} updated={} deleted={} chunks={}) graph(tools={} concepts={} claims={} edges={})",
            o.ingest.new,
            o.ingest.updated,
            o.ingest.deleted,
            o.ingest.chunks,
            o.ingest.tools,
            o.ingest.concepts,
            o.ingest.claims,
            o.ingest.edges
        ),
        Err(e) => eprintln!("[scheduler] sync error: {e:#}"),
    }
}

async fn run_compact(store: Option<&Store>) {
    match do_compact(store).await {
        Ok(s) => eprintln!(
            "[scheduler] compact done — vacuum {}ms reindex {}ms prune_query_log {} gc(tool {} concept {}) total {}ms",
            s.report.vacuum_ms,
            s.report.reindex_ms,
            s.report.prune_query_log,
            s.report.gc_tool,
            s.report.gc_concept,
            s.total_ms
        ),
        Err(e) => eprintln!("[scheduler] compact error: {e:#}"),
    }
}

async fn run_brief(
    store: Option<&Store>,
    llm: &Llm,
    vault_dir: Option<&PathBuf>,
    cfg: &config::BoringConfig,
) {
    let Some(store) = store else {
        return;
    };
    let Some(vd) = vault_dir else {
        return;
    };
    let brief_dir = vd.join("wiki");
    if let Err(e) = std::fs::create_dir_all(&brief_dir) {
        eprintln!("[scheduler] brief: cannot create wiki dir: {e:#}");
        return;
    }
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    let path = brief_dir.join(format!("daily-brief-{today}.md"));
    if path.exists() {
        eprintln!("[scheduler] daily brief already exists: {}", path.display());
        return;
    }
    let out = match ask::brief(store, llm, &[], cfg.note_lang.as_str(), None).await {
        Ok(o) => o,
        Err(e) => {
            eprintln!("[scheduler] brief generation error: {e:#}");
            return;
        }
    };
    let frontmatter = format!(
        "---\ntitle: \"Daily Brief — {today}\"\norigin: personal\ndate: {today}\nkind: note\ntags: [{GENERATED_BRIEF_TAG}]\n---\n\n"
    );
    let content = format!("{frontmatter}{}\n", out.answer);
    match vault::write_new_atomic(&path, content) {
        Ok(()) => eprintln!("[scheduler] daily brief written: {}", path.display()),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            eprintln!("[scheduler] daily brief already exists: {}", path.display());
        }
        Err(e) => eprintln!("[scheduler] brief write error: {e:#}"),
    }
}

fn parse_scheduler_hours(name: &str, raw: Option<String>, default: u64) -> Result<u64> {
    let Some(raw) = raw else {
        return Ok(default);
    };
    let hours = raw
        .parse::<u64>()
        .with_context(|| format!("{name} must be a positive integer hour count"))?;
    if hours == 0 {
        bail!("{name} must be at least 1 hour");
    }
    Ok(hours)
}

fn scheduler_hours_duration(name: &str, hours: u64) -> Result<Duration> {
    let secs = hours
        .checked_mul(SECONDS_PER_HOUR)
        .with_context(|| format!("{name} exceeds Duration seconds"))?;
    Ok(Duration::from_secs(secs))
}

fn parse_scheduler_hour_of_day(name: &str, raw: Option<String>, default: u32) -> Result<u32> {
    let Some(raw) = raw else {
        return Ok(default);
    };
    let hour = raw
        .parse::<u32>()
        .with_context(|| format!("{name} must be an integer hour in 0..=23"))?;
    if hour > 23 {
        bail!("{name} must be an integer hour in 0..=23");
    }
    Ok(hour)
}

fn sleep_until_next_local_hour(hour: u32) -> Result<tokio::time::Sleep> {
    let now = chrono::Local::now();
    let mut target = now
        .date_naive()
        .and_hms_opt(hour, 0, 0)
        .with_context(|| format!("scheduler brief hour {hour} is outside 0..=23"))?;
    if target <= now.naive_local() {
        target += chrono::Duration::days(1);
    }
    let dur = (target - now.naive_local())
        .to_std()
        .context("scheduler brief target must be in the future")?;
    Ok(tokio::time::sleep(dur))
}

pub(crate) fn spawn_scheduler(
    store: Option<Arc<Store>>,
    llm: Arc<Llm>,
    vault_dir: Arc<Option<PathBuf>>,
    cfg: Arc<config::BoringConfig>,
    sync_lock: Arc<Mutex<()>>,
    last_compact: Arc<Mutex<Option<Instant>>>,
) -> Result<()> {
    let sync_hours = parse_scheduler_hours(
        "BORING_SYNC_HOURS",
        config::env_set("BORING_SYNC_HOURS"),
        SCHEDULER_DEFAULT_SYNC_HOURS,
    )?;
    let sync_interval = scheduler_hours_duration("BORING_SYNC_HOURS", sync_hours)?;

    let compact_hours = parse_scheduler_hours(
        "BORING_COMPACT_HOURS",
        config::env_set("BORING_COMPACT_HOURS"),
        SCHEDULER_DEFAULT_COMPACT_HOURS,
    )?;
    let compact_interval = scheduler_hours_duration("BORING_COMPACT_HOURS", compact_hours)?;

    let brief_hour = parse_scheduler_hour_of_day(
        "BORING_BRIEF_HOUR",
        config::env_set("BORING_BRIEF_HOUR"),
        SCHEDULER_DEFAULT_BRIEF_HOUR,
    )?;

    tokio::spawn(async move {
        let store_ref = store.as_deref();
        // run once immediately at startup (compile only if vector off — refreshes wiki).
        // Lock serializes with HTTP /sync so callers wait for the startup baseline.
        eprintln!(
            "[scheduler] startup sync (interval={sync_hours}h, compact={compact_hours}h, vector={})",
            store.is_some()
        );
        {
            let _guard = sync_lock.lock().await;
            run_sync(store_ref, &llm, (*vault_dir).as_ref(), &cfg).await;
        }

        // Daily briefing scheduler: runs once at BORING_BRIEF_HOUR local time.
        // The generated note is tagged `daily-brief` so future briefs do not ingest themselves.
        let store2 = store.clone();
        let llm2 = Arc::clone(&llm);
        let vault_dir2 = Arc::clone(&vault_dir);
        let cfg2 = Arc::clone(&cfg);
        tokio::spawn(async move {
            loop {
                let sleep = match sleep_until_next_local_hour(brief_hour) {
                    Ok(sleep) => sleep,
                    Err(e) => {
                        eprintln!("[scheduler] brief schedule error: {e:#}");
                        return;
                    }
                };
                sleep.await;
                eprintln!("[scheduler] generating daily brief");
                run_brief(store2.as_deref(), &llm2, (*vault_dir2).as_ref(), &cfg2).await;
            }
        });

        let mut sync_ticker = tokio::time::interval(sync_interval);
        sync_ticker.tick().await; // the first tick is immediate — discard it (already ran above)
        loop {
            sync_ticker.tick().await;
            eprintln!("[scheduler] periodic sync");
            {
                let _guard = sync_lock.lock().await;
                run_sync(store_ref, &llm, (*vault_dir).as_ref(), &cfg).await;

                // Auto-compact at the next sync after the compact interval has passed.
                // This keeps maintenance aligned with write load rather than running on a fixed clock.
                let mut last = last_compact.lock().await;
                if last.is_none_or(|t| t.elapsed() >= compact_interval) {
                    run_compact(store_ref).await;
                    *last = Some(Instant::now());
                }
            }
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        SCHEDULER_DEFAULT_BRIEF_HOUR, SCHEDULER_DEFAULT_SYNC_HOURS, parse_scheduler_hour_of_day,
        parse_scheduler_hours, scheduler_hours_duration,
    };

    #[test]
    fn scheduler_interval_env_uses_default_only_when_absent() {
        assert_eq!(
            parse_scheduler_hours("BORING_SYNC_HOURS", None, SCHEDULER_DEFAULT_SYNC_HOURS).ok(),
            Some(SCHEDULER_DEFAULT_SYNC_HOURS)
        );
        assert_eq!(
            parse_scheduler_hours("BORING_SYNC_HOURS", Some("6".to_owned()), 4).ok(),
            Some(6)
        );
    }

    #[test]
    fn scheduler_rejects_malformed_interval_env() {
        let err = parse_scheduler_hours("BORING_SYNC_HOURS", Some("abc".to_owned()), 4).err();

        assert!(err.is_some_and(|err| {
            format!("{err:#}").contains("BORING_SYNC_HOURS must be a positive integer hour count")
        }));
    }

    #[test]
    fn scheduler_rejects_zero_interval_env() {
        let err = parse_scheduler_hours("BORING_SYNC_HOURS", Some("0".to_owned()), 4).err();

        assert!(err.is_some_and(|err| {
            format!("{err:#}").contains("BORING_SYNC_HOURS must be at least 1 hour")
        }));
    }

    #[test]
    fn scheduler_rejects_duration_overflow() {
        let err = scheduler_hours_duration("BORING_SYNC_HOURS", u64::MAX).err();

        assert!(err.is_some_and(|err| {
            format!("{err:#}").contains("BORING_SYNC_HOURS exceeds Duration seconds")
        }));
    }

    #[test]
    fn scheduler_brief_hour_env_uses_default_only_when_absent() {
        assert_eq!(
            parse_scheduler_hour_of_day("BORING_BRIEF_HOUR", None, SCHEDULER_DEFAULT_BRIEF_HOUR)
                .ok(),
            Some(SCHEDULER_DEFAULT_BRIEF_HOUR)
        );
        assert_eq!(
            parse_scheduler_hour_of_day("BORING_BRIEF_HOUR", Some("9".to_owned()), 8).ok(),
            Some(9)
        );
    }

    #[test]
    fn scheduler_rejects_invalid_brief_hour_env() {
        let malformed =
            parse_scheduler_hour_of_day("BORING_BRIEF_HOUR", Some("soon".to_owned()), 8).err();
        assert!(malformed.is_some_and(|err| {
            format!("{err:#}").contains("BORING_BRIEF_HOUR must be an integer hour in 0..=23")
        }));

        let out_of_range =
            parse_scheduler_hour_of_day("BORING_BRIEF_HOUR", Some("24".to_owned()), 8).err();
        assert!(out_of_range.is_some_and(|err| {
            format!("{err:#}").contains("BORING_BRIEF_HOUR must be an integer hour in 0..=23")
        }));
    }
}
