//! Store — pgvector (document/chunk/embedding/FTS) + graph (node/edge tables + recursive CTE).
//!
//! Cross-reference: ENFORCEMENT.md §A (error ADTs) · design decision D5 (claim temporal authority).
//!
//! ## Layers (engine-agnostic graph model)
//! - **pgvector** (`document`, `chunk`): vector (HNSW) + FTS (tsvector) + frontmatter columns.
//! - **graph** (`node`, `edge`): semantic ontology. node = entity, edge = typed relation.
//!   - node id convention: `doc:<source_path>` · `project:<name>` · `topic:<tag>`
//!     · `tool:<slug>` · `concept:<slug>` · `claim:<subject>:<predicate>`
//!     · typed claim nodes `decision|risk|assumption|blocked|goal|term|next:<subject>:<predicate>`.
//!   - the `document` table is the SSOT for documents; the graph references them by `doc:<path>` id (no duplicate storage).
//! - **traversal**: recursive CTE (`neighbors_khop`) — k-hop works even when the engine is not a graph DB.
//!   If the CTE proves insufficient, lift-and-shift to AGE/SurrealDB (schema is identical).
//!
//! ## Advantage over AGE
//! Every value goes through `tokio-postgres` parameter binding ($1,$2…) → eliminates the cypher string-escaping footgun.
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result};
use deadpool_postgres::{Manager, ManagerConfig, Object, Pool, RecyclingMethod, Runtime, Timeouts};

/// Pool I/O-boundary timeouts. Finite values prevent infinite hangs on
/// half-open sockets or a hung postgres process. The concrete seconds are a
/// defensive bound, not a root-cause fix; the real fix is detecting and
/// surfacing the failure rather than waiting forever. See CLAUDE.md
/// "I/O-boundary timeout" and PHILOSOPHY.md Layer-1 honesty.
const POOL_TIMEOUTS: Timeouts = Timeouts {
    wait: Some(Duration::from_secs(5)),
    create: Some(Duration::from_secs(5)),
    recycle: Some(Duration::from_secs(5)),
};

/// Connection recycling method. `Verified` runs a lightweight test query on
/// every reuse so that FIN-less cuts (idle reap, NAT timeout) cannot present a
/// dead connection as alive. The cost is ~1 RTT per acquisition; that is
/// preferred over the lie of a silently broken connection.
const POOL_RECYCLING_METHOD: RecyclingMethod = RecyclingMethod::Verified;
use pgvector::Vector;
use serde_json::{Value, json};
use tokio_postgres::types::Json as PgJson;
use tokio_postgres::{Client, NoTls};

use crate::frontmatter::{Claim, FrontMatter, GENERATED_BRIEF_TAG};

/// Ingest input (one chunk).
pub struct Doc {
    pub id: String, // "{source_path}#{idx}"
    pub content: String,
    pub embedding: Vec<f32>,
    pub front: FrontMatter,
    pub chunk_idx: usize,
}

#[derive(Debug)]
#[allow(dead_code)] // some fields are for retrieve / display only
pub struct Hit {
    pub id: String,
    pub content: String,
    pub origin: String,
    pub project: String,
    pub source_path: String,
    pub dist: f32,
    pub score: f64,
}

#[derive(Debug)]
pub struct Meta {
    pub origin: String,
    pub project: String,
    pub kind: String,
    pub source_path: String,
}

/// One recency-ordered retrieval — the full body of a single document (chunks joined). Returned by `updated_at` descending.
#[derive(Debug)]
pub struct RecentDoc {
    pub source_path: String,
    pub project: String,
    pub content: String,
    pub tags: Vec<String>,
}

/// Relation lane that produced a related document.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RelatedEvidenceKind {
    Graph,
    Claim,
}

/// Why a document was retrieved as related to another document.
#[derive(Debug)]
pub struct RelatedEvidence {
    pub kind: RelatedEvidenceKind,
    pub shared_count: i64,
    pub shared_nodes: Vec<String>,
}

/// A related document plus the deterministic graph evidence that linked it.
#[derive(Debug)]
pub struct RelatedDoc {
    pub doc: RecentDoc,
    pub evidence: RelatedEvidence,
}

/// A claim row plus its owning wiki/source document.
/// `frontmatter::Claim` remains the semantic fact shape; provenance belongs to
/// the store row so API `sources` can point at files rather than claim subjects.
#[derive(Debug, Clone)]
pub struct ClaimRecord {
    pub claim: Claim,
    pub source_path: String,
}

/// Graph size summary (for audit).
#[derive(Debug, Default)]
pub struct GraphStats {
    pub documents: usize,
    pub chunks: usize,
    pub projects: usize,
    pub topics: usize,
    pub claims: usize,
    pub decisions: usize,
    pub risks: usize,
    pub edges: usize,
}

/// GC deletion stats.
#[derive(Debug, Default)]
pub struct GcStats {
    pub tool: usize,
    pub concept: usize,
}

/// Graph-signal features for reranking a candidate against the top vector hit.
#[derive(Debug)]
pub struct GraphScore {
    pub shared_tools: i32,
    pub shared_claims: i32,
    pub degree: i32,
    pub recency_hours: f64,
}

impl GcStats {
    pub const fn total(&self) -> usize {
        self.tool + self.concept
    }
}

/// One query/retrieval event — used for memory utility analytics.
#[derive(Debug)]
pub struct QueryLogRow {
    pub id: i32,
    pub created_at: SystemTime,
    pub endpoint: String,
    pub query: String,
    pub hit_paths: Vec<String>,
    pub sources: Vec<String>,
    pub answer_snippet: String,
    pub latency_ms: Option<i32>,
    pub meta: Value,
}

/// One repeated query line mined from `query_log` (see `Store::repeated_queries`).
#[derive(Debug)]
pub struct QueryHotspot {
    pub query: String,
    pub count: i64,
    pub last_at: SystemTime,
}

/// One structured workflow event stored in Postgres. Shape follows the OpenTelemetry log model
/// while preserving the legacy adapter keys used by readiness (`component`, `event`, `status`, etc.).
#[derive(Debug)]
pub struct EventLogRow {
    pub id: i64,
    pub observed_at: SystemTime,
    pub time_unix_nano: Option<i64>,
    pub severity_text: String,
    pub severity_number: i32,
    pub service_name: String,
    pub component: String,
    pub event_name: String,
    pub status: String,
    pub trace_id: Option<String>,
    pub span_id: Option<String>,
    pub run_id: Option<String>,
    pub session_id: Option<String>,
    pub workflow: Option<String>,
    pub workflow_node: Option<String>,
    pub workflow_outcome: Option<String>,
    pub body: Value,
    pub attributes: Value,
    pub resource: Value,
}

/// Read filter for the queryable event log.
pub struct EventLogFilter<'a> {
    pub limit: i64,
    pub component: Option<&'a str>,
    pub event_name: Option<&'a str>,
    pub status: Option<&'a str>,
    pub run_id: Option<&'a str>,
    pub workflow: Option<&'a str>,
    pub since_hours: Option<i32>,
}

/// Result of a maintenance compact pass.
#[derive(Debug, Default)]
pub struct CompactReport {
    pub vacuum_ms: u128,
    pub reindex_ms: u128,
    pub prune_query_log: usize,
    pub gc_tool: usize,
    pub gc_concept: usize,
}

/// Compact report with an overall elapsed time.
#[derive(Debug, Default)]
pub struct CompactSummary {
    pub report: CompactReport,
    pub total_ms: u128,
}

/// Semantic graph stats (for audit).
#[derive(Debug, Default)]
pub struct SemanticStats {
    pub tools: usize,
    pub concepts: usize,
    pub uses: usize,
    pub about: usize,
}

/// A wiki note linked to a code symbol by `remember_code` (`doc:…` → `code:…` `code_uses` edge).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodeNoteLink {
    /// `code:<kind>:<path>:<name>` node id of the linked symbol.
    pub symbol_node_id: String,
    /// Note document path (`vault/wiki/wiki-XXXX.md`).
    pub source_path: String,
    /// Note title (empty when the frontmatter has none).
    pub title: String,
    /// First-chunk preview of the note body (may be empty).
    pub snippet: String,
}

pub struct Store {
    /// Postgres connection pool. Each query acquires a live connection so a single broken
    /// connection cannot permanently wedge the process.
    pool: Pool,
    /// Embedding dimension (= `boring.json` `embed_dim`; bge-m3 = 1024). Enforced at every embedding
    /// upsert via `checked_vector` and mirrored by the `vector(dim)` DDL columns created in `open`.
    dim: usize,
}

/// Semantic edge kinds (doc→entity) — the SSOT shared by clear/stats.
/// Kernel A: graph is tool/concept only (`uses`/`about`). Narrative (problem/attempt/solution) lives in
/// the note body markdown, not as graph nodes — so those edge kinds are gone.
const SEMANTIC_EDGE_KINDS: [&str; 3] = ["uses", "about", "claims"];
/// Exact graph-related documents use durable tool/concept overlap plus project/topic
/// proximity (`in_project`, `tagged`). Claim-axis continuity is ranked separately by
/// `claim_related_docs`; keeping it out here prevents the same claim signal from
/// consuming both relation lanes.
const RELATED_DOC_EDGE_KINDS: [&str; 2] = ["uses", "about"];
/// Code graph edge kinds — AST-derived relations between code symbols. These are
/// deterministic (tree-sitter parse), not LLM-generated, and stay in their own lane
/// so wiki semantic queries can filter them out by kind when needed.
#[allow(dead_code)] // wired in Phase 2/3 when code ingest/retrieve lands
const CODE_EDGE_KINDS: [&str; 5] = [
    "code_calls",
    "code_imports",
    "code_inherits",
    "code_contains",
    "code_uses",
];
/// Internal eval fixtures must remain searchable while `make eval` is running, but they are not
/// user memory. Recency and claim surfaces feed briefings/status, so exclude that fixture namespace.
const INTERNAL_EVAL_FIXTURE_RE: &str = r"(^|/)eval-[^/]*\.md$";

fn default_origin_key() -> &'static str {
    crate::config::Origin::Personal.as_str()
}

/// chunk id ("path#idx") → graph document node id ("doc:path").
fn doc_node_id(chunk_or_path: &str) -> String {
    let path = chunk_or_path
        .rsplit_once('#')
        .map_or(chunk_or_path, |(p, _)| p);
    format!("doc:{path}")
}

fn doc_path_from_node_id(node_id: &str) -> Result<String> {
    node_id
        .strip_prefix("doc:")
        .map(str::to_owned)
        .with_context(|| format!("document node id missing doc: prefix: {node_id}"))
}

fn claim_node_id(claim: &Claim) -> String {
    let subject_key = crate::frontmatter::claim_key(&claim.subject);
    let predicate_key = crate::frontmatter::claim_key(&claim.predicate);
    format!("claim:{subject_key}:{predicate_key}")
}

fn canonical_claim_axis(subject: &str, predicate: &str) -> (String, String) {
    (
        crate::frontmatter::claim_key(subject),
        crate::frontmatter::claim_key(predicate),
    )
}

fn typed_claim_node_id(kind: &str, claim: &Claim) -> String {
    let subject_key = crate::frontmatter::claim_key(&claim.subject);
    let predicate_key = crate::frontmatter::claim_key(&claim.predicate);
    format!("{kind}:{subject_key}:{predicate_key}")
}

fn typed_claim_axis_node_ids(subject_key: &str, predicate_key: &str) -> Vec<String> {
    crate::frontmatter::CLAIM_KINDS
        .iter()
        .filter(|kind| **kind != "fact")
        .map(|kind| format!("{kind}:{subject_key}:{predicate_key}"))
        .collect()
}

fn db_i64_count_to_usize(n: i64, label: &str) -> Result<usize> {
    usize::try_from(n).with_context(|| format!("{label} count cannot fit usize"))
}

fn db_u64_rows_to_usize(n: u64, label: &str) -> Result<usize> {
    usize::try_from(n).with_context(|| format!("{label} affected row count cannot fit usize"))
}

fn store_usize_to_i32(n: usize, label: &str) -> Result<i32> {
    i32::try_from(n).with_context(|| format!("{label} cannot fit i32"))
}

fn store_usize_to_i64(n: usize, label: &str) -> Result<i64> {
    i64::try_from(n).with_context(|| format!("{label} cannot fit i64"))
}

async fn pg_count(db: &Client, sql: &str) -> Result<usize> {
    let row = db.query_one(sql, &[]).await?;
    let n: i64 = row.get(0);
    db_i64_count_to_usize(n, "postgres")
}

async fn count_node_kind(db: &Client, kind: &str) -> Result<usize> {
    let row = db
        .query_one("SELECT count(*) FROM node WHERE kind = $1;", &[&kind])
        .await?;
    let n: i64 = row.get(0);
    db_i64_count_to_usize(n, "node kind")
}

