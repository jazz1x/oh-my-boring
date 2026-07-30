//! Serve — HTTP resident daemon (axum) + background sync scheduler.
//!
//! Cross-reference: design decision D3 (write door gated / read door open).
//!
//! Architecture:
//! - Shares `Store` + `Llm` via `Arc` (the Postgres client supports concurrent use).
//! - axum router: /health · /ask · /brief · /search · /code-search · /graph · /audit · /sync
//! - Background scheduler: `BORING_SYNC_HOURS` (default 4h) interval + one immediate run at startup.
//! - Error propagation: `AppError` → explicit HTTP status + JSON body.
use std::path::{Path, PathBuf};
use std::sync::Arc;

use tokio::sync::Mutex;

use anyhow::Result;
use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::config;
use crate::frontmatter::{is_internal_eval_fixture_path, raw_has_generated_brief_tag};
use crate::llm::Llm;
use crate::pii;
use crate::store::Store;
use crate::wiki_recall;

mod http;
mod mcp;
mod scheduler;

// ── shared state ──────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct AppState {
    /// pgvector backend. If `None`, `BORING_VECTOR=off` — retrieval is direct vault/wiki reads (wiki_recall),
    /// and remember writes the wiki note as first-class memory (no embed/graph). Vector/graph-dependent endpoints reject explicitly.
    pub(crate) store: Option<Arc<Store>>,
    pub(crate) llm: Arc<Llm>,
    /// vault root (`BORING_VAULT_DIR`). The remember target (`<vault>/wiki/wiki-NNNN.md`) + the relates_to projection root.
    pub(crate) vault_dir: Arc<Option<PathBuf>>,
    /// PII / sensitive-data gate. None when no rule files are present.
    pub(crate) pii: Arc<Option<pii::PiiScanner>>,
    /// Policy config (`boring.json`).
    pub(crate) cfg: Arc<config::BoringConfig>,
    /// Resolved path to the loaded config, so `classify_repo` writes back to the same file.
    pub(crate) cfg_path: Arc<Option<PathBuf>>,
    /// Serializes the write-maintenance lane: startup/periodic/HTTP sync plus
    /// vector-mode `remember`/`forget` relation rewrites. `/sync` waits for an
    /// in-flight writer and returns its actual outcome.
    pub(crate) sync_lock: Arc<Mutex<()>>,
    /// Resident wiki recall index (BORING_VECTOR=off path). Persists parsed/lowercased notes across
    /// requests; `refresh()` re-reads only mtime-changed files, so repeated `/search` (the recall hook
    /// fires per prompt) scores in memory instead of re-reading the whole corpus. std Mutex — the
    /// critical section is sync (refresh+score) and never held across an await.
    pub(crate) wiki_index: Arc<std::sync::Mutex<wiki_recall::WikiIndex>>,
    /// Last successful compact time, shared with scheduler so manual `/compact` resets the window.
    pub(crate) last_compact: Arc<Mutex<Option<std::time::Instant>>>,
}

impl AppState {
    /// vault/wiki directory (the retrieval target for `BORING_VECTOR=off`). None if vault is unset.
    pub(crate) fn wiki_dir(&self) -> Option<PathBuf> {
        (*self.vault_dir).as_ref().map(|v| v.join("wiki"))
    }

    /// Cached wiki recall: refresh the resident index (mtime-incremental — only changed files are
    /// re-read, so this stays honest, not stale) then score in memory. Empty when the vault is unset.
    pub(crate) fn wiki_recall(
        &self,
        query: &str,
        k: usize,
        project: Option<&str>,
        exclude_origins: &[String],
        since_hours: Option<i32>,
    ) -> Result<Vec<wiki_recall::WikiHit>> {
        let Some(dir) = self.wiki_dir() else {
            return Ok(Vec::new());
        };
        // Recover a poisoned lock instead of unwrapping (a prior panic must not wedge recall).
        let mut idx = self
            .wiki_index
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        idx.refresh(&dir)?;
        Ok(idx.search(query, k, project, exclude_origins, since_hours))
    }
}

