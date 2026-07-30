//! HTTP handlers for the ohmyboring axum API.
//!
//! Cross-reference: design decision D3 (write door gated / read door open).
use std::time::Instant;
use std::time::SystemTime;

use axum::Json;
use axum::extract::{Query, State};
use serde_json::{Value, json};

use crate::ask;
use crate::ask::STALLED_DEFAULT_OLDER_THAN_DAYS;
use crate::audit;
use crate::graph;
use crate::retrieve;
use crate::serve::{
    AppError, AppState, AskReq, AskResp, CODE_SEARCH_MAX_SYMBOLS, CONTEXT_MAX_ITEMS, CodeNoteHit,
    CodeSearchReq, CodeSearchResp, CodeSymbolHit, CompactResp, EventIngestResp, EventLogEntry,
    EventLogReq, EventLogResp, GraphReq, GraphResp, HealthResp, MCP_MAX_RESULTS, MCP_MAX_TOKENS,
    QueryLogEntry, QueryLogInput, QueryLogReq, QueryLogResp, SearchHit, SearchResp, StalledReq,
    SyncResp, SyncState, count_wiki_notes, optional_project, parse_exclude_origins,
    recall_max_chars, spawn_query_log, vector_disabled,
};
use crate::store::EventLogFilter;

const EVENT_LOG_MAX_LIMIT: i64 = 1000;
const EVENT_INGEST_MAX_BATCH: usize = 100;
const QUERY_LOG_MAX_LIMIT: i64 = 1000;

fn exclude_origins(values: &[String]) -> Result<Vec<String>, AppError> {
    parse_exclude_origins(values).map_err(AppError::bad_request)
}

pub(crate) async fn health(State(state): State<AppState>) -> Json<HealthResp> {
    // Non-blocking: try_lock reveals whether the write-maintenance lane (sync/remember/forget) is
    // mid-flight without ever waiting on it. The guard is dropped immediately, so this never blocks.
    let sync = if state.sync_lock.try_lock().is_ok() {
        SyncState::Idle
    } else {
        SyncState::Running
    };
    Json(HealthResp {
        status: "ok",
        vector: state.store.is_some(),
        sync,
        corpus_count: state.wiki_dir().as_deref().and_then(count_wiki_notes),
    })
}