async fn count_edge_kind(db: &Client, kind: &str) -> Result<usize> {
    let row = db
        .query_one("SELECT count(*) FROM edge WHERE kind = $1;", &[&kind])
        .await?;
    let n: i64 = row.get(0);
    db_i64_count_to_usize(n, "edge kind")
}

impl Store {
    // ── connect + ensure schema ───────────────────────────────────────────────

    /// PostgreSQL connect + pgvector + node/edge graph schema initialization.
    /// `dim` = the embedding dimension (`boring.json` `embed_dim`) → the `vector(dim)` columns.
    #[allow(clippy::too_many_lines)] // schema DDL grows with features; splitting only obscures the one migration block.
    pub async fn open(dsn: &str, dim: usize) -> Result<Self> {
        let pg_config: tokio_postgres::Config = dsn.parse().context("parse postgres dsn")?;
        let manager = Manager::from_config(
            pg_config,
            NoTls,
            ManagerConfig {
                recycling_method: POOL_RECYCLING_METHOD,
            },
        );
        let pool = Pool::builder(manager)
            .runtime(Runtime::Tokio1)
            .timeouts(POOL_TIMEOUTS)
            .build()?;

        // connect retry (IO boundary, graceful) — when postgres is started separately via profile
        // drudge waits up to ~10s even if it comes up first (depends_on removed → absorbs startup race).
        let client = {
            let mut tries = 0_u32;
            loop {
                match pool.get().await {
                    Ok(client) => break client,
                    Err(e) if tries < 9 => {
                        tries += 1;
                        eprintln!("[store] postgres connect retry {tries}/10 … ({e})");
                        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                    }
                    Err(e) => {
                        return Err(anyhow::Error::new(e).context(
                            "postgres connect (retries exhausted) — is Postgres up? \
                             vector mode needs `BORING_VECTOR=on make up` (starts pgvector); \
                             or run wiki-first with BORING_VECTOR unset",
                        ));
                    }
                }
            }
        };

        // DDL parameterized by the embedding dim (`embed_dim`). `vector({dim})` is the only interpolation;
        // dim is a parsed integer (no injection surface). `'{{}}'` escapes the literal empty-array default.
        client
            .batch_execute(&format!(
                "CREATE EXTENSION IF NOT EXISTS vector;
                 CREATE TABLE IF NOT EXISTS document (
                     source_path text PRIMARY KEY,
                     origin      text NOT NULL DEFAULT '',
                     project     text NOT NULL DEFAULT '',
                     kind        text NOT NULL DEFAULT '',
                     title       text,
                     tags        text[] NOT NULL DEFAULT '{{}}',
                     sha         text NOT NULL DEFAULT '',
                     extracted_sha text NOT NULL DEFAULT '',
                     updated_at  timestamptz NOT NULL DEFAULT now()
                 );
                 CREATE TABLE IF NOT EXISTS chunk (
                     id          text PRIMARY KEY,
                     source_path text NOT NULL REFERENCES document(source_path) ON DELETE CASCADE,
                     content     text NOT NULL DEFAULT '',
                     embedding   vector({dim}), -- = boring.json embed_dim (guarded in upsert_chunk)
                     origin      text NOT NULL DEFAULT '',
                     project     text NOT NULL DEFAULT '',
                     kind        text NOT NULL DEFAULT '',
                     chunk_idx   int  NOT NULL DEFAULT 0,
                     tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
                 );
                 CREATE INDEX IF NOT EXISTS chunk_hnsw ON chunk USING hnsw (embedding vector_cosine_ops);
                 CREATE INDEX IF NOT EXISTS chunk_gin  ON chunk USING gin (tsv);
                 CREATE TABLE IF NOT EXISTS node (
                     id      text PRIMARY KEY,
                     kind    text NOT NULL,
                     label   text NOT NULL DEFAULT '',
                     outcome text
                 );
                 CREATE TABLE IF NOT EXISTS edge (
                     src  text NOT NULL,
                     dst  text NOT NULL,
                     kind text NOT NULL,
                     PRIMARY KEY (src, dst, kind)
                 );
                 CREATE INDEX IF NOT EXISTS edge_src ON edge(src);
                 CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst);
                 CREATE INDEX IF NOT EXISTS node_kind ON node(kind);
                 CREATE INDEX IF NOT EXISTS edge_kind ON edge(kind);
                 ALTER TABLE document ADD COLUMN IF NOT EXISTS extracted_sha text NOT NULL DEFAULT '';
                 ALTER TABLE document ADD COLUMN IF NOT EXISTS code_sha text NOT NULL DEFAULT '';
                 ALTER TABLE document ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
                 CREATE INDEX IF NOT EXISTS document_updated ON document(updated_at DESC);
                 -- claim: temporal fact authority (Graphiti-style invalidation, scaled down for personal use).
                 --   current value of (subject,predicate) = superseded_at IS NULL. A new value seals the old.
                 CREATE TABLE IF NOT EXISTS claim (
                     subject       text NOT NULL,
                     predicate     text NOT NULL,
                     value         text NOT NULL,
                     source_path   text NOT NULL,
                     valid_from    timestamptz NOT NULL,
                     superseded_at timestamptz,
                     embedding     vector({dim}), -- = boring.json embed_dim
                     kind          text NOT NULL DEFAULT 'fact',
                     confidence    text NOT NULL DEFAULT 'certain',
                     PRIMARY KEY (subject, predicate, valid_from)
                 );
                 ALTER TABLE claim ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'fact';
                 ALTER TABLE claim ADD COLUMN IF NOT EXISTS confidence text NOT NULL DEFAULT 'certain';
                 CREATE INDEX IF NOT EXISTS claim_current ON claim(subject, predicate)
                     WHERE superseded_at IS NULL;
                 CREATE INDEX IF NOT EXISTS claim_kind ON claim(kind)
                     WHERE superseded_at IS NULL;
                 CREATE INDEX IF NOT EXISTS claim_hnsw ON claim USING hnsw (embedding vector_cosine_ops)
                     WHERE superseded_at IS NULL;
                 CREATE TABLE IF NOT EXISTS query_log (
                     id            serial PRIMARY KEY,
                     created_at    timestamptz NOT NULL DEFAULT now(),
                     endpoint      text NOT NULL,
                     query         text NOT NULL DEFAULT '',
                     hit_paths     text[] NOT NULL DEFAULT '{{}}',
                     sources       text[] NOT NULL DEFAULT '{{}}',
                     answer_snippet text NOT NULL DEFAULT '',
                     latency_ms    int,
                     meta          jsonb NOT NULL DEFAULT '{{}}'
                 );
                 /* Migration guard: existing deployments created before the `meta` column was added
                    keep their table (CREATE TABLE IF NOT EXISTS is a no-op) but lack the column.
                    This ALTER is idempotent and cheap; it prevents the runtime column-does-not-exist
                    error without requiring a manual migration script. */
                 ALTER TABLE query_log ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL DEFAULT '{{}}';
                 CREATE INDEX IF NOT EXISTS query_log_created ON query_log(created_at DESC);
                 CREATE TABLE IF NOT EXISTS event_log (
                     id               bigserial PRIMARY KEY,
                     observed_at      timestamptz NOT NULL DEFAULT now(),
                     time_unix_nano   bigint,
                     severity_text    text NOT NULL DEFAULT 'INFO',
                     severity_number  int NOT NULL DEFAULT 9,
                     service_name     text NOT NULL DEFAULT '',
                     component        text NOT NULL DEFAULT '',
                     event_name       text NOT NULL DEFAULT '',
                     status           text NOT NULL DEFAULT '',
                     trace_id         text,
                     span_id          text,
                     run_id           text,
                     session_id       text,
                     workflow         text,
                     workflow_node    text,
                     workflow_outcome text,
                     body             jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                     attributes       jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                     resource         jsonb NOT NULL DEFAULT '{{}}'::jsonb
                 );
                 CREATE INDEX IF NOT EXISTS event_log_observed ON event_log(observed_at DESC, id DESC);
                 CREATE INDEX IF NOT EXISTS event_log_component ON event_log(component, observed_at DESC);
                 CREATE INDEX IF NOT EXISTS event_log_event ON event_log(event_name, observed_at DESC);
                 CREATE INDEX IF NOT EXISTS event_log_status ON event_log(status, observed_at DESC);
                 CREATE INDEX IF NOT EXISTS event_log_run_id ON event_log(run_id, observed_at DESC);"
            ))
            .await
            .context("pgvector + graph schema")?;