pub(crate) fn optional_project(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|project| !project.is_empty())
}

pub(crate) fn parse_exclude_origins(values: &[String]) -> std::result::Result<Vec<String>, String> {
    let mut origins = Vec::new();
    for value in values {
        let candidate = value.trim();
        if candidate.is_empty() {
            continue;
        }
        let origin = candidate.parse::<config::Origin>()?.as_str().to_owned();
        if !origins.contains(&origin) {
            origins.push(origin);
        }
    }
    Ok(origins)
}

/// Input bundle for fire-and-forget query logging. Keeps `spawn_query_log` from
/// growing an eighth positional argument and makes the call sites explicit.
pub(crate) struct QueryLogInput {
    pub endpoint: &'static str,
    pub query: String,
    pub hit_paths: Vec<String>,
    pub sources: Vec<String>,
    pub answer_snippet: String,
    pub elapsed: std::time::Duration,
    pub meta: Option<Value>,
}

/// Fire-and-forget query logging. Latency and result context are recorded for
/// memory-utility analytics; failures are logged to stderr and never fail the request.
#[allow(clippy::needless_borrow)] // tokio-postgres needs &&str to coerce to &dyn ToSql.
pub(crate) fn spawn_query_log(store: Option<Arc<Store>>, input: QueryLogInput) {
    let Some(store) = store else {
        return;
    };
    tokio::spawn(async move {
        let latency_ms = i32::try_from(input.elapsed.as_millis()).ok();
        let meta = input.meta.unwrap_or_else(|| json!({}));
        if let Err(e) = store
            .log_query(
                input.endpoint,
                &input.query,
                &input.hit_paths,
                &input.sources,
                &input.answer_snippet,
                latency_ms,
                &meta,
            )
            .await
        {
            eprintln!("[query_log] {e:#}");
        }
    });
}

// ── error type (ROP: AppError → HTTP status) ────────────────────────────────

pub(crate) struct AppError {
    status: StatusCode,
    error: anyhow::Error,
}

impl AppError {
    pub(crate) fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            error: anyhow::anyhow!(message.into()),
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        #[derive(Serialize)]
        struct ErrBody {
            error: String,
        }
        let body = ErrBody {
            error: format!("{:#}", self.error),
        };
        (self.status, Json(body)).into_response()
    }
}

impl<E: Into<anyhow::Error>> From<E> for AppError {
    fn from(e: E) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            error: e.into(),
        }
    }
}

// ── request/response types ─────────────────────────────────────────────────

#[derive(Deserialize)]
pub(crate) struct AskReq {
    pub(crate) question: String,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
    #[serde(default)]
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
}

#[derive(Deserialize, Default)]
pub(crate) struct BriefReq {
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
}

