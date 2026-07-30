//! ohmyboring personal RAG — Rust (pgvector: vector + node/edge graph + recursive CTE + audit).
//! First milestone: embed → store → vector search round-trip proof (selftest).
use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use drudge::{ask, audit, config, frontmatter, graph, ingest, llm, retrieve, serve, store, vault};

#[derive(Parser)]
#[command(
    name = "drudge",
    about = "ohmyboring personal RAG (Rust, pgvector + graph CTE)"
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Stack self-test: Llm embed → pgvector store → vector search round-trip
    Selftest,
    /// Number of stored documents
    Stats,
    /// source → frontmatter → chunking → embedding → ingest (idempotent, excludes example-company)
    Ingest,
    /// Ingestion audit — origin/kind/project distribution + quality warnings
    Audit,
    /// Retrieval test (vector + BM25 RRF)
    Search { query: String },
    /// Single query — retrieval + LLM synthesis + sources
    Ask { question: String },
    /// Recency-first briefing — recency (updated_at) retrieval + supersede synthesis (morning briefing)
    Brief,
    /// Graph-expanded retrieval — vector hits → graph (edge) k-hop neighbors
    Graph {
        query: String,
        #[arg(short, long, default_value = "2")]
        depth: usize,
    },
    /// Deterministic re-ingest: walk → embed → upsert → graph (from frontmatter) → relations
    Sync,
    /// Delete orphan semantic nodes (remove edge-unreferenced legacy remnants)
    Gc,
    /// Show loaded boring.json config (for debugging / migration verification)
    Config,
    /// Graph projection — write Postgres doc↔doc relations as wiki relates_to wikilinks (Obsidian)
    Link {
        /// vault root (default: $BORING_VAULT_DIR or $HOME/oh-my-boring/vault)
        #[arg(long)]
        vault: Option<String>,
    },
    /// Code indexing — full-refresh the AST code graph from current sources (tree-sitter)
    CodeIndex {
        /// repo root to index (default: current directory)
        #[arg(long, default_value = ".")]
        root: String,
        /// Replace the graph even when the walk shares no files with the indexed one
        /// (the wrong-root footgun guard). Required for intentional root changes.
        #[arg(long)]
        force: bool,
    },
    /// HTTP resident daemon — ingest/ask/graph/audit API + background scheduler
    Serve,
    /// Maintenance compact — VACUUM/ANALYZE + REINDEX + prune old query_log + GC orphans
    Compact,
    /// Recent query/retrieval log (memory usage analytics)
    QueryLog {
        #[arg(short, long, default_value = "50")]
        limit: i64,
    },
    /// Repeated code-lane queries from query_log — what the agent keeps forgetting
    CodeHotspots {
        #[arg(long, default_value = "30")]
        days: i64,
        #[arg(long, default_value = "2")]
        min_count: i64,
        #[arg(short, long, default_value = "20")]
        limit: i64,
    },
    /// Personal vault lint/audit/maintenance
    Vault {
        #[command(subcommand)]
        sub: VaultCmd,
    },
}

#[derive(Subcommand)]
enum VaultCmd {
    /// vault/wiki/*.md consistency check (schema · frontmatter · wikilink · sources)
    Lint {
        /// vault root path (default: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// Treat warnings as errors too (exit 2)
        #[arg(long)]
        strict: bool,
    },
    /// vault graph audit (orphan · connected components · superseded)
    Audit {
        /// vault root path (default: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// Treat warnings as errors too (exit 2)
        #[arg(long)]
        strict: bool,
    },
    /// Add OKF v0.1 fields (type/description/timestamp/skills/contracts/incidents) to legacy wiki notes
    MigrateOkf {
        /// vault root path (default: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// Show what would change without writing
        #[arg(long)]
        dry_run: bool,
    },
}