pub(crate) async fn handle_ask(
    State(s): State<AppState>,
    Json(req): Json<AskReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    // vector on → synthesize from vector+graph retrieval. off → synthesize from direct vault/wiki reads.
    let project = optional_project(req.project.as_deref());
    let since_hours = nonnegative_since_hours(req.since_hours)?;
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    let out = if let Some(store) = s.store.as_ref() {
        ask::answer(
            store,
            &s.llm,
            &req.question,
            &exclude_origins,
            project,
            since_hours,
        )
        .await?
    } else {
        ask::answer_wiki(
            &s.llm,
            s.wiki_dir().as_deref(),
            &req.question,
            &exclude_origins,
            project,
            since_hours,
        )
        .await?
    };
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "ask",
            query: req.question,
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: Some(json!({
                "graph_context_chars": out.graph_context_chars,
                "graph_source_count": out.graph_source_count,
            })),
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Recency-first briefing — no question (recency retrieval). Called by the cron morning briefing.
/// Recency (updated_at) ordering depends on pgvector → rejected if `BORING_VECTOR=off`.
pub(crate) async fn handle_brief(
    State(s): State<AppState>,
    req: Option<Json<crate::serve::BriefReq>>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let exclude_origins = req.as_ref().map_or_else(
        || Ok(Vec::new()),
        |Json(req)| exclude_origins(&req.exclude_origins),
    )?;
    let since_hours = req.as_ref().and_then(|Json(req)| req.since_hours);
    let out = ask::brief(
        store,
        &s.llm,
        &exclude_origins,
        s.cfg.note_lang.as_str(),
        since_hours,
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "brief",
            query: String::new(),
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Weekly briefing — last 7 days, grouped by project.
pub(crate) async fn handle_weekly(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::WeeklyReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    let since_hours = req.since_hours;
    let until_hours = req.until_hours;
    let out = ask::weekly_brief(
        store,
        &s.llm,
        &exclude_origins,
        s.cfg.note_lang.as_str(),
        since_hours,
        until_hours,
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "weekly",
            query: String::new(),
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Project status — last 30 days for a single project.
pub(crate) async fn handle_project_status(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::StatusReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let Some(project) = optional_project(Some(&req.project)) else {
        return Err(AppError::bad_request("missing argument: project"));
    };
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    let out = ask::project_status(
        store,
        &s.llm,
        project,
        &exclude_origins,
        s.cfg.note_lang.as_str(),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "status",
            query: project.to_owned(),
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Decision register — recent decision claims.
pub(crate) async fn handle_decisions(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::DecisionsReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    let out = ask::decision_register(
        store,
        &s.llm,
        optional_project(req.project.as_deref()),
        &exclude_origins,
        s.cfg.note_lang.as_str(),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "decisions",
            query: optional_project(req.project.as_deref())
                .unwrap_or_default()
                .to_owned(),
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Risk register — recent risk/assumption/blocked claims.
pub(crate) async fn handle_risks(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::RisksReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    let out = ask::risk_register(
        store,
        &s.llm,
        optional_project(req.project.as_deref()),
        &exclude_origins,
        s.cfg.note_lang.as_str(),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "risks",
            query: optional_project(req.project.as_deref())
                .unwrap_or_default()
                .to_owned(),
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Next-action register — recent `next` claims plus active `blocked` claims.
pub(crate) async fn handle_next_actions(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::NextActionsReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    let out = ask::next_action_register(
        store,
        &s.llm,
        optional_project(req.project.as_deref()),
        &exclude_origins,
        s.cfg.note_lang.as_str(),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "next_actions",
            query: optional_project(req.project.as_deref())
                .unwrap_or_default()
                .to_owned(),
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Stalled register — `next`/`blocked` claims older than N days.
pub(crate) async fn handle_stalled(
    State(s): State<AppState>,
    Json(req): Json<StalledReq>,
) -> Result<Json<AskResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    let out = ask::stalled_register(
        store,
        &s.llm,
        optional_project(req.project.as_deref()),
        &exclude_origins,
        s.cfg.note_lang.as_str(),
        req.older_than_days
            .unwrap_or(STALLED_DEFAULT_OLDER_THAN_DAYS),
    )
    .await?;
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "stalled",
            query: optional_project(req.project.as_deref())
                .unwrap_or_default()
                .to_owned(),
            hit_paths: out.sources.clone(),
            sources: out.sources.clone(),
            answer_snippet: out.answer.chars().take(280).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(AskResp {
        answer: out.answer,
        sources: out.sources,
    }))
}

/// Structured context card for agent session start — decisions/risks/facts/glossary/next_actions as claim lists.
/// Uses recency ordering (no vector search), so it works when BORING_VECTOR=off.
pub(crate) async fn handle_context(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::ContextReq>,
) -> Result<Json<ask::ContextCard>, AppError> {
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    // Context can be served from the vault even when the vector backend is off, because it only
    // needs current claims by recency. Fall back to an empty card if no store is available.
    let card = if let Some(store) = s.store.as_ref() {
        ask::context_card(
            store,
            optional_project(req.project.as_deref()),
            &exclude_origins,
            req.max_items.clamp(1, CONTEXT_MAX_ITEMS),
            s.cfg.note_lang.as_str(),
        )
        .await?
    } else {
        ask::ContextCard {
            decisions: vec![],
            risks: vec![],
            facts: vec![],
            glossary: vec![],
            next_actions: vec![],
            language: s.cfg.note_lang.as_str().to_owned(),
        }
    };
    Ok(Json(card))
}

pub(crate) async fn handle_search(
    State(s): State<AppState>,
    Json(req): Json<crate::serve::SearchReq>,
) -> Result<Json<SearchResp>, AppError> {
    let started = Instant::now();
    let max_results = req.max_results.clamp(1, MCP_MAX_RESULTS);
    let max_tokens = req.max_tokens.clamp(1, MCP_MAX_TOKENS);
    let max_chars = recall_max_chars(max_tokens)?;
    let project = optional_project(req.project.as_deref());
    let since_hours = nonnegative_since_hours(req.since_hours)?;
    let exclude_origins = exclude_origins(&req.exclude_origins)?;
    // vector-first: /search is the external accuracy contract (eval gate). Use the strongest
    // retriever when available; fall back to direct wiki reads only when vector is off.
    let mapped: Vec<SearchHit> = if let Some(store) = s.store.as_ref() {
        retrieve::retrieve_budget(
            store,
            &s.llm,
            &req.query,
            max_results,
            max_chars,
            &exclude_origins,
            project,
            since_hours,
            false,
        )
        .await?
        .into_iter()
        .map(|h| SearchHit {
            id: h.id,
            origin: h.origin,
            project: h.project,
            source_path: h.source_path,
            snippet: h.content,
        })
        .collect()
    } else {
        crate::wiki_recall::trim_hits_to_budget(
            s.wiki_recall(
                &req.query,
                max_results,
                project,
                &exclude_origins,
                since_hours,
            )?,
            max_results,
            max_chars,
        )
        .into_iter()
        .map(|h| SearchHit {
            id: h.id,
            origin: h.origin,
            project: h.project,
            source_path: h.source_path,
            snippet: h.snippet,
        })
        .collect()
    };
    let hit_paths: Vec<String> = mapped.iter().map(|h| h.source_path.clone()).collect();
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "search",
            query: req.query.clone(),
            hit_paths,
            sources: vec![],
            answer_snippet: mapped
                .first()
                .map(|h| h.snippet.chars().take(200).collect())
                .unwrap_or_default(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(SearchResp { hits: mapped }))
}

/// Code-graph search — AST symbols (tree-sitter indexed) matching the query substring,
/// plus wiki notes the user linked to those symbols via `remember_code`.
/// The code lane is pgvector-only (graph store), so it rejects under `BORING_VECTOR=off`.
pub(crate) async fn handle_code_search(
    State(s): State<AppState>,
    Json(req): Json<CodeSearchReq>,
) -> Result<Json<CodeSearchResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?; // code graph is pgvector-only
    let max_symbols = req.max_symbols.clamp(1, CODE_SEARCH_MAX_SYMBOLS);
    let symbols = store
        .search_code_symbols(&req.query, i64::try_from(max_symbols).unwrap_or(20))
        .await?;
    let node_ids: Vec<String> = symbols
        .iter()
        .map(crate::codegraph::CodeSymbol::node_id)
        .collect();
    let hits: Vec<CodeSymbolHit> = symbols
        .into_iter()
        .map(|sym| CodeSymbolHit {
            kind: sym.kind.as_str().to_owned(),
            name: sym.name,
            source_path: sym.source_path,
            signature: sym.signature,
        })
        .collect();
    let notes: Vec<CodeNoteHit> = store
        .code_notes_for_symbols(&node_ids)
        .await?
        .into_iter()
        .take(max_symbols)
        .map(|link| {
            let (symbol_name, symbol_path) = split_code_note_symbol(&link.symbol_node_id);
            CodeNoteHit {
                source_path: link.source_path,
                title: link.title,
                snippet: link.snippet,
                symbol_name,
                symbol_path,
            }
        })
        .collect();
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "code-search",
            query: req.query.clone(),
            hit_paths: hits.iter().map(|h| h.source_path.clone()).collect(),
            sources: vec![],
            answer_snippet: hits
                .first()
                .map(|h| h.name.chars().take(200).collect())
                .unwrap_or_default(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(CodeSearchResp { hits, notes }))
}

/// Split a `code:<kind>:<path>:<name>` node id into (name, path) for display.
/// Mirrors `CodeSymbol::from_node_id` but never rejects — a note link must surface
/// even when the symbol's extension is outside the indexed language set.
fn split_code_note_symbol(node_id: &str) -> (String, String) {
    let rest = node_id.strip_prefix("code:").unwrap_or(node_id);
    let mut parts = rest.splitn(3, ':');
    let _kind = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("");
    let name = parts.next().unwrap_or("");
    (name.to_owned(), path.to_owned())
}

pub(crate) async fn handle_graph(
    State(s): State<AppState>,
    Json(req): Json<GraphReq>,
) -> Result<Json<GraphResp>, AppError> {
    let started = Instant::now();
    let store = s.store.as_ref().ok_or_else(vector_disabled)?; // graph is pgvector-only
    let depth = req.depth.unwrap_or(2);
    let out = graph::query(store, &s.llm, &req.query, depth).await?;
    let hit = if out.hit.is_empty() {
        vec![]
    } else {
        vec![out.hit.clone()]
    };
    spawn_query_log(
        s.store.clone(),
        QueryLogInput {
            endpoint: "graph",
            query: req.query.clone(),
            hit_paths: hit,
            sources: vec![],
            answer_snippet: out.hit.chars().take(200).collect(),
            elapsed: started.elapsed(),
            meta: None,
        },
    );
    Ok(Json(GraphResp {
        hit: out.hit,
        graph_neighbors: out.graph_neighbors,
        semantic_neighbors: out.semantic_neighbors,
    }))
}

pub(crate) async fn handle_audit(
    State(s): State<AppState>,
) -> Result<Json<audit::AuditStats>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?; // ingest stats are pgvector-only
    let stats = audit::stats(store, s.cfg.allow_company_origin).await?;
    Ok(Json(stats))
}

/// Recent query/retrieval log — for memory-utility analytics.
pub(crate) async fn handle_query_log(
    State(s): State<AppState>,
    Query(params): Query<QueryLogReq>,
) -> Result<Json<QueryLogResp>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let limit = bounded_limit(params.limit, "limit", QUERY_LOG_MAX_LIMIT)?;
    let rows = store.recent_queries(limit).await?;
    let entries = rows
        .into_iter()
        .map(|r| QueryLogEntry {
            id: r.id,
            created_at: format!("{:?}", r.created_at),
            endpoint: r.endpoint,
            query: r.query,
            hit_paths: r.hit_paths,
            sources: r.sources,
            answer_snippet: r.answer_snippet,
            latency_ms: r.latency_ms,
        })
        .collect();
    Ok(Json(QueryLogResp { entries }))
}

/// Recent adapter/workflow events stored in Postgres. The payload is OpenTelemetry-shaped
/// (`otel`) while keeping legacy top-level keys for filtering and readability.
pub(crate) async fn handle_events(
    State(s): State<AppState>,
    Query(params): Query<EventLogReq>,
) -> Result<Json<EventLogResp>, AppError> {
    let since_hours = nonnegative_since_hours(params.since_hours)?;
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let limit = bounded_limit(params.limit, "limit", EVENT_LOG_MAX_LIMIT)?;
    let rows = store
        .recent_events(EventLogFilter {
            limit,
            component: params.component.as_deref(),
            event_name: params.event_name.as_deref(),
            status: params.status.as_deref(),
            run_id: params.run_id.as_deref(),
            workflow: params.workflow.as_deref(),
            since_hours,
        })
        .await?;
    let entries = rows
        .into_iter()
        .map(|r| {
            let observed_at = system_time_rfc3339(r.observed_at);
            let severity_text = r.severity_text;
            let event_name = r.event_name;
            let trace_id = r.trace_id;
            let span_id = r.span_id;
            let body = r.body;
            let attributes = r.attributes;
            let resource = r.resource;
            let otel = json!({
                "observed_timestamp": observed_at.clone(),
                "time_unix_nano": r.time_unix_nano,
                "severity_text": severity_text.clone(),
                "severity_number": r.severity_number,
                "body": body.clone(),
                "attributes": attributes.clone(),
                "resource": resource.clone(),
                "trace_id": trace_id.clone(),
                "span_id": span_id.clone(),
                "event_name": event_name.clone(),
            });
            EventLogEntry {
                id: r.id,
                observed_at,
                time_unix_nano: r.time_unix_nano,
                severity_text,
                severity_number: r.severity_number,
                service_name: r.service_name,
                component: r.component,
                event_name,
                status: r.status,
                trace_id,
                span_id,
                run_id: r.run_id,
                session_id: r.session_id,
                workflow: r.workflow,
                workflow_node: r.workflow_node,
                workflow_outcome: r.workflow_outcome,
                body,
                attributes,
                resource,
                otel,
            }
        })
        .collect();
    Ok(Json(EventLogResp { entries }))
}

/// Store one event or an `{events: [...]}` batch in the local event DB.
pub(crate) async fn handle_event_ingest(
    State(s): State<AppState>,
    Json(req): Json<Value>,
) -> Result<Json<EventIngestResp>, AppError> {
    let store = s.store.as_ref().ok_or_else(vector_disabled)?;
    let events = event_batch(req)?;
    let accepted = events.len();
    for event in events {
        store.log_event(&event).await?;
    }
    Ok(Json(EventIngestResp { accepted }))
}

pub(crate) async fn handle_sync(State(s): State<AppState>) -> Result<Json<SyncResp>, AppError> {
    let _guard = s.sync_lock.lock().await;
    let o = super::scheduler::do_sync(s.store.as_deref(), &s.llm, (*s.vault_dir).as_ref(), &s.cfg)
        .await?;
    Ok(Json(SyncResp {
        ingest_new: o.ingest.new,
        ingest_updated: o.ingest.updated,
        ingest_deleted: o.ingest.deleted,
        ingest_chunks: o.ingest.chunks,
        graph_tools: o.ingest.tools,
        graph_concepts: o.ingest.concepts,
        graph_claims: o.ingest.claims,
        graph_edges: o.ingest.edges,
        total_chunks: o.total_chunks,
        total_edges: o.total_edges,
    }))
}

fn system_time_rfc3339(value: SystemTime) -> String {
    let datetime: chrono::DateTime<chrono::Utc> = value.into();
    datetime.to_rfc3339()
}

fn nonnegative_since_hours(since_hours: Option<i32>) -> Result<Option<i32>, AppError> {
    if since_hours.is_some_and(|hours| hours < 0) {
        return Err(AppError::bad_request("since_hours must be >= 0"));
    }
    Ok(since_hours)
}

fn bounded_limit(value: i64, key: &str, cap: i64) -> Result<i64, AppError> {
    if value < 0 {
        return Err(AppError::bad_request(format!("{key} must be >= 0")));
    }
    Ok(value.clamp(1, cap))
}

fn event_batch(req: Value) -> Result<Vec<Value>, AppError> {
    if let Some(items) = req.get("events").and_then(Value::as_array) {
        if items.len() > EVENT_INGEST_MAX_BATCH {
            return Err(AppError::bad_request(format!(
                "events batch too large: max {EVENT_INGEST_MAX_BATCH}"
            )));
        }
        Ok(items.clone())
    } else {
        Ok(vec![req])
    }
}

/// Maintenance compact: VACUUM/ANALYZE + REINDEX + query_log pruning + orphan GC.
pub(crate) async fn handle_compact(
    State(s): State<AppState>,
) -> Result<Json<CompactResp>, AppError> {
    let _guard = s.sync_lock.lock().await;
    let summary = super::scheduler::do_compact(s.store.as_deref()).await?;
    *s.last_compact.lock().await = Some(Instant::now());
    Ok(Json(CompactResp {
        vacuum_ms: summary.report.vacuum_ms,
        reindex_ms: summary.report.reindex_ms,
        prune_query_log: summary.report.prune_query_log,
        gc_tool: summary.report.gc_tool,
        gc_concept: summary.report.gc_concept,
        total_ms: summary.total_ms,
    }))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use axum::http::StatusCode;
    use axum::response::IntoResponse;
    use serde_json::json;

    use super::{EVENT_INGEST_MAX_BATCH, bounded_limit, event_batch, nonnegative_since_hours};

    #[test]
    fn event_since_hours_rejects_negative_window() {
        let Err(err) = nonnegative_since_hours(Some(-1)) else {
            panic!("negative since_hours should fail");
        };
        assert_eq!(err.into_response().status(), StatusCode::BAD_REQUEST);
        let Ok(hours) = nonnegative_since_hours(Some(24)) else {
            panic!("positive since_hours should pass");
        };
        assert_eq!(hours, Some(24));
    }

    #[test]
    fn bounded_limit_rejects_negative_and_preserves_existing_bounds() {
        let Err(err) = bounded_limit(-1, "limit", 1000) else {
            panic!("negative limit should fail");
        };
        assert_eq!(err.into_response().status(), StatusCode::BAD_REQUEST);

        let Ok(min_limit) = bounded_limit(0, "limit", 1000) else {
            panic!("zero limit should preserve the existing minimum");
        };
        assert_eq!(min_limit, 1);

        let Ok(capped_limit) = bounded_limit(1500, "limit", 1000) else {
            panic!("oversized limit should preserve the existing cap");
        };
        assert_eq!(capped_limit, 1000);
    }

    #[test]
    fn event_batch_rejects_oversized_batch_without_truncating() {
        let events: Vec<_> = (0..=EVENT_INGEST_MAX_BATCH)
            .map(|i| json!({"event": "x", "i": i}))
            .collect();
        let Err(err) = event_batch(json!({"events": events})) else {
            panic!("oversized batch should fail");
        };
        assert_eq!(err.into_response().status(), StatusCode::BAD_REQUEST);
    }
}