#[derive(Serialize)]
pub(crate) struct AskResp {
    pub(crate) answer: String,
    pub(crate) sources: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct SearchReq {
    pub(crate) query: String,
    #[serde(default = "default_max_results")]
    pub(crate) max_results: usize,
    #[serde(default = "default_max_tokens")]
    pub(crate) max_tokens: usize,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
    #[serde(default)]
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
}

fn default_max_results() -> usize {
    MCP_DEFAULT_RESULTS
}

fn default_max_tokens() -> usize {
    MCP_DEFAULT_TOKENS
}

#[derive(Serialize)]
pub(crate) struct SearchHit {
    pub(crate) id: String,
    pub(crate) origin: String,
    pub(crate) project: String,
    pub(crate) source_path: String,
    pub(crate) snippet: String,
}

#[derive(Serialize)]
pub(crate) struct SearchResp {
    pub(crate) hits: Vec<SearchHit>,
}

#[derive(Deserialize)]
pub(crate) struct CodeSearchReq {
    pub(crate) query: String,
    #[serde(default = "default_code_search_max_symbols")]
    pub(crate) max_symbols: usize,
}

fn default_code_search_max_symbols() -> usize {
    CODE_SEARCH_DEFAULT_SYMBOLS
}

/// One AST code-graph symbol as returned by `/code-search`.
#[derive(Serialize)]
pub(crate) struct CodeSymbolHit {
    pub(crate) kind: String,
    pub(crate) name: String,
    pub(crate) source_path: String,
    pub(crate) signature: String,
}

/// A wiki note linked to a matched code symbol by `remember_code` (`code_uses` edge).
#[derive(Serialize)]
pub(crate) struct CodeNoteHit {
    pub(crate) source_path: String,
    pub(crate) title: String,
    pub(crate) snippet: String,
    pub(crate) symbol_name: String,
    pub(crate) symbol_path: String,
}

#[derive(Serialize)]
pub(crate) struct CodeSearchResp {
    pub(crate) hits: Vec<CodeSymbolHit>,
    /// Notes the user deliberately linked to the matched symbols (empty until
    /// `remember_code` is used); survives re-indexing.
    pub(crate) notes: Vec<CodeNoteHit>,
}

#[derive(Deserialize)]
pub(crate) struct GraphReq {
    pub(crate) query: String,
    #[serde(default)]
    pub(crate) depth: Option<usize>,
}

#[derive(Deserialize)]
pub(crate) struct WeeklyReq {
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
    #[serde(default)]
    pub(crate) until_hours: Option<i32>,
}

#[derive(Deserialize)]
pub(crate) struct StatusReq {
    pub(crate) project: String,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct DecisionsReq {
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct RisksReq {
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct NextActionsReq {
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct StalledReq {
    pub(crate) project: Option<String>,
    pub(crate) older_than_days: Option<u32>,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
}

#[derive(Deserialize)]
pub(crate) struct ContextReq {
    pub(crate) project: Option<String>,
    #[serde(default)]
    pub(crate) exclude_origins: Vec<String>,
    #[serde(default = "default_context_max_items")]
    pub(crate) max_items: usize,
}

fn default_context_max_items() -> usize {
    CONTEXT_DEFAULT_ITEMS
}

#[derive(Serialize)]
pub(crate) struct GraphResp {
    pub(crate) hit: String,
    pub(crate) graph_neighbors: Vec<String>,
    pub(crate) semantic_neighbors: Vec<String>,
}

#[derive(Serialize)]
pub(crate) struct SyncResp {
    pub(crate) ingest_new: usize,
    pub(crate) ingest_updated: usize,
    pub(crate) ingest_deleted: usize,
    pub(crate) ingest_chunks: usize,
    pub(crate) graph_tools: usize,
    pub(crate) graph_concepts: usize,
    pub(crate) graph_claims: usize,
    pub(crate) graph_edges: usize,
    /// Total corpus size after sync (independent of whether this run produced deltas). `null` when the
    /// post-sync audit was unavailable — reported honestly as "not measured", never fabricated as 0.
    pub(crate) total_chunks: Option<usize>,
    pub(crate) total_edges: Option<usize>,
}

#[derive(Serialize)]
pub(crate) struct CompactResp {
    pub(crate) vacuum_ms: u128,
    pub(crate) reindex_ms: u128,
    pub(crate) prune_query_log: usize,
    pub(crate) gc_tool: usize,
    pub(crate) gc_concept: usize,
    pub(crate) total_ms: u128,
}

#[derive(Deserialize)]
pub(crate) struct QueryLogReq {
    #[serde(default = "default_query_log_limit")]
    pub(crate) limit: i64,
}

fn default_query_log_limit() -> i64 {
    50
}

#[derive(Serialize)]
pub(crate) struct QueryLogResp {
    pub(crate) entries: Vec<QueryLogEntry>,
}

#[derive(Serialize)]
pub(crate) struct QueryLogEntry {
    pub(crate) id: i32,
    pub(crate) created_at: String,
    pub(crate) endpoint: String,
    pub(crate) query: String,
    pub(crate) hit_paths: Vec<String>,
    pub(crate) sources: Vec<String>,
    pub(crate) answer_snippet: String,
    pub(crate) latency_ms: Option<i32>,
}

#[derive(Deserialize)]
pub(crate) struct EventLogReq {
    #[serde(default = "default_event_log_limit")]
    pub(crate) limit: i64,
    #[serde(default)]
    pub(crate) component: Option<String>,
    #[serde(default, rename = "event")]
    pub(crate) event_name: Option<String>,
    #[serde(default)]
    pub(crate) status: Option<String>,
    #[serde(default)]
    pub(crate) run_id: Option<String>,
    #[serde(default)]
    pub(crate) workflow: Option<String>,
    #[serde(default)]
    pub(crate) since_hours: Option<i32>,
}

fn default_event_log_limit() -> i64 {
    50
}

#[derive(Serialize)]
pub(crate) struct EventLogResp {
    pub(crate) entries: Vec<EventLogEntry>,
}

#[derive(Serialize)]
pub(crate) struct EventLogEntry {
    pub(crate) id: i64,
    pub(crate) observed_at: String,
    pub(crate) time_unix_nano: Option<i64>,
    pub(crate) severity_text: String,
    pub(crate) severity_number: i32,
    pub(crate) service_name: String,
    pub(crate) component: String,
    #[serde(rename = "event")]
    pub(crate) event_name: String,
    pub(crate) status: String,
    pub(crate) trace_id: Option<String>,
    pub(crate) span_id: Option<String>,
    pub(crate) run_id: Option<String>,
    pub(crate) session_id: Option<String>,
    pub(crate) workflow: Option<String>,
    pub(crate) workflow_node: Option<String>,
    pub(crate) workflow_outcome: Option<String>,
    pub(crate) body: Value,
    pub(crate) attributes: Value,
    pub(crate) resource: Value,
    pub(crate) otel: Value,
}

#[derive(Serialize)]
pub(crate) struct EventIngestResp {
    pub(crate) accepted: usize,
}

// ── shared handler helpers ──────────────────────────────────────────────────

/// Whether a sync/remember/forget is mid-flight (holds the sync lock). An enum, not a string —
/// the two states are closed at the type so an impossible third value can't exist (Layer 1: ADT).
#[derive(Serialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
pub(crate) enum SyncState {
    Running,
    Idle,
}

#[derive(Serialize)]
pub(crate) struct HealthResp {
    pub(crate) status: &'static str,
    pub(crate) vector: bool,
    /// "running" while a sync/remember/forget holds the sync lock, else "idle". Lets `make up` callers
    /// tell a still-warming corpus (empty results are expected) from a genuinely empty one.
    pub(crate) sync: SyncState,
    /// Source wiki note count (vault/wiki/*.md, excluding generated briefs and internal eval fixtures)
    /// — the corpus size in both modes. `null` when the vault is unset/unreadable (kept best-effort so
    /// /health stays a liveness probe).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) corpus_count: Option<usize>,
}

/// Best-effort count of source wiki notes (`vault/wiki/*.md`, excluding generated briefs and internal
/// eval fixtures). `None` on any IO error — `/health` must stay a liveness signal, so an
/// unreadable/absent vault reports "unknown" (null), never fails the probe.
pub(crate) fn count_wiki_notes(wiki_dir: &Path) -> Option<usize> {
    let entries = std::fs::read_dir(wiki_dir).ok()?;
    let mut count = 0;
    for entry in entries {
        let path = entry.ok()?.path();
        if path.extension().and_then(|x| x.to_str()) != Some("md") {
            continue;
        }
        let source_path = path.to_string_lossy();
        if is_internal_eval_fixture_path(source_path.as_ref()) {
            continue;
        }
        let raw = std::fs::read_to_string(&path).ok()?;
        if !raw_has_generated_brief_tag(&raw) {
            count += 1;
        }
    }
    Some(count)
}

/// The explicit rejection (not silence) that vector/graph-dependent endpoints return under `BORING_VECTOR=off`.
pub(crate) fn vector_disabled() -> AppError {
    anyhow::anyhow!(
        "BORING_VECTOR=off — this feature requires the vector backend (pgvector). Set BORING_VECTOR=on and start Postgres."
    )
    .into()
}

/// The same rejection mapped into the MCP `(code, message)` tuple — for vector-only tools
/// (neighbors/claims/corpus_status). SSOT with `vector_disabled`; never `unwrap` the store (ROP).
pub(crate) fn vec_off_rpc() -> (i32, String) {
    (-32603, format!("{:#}", vector_disabled().error))
}

/// Hard ceiling on agent-supplied recall budget to prevent token/DoS explosions.
pub(crate) const MCP_DEFAULT_RESULTS: usize = 5;
pub(crate) const MCP_MAX_RESULTS: usize = 50;
pub(crate) const MCP_DEFAULT_TOKENS: usize = 2_000;
pub(crate) const MCP_MAX_TOKENS: usize = 16_384;
pub(crate) const CONTEXT_DEFAULT_ITEMS: usize = 5;
pub(crate) const CONTEXT_MAX_ITEMS: usize = 20;
pub(crate) const CODE_SEARCH_DEFAULT_SYMBOLS: usize = 5;
pub(crate) const CODE_SEARCH_MAX_SYMBOLS: usize = 20;
pub(crate) const CHARS_PER_TOKEN_ESTIMATE: usize = 4;

pub(crate) fn recall_max_chars(max_tokens: usize) -> Result<usize> {
    max_tokens
        .checked_mul(CHARS_PER_TOKEN_ESTIMATE)
        .ok_or_else(|| anyhow::anyhow!("recall max_tokens cannot fit character budget"))
}

// ── entry point ─────────────────────────────────────────────────────────────

pub async fn run(store: Option<Store>, llm: Llm, cfg: config::BoringConfig) -> Result<()> {
    // vault root — when set, sync includes the raw→wiki compile stage.
    let vault_dir: Option<PathBuf> = config::env_set("BORING_VAULT_DIR").map(PathBuf::from);

    // Remember which config file we loaded so `classify_repo` writes back to the same file.
    let cfg_path = config::discover_path();

    let addr = config::env_set("BORING_HTTP_ADDR").unwrap_or_else(|| "0.0.0.0:7700".to_owned());

    let last_compact = Arc::new(Mutex::new(None));
    let pii = vault_dir
        .as_ref()
        .map(|vd| crate::pii::PiiScanner::load_from_vault(vd))
        .transpose()?
        .flatten();
    let state = AppState {
        store: store.map(Arc::new),
        llm: Arc::new(llm),
        vault_dir: Arc::new(vault_dir),
        pii: Arc::new(pii),
        cfg: Arc::new(cfg),
        cfg_path: Arc::new(cfg_path),
        sync_lock: Arc::new(Mutex::new(())),
        last_compact: Arc::clone(&last_compact),
        wiki_index: Arc::new(std::sync::Mutex::new(wiki_recall::WikiIndex::default())),
    };

    scheduler::spawn_scheduler(
        state.store.clone(),
        Arc::clone(&state.llm),
        Arc::clone(&state.vault_dir),
        Arc::clone(&state.cfg),
        Arc::clone(&state.sync_lock),
        Arc::clone(&last_compact),
    )?;
    // cfg_path is only used by the HTTP/MCP handlers; the scheduler does not need it.

    let router = axum::Router::new()
        .route("/health", get(http::health))
        .route("/ask", post(http::handle_ask))
        .route("/brief", post(http::handle_brief))
        .route("/weekly", post(http::handle_weekly))
        .route("/status", post(http::handle_project_status))
        .route("/decisions", post(http::handle_decisions))
        .route("/risks", post(http::handle_risks))
        .route("/next_actions", post(http::handle_next_actions))
        .route("/stalled", post(http::handle_stalled))
        .route("/context", post(http::handle_context))
        .route("/search", post(http::handle_search))
        .route("/code-search", post(http::handle_code_search))
        .route("/graph", post(http::handle_graph))
        .route("/audit", get(http::handle_audit))
        .route("/query-log", get(http::handle_query_log))
        .route(
            "/events",
            get(http::handle_events).post(http::handle_event_ingest),
        )
        .route(
            "/otel-events",
            get(http::handle_events).post(http::handle_event_ingest),
        )
        .route("/sync", post(http::handle_sync))
        .route("/compact", post(http::handle_compact))
        .route("/mcp", get(mcp::handle_mcp_get).post(mcp::handle_mcp)) // MCP-over-HTTP (Streamable HTTP: GET SSE + POST JSON-RPC)
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| anyhow::anyhow!("bind {addr}: {e}"))?;
    eprintln!("[serve] listening on {addr}");

    axum::serve(listener, router)
        .await
        .map_err(|e| anyhow::anyhow!("axum serve: {e}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::{
        CHARS_PER_TOKEN_ESTIMATE, count_wiki_notes, optional_project, parse_exclude_origins,
        recall_max_chars,
    };
    use crate::frontmatter::GENERATED_BRIEF_TAG;

    #[test]
    fn optional_project_trims_and_filters_empty_values() {
        assert_eq!(optional_project(Some(" omb ")), Some("omb"));
        assert_eq!(optional_project(Some("   ")), None);
        assert_eq!(optional_project(None), None);
    }

    #[test]
    fn parse_exclude_origins_trims_dedupes_and_rejects_unknown_values() {
        let origins = vec![
            " company ".to_owned(),
            "company".to_owned(),
            String::new(),
            "mirror".to_owned(),
        ];
        assert_eq!(
            parse_exclude_origins(&origins).unwrap(),
            vec!["company".to_owned(), "mirror".to_owned()]
        );

        let invalid = vec!["work".to_owned()];
        let err = parse_exclude_origins(&invalid).unwrap_err();
        assert!(err.contains("invalid origin: work"));
    }

    #[test]
    fn recall_max_chars_preserves_token_budget_estimate() {
        assert_eq!(
            recall_max_chars(2_000).ok(),
            Some(2_000 * CHARS_PER_TOKEN_ESTIMATE)
        );
    }

    #[test]
    fn recall_max_chars_rejects_unrepresentable_budget() {
        assert!(matches!(
            recall_max_chars(usize::MAX),
            Err(err)
                if format!("{err:#}").contains("recall max_tokens cannot fit character budget")
        ));
    }

    #[test]
    fn health_corpus_count_excludes_generated_brief_and_eval_artifacts() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("wiki-0001.md"),
            "---\ntitle: source memory\n---\nreal memory",
        )
        .unwrap();
        std::fs::write(
            dir.path().join("daily-brief-2026-07-02.md"),
            format!("---\ntags: [{GENERATED_BRIEF_TAG}]\n---\ngenerated summary"),
        )
        .unwrap();
        std::fs::write(
            dir.path().join("wiki-0002.md"),
            format!("---\ntags:\n  - {GENERATED_BRIEF_TAG}\n---\ngenerated summary"),
        )
        .unwrap();
        std::fs::write(
            dir.path().join("eval-health.md"),
            "---\ntitle: eval source\n---\neval fixture",
        )
        .unwrap();
        std::fs::write(dir.path().join("scratch.txt"), "not wiki").unwrap();

        assert_eq!(count_wiki_notes(dir.path()), Some(1));
    }
}