#[tokio::main]
#[allow(clippy::too_many_lines)]
async fn main() -> Result<()> {
    // Rejection message vector-only CLI commands return when off (not silent, ROP). The daemon (serve) runs in wiki mode when off.
    const VEC_OFF: &str = "BORING_VECTOR=off — this command requires the vector backend (pgvector). The daemon (serve) runs in wiki-recall mode when off.";

    let cli = Cli::parse();
    // BORING_VECTOR: default off = wiki first-class (no Postgres connection, simple). Turn on to enable pgvector (vector+graph).
    // unset/off → don't open Store → start engine/CLI without Postgres. (aligned with the wiki-primary trend)
    let cfg = config::BoringConfig::load(None)?;

    let vector_on = config::env_set("BORING_VECTOR")
        .is_some_and(|v| matches!(v.to_lowercase().as_str(), "on" | "1" | "true" | "yes"));
    let store: Option<store::Store> = if vector_on {
        let dsn = std::env::var("PG_DSN")
            .unwrap_or_else(|_| "postgresql://boring:boring@localhost:5432/boring".to_owned());
        // embed_dim (boring.json) sizes the vector columns — the kernel's only model knob.
        Some(store::Store::open(&dsn, cfg.embed_dim as usize).await?)
    } else {
        None
    };

    match cli.cmd {
        Cmd::Selftest => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let docs = [
                (
                    "doc:rust",
                    "Rust is a systems programming language that delivers memory safety and performance at once.",
                ),
                (
                    "doc:coffee",
                    "Espresso is extracted by forcing hot water through finely ground beans at high pressure.",
                ),
                (
                    "doc:db",
                    "Postgres provides vector search via pgvector and graphs via node/edge tables with recursive CTEs.",
                ),
            ];
            println!("1) embed + store ({} docs)", docs.len());
            for (id, text) in docs {
                let emb = ol.embed(text).await?;
                let front = frontmatter::FrontMatter {
                    origin: "personal".to_owned(),
                    project: "oh-my-boring".to_owned(),
                    source_path: (*id).to_owned(),
                    ..Default::default()
                };
                // chunk.source_path REFERENCES document(source_path) — call upsert_document
                // first to guarantee the parent record so the FK is satisfied.
                store
                    .upsert_document(&front, "selftest", std::time::SystemTime::now())
                    .await?;
                store
                    .upsert_chunk(&store::Doc {
                        id: (*id).into(),
                        content: (*text).into(),
                        embedding: emb,
                        front,
                        chunk_idx: 0,
                    })
                    .await?;
            }

            let query = "how to use vectors and graphs in a database";
            println!("2) query: {query:?}");
            let qe = ol.embed(query).await?;
            let hits = store.vector_search(&qe, 3).await?;
            println!("3) vector search results (top-{}):", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(34).collect();
                println!("   [dist={:.4}] {} ({}) — {}", h.dist, h.id, h.origin, snip);
            }
            // GOAL check: the 'db' document must rank first (semantically closest to the query)
            match hits.first() {
                Some(h) if h.id == "doc:db" => {
                    println!("✅ ranking correct (doc:db #1) — vector search OK");
                }
                Some(h) => println!("⚠️ #1 is not doc:db: {} — check embedding/distance", h.id),
                None => println!("❌ 0 hits — vector search still failing"),
            }
        }
        Cmd::Stats => {
            let store = store.as_ref().context(VEC_OFF)?;
            println!("knowledge docs: {}", store.count().await?);
        }
        Cmd::Ingest => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let source_dirs = cfg.source_dirs();
            println!("sources: {source_dirs:?}");
            let s = ingest::run(store, &ol, &cfg, &source_dirs).await?;
            println!(
                "scanned={} new={} updated={} unchanged={} deleted={} skipped={} chunks={}",
                s.scanned, s.new, s.updated, s.unchanged, s.deleted, s.skipped, s.chunks
            );
        }
        Cmd::Audit => {
            let store = store.as_ref().context(VEC_OFF)?;
            audit::run(store, cfg.allow_company_origin).await?;
        }
        Cmd::Search { query } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let hits = retrieve::retrieve(store, &ol, &query, 5, &[], None, None, false).await?;
            println!("'{query}' → {} hits", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(50).collect();
                println!("  [{}/{}] {} — {snip}", h.origin, h.project, h.id);
            }
        }
        Cmd::Ask { question } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            ask::run(store, &ol, &question, &[], None, None).await?;
        }
        Cmd::Brief => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let out = ask::brief(store, &ol, &[], cfg.note_lang.as_str(), None).await?;
            println!("{}\n", out.answer);
            if !out.sources.is_empty() {
                println!("sources:");
                for src in &out.sources {
                    println!("  - {src}");
                }
            }
        }
        Cmd::Graph { query, depth } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            graph::run(store, &ol, &query, depth).await?;
        }
        Cmd::Sync => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            // Kernel A corpus = the vault's wiki dir (agent-written notes), not raw transcripts.
            let vault_wiki = config::env_set("BORING_VAULT_DIR").map_or_else(
                || {
                    format!(
                        "{}/oh-my-boring/vault/wiki",
                        std::env::var("HOME").unwrap_or_default()
                    )
                },
                |v| format!("{v}/wiki"),
            );
            let is = ingest::run(store, &ol, &cfg, &[vault_wiki]).await?;
            println!(
                "sync: ingest(new={} updated={} deleted={} chunks={}) graph(tools={} concepts={} claims={} edges={})",
                is.new,
                is.updated,
                is.deleted,
                is.chunks,
                is.tools,
                is.concepts,
                is.claims,
                is.edges,
            );
            let ss = store.semantic_stats().await?;
            println!(
                "semantic audit: tool {} · concept {} · uses {} · about {}",
                ss.tools, ss.concepts, ss.uses, ss.about
            );
        }
        Cmd::Link { vault } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let vault_root = vault
                .or_else(|| config::env_set("BORING_VAULT_DIR"))
                .unwrap_or_else(|| {
                    format!(
                        "{}/oh-my-boring/vault",
                        std::env::var("HOME").unwrap_or_default()
                    )
                });
            let n = vault::project_links(store, std::path::Path::new(&vault_root), 6).await?;
            println!("graph→obsidian: updated relates_to of {n} wiki notes");
        }
        Cmd::CodeIndex { root, force } => {
            let store = store.as_ref().context(VEC_OFF)?;
            if !cfg.code_index.enabled {
                println!("code indexing disabled — set code_index.enabled in boring.json");
                return Ok(());
            }
            let stats = drudge::codegraph::ingest::walk_directory(
                std::path::Path::new(&root),
                &cfg.code_index,
            )?;
            if stats.symbols.is_empty() {
                // Refuse to wipe on an empty walk — a wrong --root would otherwise
                // delete the whole code graph (full-refresh semantics below).
                println!(
                    "code-index: no symbols found under {root} — existing graph untouched (check --root/config)"
                );
                return Ok(());
            }
            // Wrong-root guard: replacing the graph with a walk that shares NO files
            // with the indexed one is almost always a mistake (e.g. --root data/eval/...
            // instead of the repo root). Require --force for intentional root changes.
            let existing_files = store.code_indexed_files().await?;
            if !force && !existing_files.is_empty() {
                let new_files: std::collections::BTreeSet<&str> = stats
                    .symbols
                    .iter()
                    .map(|s| s.source_path.as_str())
                    .collect();
                if !new_files.iter().any(|f| existing_files.contains(*f)) {
                    anyhow::bail!(
                        "code-index: walk under {root} shares no files with the indexed graph \
                         ({} files) — refusing to replace it. Pass --force if this root change \
                         is intentional.",
                        existing_files.len()
                    );
                }
            }
            // Full refresh: replace the graph with this walk's result (stale symbols of
            // renamed/deleted files disappear); doc→code note edges survive and re-attach.
            store.clear_code_graph_preserving_doc_edges().await?;
            for symbol in &stats.symbols {
                store.upsert_code_symbol(symbol).await?;
            }
            for relation in &stats.relations {
                store.upsert_code_relation(relation).await?;
            }
            let note_edges_gc = store.gc_dangling_code_note_edges().await?;
            println!(
                "code-index: files={} symbols={} relations={} skipped={} errors={} note_edges_gc={}",
                stats.files_parsed,
                stats.symbols_upserted,
                stats.relations_upserted,
                stats.files_skipped,
                stats.parse_errors,
                note_edges_gc,
            );
        }
        Cmd::Gc => {
            let store = store.as_ref().context(VEC_OFF)?;
            let gc = store.gc_orphans().await?;
            println!(
                "gc orphans — tool: {} · concept: {} · total: {}",
                gc.tool,
                gc.concept,
                gc.total()
            );
        }
        Cmd::Config => {
            println!("{}", serde_json::to_string_pretty(&cfg)?);
        }
        Cmd::Serve => {
            let ol = llm::Llm::from_config(&cfg);
            // Move store ownership into serve::run — single-process DB owner pattern.
            serve::run(store, ol, cfg).await?;
        }
        Cmd::Compact => {
            let store = store.as_ref().context(VEC_OFF)?;
            let summary = store.compact().await?;
            println!(
                "compact done — vacuum {}ms, reindex {}ms, prune_query_log {}, gc(tool {} concept {}), total {}ms",
                summary.report.vacuum_ms,
                summary.report.reindex_ms,
                summary.report.prune_query_log,
                summary.report.gc_tool,
                summary.report.gc_concept,
                summary.total_ms,
            );
        }
        Cmd::QueryLog { limit } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let rows = store.recent_queries(limit.clamp(1, 1000)).await?;
            for r in rows {
                let ts = format!("{:?}", r.created_at);
                println!(
                    "[{}] {:<10} {:>5}ms  q={:?}  hits={:?}",
                    ts,
                    r.endpoint,
                    r.latency_ms
                        .map_or_else(|| "?".to_string(), |n| n.to_string()),
                    r.query,
                    if r.hit_paths.is_empty() {
                        r.sources
                    } else {
                        r.hit_paths
                    }
                );
            }
        }
        Cmd::CodeHotspots {
            days,
            min_count,
            limit,
        } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let rows = store
                .repeated_queries(
                    days.clamp(1, 365),
                    min_count.clamp(2, 100),
                    limit.clamp(1, 100),
                )
                .await?;
            let code_rows: Vec<_> = rows
                .into_iter()
                .filter(|r| retrieve::is_code_query(&r.query))
                .collect();
            if code_rows.is_empty() {
                println!("no repeated code queries in the window — nothing forgotten yet");
                return Ok(());
            }
            for r in code_rows {
                println!("{}× {:?}", r.count, r.query);
                for line in retrieve::code_context(store, &r.query, 3, 120).await? {
                    println!("    → {line}");
                }
            }
        }
        Cmd::Vault { sub } => {
            let default_vault = format!(
                "{}/oh-my-boring/vault",
                std::env::var("HOME").unwrap_or_default()
            );
            match sub {
                VaultCmd::Lint { vault, strict } => {
                    // Lint does not need Postgres — release the connection.
                    drop(store);
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or(default_vault));
                    let code = vault::run_lint(&vault_root, strict)?;
                    std::process::exit(code);
                }
                VaultCmd::Audit { vault, strict } => {
                    // Audit does not need Postgres either.
                    drop(store);
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or(default_vault));
                    let code = vault::run_audit(&vault_root, strict)?;
                    std::process::exit(code);
                }
                VaultCmd::MigrateOkf { vault, dry_run } => {
                    drop(store);
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or(default_vault));
                    let (examined, changed) = vault::run_migrate_okf(&vault_root, dry_run)?;
                    println!("migrate-okf: examined {examined} notes, changed {changed} notes");
                    if dry_run {
                        println!("(dry-run — no files were modified)");
                    }
                    std::process::exit(0);
                }
            }
        }
    }
    Ok(())
}