        Ok(Self { pool, dim })
    }

    /// Acquire a live connection from the pool. Callers receive a fresh/recycled connection,
    /// so a single broken connection cannot wedge the whole store.
    async fn db(&self) -> Result<Object> {
        self.pool
            .get()
            .await
            .context("acquire postgres connection from pool")
    }

    /// Run a single `SELECT 1` probe. Returns `Ok(())` when Postgres is reachable and able to
    /// execute a query; returns an error otherwise. Used by `/health` to surface DB health.
    pub async fn liveness_probe(&self) -> Result<()> {
        let client = self.db().await?;
        client
            .query_one("SELECT 1;", &[])
            .await
            .context("db liveness probe")?;
        Ok(())
    }

    /// Build a pgvector `Vector` after checking the dimension matches the `vector(dim)` columns.
    /// Single boundary guard shared by every embedding insert (chunk + claim) — parse-don't-validate:
    /// a model whose output dim ≠ `embed_dim` fails loud here with an actionable message, not with a
    /// cryptic Postgres error deep in the fire-and-forget scheduler.
    fn checked_vector(&self, embedding: &[f32]) -> Result<Vector> {
        if embedding.len() != self.dim {
            anyhow::bail!(
                "embedding dim mismatch: got {}, expected {}. boring.json embed_model must output \
                 {}-dim vectors (embed_dim), or change embed_dim + `make reset`.",
                embedding.len(),
                self.dim,
                self.dim
            );
        }
        Ok(Vector::from(embedding.to_vec()))
    }

    // ── document ─────────────────────────────────────────────────────────────

    pub async fn get_doc_sha(&self, path: &str) -> Result<Option<String>> {
        let rows = self
            .db()
            .await?
            .query("SELECT sha FROM document WHERE source_path = $1;", &[&path])
            .await?;
        Ok(rows.first().map(|r| r.get::<_, String>(0)))
    }

    pub async fn all_doc_paths(&self) -> Result<Vec<String>> {
        let rows = self
            .db()
            .await?
            .query("SELECT source_path FROM document;", &[])
            .await?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Document mtime — the `valid_from` of a claim (temporal sort key).
    pub async fn doc_updated_at(&self, path: &str) -> Result<Option<SystemTime>> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT updated_at FROM document WHERE source_path = $1;",
                &[&path],
            )
            .await?;
        Ok(rows.first().map(|r| r.get::<_, SystemTime>(0)))
    }

    /// Update only the recency of documents whose content is unchanged (same sha) — mtime backfill without re-embedding.
    /// Ensures the recency-first sort key (`updated_at`) is also populated for existing documents.
    pub async fn set_updated_at(&self, path: &str, updated_at: SystemTime) -> Result<()> {
        self.db()
            .await?
            .execute(
                "UPDATE document SET updated_at = $2
                 WHERE source_path = $1 AND updated_at IS DISTINCT FROM $2;",
                &[&path, &updated_at],
            )
            .await
            .context("touch updated_at")?;
        Ok(())
    }

    /// Top-N documents by recency — full body (chunks joined). Retrieval for the recency-first/supersede briefing.
    /// Ordered by `updated_at` descending rather than semantic similarity = "most recently changed knowledge on top".
    /// If `since_hours` is Some, only documents updated within that window are returned.
    /// If `until_hours` is also Some, documents updated after `now() - make_interval(hours => $until)`
    /// are excluded, producing a hard date window.
    pub async fn recent_docs(
        &self,
        limit: i64,
        exclude_origins: &[String],
        since_hours: Option<i32>,
        until_hours: Option<i32>,
        project: Option<&str>,
    ) -> Result<Vec<RecentDoc>> {
        let default_origin = default_origin_key();
        let rows = match (since_hours, until_hours) {
            (Some(since), Some(until)) => self
                .db()
                .await?
                .query(
                    "SELECT d.source_path, d.project, d.tags,
                                string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                         FROM document d
                         JOIN chunk c ON c.source_path = d.source_path
                         WHERE NOT (COALESCE(NULLIF(d.origin, ''), $8) = ANY($2))
                            AND d.updated_at >= now() - make_interval(hours => $3)
                            AND d.updated_at <= now() - make_interval(hours => $4)
                            AND ($5::text IS NULL OR d.project = $5)
                            AND d.source_path !~ $6
                            AND NOT ($7 = ANY(d.tags))
                         GROUP BY d.source_path, d.project, d.tags, d.updated_at
                         ORDER BY d.updated_at DESC
                         LIMIT $1;",
                    &[
                        &limit,
                        &exclude_origins,
                        &since,
                        &until,
                        &project,
                        &INTERNAL_EVAL_FIXTURE_RE,
                        &GENERATED_BRIEF_TAG,
                        &default_origin,
                    ],
                )
                .await
                .context("recent docs with time window")?,
            (Some(hours), None) => self
                .db()
                .await?
                .query(
                    "SELECT d.source_path, d.project, d.tags,
                                string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                         FROM document d
                         JOIN chunk c ON c.source_path = d.source_path
                         WHERE NOT (COALESCE(NULLIF(d.origin, ''), $7) = ANY($2))
                            AND d.updated_at >= now() - make_interval(hours => $3)
                            AND ($4::text IS NULL OR d.project = $4)
                            AND d.source_path !~ $5
                           AND NOT ($6 = ANY(d.tags))
                         GROUP BY d.source_path, d.project, d.tags, d.updated_at
                         ORDER BY d.updated_at DESC
                         LIMIT $1;",
                    &[
                        &limit,
                        &exclude_origins,
                        &hours,
                        &project,
                        &INTERNAL_EVAL_FIXTURE_RE,
                        &GENERATED_BRIEF_TAG,
                        &default_origin,
                    ],
                )
                .await
                .context("recent docs with time window")?,
            (None, _) => self
                .db()
                .await?
                .query(
                    "SELECT d.source_path, d.project, d.tags,
                                string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                         FROM document d
                         JOIN chunk c ON c.source_path = d.source_path
                         WHERE NOT (COALESCE(NULLIF(d.origin, ''), $6) = ANY($2))
                            AND ($3::text IS NULL OR d.project = $3)
                            AND d.source_path !~ $4
                            AND NOT ($5 = ANY(d.tags))
                         GROUP BY d.source_path, d.project, d.tags, d.updated_at
                         ORDER BY d.updated_at DESC
                         LIMIT $1;",
                    &[
                        &limit,
                        &exclude_origins,
                        &project,
                        &INTERNAL_EVAL_FIXTURE_RE,
                        &GENERATED_BRIEF_TAG,
                        &default_origin,
                    ],
                )
                .await
                .context("recent docs")?,
        };
        Ok(rows
            .iter()
            .map(|r| RecentDoc {
                source_path: r.get(0),
                project: r.get(1),
                tags: r.get(2),
                content: r.get(3),
            })
            .collect())
    }

    /// Document↔document relations — other documents that share **concrete** tool/concept nodes,
    /// ordered by shared count descending. The basis for the Obsidian relates_to projection.
    /// 2-hop over the graph (edge): doc → (shared dst) ← otherDoc.
    /// Project/topic and claim-axis links are excluded here; those have separate relation lanes.
    /// Projection candidates stay inside the source document's origin boundary.
    /// Requires at least 2 shared nodes to link (cuts the noise of an accidental single overlap).
    pub async fn related_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let doc_id = doc_node_id(source_path);
        let edge_kinds = RELATED_DOC_EDGE_KINDS.to_vec();
        let default_origin = default_origin_key();
        let rows = self
            .db()
            .await?
            .query(
                "WITH source_doc AS (
                     SELECT COALESCE(NULLIF(origin, ''), $7) AS origin
                     FROM document
                     WHERE source_path = $6
                  ),
                  self_nodes AS (
                      SELECT dst, kind FROM edge WHERE src = $1 AND kind = ANY($3)
                 )
                 SELECT e.src, count(DISTINCT e.dst) AS shared
                 FROM edge e
                 JOIN self_nodes sn ON e.dst = sn.dst AND e.kind = sn.kind
                 JOIN document d ON ('doc:' || d.source_path) = e.src
                 JOIN source_doc sd ON COALESCE(NULLIF(d.origin, ''), $7) = sd.origin
                 WHERE e.src <> $1 AND e.src LIKE 'doc:%'
                   AND d.source_path !~ $4
                   AND NOT ($5 = ANY(d.tags))
                 GROUP BY e.src
                 HAVING count(DISTINCT e.dst) >= 2
                 ORDER BY shared DESC, e.src ASC
                 LIMIT $2;",
                &[
                    &doc_id,
                    &limit,
                    &edge_kinds,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &source_path,
                    &default_origin,
                ],
            )
            .await
            .context("related docs")?;
        // 'doc:<source_path>' → restore source_path
        rows.iter()
            .map(|r| {
                let id: String = r.get(0);
                doc_path_from_node_id(&id)
            })
            .collect()
    }

    /// Documents that share at least one claim identity with `source_path`.
    /// A shared `(subject,predicate)` is a strong temporal-continuity signal, so
    /// this complements `related_docs` without lowering its >=2 shared-node noise gate.
    /// Projection candidates stay inside the source document's origin boundary.
    pub async fn claim_related_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let doc_id = doc_node_id(source_path);
        let default_origin = default_origin_key();
        let rows = self
            .db()
            .await?
            .query(
                "WITH source_doc AS (
                     SELECT COALESCE(NULLIF(origin, ''), $6) AS origin
                     FROM document
                     WHERE source_path = $5
                  ),
                  self_claims AS (
                      SELECT dst FROM edge WHERE src = $1 AND kind = 'claims'
                 )
                 SELECT e.src, count(DISTINCT e.dst) AS shared
                 FROM edge e
                 JOIN self_claims sc ON e.dst = sc.dst
                 JOIN document d ON ('doc:' || d.source_path) = e.src
                 JOIN source_doc sd ON COALESCE(NULLIF(d.origin, ''), $6) = sd.origin
                 WHERE e.src <> $1 AND e.src LIKE 'doc:%' AND e.kind = 'claims'
                   AND d.source_path !~ $3
                   AND NOT ($4 = ANY(d.tags))
                 GROUP BY e.src
                 ORDER BY shared DESC, e.src ASC
                 LIMIT $2;",
                &[
                    &doc_id,
                    &limit,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &source_path,
                    &default_origin,
                ],
            )
            .await
            .context("claim related docs")?;
        rows.iter()
            .map(|r| {
                let id: String = r.get(0);
                doc_path_from_node_id(&id)
            })
            .collect()
    }

    /// k-hop graph expansion over the document↔entity↔document path using a recursive CTE.
    /// Returns distinct document source_paths ordered by minimum doc-hop distance and then
    /// by the number of shared first-hop entities. Stays inside the source document's origin
    /// boundary and excludes eval fixtures / generated briefs.
    pub async fn related_docs_khop(
        &self,
        source_path: &str,
        k: usize,
        limit: i64,
    ) -> Result<Vec<String>> {
        Ok(self
            .khop_doc_rows(
                source_path,
                &RELATED_DOC_EDGE_KINDS,
                k,
                limit,
                &[],
                None,
                2,
                false,
            )
            .await?
            .into_iter()
            .map(|rd| rd.doc.source_path)
            .collect())
    }

    /// k-hop claim-axis expansion over `claims` edges. Same boundary/filter rules as
    /// `related_docs_khop`, but a single shared claim axis is enough to link documents.
    pub async fn claim_related_docs_khop(
        &self,
        source_path: &str,
        k: usize,
        limit: i64,
    ) -> Result<Vec<String>> {
        Ok(self
            .khop_doc_rows(source_path, &["claims"], k, limit, &[], None, 1, true)
            .await?
            .into_iter()
            .map(|rd| rd.doc.source_path)
            .collect())
    }

    /// Shared recursive CTE implementation for graph/claim k-hop related documents.
    /// `min_shared` is the number of distinct shared first-hop entities required for a
    /// candidate to be returned (>=2 for the durable graph lane, >=1 for claim axis).
    #[allow(clippy::too_many_arguments, clippy::too_many_lines)]
    async fn khop_doc_rows(
        &self,
        source_path: &str,
        edge_kinds: &[&str],
        k: usize,
        limit: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        min_shared: i64,
        claim_axis: bool,
    ) -> Result<Vec<RelatedDoc>> {
        if k == 0 {
            return Ok(Vec::new());
        }
        let doc_id = doc_node_id(source_path);
        let max_edge_hop = store_usize_to_i32(k * 2, "khop max edge hop")?;
        let kinds = edge_kinds.to_vec();
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "WITH RECURSIVE source_doc AS (
                     SELECT COALESCE(NULLIF(origin, ''), $8) AS origin
                     FROM document
                     WHERE source_path = $7
                  ),
                  walk AS (
                      SELECT $1::text AS node, 0 AS hop, NULL::text AS shared_node,
                             ARRAY[$1]::text[] AS path
                      UNION ALL
                      SELECT nxt.node,
                             w.hop + 1,
                             CASE WHEN w.hop = 0 THEN nxt.node ELSE w.shared_node END,
                             w.path || nxt.node
                      FROM walk w
                      JOIN LATERAL (
                          SELECT e.dst AS node FROM edge e
                           WHERE e.src = w.node AND e.kind = ANY($2)
                          UNION
                          SELECT e.src AS node FROM edge e
                           WHERE e.dst = w.node AND e.kind = ANY($2)
                      ) nxt ON NOT nxt.node = ANY(w.path)
                      WHERE w.hop < $3
                  ),
                  ranked AS (
                      SELECT d.source_path,
                             MIN(w.hop / 2) AS doc_hop,
                             COUNT(DISTINCT w.shared_node) AS shared,
                             array_agg(
                               DISTINCT CASE WHEN $9
                                 THEN replace(regexp_replace(w.shared_node, '^claim:', ''), ':', ' / ')
                                 ELSE COALESCE(n.label, w.shared_node)
                               END
                               ORDER BY CASE WHEN $9
                                 THEN replace(regexp_replace(w.shared_node, '^claim:', ''), ':', ' / ')
                                 ELSE COALESCE(n.label, w.shared_node)
                               END
                             ) AS shared_nodes
                       FROM walk w
                       JOIN document d ON ('doc:' || d.source_path) = w.node
                       JOIN source_doc sd ON COALESCE(NULLIF(d.origin, ''), $8) = sd.origin
                       LEFT JOIN node n ON n.id = w.shared_node
                       WHERE w.node LIKE 'doc:%'
                         AND w.node <> $1
                         AND w.hop > 0
                         AND w.hop <= $4
                         AND NOT (COALESCE(NULLIF(d.origin, ''), $8) = ANY($5))
                         AND ($6::text IS NULL OR d.project = $6)
                         AND d.source_path !~ $10
                         AND NOT ($11 = ANY(d.tags))
                       GROUP BY d.source_path
                       HAVING COUNT(DISTINCT w.shared_node) >= $12
                       ORDER BY doc_hop, shared DESC, d.source_path ASC
                       LIMIT $13
                  )
                 SELECT d.source_path, d.project, d.tags,
                        string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content,
                        r.shared,
                        r.shared_nodes
                 FROM ranked r
                 JOIN document d ON d.source_path = r.source_path
                 JOIN chunk c ON c.source_path = d.source_path
                 GROUP BY d.source_path, d.project, d.tags, r.doc_hop, r.shared, r.shared_nodes
                 ORDER BY r.doc_hop, r.shared DESC, d.source_path ASC;",
                &[
                    &doc_id,
                    &kinds,
                    &max_edge_hop,
                    &max_edge_hop,
                    &exclude_origins,
                    &project,
                    &source_path,
                    &default_origin,
                    &claim_axis,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &min_shared,
                    &limit,
                ],
            )
            .await
            .context("khop related doc rows")?;
        Ok(rows
            .iter()
            .map(|r| RelatedDoc {
                doc: RecentDoc {
                    source_path: r.get(0),
                    project: r.get(1),
                    tags: r.get(2),
                    content: r.get(3),
                },
                evidence: RelatedEvidence {
                    kind: if claim_axis {
                        RelatedEvidenceKind::Claim
                    } else {
                        RelatedEvidenceKind::Graph
                    },
                    shared_count: r.get(4),
                    shared_nodes: r.get(5),
                },
            })
            .collect())
    }

    /// Claim-axis related document bodies for briefing context. This stays separate from
    /// `related_doc_content` so tool/concept GraphRAG and claim-continuity evidence cannot
    /// masquerade as the same relation lane.
    pub async fn claim_related_doc_content(
        &self,
        source_path: &str,
        limit: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        depth: Option<usize>,
    ) -> Result<Vec<RelatedDoc>> {
        if let Some(k) = depth {
            return self
                .khop_doc_rows(
                    source_path,
                    &["claims"],
                    k,
                    limit,
                    exclude_origins,
                    project,
                    1,
                    true,
                )
                .await;
        }
        let doc_id = doc_node_id(source_path);
        let default_origin = default_origin_key();
        let rows = self
            .db()
            .await?
            .query(
                "WITH source_doc AS (
                     SELECT COALESCE(NULLIF(origin, ''), $8) AS origin
                     FROM document
                     WHERE source_path = $7
                  ),
                  self_claims AS (
                      SELECT dst FROM edge WHERE src = $1 AND kind = 'claims'
                  ),
                  ranked AS (
                      SELECT e.src AS doc_node,
                             count(DISTINCT e.dst) AS shared,
                             array_agg(
                               DISTINCT replace(regexp_replace(sc.dst, '^claim:', ''), ':', ' / ')
                               ORDER BY replace(regexp_replace(sc.dst, '^claim:', ''), ':', ' / ')
                             ) AS shared_nodes
                       FROM edge e
                       JOIN self_claims sc ON e.dst = sc.dst
                       JOIN document d ON ('doc:' || d.source_path) = e.src
                       JOIN source_doc sd ON COALESCE(NULLIF(d.origin, ''), $8) = sd.origin
                       WHERE e.src <> $1 AND e.src LIKE 'doc:%' AND e.kind = 'claims'
                         AND NOT (COALESCE(NULLIF(d.origin, ''), $8) = ANY($3))
                         AND ($4::text IS NULL OR d.project = $4)
                         AND d.source_path !~ $5
                         AND NOT ($6 = ANY(d.tags))
                       GROUP BY e.src ORDER BY shared DESC, e.src ASC LIMIT $2
                  )
                 SELECT d.source_path, d.project, d.tags,
                        string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content,
                        r.shared,
                        r.shared_nodes
                 FROM ranked r
                 JOIN document d ON ('doc:' || d.source_path) = r.doc_node
                 JOIN chunk c ON c.source_path = d.source_path
                 GROUP BY d.source_path, d.project, d.tags, r.shared, r.shared_nodes
                 ORDER BY r.shared DESC, d.source_path ASC;",
                &[
                    &doc_id,
                    &limit,
                    &exclude_origins,
                    &project,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &source_path,
                    &default_origin,
                ],
            )
            .await
            .context("claim related doc content")?;
        Ok(rows
            .iter()
            .map(|r| RelatedDoc {
                doc: RecentDoc {
                    source_path: r.get(0),
                    project: r.get(1),
                    tags: r.get(2),
                    content: r.get(3),
                },
                evidence: RelatedEvidence {
                    kind: RelatedEvidenceKind::Claim,
                    shared_count: r.get(4),
                    shared_nodes: r.get(5),
                },
            })
            .collect())
    }

    /// Documents semantically nearest to `source_path` by chunk-embedding cosine — the MEANING-based
    /// complement to `related_docs`. The vector index is only a candidate finder for visible
    /// `relates_to` links: a candidate must also share the same project or at least one deterministic
    /// graph edge (`uses`/`about`/`claims`) with the source. This keeps Obsidian links from becoming
    /// embedding-only guesses while still catching same-project notes written in different words.
    /// Projection candidates stay inside the source document's origin boundary.
    pub async fn semantic_related_docs(
        &self,
        source_path: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<String>> {
        let default_origin = default_origin_key();
        let rows = self
            .db()
            .await?
            .query(
                "WITH src AS (
                     SELECT c.embedding, d.project, COALESCE(NULLIF(d.origin, ''), $6) AS origin
                      FROM chunk c JOIN document d ON d.source_path = c.source_path
                      WHERE c.source_path = $1 AND c.embedding IS NOT NULL
                    )
                   SELECT c.source_path, MIN(c.embedding <=> s.embedding)::float8 AS dist
                   FROM chunk c
                   JOIN document d ON d.source_path = c.source_path
                   CROSS JOIN src s
                   WHERE c.source_path <> $1 AND c.embedding IS NOT NULL
                      AND c.source_path !~ $4
                      AND NOT ($5 = ANY(d.tags))
                      AND COALESCE(NULLIF(d.origin, ''), $6) = s.origin
                      AND (
                          (d.project <> '' AND d.project = s.project)
                          OR EXISTS (
                             SELECT 1
                             FROM edge self_edge
                             JOIN edge other_edge
                               ON other_edge.dst = self_edge.dst
                              AND other_edge.kind = self_edge.kind
                             WHERE self_edge.src = ('doc:' || $1)
                               AND other_edge.src = ('doc:' || c.source_path)
                               AND self_edge.kind IN ('uses', 'about', 'claims')
                         )
                     )
                   GROUP BY c.source_path
                   HAVING MIN(c.embedding <=> s.embedding) <= $2
                   ORDER BY dist ASC
                 LIMIT $3;",
                &[
                    &source_path,
                    &max_dist,
                    &limit,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await
            .context("semantic related docs")?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Graph-signal features for every candidate relative to the top vector hit.
    /// Counts shared tool/concept nodes, shared claim-axis nodes, total graph degree,
    /// and recency decay (1.0 = just updated).
    pub async fn graph_rerank_features(
        &self,
        query_top: &Hit,
        candidates: &[Hit],
    ) -> Result<Vec<GraphScore>> {
        let top_id = doc_node_id(&query_top.source_path);
        let mut out = Vec::with_capacity(candidates.len());
        for candidate in candidates {
            let candidate_id = doc_node_id(&candidate.source_path);
            let row = self
                .db()
                .await?
                .query_one(
                    "SELECT
                        (SELECT count(DISTINCT e1.dst)
                           FROM edge e1
                           JOIN edge e2
                             ON e2.dst = e1.dst AND e2.kind = e1.kind
                          WHERE e1.src = $1 AND e2.src = $2 AND e1.kind IN ('uses', 'about'))
                          AS shared_tools,
                        (SELECT count(DISTINCT e1.dst)
                           FROM edge e1
                           JOIN edge e2 ON e2.dst = e1.dst
                          WHERE e1.src = $1 AND e2.src = $2 AND e1.kind = 'claims')
                          AS shared_claims,
                        (SELECT count(*) FROM edge WHERE src = $2 OR dst = $2) AS degree,
                        (SELECT updated_at FROM document WHERE source_path = $3) AS updated_at;",
                    &[&top_id, &candidate_id, &candidate.source_path],
                )
                .await
                .context("graph rerank features")?;
            let shared_tools: i64 = row.get(0);
            let shared_claims: i64 = row.get(1);
            let degree: i64 = row.get(2);
            let updated_at: Option<SystemTime> = row.get(3);
            let recency_hours = updated_at.map_or(f64::INFINITY, |t| {
                let elapsed = SystemTime::now().duration_since(t).unwrap_or_default();
                elapsed.as_secs_f64() / 3600.0
            });
            out.push(GraphScore {
                shared_tools: i32::try_from(shared_tools).unwrap_or(i32::MAX),
                shared_claims: i32::try_from(shared_claims).unwrap_or(i32::MAX),
                degree: i32::try_from(degree).unwrap_or(i32::MAX),
                recency_hours,
            });
        }
        Ok(out)
    }

    /// GraphRAG retrieval: the body of the top-N connected documents that **share a concrete concept/tool** with a document.
    /// Surfaces, via the graph, the right answer that the vector buried in noise.
    /// Claim-axis continuity is injected separately as current-claim authority, not duplicated as graph context.
    pub async fn related_doc_content(
        &self,
        source_path: &str,
        limit: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        depth: Option<usize>,
    ) -> Result<Vec<RelatedDoc>> {
        if let Some(k) = depth {
            return self
                .khop_doc_rows(
                    source_path,
                    &RELATED_DOC_EDGE_KINDS,
                    k,
                    limit,
                    exclude_origins,
                    project,
                    2,
                    false,
                )
                .await;
        }
        let doc_id = doc_node_id(source_path);
        let edge_kinds = RELATED_DOC_EDGE_KINDS.to_vec();
        let default_origin = default_origin_key();
        let rows = self
            .db()
            .await?
            .query(
                "WITH source_doc AS (
                     SELECT COALESCE(NULLIF(origin, ''), $9) AS origin
                     FROM document
                     WHERE source_path = $8
                  ),
                  self_nodes AS (
                      SELECT dst, kind FROM edge WHERE src = $1 AND kind = ANY($3)
                  ),
                  ranked AS (
                      SELECT e.src AS doc_node,
                             count(DISTINCT COALESCE(n.label, sn.dst)) AS shared,
                             array_agg(
                               DISTINCT COALESCE(n.label, sn.dst)
                               ORDER BY COALESCE(n.label, sn.dst)
                             ) AS shared_nodes
                       FROM edge e
                       JOIN self_nodes sn ON e.dst = sn.dst AND e.kind = sn.kind
                       LEFT JOIN node n ON n.id = e.dst
                       JOIN document d ON ('doc:' || d.source_path) = e.src
                       JOIN source_doc sd ON COALESCE(NULLIF(d.origin, ''), $9) = sd.origin
                       WHERE e.src <> $1 AND e.src LIKE 'doc:%'
                         AND NOT (COALESCE(NULLIF(d.origin, ''), $9) = ANY($4))
                         AND ($5::text IS NULL OR d.project = $5)
                         AND d.source_path !~ $6
                         AND NOT ($7 = ANY(d.tags))
                       GROUP BY e.src
                       HAVING count(DISTINCT COALESCE(n.label, sn.dst)) >= 2
                       ORDER BY shared DESC, e.src ASC LIMIT $2
                 )
                 SELECT d.source_path, d.project, d.tags,
                        string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content,
                        r.shared,
                        r.shared_nodes
                 FROM ranked r
                 JOIN document d ON ('doc:' || d.source_path) = r.doc_node
                 JOIN chunk c ON c.source_path = d.source_path
                 GROUP BY d.source_path, d.project, d.tags, r.shared, r.shared_nodes
                 ORDER BY r.shared DESC, d.source_path ASC;",
                &[
                    &doc_id,
                    &limit,
                    &edge_kinds,
                    &exclude_origins,
                    &project,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &source_path,
                    &default_origin,
                ],
            )
            .await
            .context("related doc content")?;
        Ok(rows
            .iter()
            .map(|r| RelatedDoc {
                doc: RecentDoc {
                    source_path: r.get(0),
                    project: r.get(1),
                    tags: r.get(2),
                    content: r.get(3),
                },
                evidence: RelatedEvidence {
                    kind: RelatedEvidenceKind::Graph,
                    shared_count: r.get(4),
                    shared_nodes: r.get(5),
                },
            })
            .collect())
    }

    /// The most recent other documents in the same project — fallback links for isolated documents (0 concept overlap).
    /// Supplements only when there are no concept-based links to prevent orphans, but only a few so it doesn't become a mesh.
    /// Projection candidates stay inside the source document's origin boundary.
    pub async fn recent_project_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let default_origin = default_origin_key();
        let rows = self
            .db()
            .await?
            .query(
                "SELECT d2.source_path FROM document d1
                 JOIN document d2 ON d2.project = d1.project
                     AND COALESCE(NULLIF(d2.origin, ''), $5) = COALESCE(NULLIF(d1.origin, ''), $5)
                     AND d2.source_path <> d1.source_path
                  WHERE d1.source_path = $1 AND d1.project <> ''
                   AND d2.source_path !~ $3
                   AND NOT ($4 = ANY(d2.tags))
                 ORDER BY d2.updated_at DESC
                 LIMIT $2;",
                &[
                    &source_path,
                    &limit,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await
            .context("recent project docs")?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Temporal fact claim upsert + supersede. For the same canonical `(subject,predicate)`, old values are
    /// sealed via `superseded_at`, and only the latest `valid_from` row is current (NULL). Idempotent (re-ingesting the same row is harmless).
    /// 0 extra gemma calls — takes the claims that extract already produced and canonicalizes the identity axis at the store boundary.
    #[allow(clippy::too_many_arguments)]
    pub async fn upsert_claim(
        &self,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        valid_from: SystemTime,
        embedding: &[f32],
        kind: &str,
        confidence: &str,
    ) -> Result<()> {
        let vec = self.checked_vector(embedding)?; // dim guard (shared with upsert_chunk)
        let (subject_key, predicate_key) = canonical_claim_axis(subject, predicate);
        self.db().await?
            .execute(
                "INSERT INTO claim (subject, predicate, value, source_path, valid_from, embedding, kind, confidence)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (subject, predicate, valid_from) DO UPDATE SET
                     value = EXCLUDED.value, source_path = EXCLUDED.source_path,
                     embedding = EXCLUDED.embedding, kind = EXCLUDED.kind, confidence = EXCLUDED.confidence;",
                &[
                    &subject_key,
                    &predicate_key,
                    &value,
                    &source_path,
                    &valid_from,
                    &vec,
                    &kind,
                    &confidence,
                ],
            )
            .await
            .context("insert claim")?;
        // seal everything below the latest valid_from, leaving only the single latest row as current.
        self.db()
            .await?
            .execute(
                "UPDATE claim c SET superseded_at = m.mx
                 FROM (SELECT subject, predicate, max(valid_from) AS mx FROM claim
                       WHERE subject = $1 AND predicate = $2 GROUP BY subject, predicate) m
                 WHERE c.subject = m.subject AND c.predicate = m.predicate
                   AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx;",
                &[&subject_key, &predicate_key],
            )
            .await
            .context("seal old claims")?;
        self.db()
            .await?
            .execute(
                "UPDATE claim SET superseded_at = NULL
                 WHERE subject = $1 AND predicate = $2 AND superseded_at IS NOT NULL
                   AND valid_from = (SELECT max(valid_from) FROM claim
                                     WHERE subject = $1 AND predicate = $2);",
                &[&subject_key, &predicate_key],
            )
            .await
            .context("unseal latest claim")?;
        Ok(())
    }

    /// Upsert graph nodes/edges for a claim: `doc —claims→ claim:{subject}:{predicate}` and,
    /// for non-fact claims, a typed node (`decision:|risk:...`) plus an `is_a` edge.
    /// Also links the claim node to `project:{project}` when a project is present.
    pub async fn upsert_claim_node(
        &self,
        path: &str,
        project: &str,
        claim: &crate::frontmatter::Claim,
    ) -> Result<()> {
        let claim_id = claim_node_id(claim);
        let label = format!("{}: {}", claim.predicate, claim.value);
        let kind = claim.kind();
        let confidence = claim.confidence();

        // claim node
        self.upsert_node(&claim_id, "claim", &label, Some(kind))
            .await?;

        // typed node for decisions/risks/etc.
        if kind != "fact" {
            let typed_id = typed_claim_node_id(kind, claim);
            let typed_label = format!("{} — {}", claim.subject, claim.value);
            self.upsert_node(&typed_id, kind, &typed_label, Some(confidence))
                .await?;
            self.upsert_edge(&claim_id, &typed_id, "is_a").await?;
        }

        // doc -> claim edge
        let doc_id = doc_node_id(path);
        self.upsert_edge(&doc_id, &claim_id, "claims").await?;

        // claim -> project edge
        if !project.is_empty() {
            let project_id = format!("project:{project}");
            self.upsert_edge(&claim_id, &project_id, "about").await?;
        }

        Ok(())
    }

    async fn refresh_claim_axis_projection(
        &self,
        subject_key: &str,
        predicate_key: &str,
    ) -> Result<()> {
        let claim_id = format!("claim:{subject_key}:{predicate_key}");
        let typed_ids = typed_claim_axis_node_ids(subject_key, predicate_key);
        let row = self
            .db()
            .await?
            .query_opt(
                "SELECT c.value, c.kind, c.confidence, COALESCE(d.project, '') AS project
                 FROM claim c
                 LEFT JOIN document d ON d.source_path = c.source_path
                 WHERE c.subject = $1 AND c.predicate = $2 AND c.superseded_at IS NULL
                 ORDER BY c.valid_from DESC
                 LIMIT 1;",
                &[&subject_key, &predicate_key],
            )
            .await
            .context("read current claim for graph projection")?;
        let Some(row) = row else {
            let mut node_ids = typed_ids;
            node_ids.push(claim_id);
            self.db()
                .await?
                .execute(
                    "DELETE FROM edge WHERE src = ANY($1::text[]) OR dst = ANY($1::text[]);",
                    &[&node_ids],
                )
                .await
                .context("delete empty claim-axis graph edges")?;
            self.db()
                .await?
                .execute("DELETE FROM node WHERE id = ANY($1::text[]);", &[&node_ids])
                .await
                .context("delete empty claim-axis graph nodes")?;
            return Ok(());
        };

        let value: String = row.get(0);
        let kind: String = row.get(1);
        let confidence: String = row.get(2);
        let project: String = row.get(3);
        self.db()
            .await?
            .execute(
                "DELETE FROM edge WHERE src = $1 AND kind IN ('is_a', 'about');",
                &[&claim_id],
            )
            .await
            .context("clear stale claim-axis outgoing graph edges")?;
        self.db()
            .await?
            .execute(
                "DELETE FROM edge WHERE src = ANY($1::text[]) OR dst = ANY($1::text[]);",
                &[&typed_ids],
            )
            .await
            .context("clear stale typed claim graph edges")?;
        self.db()
            .await?
            .execute(
                "DELETE FROM node WHERE id = ANY($1::text[]);",
                &[&typed_ids],
            )
            .await
            .context("clear stale typed claim graph nodes")?;

        let label = format!("{predicate_key}: {value}");
        self.upsert_node(&claim_id, "claim", &label, Some(&kind))
            .await?;
        if kind != "fact" {
            let typed_id = format!("{kind}:{subject_key}:{predicate_key}");
            let typed_label = format!("{subject_key} — {value}");
            self.upsert_node(&typed_id, &kind, &typed_label, Some(&confidence))
                .await?;
            self.upsert_edge(&claim_id, &typed_id, "is_a").await?;
        }
        if !project.is_empty() {
            let project_id = format!("project:{project}");
            self.upsert_node(&project_id, "project", &project, None)
                .await?;
            self.upsert_edge(&claim_id, &project_id, "about").await?;
        }
        Ok(())
    }

    /// Top-k **current** claims (superseded_at IS NULL) by recency (valid_from desc). For injecting authority into the briefing.
    pub async fn recent_claims(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
    ) -> Result<Vec<Claim>> {
        Ok(self
            .recent_claim_records(k, project, kinds, exclude_origins)
            .await?
            .into_iter()
            .map(|record| record.claim)
            .collect())
    }

    /// Top-k **current** claims with source provenance preserved.
    pub async fn recent_claim_records(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
    ) -> Result<Vec<ClaimRecord>> {
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.source_path FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                  WHERE c.superseded_at IS NULL
                    AND ($2::text IS NULL OR d.project = $2)
                    AND ($3::text[] IS NULL OR c.kind = ANY($3))
                    AND NOT (COALESCE(NULLIF(d.origin, ''), $7) = ANY($4))
                    AND d.source_path !~ $5
                    AND NOT ($6 = ANY(d.tags))
                  ORDER BY c.valid_from DESC
                 LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await
            .context("recent claims")?;
        Ok(rows.iter().map(row_to_claim_record).collect())
    }

    /// Stalled claims: current claims whose valid_from is older than `older_than_days`.
    /// Ordered oldest-first so the longest-frozen items surface first.
    pub async fn stalled_claims(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
        older_than_days: i64,
    ) -> Result<Vec<Claim>> {
        Ok(self
            .stalled_claim_records(k, project, kinds, exclude_origins, older_than_days)
            .await?
            .into_iter()
            .map(|record| record.claim)
            .collect())
    }

    /// Stalled claims with source provenance preserved.
    pub async fn stalled_claim_records(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
        older_than_days: i64,
    ) -> Result<Vec<ClaimRecord>> {
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.source_path FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL
                    AND c.valid_from < (NOW() - INTERVAL '1 day' * ($5::bigint))
                    AND ($2::text IS NULL OR d.project = $2)
                    AND ($3::text[] IS NULL OR c.kind = ANY($3))
                    AND NOT (COALESCE(NULLIF(d.origin, ''), $8) = ANY($4))
                    AND d.source_path !~ $6
                    AND NOT ($7 = ANY(d.tags))
                  ORDER BY c.valid_from ASC
                 LIMIT $1;",
                &[
                    &k,
                    &project,
                    &kinds,
                    &exclude_origins,
                    &older_than_days,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await
            .context("stalled claims")?;
        Ok(rows.iter().map(row_to_claim_record).collect())
    }

    /// Query embedding → top-k **current** claims (superseded_at IS NULL). Authority retrieval.
    pub async fn current_claims(
        &self,
        query_emb: &[f32],
        k: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        kinds: Option<&[String]>,
    ) -> Result<Vec<Claim>> {
        Ok(self
            .current_claim_records(query_emb, k, exclude_origins, project, kinds)
            .await?
            .into_iter()
            .map(|record| record.claim)
            .collect())
    }

    /// Query embedding → top-k **current** claims with source provenance preserved.
    pub async fn current_claim_records(
        &self,
        query_emb: &[f32],
        k: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        kinds: Option<&[String]>,
    ) -> Result<Vec<ClaimRecord>> {
        let vec = Vector::from(query_emb.to_vec());
        // Honor the SAME origin boundary the recall path applies (retrieve::merge_hits filters by
        // exclude_origins). Claims carry no origin column, but their parent document does — JOIN and
        // filter on it so an injected/cross-origin claim cannot bypass an exclusion that the recalled
        // chunks in the same answer respect (Layer 1: one answer, one consistent origin boundary).
        // Empty legacy origins are normalized to the same default-personal key the wiki recall path uses.
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "SELECT c.subject, c.predicate, c.value, c.kind, c.confidence, c.source_path FROM claim c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.superseded_at IS NULL AND c.embedding IS NOT NULL
                   AND NOT (COALESCE(NULLIF(d.origin, ''), $8) = ANY($3))
                    AND ($4::text IS NULL OR d.project = $4)
                    AND ($5::text[] IS NULL OR c.kind = ANY($5))
                    AND d.source_path !~ $6
                   AND NOT ($7 = ANY(d.tags))
                 ORDER BY c.embedding <=> $1
                 LIMIT $2;",
                &[
                    &vec,
                    &k,
                    &exclude_origins,
                    &project,
                    &kinds,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await
            .context("current claims")?;
        Ok(rows.iter().map(row_to_claim_record).collect())
    }

    /// document upsert + project/topic nodes + in_project/tagged edge regeneration (idempotent).
    /// `updated_at` = source file mtime (the true recency signal) — the sort key for recency-first retrieval.
    pub async fn upsert_document(
        &self,
        front: &FrontMatter,
        sha: &str,
        updated_at: SystemTime,
    ) -> Result<()> {
        let path = &front.source_path;
        let title_ref: Option<&str> = front.title.as_deref();
        self.db().await?
            .execute(
                "INSERT INTO document (source_path, origin, project, kind, title, tags, sha, updated_at)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (source_path) DO UPDATE SET
                     origin = EXCLUDED.origin, project = EXCLUDED.project, kind = EXCLUDED.kind,
                     title = EXCLUDED.title, tags = EXCLUDED.tags, sha = EXCLUDED.sha,
                     updated_at = EXCLUDED.updated_at;",
                &[
                    path,
                    &front.origin,
                    &front.project,
                    &front.kind,
                    &title_ref,
                    &front.tags,
                    &sha,
                    &updated_at,
                ],
            )
            .await
            .context("upsert document")?;

        let doc_id = doc_node_id(path);

        // project node + in_project edge
        if !front.project.is_empty() {
            let pid = format!("project:{}", front.project);
            self.upsert_node(&pid, "project", &front.project, None)
                .await?;
            self.upsert_edge(&doc_id, &pid, "in_project").await?;
        }

        // tagged: remove existing then regenerate (idempotent)
        self.db()
            .await?
            .execute(
                "DELETE FROM edge WHERE src = $1 AND kind = 'tagged';",
                &[&doc_id],
            )
            .await?;
        for tag in &front.tags {
            let tid = format!("topic:{tag}");
            self.upsert_node(&tid, "topic", tag, None).await?;
            self.upsert_edge(&doc_id, &tid, "tagged").await?;
        }
        Ok(())
    }

    /// Prune chunks at or beyond `from_idx` for a document — the stale tail left when a re-ingested
    /// note has FEWER chunks than before. Used by the upsert-then-prune re-ingest so a reader never
    /// sees an empty/half-deleted chunk set (no delete-first window). `from_idx == new chunk count`.
    pub async fn prune_chunks_from(&self, path: &str, from_idx: usize) -> Result<()> {
        let from = store_usize_to_i32(from_idx, "chunk prune start index")?;
        self.db()
            .await?
            .execute(
                "DELETE FROM chunk WHERE source_path = $1 AND chunk_idx >= $2;",
                &[&path, &from],
            )
            .await?;
        Ok(())
    }

    /// Remove (prune) document + chunks (CASCADE) + graph edges + claims (explicit; claim has no FK).
    pub async fn delete_document(&self, path: &str) -> Result<()> {
        self.db()
            .await?
            .execute("DELETE FROM document WHERE source_path = $1;", &[&path])
            .await?;
        let doc_id = doc_node_id(path);
        self.db()
            .await?
            .execute("DELETE FROM edge WHERE src = $1 OR dst = $1;", &[&doc_id])
            .await?;
        // claim has NO FK to document (unlike chunk's ON DELETE CASCADE) so the document delete does
        // not cascade here. Capture affected axes first, then re-seal their remaining history after
        // deleting this source path so exactly one latest row stays current.
        let affected_axes: Vec<(String, String)> = self
            .db()
            .await?
            .query(
                "SELECT DISTINCT subject, predicate FROM claim WHERE source_path = $1;",
                &[&path],
            )
            .await?
            .into_iter()
            .map(|r| (r.get::<_, String>(0), r.get::<_, String>(1)))
            .collect();
        self.db()
            .await?
            .execute("DELETE FROM claim WHERE source_path = $1;", &[&path])
            .await?;
        for (subject_key, predicate_key) in affected_axes {
            self.db()
                .await?
                .execute(
                    "UPDATE claim c SET superseded_at = m.mx
                     FROM (SELECT subject, predicate, max(valid_from) AS mx FROM claim
                           WHERE subject = $1 AND predicate = $2 GROUP BY subject, predicate) m
                     WHERE c.subject = m.subject AND c.predicate = m.predicate
                       AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx;",
                    &[&subject_key, &predicate_key],
                )
                .await
                .context("re-seal remaining claims after document delete")?;
            self.db()
                .await?
                .execute(
                    "UPDATE claim SET superseded_at = NULL
                     WHERE subject = $1 AND predicate = $2 AND superseded_at IS NOT NULL
                       AND valid_from = (SELECT max(valid_from) FROM claim
                                         WHERE subject = $1 AND predicate = $2);",
                    &[&subject_key, &predicate_key],
                )
                .await
                .context("unseal latest remaining claim after document delete")?;
            self.refresh_claim_axis_projection(&subject_key, &predicate_key)
                .await
                .context("refresh claim graph projection after document delete")?;
        }
        Ok(())
    }

    // ── chunk (embedding) ─────────────────────────────────────────────────────

    pub async fn upsert_chunk(&self, d: &Doc) -> Result<()> {
        let vec = self.checked_vector(&d.embedding)?; // dim guard (shared with upsert_claim)
        let idx = store_usize_to_i32(d.chunk_idx, "chunk index")?;
        self.db().await?
            .execute(
                "INSERT INTO chunk (id, source_path, content, embedding, origin, project, kind, chunk_idx)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (id) DO UPDATE SET
                     content = EXCLUDED.content, embedding = EXCLUDED.embedding, origin = EXCLUDED.origin,
                     project = EXCLUDED.project, kind = EXCLUDED.kind, chunk_idx = EXCLUDED.chunk_idx;",
                &[
                    &d.id, &d.front.source_path, &d.content, &vec,
                    &d.front.origin, &d.front.project, &d.front.kind, &idx,
                ],
            )
            .await
            .context("upsert chunk")?;
        Ok(())
    }

    // ── retrieval ─────────────────────────────────────────────────────────────

    pub async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>> {
        let qvec = Vector::from(vec.to_vec());
        let k_i64 = store_usize_to_i64(k, "vector search limit")?;
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "SELECT c.id, c.content, COALESCE(NULLIF(c.origin, ''), $5), c.project, c.source_path,
                        (c.embedding <=> $1)::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE NOT ($3 = ANY(d.tags))
                   AND d.source_path !~ $4
                 ORDER BY c.embedding <=> $1
                 LIMIT $2;",
                &[
                    &qvec,
                    &k_i64,
                    &GENERATED_BRIEF_TAG,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &default_origin,
                ],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    pub async fn text_search(&self, query: &str, k: usize) -> Result<Vec<Hit>> {
        let k_i64 = store_usize_to_i64(k, "text search limit")?;
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "SELECT c.id, c.content, COALESCE(NULLIF(c.origin, ''), $5), c.project, c.source_path,
                        ts_rank(c.tsv, plainto_tsquery('simple', $1))::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.tsv @@ plainto_tsquery('simple', $1)
                   AND NOT ($3 = ANY(d.tags))
                   AND d.source_path !~ $4
                 ORDER BY dist DESC
                 LIMIT $2;",
                &[
                    &query,
                    &k_i64,
                    &GENERATED_BRIEF_TAG,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &default_origin,
                ],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    /// Vector search with optional project and recency filters.
    /// `since_hours` restricts to chunks whose parent document was updated within the window.
    /// Filtered `/search` retrieval intentionally leaves eval fixtures searchable:
    /// `make eval` copies `eval-*.md` into `vault/wiki` and calls `/search`.
    /// Source-memory and briefing surfaces filter fixtures at their own boundaries.
    pub async fn vector_search_filtered(
        &self,
        vec: &[f32],
        k: usize,
        exclude_origins: &[String],
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let qvec = Vector::from(vec.to_vec());
        let k_i64 = store_usize_to_i64(k, "filtered vector search limit")?;
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "SELECT c.id, c.content, COALESCE(NULLIF(c.origin, ''), $7), c.project, c.source_path,
                        (c.embedding <=> $1)::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE NOT (COALESCE(NULLIF(d.origin, ''), $7) = ANY($3))
                   AND ($4::text IS NULL OR c.project = $4)
                   AND ($5::int IS NULL OR d.updated_at >= now() - make_interval(hours => $5))
                   AND NOT ($6 = ANY(d.tags))
                  ORDER BY c.embedding <=> $1
                  LIMIT $2;",
                &[
                    &qvec,
                    &k_i64,
                    &exclude_origins,
                    &project,
                    &since_hours,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    /// Find the single nearest document by mean chunk distance. Used at the remember write gate
    /// as a candidate finder; callers must corroborate against the wiki SSOT before skipping.
    /// Returns `(source_path, distance)` if within `max_dist`.
    pub async fn nearest_document(
        &self,
        vec: &[f32],
        max_dist: f64,
    ) -> Result<Option<(String, f64)>> {
        Ok(self.nearest_documents(vec, max_dist, 1).await?.pop())
    }

    /// Find nearest documents by mean chunk distance. Used when vector similarity is a
    /// candidate finder and a caller needs to inspect more than the closest embedding hit.
    pub async fn nearest_documents(
        &self,
        vec: &[f32],
        max_dist: f64,
        limit: i64,
    ) -> Result<Vec<(String, f64)>> {
        let qvec = Vector::from(vec.to_vec());
        let rows = self
            .db()
            .await?
            .query(
                "SELECT d.source_path, MIN(c.embedding <=> $1)::float8 AS dist
                   FROM document d
                   JOIN chunk c ON c.source_path = d.source_path
                     WHERE c.embedding IS NOT NULL
                       AND NOT ($4 = ANY(d.tags))
                       AND d.source_path !~ $5
                     GROUP BY d.source_path
                     HAVING MIN(c.embedding <=> $1) <= $2
                     ORDER BY dist ASC, d.source_path ASC
                     LIMIT $3;",
                &[
                    &qvec,
                    &max_dist,
                    &limit,
                    &GENERATED_BRIEF_TAG,
                    &INTERNAL_EVAL_FIXTURE_RE,
                ],
            )
            .await
            .context("nearest documents")?;
        Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
    }

    /// Find nearest documents inside the duplicate gate's origin/project boundary.
    /// This keeps incompatible candidates from consuming the SQL `LIMIT` before
    /// the caller corroborates the match against the wiki SSOT.
    pub async fn nearest_documents_for_duplicate_boundary(
        &self,
        vec: &[f32],
        max_dist: f64,
        limit: i64,
        origin: &str,
        project: Option<&str>,
    ) -> Result<Vec<(String, f64)>> {
        let qvec = Vector::from(vec.to_vec());
        let default_origin = default_origin_key();
        let rows = self
            .db()
            .await?
            .query(
                "SELECT d.source_path, MIN(c.embedding <=> $1)::float8 AS dist
                   FROM document d
                   JOIN chunk c ON c.source_path = d.source_path
                   WHERE c.embedding IS NOT NULL
                      AND COALESCE(NULLIF(d.origin, ''), $8) = $4
                      AND ($5::text IS NULL OR d.project = $5 OR d.project = '')
                      AND d.source_path !~ $6
                      AND NOT ($7 = ANY(d.tags))
                   GROUP BY d.source_path
                   HAVING MIN(c.embedding <=> $1) <= $2
                   ORDER BY dist ASC, d.source_path ASC
                   LIMIT $3;",
                &[
                    &qvec,
                    &max_dist,
                    &limit,
                    &origin,
                    &project,
                    &INTERNAL_EVAL_FIXTURE_RE,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await
            .context("nearest documents for duplicate boundary")?;
        Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
    }

    /// Full-text search with optional project and recency filters.
    /// Keeps the same eval-gate exception as `vector_search_filtered`.
    pub async fn text_search_filtered(
        &self,
        query: &str,
        k: usize,
        exclude_origins: &[String],
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        let k_i64 = store_usize_to_i64(k, "filtered text search limit")?;
        let default_origin = default_origin_key();
        let rows = self.db().await?
            .query(
                "SELECT c.id, c.content, COALESCE(NULLIF(c.origin, ''), $7), c.project, c.source_path,
                        ts_rank(c.tsv, plainto_tsquery('simple', $1))::float4 AS dist
                 FROM chunk c
                 JOIN document d ON d.source_path = c.source_path
                 WHERE c.tsv @@ plainto_tsquery('simple', $1)
                   AND NOT (COALESCE(NULLIF(d.origin, ''), $7) = ANY($3))
                   AND ($4::text IS NULL OR c.project = $4)
                   AND ($5::int IS NULL OR d.updated_at >= now() - make_interval(hours => $5))
                   AND NOT ($6 = ANY(d.tags))
                 ORDER BY dist DESC
                 LIMIT $2;",
                &[
                    &query,
                    &k_i64,
                    &exclude_origins,
                    &project,
                    &since_hours,
                    &GENERATED_BRIEF_TAG,
                    &default_origin,
                ],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    pub async fn count(&self) -> Result<usize> {
        pg_count(&*self.db().await?, "SELECT count(*) FROM chunk;").await
    }

    pub async fn all_meta(&self) -> Result<Vec<Meta>> {
        let rows = self
            .db()
            .await?
            .query("SELECT origin, project, kind, source_path FROM chunk;", &[])
            .await?;
        Ok(rows
            .into_iter()
            .map(|r| Meta {
                origin: r.get(0),
                project: r.get(1),
                kind: r.get(2),
                source_path: r.get(3),
            })
            .collect())
    }

    // ── query log (memory usage analytics) ────────────────────────────────────

    #[allow(clippy::needless_borrow)] // tokio-postgres params need &&str to coerce to &dyn ToSql.
    #[allow(clippy::too_many_arguments)] // TODO: bundle into a LogQueryInput struct when the surface stabilizes.
    pub async fn log_query(
        &self,
        endpoint: &str,
        query: &str,
        hit_paths: &[String],
        sources: &[String],
        answer_snippet: &str,
        latency_ms: Option<i32>,
        meta: &Value,
    ) -> Result<()> {
        // Scrub secrets a user may have pasted into a question/answer BEFORE they persist — the same
        // leak-boundary the remember path applies. query_log is exported by backup-db and served by
        // /query-log, so storing raw Q&A would leak tokens outside the redaction guarantee.
        let re = crate::redact::build_secret_re().context("build query_log secret scrub regex")?;
        let query = crate::redact::redact(re, query);
        let answer_snippet = crate::redact::redact(re, answer_snippet);
        self.db().await?
            .execute(
                "INSERT INTO query_log (endpoint, query, hit_paths, sources, answer_snippet, latency_ms, meta)
                 VALUES ($1, $2, $3, $4, $5, $6, $7);",
                &[
                    &endpoint,
                    &query,
                    &hit_paths,
                    &sources,
                    &answer_snippet,
                    &latency_ms,
                    &PgJson(meta),
                ],
            )
            .await
            .context("log query")?;
        Ok(())
    }

    pub async fn recent_queries(&self, limit: i64) -> Result<Vec<QueryLogRow>> {
        let rows = self.db().await?
            .query(
                "SELECT id, created_at, endpoint, query, hit_paths, sources, answer_snippet, latency_ms, meta
                 FROM query_log ORDER BY created_at DESC LIMIT $1;",
                &[&limit],
            )
            .await
            .context("recent queries")?;
        Ok(rows
            .into_iter()
            .map(|r| QueryLogRow {
                id: r.get(0),
                created_at: r.get(1),
                endpoint: r.get(2),
                query: r.get(3),
                hit_paths: r.get(4),
                sources: r.get(5),
                answer_snippet: r.get(6),
                latency_ms: r.get(7),
                meta: r.get(8),
            })
            .collect())
    }

    /// Queries asked at least `min_count` times within the last `days` days,
    /// most frequent first. Raw aggregate — the caller filters the lane
    /// (e.g. code queries via `retrieve::is_code_query`).
    pub async fn repeated_queries(
        &self,
        days: i64,
        min_count: i64,
        limit: i64,
    ) -> Result<Vec<QueryHotspot>> {
        let days_i32 = i32::try_from(days).context("repeated queries days overflow")?;
        let rows = self
            .db()
            .await?
            .query(
                "SELECT lower(btrim(query)) AS q, count(*) AS n, max(created_at) AS last_at
                 FROM query_log
                 WHERE created_at > now() - make_interval(days => $1)
                   AND length(btrim(query)) > 0
                 GROUP BY q
                 HAVING count(*) >= $2
                 ORDER BY n DESC, last_at DESC
                 LIMIT $3;",
                &[&days_i32, &min_count, &limit],
            )
            .await
            .context("repeated queries")?;
        Ok(rows
            .into_iter()
            .map(|r| QueryHotspot {
                query: r.get(0),
                count: r.get(1),
                last_at: r.get(2),
            })
            .collect())
    }

    /// Store adapter/workflow events in the local DB using an OpenTelemetry-shaped log row.
    ///
    /// The local event DB is the queryable primary sink for ops dashboards, HTTP, and MCP.
    /// Host adapters may keep an NDJSON fallback spool when the engine is unavailable.
    pub async fn log_event(&self, event: &Value) -> Result<()> {
        let event = redact_json_value(event);
        let otel = event.get("otel").and_then(Value::as_object);
        let component = text_field(&event, "component").unwrap_or_default();
        let event_name = otel
            .and_then(|o| o.get("event_name"))
            .and_then(Value::as_str)
            .map(str::to_owned)
            .or_else(|| text_field(&event, "event"))
            .unwrap_or_default();
        let status = text_field(&event, "status").unwrap_or_default();
        let severity_text = otel
            .and_then(|o| o.get("severity_text"))
            .and_then(Value::as_str)
            .map_or_else(|| severity_text_for_status(&status), str::to_owned);
        let severity_number = otel
            .and_then(|o| o.get("severity_number"))
            .and_then(Value::as_i64)
            .and_then(|n| i32::try_from(n).ok())
            .unwrap_or_else(|| severity_number_for_text(&severity_text));
        let time_unix_nano = otel
            .and_then(|o| o.get("time_unix_nano"))
            .and_then(Value::as_i64);
        let trace_id = otel
            .and_then(|o| o.get("trace_id"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let span_id = otel
            .and_then(|o| o.get("span_id"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let body = otel
            .and_then(|o| o.get("body"))
            .cloned()
            .unwrap_or_else(|| json!({"event.name": event_name, "status": status}));
        let attributes = otel
            .and_then(|o| o.get("attributes"))
            .cloned()
            .unwrap_or_else(|| event.clone());
        let resource = otel
            .and_then(|o| o.get("resource"))
            .cloned()
            .unwrap_or_else(|| {
                json!({"attributes": {
                    "service.name": component,
                    "service.namespace": "oh-my-boring"
                }})
            });
        let service_name = resource
            .get("attributes")
            .and_then(|a| a.get("service.name"))
            .and_then(Value::as_str)
            .map_or_else(|| component.clone(), str::to_owned);

        let run_id = text_field(&event, "run_id");
        let session_id = text_field(&event, "session_id");
        let workflow = text_field(&event, "workflow");
        let workflow_node = text_field(&event, "workflow_node");
        let workflow_outcome = text_field(&event, "workflow_outcome");

        self.db().await?
            .execute(
                "INSERT INTO event_log (
                     time_unix_nano, severity_text, severity_number, service_name,
                     component, event_name, status, trace_id, span_id, run_id, session_id,
                     workflow, workflow_node, workflow_outcome, body, attributes, resource
                 )
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17);",
                &[
                    &time_unix_nano,
                    &severity_text,
                    &severity_number,
                    &service_name,
                    &component,
                    &event_name,
                    &status,
                    &trace_id,
                    &span_id,
                    &run_id,
                    &session_id,
                    &workflow,
                    &workflow_node,
                    &workflow_outcome,
                    &PgJson(&body),
                    &PgJson(&attributes),
                    &PgJson(&resource),
                ],
            )
            .await
            .context("log event")?;
        Ok(())
    }

    pub async fn recent_events(&self, filter: EventLogFilter<'_>) -> Result<Vec<EventLogRow>> {
        let rows = self
            .db()
            .await?
            .query(
                "SELECT id, observed_at, time_unix_nano, severity_text, severity_number,
                        service_name, component, event_name, status, trace_id, span_id,
                        run_id, session_id, workflow, workflow_node, workflow_outcome,
                        body, attributes, resource
                 FROM event_log
                 WHERE ($2::text IS NULL OR component = $2)
                   AND ($3::text IS NULL OR event_name = $3)
                   AND ($4::text IS NULL OR status = $4)
                   AND ($5::text IS NULL OR run_id = $5)
                   AND ($6::text IS NULL OR workflow = $6)
                   AND ($7::int IS NULL OR observed_at >= now() - make_interval(hours => $7))
                 ORDER BY observed_at DESC, id DESC
                 LIMIT $1;",
                &[
                    &filter.limit,
                    &filter.component,
                    &filter.event_name,
                    &filter.status,
                    &filter.run_id,
                    &filter.workflow,
                    &filter.since_hours,
                ],
            )
            .await
            .context("recent events")?;
        Ok(rows
            .into_iter()
            .map(|r| EventLogRow {
                id: r.get(0),
                observed_at: r.get(1),
                time_unix_nano: r.get(2),
                severity_text: r.get(3),
                severity_number: r.get(4),
                service_name: r.get(5),
                component: r.get(6),
                event_name: r.get(7),
                status: r.get(8),
                trace_id: r.get(9),
                span_id: r.get(10),
                run_id: r.get(11),
                session_id: r.get(12),
                workflow: r.get(13),
                workflow_node: r.get(14),
                workflow_outcome: r.get(15),
                body: r.get::<_, PgJson<Value>>(16).0,
                attributes: r.get::<_, PgJson<Value>>(17).0,
                resource: r.get::<_, PgJson<Value>>(18).0,
            })
            .collect())
    }

    /// Maintenance compact: VACUUM ANALYZE + REINDEX TABLE CONCURRENTLY + old query_log
    /// pruning + orphan semantic-node GC. Returns a report of what happened.
    ///
    /// VACUUM and REINDEX CONCURRENTLY cannot run inside a transaction block. We send each
    /// statement through its own `batch_execute` call so the simple-query protocol keeps them
    /// in autocommit mode rather than wrapping multiple statements in an implicit transaction.
    pub async fn compact(&self) -> Result<CompactSummary> {
        let mut report = CompactReport::default();
        let started = std::time::Instant::now();

        let t0 = std::time::Instant::now();
        for table in [
            "document",
            "chunk",
            "node",
            "edge",
            "claim",
            "query_log",
            "event_log",
        ] {
            self.db()
                .await?
                .batch_execute(&format!("VACUUM ANALYZE {table};"))
                .await
                .with_context(|| format!("vacuum analyze {table}"))?;
        }
        report.vacuum_ms = t0.elapsed().as_millis();

        let t0 = std::time::Instant::now();
        for table in [
            "document",
            "chunk",
            "node",
            "edge",
            "claim",
            "query_log",
            "event_log",
        ] {
            self.db()
                .await?
                .batch_execute(&format!("REINDEX TABLE CONCURRENTLY {table};"))
                .await
                .with_context(|| format!("reindex table {table}"))?;
        }
        report.reindex_ms = t0.elapsed().as_millis();

        let pruned = self
            .db()
            .await?
            .execute(
                "DELETE FROM query_log WHERE created_at < now() - interval '90 days';",
                &[],
            )
            .await
            .context("prune query_log")?;
        report.prune_query_log = db_u64_rows_to_usize(pruned, "query_log prune")?;

        let gc = self.gc_orphans().await.context("gc orphans")?;
        report.gc_tool = gc.tool;
        report.gc_concept = gc.concept;

        Ok(CompactSummary {
            report,
            total_ms: started.elapsed().as_millis(),
        })
    }

    // ── graph helpers (node/edge upsert) ───────────────────────────────────────

    async fn upsert_node(
        &self,
        id: &str,
        kind: &str,
        label: &str,
        outcome: Option<&str>,
    ) -> Result<()> {
        self.db().await?
            .execute(
                "INSERT INTO node (id, kind, label, outcome) VALUES ($1, $2, $3, $4)
                 ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                &[&id, &kind, &label, &outcome],
            )
            .await
            .context("upsert node")?;
        Ok(())
    }

    async fn upsert_edge(&self, src: &str, dst: &str, kind: &str) -> Result<()> {
        self.db()
            .await?
            .execute(
                "INSERT INTO edge (src, dst, kind) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING;",
                &[&src, &dst, &kind],
            )
            .await
            .context("upsert edge")?;
        Ok(())
    }

    // ── semantic nodes ──────────────────────────────────────────────────────────

    pub async fn upsert_tool(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("tool:{slug}"), "tool", text, None)
            .await
    }
    pub async fn upsert_concept(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("concept:{slug}"), "concept", text, None)
            .await
    }

    /// Remove this document's semantic edges (uses/about) — makes the deterministic graph rebuild idempotent.
    pub async fn clear_semantic_edges(&self, doc_path: &str) -> Result<()> {
        let doc_id = doc_node_id(doc_path);
        let kinds: Vec<&str> = SEMANTIC_EDGE_KINDS.to_vec();
        self.db()
            .await?
            .execute(
                "DELETE FROM edge WHERE src = $1 AND kind = ANY($2);",
                &[&doc_id, &kinds],
            )
            .await?;
        Ok(())
    }

    // ── semantic edges (doc → entity) ───────────────────────────────────────────

    pub async fn relate_doc_tool(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(&doc_node_id(doc_path), &format!("tool:{slug}"), "uses")
            .await
    }
    pub async fn relate_doc_concept(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(&doc_node_id(doc_path), &format!("concept:{slug}"), "about")
            .await
    }

    // ── graph retrieval ───────────────────────────────────────────────────────

    /// Structural neighbors (project/topic) — 1-hop from the chunk's document. Returns labels.
    pub async fn graph_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db()
            .await?
            .query(
                "SELECT n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = $1 AND e.kind IN ('in_project', 'tagged', 'claims');",
                &[&doc_id],
            )
            .await?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Semantic neighbors (tool/concept/claim) — 1-hop from the document. Returns labels.
    pub async fn semantic_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db()
            .await?
            .query(
                "SELECT n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = $1 AND e.kind = ANY($2);",
                &[&doc_id, &SEMANTIC_EDGE_KINDS.to_vec()],
            )
            .await?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    // ── stats / GC ──────────────────────────────────────────────────────────────

    pub async fn graph_stats(&self) -> Result<GraphStats> {
        let db = self.db().await?;
        Ok(GraphStats {
            documents: pg_count(&db, "SELECT count(*) FROM document;").await?,
            chunks: pg_count(&db, "SELECT count(*) FROM chunk;").await?,
            projects: count_node_kind(&db, "project").await?,
            topics: count_node_kind(&db, "topic").await?,
            claims: count_node_kind(&db, "claim").await?,
            decisions: count_node_kind(&db, "decision").await?,
            risks: count_node_kind(&db, "risk").await?,
            edges: pg_count(&db, "SELECT count(*) FROM edge;").await?,
        })
    }

    pub async fn semantic_stats(&self) -> Result<SemanticStats> {
        let db = self.db().await?;
        Ok(SemanticStats {
            tools: count_node_kind(&db, "tool").await?,
            concepts: count_node_kind(&db, "concept").await?,
            uses: count_edge_kind(&db, "uses").await?,
            about: count_edge_kind(&db, "about").await?,
        })
    }

    /// Remove orphan semantic nodes — entity nodes not referenced by any edge.
    pub async fn gc_orphans(&self) -> Result<GcStats> {
        let mut gc = GcStats::default();
        for kind in ["tool", "concept"] {
            let n = self
                .db()
                .await?
                .execute(
                    "DELETE FROM node WHERE kind = $1
                       AND id NOT IN (SELECT src FROM edge UNION SELECT dst FROM edge);",
                    &[&kind],
                )
                .await?;
            let c = db_u64_rows_to_usize(n, "semantic orphan gc")?;
            match kind {
                "tool" => gc.tool = c,
                _ => gc.concept = c,
            }
        }
        Ok(gc)
    }

    // ─────────────────────────────────────────────────────────────
    // Code graph lane (Phase 3) — deterministic AST symbols + relations
    // ─────────────────────────────────────────────────────────────

    /// Upsert one AST-parsed code symbol as a graph node.
    pub async fn upsert_code_symbol(&self, symbol: &crate::codegraph::CodeSymbol) -> Result<()> {
        let id = symbol.node_id();
        self.db().await?
            .execute(
                "INSERT INTO node (id, kind, label, outcome)
                 VALUES ($1, $2, $3, $4)
                 ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                &[
                    &id,
                    &format!("code_{}", symbol.kind.as_str()),
                    &symbol.name,
                    &symbol.signature,
                ],
            )
            .await
            .context("upsert code symbol")?;
        Ok(())
    }

    /// Insert a code symbol node only when missing (`ON CONFLICT DO NOTHING`).
    /// Used by `remember_code`, which knows only path+name+kind and must never clobber
    /// the parsed signature of an already-indexed symbol (the upsert above would).
    pub async fn ensure_code_symbol_stub(
        &self,
        symbol: &crate::codegraph::CodeSymbol,
    ) -> Result<()> {
        let id = symbol.node_id();
        self.db()
            .await?
            .execute(
                "INSERT INTO node (id, kind, label, outcome)
                 VALUES ($1, $2, $3, $4)
                 ON CONFLICT (id) DO NOTHING;",
                &[
                    &id,
                    &format!("code_{}", symbol.kind.as_str()),
                    &symbol.name,
                    &symbol.signature,
                ],
            )
            .await
            .context("ensure code symbol stub")?;
        Ok(())
    }

    /// Distinct source files currently present in the code graph (parsed out of
    /// `code:<kind>:<path>:<name>` node ids). Used by `code-index` to refuse replacing
    /// the graph with a walk that shares no files — the wrong-root footgun.
    pub async fn code_indexed_files(&self) -> Result<std::collections::BTreeSet<String>> {
        let rows = self
            .db()
            .await?
            .query("SELECT id FROM node WHERE id LIKE 'code:%';", &[])
            .await
            .context("code indexed files")?;
        Ok(rows
            .iter()
            .filter_map(|r| {
                let id: String = r.get(0);
                let rest = id.strip_prefix("code:")?;
                let mut parts = rest.splitn(3, ':');
                parts.next()?; // kind
                parts.next().map(str::to_owned) // path (names may contain ':', paths cannot)
            })
            .collect())
    }

    /// Upsert one code relation edge between two symbols.
    pub async fn upsert_code_relation(
        &self,
        relation: &crate::codegraph::CodeRelation,
    ) -> Result<()> {
        let src = relation.from.node_id();
        let dst = relation.to.node_id();
        let kind = relation.kind.as_str();
        self.db()
            .await?
            .execute(
                "INSERT INTO edge (src, dst, kind)
                 VALUES ($1, $2, $3)
                 ON CONFLICT (src, dst, kind) DO NOTHING;",
                &[&src, &dst, &kind],
            )
            .await
            .context("upsert code relation")?;
        Ok(())
    }

    /// Upsert an edge from a wiki document node to a code symbol node.
    /// Used by `remember_code` to link a note to the AST code graph.
    pub async fn upsert_doc_code_relation(
        &self,
        doc_source_path: &str,
        symbol: &crate::codegraph::CodeSymbol,
        kind: crate::codegraph::CodeRelationKind,
    ) -> Result<()> {
        let src = doc_node_id(doc_source_path);
        let dst = symbol.node_id();
        let kind = kind.as_str();
        self.db()
            .await?
            .execute(
                "INSERT INTO edge (src, dst, kind)
                 VALUES ($1, $2, $3)
                 ON CONFLICT (src, dst, kind) DO NOTHING;",
                &[&src, &dst, &kind],
            )
            .await
            .context("upsert doc code relation")?;
        Ok(())
    }

    /// Wipe the whole code graph (`code:*` nodes + code↔code edges) so a re-index pass
    /// replaces it with the current walk result — stale symbols of renamed/deleted files
    /// disappear instead of accumulating. Doc→code `code_uses` edges written by
    /// `remember_code` are deliberately preserved: symbol node ids are deterministic, so
    /// they re-attach after re-upsert; edges to renamed-away symbols dangle inertly
    /// (the edge table has no FK) until `gc_dangling_code_note_edges` reclaims them.
    pub async fn clear_code_graph_preserving_doc_edges(&self) -> Result<()> {
        self.db()
            .await?
            .execute(
                "DELETE FROM edge WHERE src LIKE 'code:%' AND dst LIKE 'code:%';",
                &[],
            )
            .await
            .context("clear code-code edges")?;
        self.db()
            .await?
            .execute("DELETE FROM node WHERE id LIKE 'code:%';", &[])
            .await
            .context("clear code nodes")?;
        Ok(())
    }

    /// Delete doc→code `code_uses` edges whose symbol node no longer exists (the symbol
    /// was renamed or its file removed). Returns the number of reclaimed edges. Runs at
    /// the end of a re-index pass, when the code graph reflects the current tree.
    pub async fn gc_dangling_code_note_edges(&self) -> Result<usize> {
        let n = self
            .db()
            .await?
            .execute(
                "DELETE FROM edge
                 WHERE kind = 'code_uses' AND src LIKE 'doc:%' AND dst LIKE 'code:%'
                   AND dst NOT IN (SELECT id FROM node);",
                &[],
            )
            .await
            .context("gc dangling code note edges")?;
        db_u64_rows_to_usize(n, "code note edge gc")
    }

    /// Wiki notes linked to any of the given code symbol node ids (`code:…`) via
    /// `code_uses` edges. Title comes from the document row, the snippet from chunk #0.
    pub async fn code_notes_for_symbols(
        &self,
        symbol_node_ids: &[String],
    ) -> Result<Vec<CodeNoteLink>> {
        if symbol_node_ids.is_empty() {
            return Ok(Vec::new());
        }
        let rows = self.db().await?
            .query(
                "SELECT e.dst, d.source_path, COALESCE(d.title, ''), COALESCE(left(c.content, 200), '')
                 FROM edge e
                 JOIN document d ON d.source_path = substring(e.src from 5)
                 LEFT JOIN chunk c ON c.source_path = d.source_path AND c.chunk_idx = 0
                 WHERE e.dst = ANY($1) AND e.kind = 'code_uses' AND e.src LIKE 'doc:%'
                 ORDER BY d.source_path, e.dst;",
                &[&symbol_node_ids],
            )
            .await
            .context("code notes for symbols")?;
        Ok(rows
            .iter()
            .map(|r| CodeNoteLink {
                symbol_node_id: r.get(0),
                source_path: r.get(1),
                title: r.get(2),
                snippet: r.get(3),
            })
            .collect())
    }

    /// K-hop neighbors of a code symbol through code edges.
    pub async fn code_neighbors_khop(
        &self,
        symbol_node_id: &str,
        k: usize,
        limit: i64,
    ) -> Result<Vec<String>> {
        if k == 0 {
            return Ok(Vec::new());
        }
        let max_hop = store_usize_to_i32(k * 2, "code khop max edge hop")?;
        let rows = self
            .db()
            .await?
            .query(
                "WITH RECURSIVE walk AS (
                     SELECT $1::text AS node, 0 AS hop, ARRAY[$1]::text[] AS path
                     UNION ALL
                     SELECT nxt.node,
                            w.hop + 1,
                            w.path || nxt.node
                     FROM walk w
                     JOIN LATERAL (
                         SELECT e.dst AS node FROM edge e
                          WHERE e.src = w.node AND e.kind = ANY($2)
                         UNION
                         SELECT e.src AS node FROM edge e
                          WHERE e.dst = w.node AND e.kind = ANY($2)
                     ) nxt ON NOT nxt.node = ANY(w.path)
                     WHERE w.hop < $3
                 )
                 SELECT DISTINCT node FROM walk
                 WHERE node LIKE 'code:%' AND node <> $1 AND hop > 0 AND hop <= $4
                 ORDER BY node
                 LIMIT $5;",
                &[
                    &symbol_node_id,
                    &CODE_EDGE_KINDS.to_vec(),
                    &max_hop,
                    &max_hop,
                    &limit,
                ],
            )
            .await
            .context("code neighbors khop")?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// Find code symbols whose name or signature matches a query substring.
    /// Used by recall to surface code context for coding questions.
    pub async fn search_code_symbols(
        &self,
        query: &str,
        limit: i64,
    ) -> Result<Vec<crate::codegraph::CodeSymbol>> {
        let pattern = format!("%{query}%");
        let rows = self
            .db()
            .await?
            .query(
                "SELECT id, label, outcome
                 FROM node
                 WHERE id LIKE 'code:%' AND (label ILIKE $1 OR outcome ILIKE $1)
                 ORDER BY label
                 LIMIT $2;",
                &[&pattern, &limit],
            )
            .await
            .context("search code symbols")?;
        Ok(rows
            .iter()
            .filter_map(|r| {
                let id: String = r.get(0);
                let label: String = r.get(1);
                let outcome: Option<String> = r.get(2);
                crate::codegraph::CodeSymbol::from_node_id(&id, &label, outcome)
            })
            .collect())
    }
}

fn text_field(event: &Value, key: &str) -> Option<String> {
    event
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

fn severity_text_for_status(status: &str) -> String {
    match status.to_ascii_lowercase().as_str() {
        "failed" | "failure" | "error" => "ERROR".to_owned(),
        "warn" | "warning" => "WARN".to_owned(),
        "debug" => "DEBUG".to_owned(),
        "trace" => "TRACE".to_owned(),
        _ => "INFO".to_owned(),
    }
}

fn severity_number_for_text(severity_text: &str) -> i32 {
    match severity_text.to_ascii_uppercase().as_str() {
        "TRACE" => 1,
        "DEBUG" => 5,
        "WARN" => 13,
        "ERROR" => 17,
        "FATAL" => 21,
        _ => 9,
    }
}

fn redact_json_value(value: &Value) -> Value {
    let Ok(re) = crate::redact::build_secret_re() else {
        return value.clone();
    };
    match value {
        Value::String(s) => Value::String(crate::redact::redact(re, s)),
        Value::Array(items) => Value::Array(items.iter().map(redact_json_value).collect()),
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (k.clone(), redact_json_value(v)))
                .collect(),
        ),
        _ => value.clone(),
    }
}

fn row_to_hit(r: &tokio_postgres::Row) -> Hit {
    Hit {
        id: r.get(0),
        content: r.get(1),
        origin: r.get(2),
        project: r.get(3),
        source_path: r.get(4),
        dist: r.get(5),
        score: 0.0,
    }
}

fn row_to_claim_record(r: &tokio_postgres::Row) -> ClaimRecord {
    ClaimRecord {
        claim: Claim {
            subject: r.get(0),
            predicate: r.get(1),
            value: r.get(2),
            kind: r.get(3),
            confidence: r.get(4),
        },
        source_path: r.get(5),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        Manager, Pool, Store, canonical_claim_axis, claim_node_id, db_i64_count_to_usize,
        db_u64_rows_to_usize, doc_path_from_node_id, store_usize_to_i32, store_usize_to_i64,
        typed_claim_node_id,
    };
    use crate::frontmatter::Claim;
    use anyhow::Context;
    use tokio_postgres::NoTls;

    #[test]
    fn db_count_conversion_rejects_impossible_negative_count() {
        let err = db_i64_count_to_usize(-1, "postgres").err();

        assert!(
            err.is_some_and(|err| format!("{err:#}").contains("postgres count cannot fit usize"))
        );
    }

    #[test]
    fn db_row_conversion_preserves_reported_rows() {
        assert_eq!(db_u64_rows_to_usize(3, "query_log prune").ok(), Some(3));
    }

    #[test]
    fn store_index_conversion_rejects_unrepresentable_chunk_index() {
        let err = store_usize_to_i32(usize::MAX, "chunk index").err();

        assert!(err.is_some_and(|err| format!("{err:#}").contains("chunk index cannot fit i32")));
    }

    #[test]
    fn store_limit_conversion_preserves_requested_limit() {
        assert_eq!(store_usize_to_i64(7, "search limit").ok(), Some(7));
    }

    #[test]
    fn doc_path_parser_rejects_missing_doc_prefix() {
        assert_eq!(
            doc_path_from_node_id("doc:vault/wiki/wiki-0001.md").ok(),
            Some("vault/wiki/wiki-0001.md".to_owned())
        );

        let err = doc_path_from_node_id("claim:omb:status").err();

        assert!(err.is_some_and(|err| {
            format!("{err:#}").contains("document node id missing doc: prefix")
        }));
    }

    #[test]
    fn claim_graph_ids_are_canonical() {
        let claim = Claim {
            subject: "  OH-my   Boring ".to_owned(),
            predicate: "Release   Version".to_owned(),
            value: "0.1.0".to_owned(),
            kind: "decision".to_owned(),
            confidence: "certain".to_owned(),
        };

        assert_eq!(claim_node_id(&claim), "claim:oh my boring:release version");
        assert_eq!(
            typed_claim_node_id("decision", &claim),
            "decision:oh my boring:release version"
        );
    }

    #[test]
    fn claim_storage_axis_is_canonical() {
        assert_eq!(
            canonical_claim_axis("  OH-my   Boring ", "raw_witness"),
            ("oh my boring".to_owned(), "raw witness".to_owned())
        );
    }

    #[tokio::test]
    async fn liveness_probe_fails_when_postgres_is_unreachable() -> anyhow::Result<()> {
        let pg_config: tokio_postgres::Config = "postgresql://boring:boring@127.0.0.1:1/boring"
            .parse()
            .context("parse probe dsn")?;
        let manager = Manager::new(pg_config, NoTls);
        let pool = Pool::builder(manager).build().context("build probe pool")?;
        let store = Store { pool, dim: 1024 };

        let result = store.liveness_probe().await;
        assert!(
            result.is_err(),
            "liveness probe must fail when postgres is unreachable"
        );
        Ok(())
    }
}
