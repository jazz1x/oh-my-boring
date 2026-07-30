//! Rust integration tests for the Storage Layer contract.
//!
//! These tests exercise the live PostgreSQL backend, NOT the HTTP/MCP surface
//! (that belongs in `scripts/e2e.sh` and `data/eval/run_eval.py`). They need a
//! Postgres instance reachable via `BORING_TEST_DATABASE_URL`. If the variable is
//! unset, the tests are skipped with a clear message.
//!
//! Run via (serially — they share one DB and `compact()` does a global `REINDEX CONCURRENTLY`,
//! which conflicts with other tests' open connections under the default parallel runner):
//!   `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/boring_test \`
//!   `  cargo test -p drudge --test store_integration -- --test-threads=1`
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::audit;
use drudge::codegraph::{CodeLanguage, CodeRelation, CodeRelationKind, CodeSymbol, CodeSymbolKind};
use drudge::frontmatter::{Claim, FrontMatter};
use drudge::store::{Doc, EventLogFilter, Hit, RelatedEvidenceKind, Store};
use serde_json::json;
use tokio_postgres::{Client, NoTls};

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

async fn connect(dsn: &str) -> Client {
    let (client, conn) = tokio_postgres::connect(dsn, NoTls)
        .await
        .expect("connect to Postgres");
    tokio::spawn(conn);
    client
}

fn unique_path(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("/vault/wiki/{prefix}-{ts}.md")
}

fn dummy_frontmatter(path: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: "test".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("test note".to_string()),
        tags: vec!["test".to_string()],
        ..Default::default()
    }
}

fn origin_stat_count(rows: &[(String, usize)], origin: &str) -> usize {
    rows.iter()
        .find_map(|(key, count)| (key == origin).then_some(*count))
        .unwrap_or_default()
}

async fn count_claims(db: &Client, path: &str) -> i64 {
    db.query_one(
        "SELECT count(*) FROM claim WHERE source_path = $1;",
        &[&path],
    )
    .await
    .expect("count claims")
    .get(0)
}

/// Ensure VACUUM/REINDEX CONCURRENTLY run outside a transaction block.
#[tokio::test]
async fn compact_succeeds_in_autocommit_mode() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let summary = store.compact().await.expect("compact should not fail");
    assert!(summary.total_ms > 0, "compact should report elapsed time");
}

#[tokio::test]
async fn doc_updated_at_returns_none_for_missing_document() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let path = unique_path("missing-doc-updated-at");

    let updated_at = store.doc_updated_at(&path).await.expect("doc updated_at");

    assert!(
        updated_at.is_none(),
        "missing document must not invent a recency timestamp"
    );
}

/// Workflow events are stored in Postgres as OpenTelemetry-shaped rows while keeping legacy
/// filter keys (`component`, `event`, `status`, `run_id`) queryable.
#[tokio::test]
async fn event_log_round_trips_otel_projection() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let run_id = format!(
        "event-roundtrip-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let fake_key = ["sk", "-ant", "-abcdefghij1234567890XYZ"].join("");

    store
        .log_event(&json!({
            "ts": "2026-07-01T00:00:00Z",
            "component": "guard",
            "event": "structural_guard",
            "status": "failed",
            "run_id": run_id,
            "credential": fake_key,
            "workflow": "memory_ingest",
            "workflow_node": "remember",
            "workflow_outcome": "failed",
            "otel": {
                "time_unix_nano": 1_782_864_000_000_000_000_i64,
                "severity_text": "ERROR",
                "severity_number": 17,
                "event_name": "structural_guard"
            }
        }))
        .await
        .expect("log event");

    let rows = store
        .recent_events(EventLogFilter {
            limit: 10,
            component: Some("guard"),
            event_name: Some("structural_guard"),
            status: Some("failed"),
            run_id: Some(&run_id),
            workflow: Some("memory_ingest"),
            since_hours: None,
        })
        .await
        .expect("recent events");
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert_eq!(row.severity_text, "ERROR");
    assert_eq!(row.severity_number, 17);
    assert_eq!(row.workflow_node.as_deref(), Some("remember"));
    assert_eq!(row.time_unix_nano, Some(1_782_864_000_000_000_000));
    let attrs = row.attributes.to_string();
    assert!(
        !attrs.contains("sk-ant-"),
        "event attributes must be redacted"
    );
    assert!(
        attrs.contains("REDACTED"),
        "redacted marker should remain visible"
    );

    let db = connect(&dsn).await;
    db.execute("DELETE FROM event_log WHERE run_id = $1;", &[&run_id])
        .await
        .expect("cleanup event");
}

#[tokio::test]
async fn audit_tallies_missing_origin_as_personal_but_keeps_quality_signal() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let before = audit::stats(&store, true).await.expect("audit before");
    let legacy_path = unique_path("audit-legacy-empty-origin");
    let mut legacy_front = dummy_frontmatter(&legacy_path);
    legacy_front.origin.clear();
    store
        .upsert_document(&legacy_front, "sha-audit-origin", SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_chunk(&Doc {
            id: format!("{legacy_path}#0"),
            content: "audit legacy origin should count as semantic personal".to_owned(),
            embedding: vec![0.0_f32; 1024],
            front: legacy_front,
            chunk_idx: 0,
        })
        .await
        .expect("upsert chunk");

    let after = audit::stats(&store, true).await.expect("audit after");
    assert_eq!(
        origin_stat_count(&after.by_origin, "personal"),
        origin_stat_count(&before.by_origin, "personal") + 1,
        "semantic origin distribution should match recall's missing-origin-as-personal policy"
    );
    assert_eq!(
        after.missing_origin,
        before.missing_origin + 1,
        "raw missing-origin metadata should remain visible as a quality signal"
    );

    store
        .delete_document(&legacy_path)
        .await
        .expect("cleanup audit document");
}

async fn upsert_claim_fixture_doc(
    store: &Store,
    front: &FrontMatter,
    subject: &str,
    embedding: &[f32],
) {
    store
        .upsert_document(front, "sha-claim-origin", SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim(
            subject,
            "is",
            "x",
            &front.source_path,
            SystemTime::now(),
            embedding,
            "fact",
            "certain",
        )
        .await
        .expect("upsert claim");
}

fn claim_subjects(rows: Vec<Claim>) -> Vec<String> {
    rows.into_iter().map(|claim| claim.subject).collect()
}

struct ClaimOriginFixture {
    p_path: String,
    legacy_path: String,
    c_path: String,
    b_path: String,
    p_subj: String,
    legacy_subj: String,
    c_subj: String,
    b_subj: String,
}

impl ClaimOriginFixture {
    fn cleanup_paths(&self) -> [&str; 4] {
        [&self.p_path, &self.legacy_path, &self.c_path, &self.b_path]
    }
}

async fn prepare_claim_origin_fixture(store: &Store) -> ClaimOriginFixture {
    let p_path = unique_path("claim-origin-personal");
    let legacy_path = unique_path("claim-origin-legacy-personal");
    let c_path = unique_path("claim-origin-company");
    let b_path = unique_path("claim-origin-brief");
    let fixture = ClaimOriginFixture {
        // Punctuation-free subjects: claims are stored in canonical claim-key form
        // (non-alphanumerics → spaces), so these read back unchanged.
        p_subj: format!("subjpersonal{}", p_path.len()),
        legacy_subj: format!("subjlegacypersonal{}", legacy_path.len()),
        c_subj: format!("subjcompany{}", c_path.len()),
        b_subj: format!("subjbrief{}", b_path.len()),
        p_path,
        legacy_path,
        c_path,
        b_path,
    };

    let mut p_front = dummy_frontmatter(&fixture.p_path);
    p_front.origin = "personal".to_string();
    let mut legacy_front = dummy_frontmatter(&fixture.legacy_path);
    legacy_front.origin.clear();
    let mut c_front = dummy_frontmatter(&fixture.c_path);
    c_front.origin = "company".to_string();
    let mut b_front = dummy_frontmatter(&fixture.b_path);
    b_front.tags.push("daily-brief".to_owned());

    let mut emb_p = vec![0.0_f32; 1024];
    emb_p[0] = 1.0;
    let mut emb_legacy = vec![0.0_f32; 1024];
    emb_legacy[0] = 0.9;
    let mut emb_c = vec![0.0_f32; 1024];
    emb_c[1] = 1.0;
    let mut emb_b = vec![0.0_f32; 1024];
    emb_b[2] = 1.0;

    for (front, subj, emb) in [
        (&p_front, &fixture.p_subj, &emb_p),
        (&legacy_front, &fixture.legacy_subj, &emb_legacy),
        (&c_front, &fixture.c_subj, &emb_c),
        (&b_front, &fixture.b_subj, &emb_b),
    ] {
        upsert_claim_fixture_doc(store, front, subj, emb).await;
    }

    fixture
}

/// Ensure current_claims honors exclude_origins (a claim's origin comes from its parent document via
/// the JOIN), so a claim can't bypass the same origin boundary the recalled chunks in an answer respect.
#[tokio::test]
async fn current_claims_honors_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let fixture = prepare_claim_origin_fixture(&store).await;
    let query = [0.5_f32; 1024]; // near both

    // No exclusion → both visible.
    let all = claim_subjects(
        store
            .current_claims(&query, 20, &[], None, None)
            .await
            .expect("claims all"),
    );
    assert!(
        all.contains(&fixture.p_subj)
            && all.contains(&fixture.legacy_subj)
            && all.contains(&fixture.c_subj),
        "both origins visible with no exclusion"
    );
    assert!(
        !all.contains(&fixture.b_subj),
        "generated briefing claims must not feed current claim authority"
    );

    // Exclude company → company claim must be filtered out, personal kept.
    let filtered = claim_subjects(
        store
            .current_claims(&query, 20, &["company".to_string()], None, None)
            .await
            .expect("claims filtered"),
    );
    assert!(
        filtered.contains(&fixture.p_subj),
        "personal claim must survive the company exclusion"
    );
    assert!(
        filtered.contains(&fixture.legacy_subj),
        "legacy personal claim must survive the company exclusion"
    );
    assert!(
        !filtered.contains(&fixture.c_subj),
        "company claim must be excluded"
    );

    let filtered_records = store
        .current_claim_records(&query, 20, &["company".to_string()], None, None)
        .await
        .expect("claim records filtered");
    assert!(
        filtered_records
            .iter()
            .any(|record| record.claim.subject == fixture.p_subj
                && record.source_path == fixture.p_path),
        "personal claim record should keep its source_path provenance"
    );
    assert!(
        !filtered_records
            .iter()
            .any(|record| record.source_path == fixture.c_path),
        "company claim record must stay excluded"
    );
    assert!(
        !filtered_records
            .iter()
            .any(|record| record.source_path == fixture.b_path),
        "generated briefing claim record must stay excluded"
    );

    let personal_filtered = claim_subjects(
        store
            .current_claims(&query, 20, &["personal".to_string()], None, None)
            .await
            .expect("claims personal filtered"),
    );
    assert!(
        !personal_filtered.contains(&fixture.p_subj),
        "personal claim must be excluded by personal exclusion"
    );
    assert!(
        !personal_filtered.contains(&fixture.legacy_subj),
        "legacy empty-origin claim must not bypass personal exclusion"
    );
    assert!(
        personal_filtered.contains(&fixture.c_subj),
        "company claim should remain when only personal is excluded"
    );

    let recent_personal_filtered = claim_subjects(
        store
            .recent_claims(20, None, None, &["personal".to_string()])
            .await
            .expect("recent claims personal filtered"),
    );
    assert!(
        !recent_personal_filtered.contains(&fixture.legacy_subj),
        "legacy empty-origin recent claim must not bypass personal exclusion"
    );

    let cleanup_paths = fixture.cleanup_paths();
    cleanup_docs(&store, &cleanup_paths).await;
}

/// Ensure delete_document removes not only document/edge rows but also claims,
/// because claim has no FK to document.
#[tokio::test]
async fn delete_document_removes_claims() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let path = unique_path("delete-claim-test");
    let mut front = dummy_frontmatter(&path);
    front.claims.push(Claim {
        subject: "test-subject".to_string(),
        predicate: "has".to_string(),
        value: "value".to_string(),
        kind: "fact".to_string(),
        confidence: "certain".to_string(),
    });

    store
        .upsert_document(&front, "sha1", SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim(
            &front.claims[0].subject,
            &front.claims[0].predicate,
            &front.claims[0].value,
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
            &front.claims[0].kind,
            &front.claims[0].confidence,
        )
        .await
        .expect("upsert claim");

    assert_eq!(
        count_claims(&db, &path).await,
        1,
        "claim should exist before delete"
    );

    store.delete_document(&path).await.expect("delete document");

    assert_eq!(
        count_claims(&db, &path).await,
        0,
        "claim should be removed with document"
    );
}

fn raw_witness_decision_claim(value: &str) -> Claim {
    Claim {
        subject: "oh my boring".to_owned(),
        predicate: "raw witness".to_owned(),
        value: value.to_owned(),
        kind: "decision".to_owned(),
        confidence: "certain".to_owned(),
    }
}

async fn upsert_claim_with_graph(store: &Store, path: &str, claim: &Claim, valid_from: SystemTime) {
    store
        .upsert_claim(
            &claim.subject,
            &claim.predicate,
            &claim.value,
            path,
            valid_from,
            &[0.1_f32; 1024],
            &claim.kind,
            &claim.confidence,
        )
        .await
        .expect("upsert claim");
    store
        .upsert_claim_node(path, "omb", claim)
        .await
        .expect("upsert claim node");
}

#[tokio::test]
async fn delete_document_reseals_remaining_claim_history() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let older_path = unique_path("delete-claim-reseal-older");
    let newer_path = unique_path("delete-claim-reseal-newer");
    let mut older_front = dummy_frontmatter(&older_path);
    older_front.project = "omb".to_owned();
    let mut newer_front = dummy_frontmatter(&newer_path);
    newer_front.project = "omb".to_owned();
    store
        .upsert_document(&older_front, "sha-older", SystemTime::now())
        .await
        .expect("upsert older doc");
    store
        .upsert_document(&newer_front, "sha-newer", SystemTime::now())
        .await
        .expect("upsert newer doc");

    let older = SystemTime::now()
        .checked_sub(Duration::from_mins(2))
        .expect("valid older timestamp");
    let newer = SystemTime::now()
        .checked_sub(Duration::from_mins(1))
        .expect("valid newer timestamp");
    let older_claim = raw_witness_decision_claim("copy before distill");
    let newer_claim = raw_witness_decision_claim("retain for 90 days");
    upsert_claim_with_graph(&store, &older_path, &older_claim, older).await;
    upsert_claim_with_graph(&store, &newer_path, &newer_claim, newer).await;

    store
        .delete_document(&newer_path)
        .await
        .expect("delete newer doc");

    let records = store
        .recent_claim_records(10, Some("omb"), Some(&["decision".to_owned()]), &[])
        .await
        .expect("recent decision records");
    let axis: Vec<_> = records
        .iter()
        .filter(|record| {
            record.claim.subject == "oh my boring" && record.claim.predicate == "raw witness"
        })
        .collect();
    assert_eq!(axis.len(), 1);
    assert_eq!(axis[0].claim.value, "copy before distill");
    assert_eq!(axis[0].source_path, older_path);
    let semantic_neighbors = store
        .semantic_neighbors(&older_path)
        .await
        .expect("semantic neighbors after delete");
    assert!(
        semantic_neighbors
            .iter()
            .any(|label| label == "raw witness: copy before distill"),
        "remaining document should expose the resealed current claim value"
    );
    assert!(
        !semantic_neighbors
            .iter()
            .any(|label| label == "raw witness: retain for 90 days"),
        "deleted newer claim value must not remain as the claim graph label"
    );

    store
        .delete_document(&older_path)
        .await
        .expect("cleanup older");
}

/// Claim-axis related docs should survive subject/predicate spelling variants.
#[tokio::test]
async fn claim_related_docs_links_shared_claim_identity() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let a_path = unique_path("claim-related-a");
    let b_path = unique_path("claim-related-b");
    let c_path = unique_path("claim-related-c");
    let a_front = dummy_frontmatter(&a_path);
    let b_front = dummy_frontmatter(&b_path);
    let c_front = dummy_frontmatter(&c_path);
    for front in [&a_front, &b_front, &c_front] {
        store
            .upsert_document(front, "sha-claim-related", SystemTime::now())
            .await
            .expect("upsert doc");
    }

    let a_claim = Claim {
        subject: "release train".to_owned(),
        predicate: "release-version".to_owned(),
        value: "0.2.0".to_owned(),
        kind: "fact".to_owned(),
        confidence: "certain".to_owned(),
    };
    let b_claim = Claim {
        subject: "release-train".to_owned(),
        predicate: "release version".to_owned(),
        value: "0.2.1".to_owned(),
        kind: "fact".to_owned(),
        confidence: "certain".to_owned(),
    };
    let c_claim = Claim {
        subject: "release train".to_owned(),
        predicate: "status".to_owned(),
        value: "blocked".to_owned(),
        kind: "blocked".to_owned(),
        confidence: "certain".to_owned(),
    };

    store
        .upsert_claim_node(&a_path, "test", &a_claim)
        .await
        .expect("upsert claim a");
    store
        .upsert_claim_node(&b_path, "test", &b_claim)
        .await
        .expect("upsert claim b");
    store
        .upsert_claim_node(&c_path, "test", &c_claim)
        .await
        .expect("upsert claim c");

    let related = store
        .claim_related_docs(&a_path, 10)
        .await
        .expect("claim related docs");
    assert!(
        related.contains(&b_path),
        "spelling variants on the same claim axis should link"
    );
    assert!(
        !related.contains(&c_path),
        "different predicates should not link through the claim axis"
    );

    store.delete_document(&a_path).await.expect("cleanup a");
    store.delete_document(&b_path).await.expect("cleanup b");
    store.delete_document(&c_path).await.expect("cleanup c");
}

/// Current claim authority should not fork when callers use different casing or separators.
#[tokio::test]
async fn claim_upsert_canonicalizes_axis_before_supersede() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-axis-current");
    let mut front = dummy_frontmatter(&path);
    front.project = "omb".to_owned();
    store
        .upsert_document(&front, "sha-claim-axis", SystemTime::now())
        .await
        .expect("upsert doc");

    let older = SystemTime::now()
        .checked_sub(Duration::from_mins(1))
        .expect("valid older timestamp");
    let newer = SystemTime::now();
    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            "OH-my Boring",
            "raw_witness",
            "copy before distill",
            &path,
            older,
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert older claim");
    store
        .upsert_claim(
            "oh my boring",
            "raw-witness",
            "retain for 90 days",
            &path,
            newer,
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert newer claim");

    let records = store
        .recent_claim_records(10, Some("omb"), Some(&["decision".to_owned()]), &[])
        .await
        .expect("recent decision records");
    let axis: Vec<_> = records
        .iter()
        .filter(|record| {
            record.claim.subject == "oh my boring" && record.claim.predicate == "raw witness"
        })
        .collect();
    assert_eq!(axis.len(), 1);
    assert_eq!(axis[0].claim.value, "retain for 90 days");

    store.delete_document(&path).await.expect("cleanup");
}

async fn upsert_test_docs(store: &Store, fronts: &[&FrontMatter], sha: &str) {
    for front in fronts {
        store
            .upsert_document(front, sha, SystemTime::now())
            .await
            .expect("upsert doc");
    }
}

fn relation_lane_claims() -> [Claim; 2] {
    [
        Claim {
            subject: "release train".to_owned(),
            predicate: "release-version".to_owned(),
            value: "0.2.0".to_owned(),
            kind: "fact".to_owned(),
            confidence: "certain".to_owned(),
        },
        Claim {
            subject: "release train".to_owned(),
            predicate: "status".to_owned(),
            value: "ready".to_owned(),
            kind: "fact".to_owned(),
            confidence: "certain".to_owned(),
        },
    ]
}

async fn add_relation_lane_claim_edges(store: &Store, source_path: &str, claim_only_path: &str) {
    for claim in &relation_lane_claims() {
        store
            .upsert_claim_node(source_path, "test", claim)
            .await
            .expect("source claim edge");
        store
            .upsert_claim_node(claim_only_path, "test", claim)
            .await
            .expect("claim-only claim edge");
    }
}

async fn add_relation_lane_graph_edges(store: &Store, source_path: &str, graph_path: &str) {
    store
        .upsert_tool("relationlanetool", "relation lane tool")
        .await
        .expect("upsert tool");
    store
        .upsert_concept("relationlaneconcept", "relation lane concept")
        .await
        .expect("upsert concept");
    store
        .upsert_concept("relationlanetoolconcept", "relation lane tool")
        .await
        .expect("upsert duplicate-label concept");
    for path in [source_path, graph_path] {
        store
            .relate_doc_tool(path, "relationlanetool")
            .await
            .expect("tool edge");
        store
            .relate_doc_concept(path, "relationlaneconcept")
            .await
            .expect("concept edge");
        store
            .relate_doc_concept(path, "relationlanetoolconcept")
            .await
            .expect("duplicate-label concept edge");
    }
}

async fn add_relation_lane_chunks(
    store: &Store,
    claim_front: &FrontMatter,
    graph_front: &FrontMatter,
) {
    for (front, content) in [
        (claim_front, "claim-only content"),
        (graph_front, "graph content"),
    ] {
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: content.to_owned(),
                embedding: vec![0.1; 1024],
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }
}

async fn assert_claim_lane_content(store: &Store, source_path: &str, claim_only_path: &str) {
    let claim_content = store
        .claim_related_doc_content(source_path, 10, &[], None, None)
        .await
        .expect("claim related doc content");
    let claim_doc = claim_content
        .iter()
        .find(|doc| doc.doc.source_path == claim_only_path)
        .expect("claim related doc");
    assert_eq!(claim_doc.evidence.kind, RelatedEvidenceKind::Claim);
    assert_eq!(claim_doc.evidence.shared_count, 2);
    assert_eq!(
        usize::try_from(claim_doc.evidence.shared_count),
        Ok(claim_doc.evidence.shared_nodes.len()),
        "claim relation count should match distinct displayed claim axes"
    );
    assert!(
        claim_doc
            .evidence
            .shared_nodes
            .iter()
            .any(|node| node == "release train / release version")
    );
    assert!(
        claim_doc
            .evidence
            .shared_nodes
            .iter()
            .any(|node| node == "release train / status")
    );
}

async fn cleanup_docs(store: &Store, paths: &[&str]) {
    for path in paths {
        store.delete_document(path).await.expect("cleanup doc");
    }
}

struct RelatedBoundaryFixture {
    source: String,
    personal: String,
    legacy_personal: String,
    company: String,
    other_project: String,
    eval: String,
    generated: String,
}

impl RelatedBoundaryFixture {
    fn paths(&self) -> [&str; 7] {
        [
            &self.source,
            &self.personal,
            &self.legacy_personal,
            &self.company,
            &self.other_project,
            &self.eval,
            &self.generated,
        ]
    }
}

fn related_boundary_front(path: &str, project: &str, origin: &str) -> FrontMatter {
    let mut front = dummy_frontmatter(path);
    project.clone_into(&mut front.project);
    origin.clone_into(&mut front.origin);
    front
}

async fn related_content_paths(
    store: &Store,
    source_path: &str,
    exclude_origins: &[String],
    project: Option<&str>,
) -> Vec<String> {
    store
        .related_doc_content(source_path, 10, exclude_origins, project, None)
        .await
        .expect("related doc content")
        .into_iter()
        .map(|doc| doc.doc.source_path)
        .collect()
}

async fn recent_doc_paths(
    store: &Store,
    exclude_origins: &[String],
    project: Option<&str>,
) -> Vec<String> {
    store
        .recent_docs(20, exclude_origins, None, None, project)
        .await
        .expect("recent docs")
        .into_iter()
        .map(|doc| doc.source_path)
        .collect()
}

async fn relation_candidate_paths(store: &Store, source_path: &str) -> [Vec<String>; 4] {
    let graph = store
        .related_docs(source_path, 10)
        .await
        .expect("graph related docs");
    let claim = store
        .claim_related_docs(source_path, 10)
        .await
        .expect("claim related docs");
    let semantic = store
        .semantic_related_docs(source_path, 10, 0.2)
        .await
        .expect("semantic related docs");
    let recent_project = store
        .recent_project_docs(source_path, 10)
        .await
        .expect("recent project docs");
    [graph, claim, semantic, recent_project]
}

fn assert_no_internal_eval_fixture(paths: &[String], eval_path: &str) {
    assert!(
        !paths.iter().any(|path| path == eval_path),
        "internal eval fixtures must never feed relation projection candidates"
    );
}

fn assert_no_generated_brief(paths: &[String], generated_path: &str) {
    assert!(
        !paths.iter().any(|path| path == generated_path),
        "generated briefing notes must never feed memory candidate surfaces"
    );
}

fn assert_no_cross_origin_candidate(paths: &[String], company_path: &str) {
    assert!(
        !paths.iter().any(|path| path == company_path),
        "projection relation candidates must stay inside the source origin boundary"
    );
}

async fn prepare_related_boundary_fixture(store: &Store) -> RelatedBoundaryFixture {
    let fixture = RelatedBoundaryFixture {
        source: unique_path("related-boundary-source"),
        personal: unique_path("related-boundary-personal"),
        legacy_personal: unique_path("related-boundary-legacy-personal"),
        company: unique_path("related-boundary-company"),
        other_project: unique_path("related-boundary-other-project"),
        eval: unique_path("eval-related-boundary"),
        generated: unique_path("related-boundary-generated-brief"),
    };
    let source_front = related_boundary_front(&fixture.source, "related-alpha", "personal");
    let personal_front = related_boundary_front(&fixture.personal, "related-alpha", "personal");
    let legacy_personal_front =
        related_boundary_front(&fixture.legacy_personal, "related-alpha", "");
    let company_front = related_boundary_front(&fixture.company, "related-alpha", "company");
    let other_project_front =
        related_boundary_front(&fixture.other_project, "related-beta", "personal");
    let eval_front = related_boundary_front(&fixture.eval, "related-alpha", "personal");
    let mut generated_front =
        related_boundary_front(&fixture.generated, "related-alpha", "personal");
    generated_front.tags.push("daily-brief".to_owned());

    upsert_test_docs(
        store,
        &[
            &source_front,
            &personal_front,
            &legacy_personal_front,
            &company_front,
            &other_project_front,
            &eval_front,
            &generated_front,
        ],
        "sha-related-boundary",
    )
    .await;

    let relation_tag = fixture
        .source
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect::<String>();
    let tool = format!("tool{relation_tag}");
    let concept = format!("concept{relation_tag}");
    store.upsert_tool(&tool, &tool).await.expect("upsert tool");
    store
        .upsert_concept(&concept, &concept)
        .await
        .expect("upsert concept");

    for path in fixture.paths() {
        store.relate_doc_tool(path, &tool).await.expect("tool edge");
        store
            .relate_doc_concept(path, &concept)
            .await
            .expect("concept edge");
    }
    let claim = Claim {
        subject: relation_tag,
        predicate: "boundary".to_owned(),
        value: "shared".to_owned(),
        kind: "fact".to_owned(),
        confidence: "certain".to_owned(),
    };
    for path in fixture.paths() {
        store
            .upsert_claim_node(path, "related-alpha", &claim)
            .await
            .expect("claim edge");
    }
    for front in [
        &source_front,
        &personal_front,
        &legacy_personal_front,
        &company_front,
        &other_project_front,
        &eval_front,
        &generated_front,
    ] {
        let content = if front.source_path.as_str() == generated_front.source_path.as_str() {
            "generatedbriefingsentinel".to_owned()
        } else {
            format!("content for {}", front.source_path)
        };
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content,
                embedding: vec![0.1; 1024],
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }
    fixture
}

/// Claim-axis continuity is its own relation lane. A doc that shares multiple
/// claims should be visible via `claim_related_docs`, but it must not consume the
/// exact graph lane or GraphRAG content lane reserved for tool/concept overlap.
#[tokio::test]
async fn claim_axis_does_not_leak_into_graph_related_lanes() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let source_path = unique_path("relation-lane-source");
    let claim_only_path = unique_path("relation-lane-claim-only");
    let graph_path = unique_path("relation-lane-graph");
    let source_front = dummy_frontmatter(&source_path);
    let claim_front = dummy_frontmatter(&claim_only_path);
    let graph_front = dummy_frontmatter(&graph_path);

    upsert_test_docs(
        &store,
        &[&source_front, &claim_front, &graph_front],
        "sha-relation-lanes",
    )
    .await;
    add_relation_lane_claim_edges(&store, &source_path, &claim_only_path).await;
    add_relation_lane_graph_edges(&store, &source_path, &graph_path).await;
    add_relation_lane_chunks(&store, &claim_front, &graph_front).await;

    let claim_related = store
        .claim_related_docs(&source_path, 10)
        .await
        .expect("claim related docs");
    assert!(
        claim_related.contains(&claim_only_path),
        "shared claim axes should remain visible in the claim lane"
    );

    let graph_related = store
        .related_docs(&source_path, 10)
        .await
        .expect("graph related docs");
    assert!(
        graph_related.contains(&graph_path),
        "tool/concept overlap should remain visible in the graph lane"
    );
    assert!(
        !graph_related.contains(&claim_only_path),
        "claim-only overlap must not leak into the graph lane"
    );

    let graph_content_paths = store
        .related_doc_content(&source_path, 10, &[], None, None)
        .await
        .expect("related doc content")
        .into_iter()
        .map(|doc| doc.doc.source_path)
        .collect::<Vec<_>>();
    assert!(
        graph_content_paths.contains(&graph_path),
        "tool/concept overlap should feed GraphRAG content"
    );
    assert!(
        !graph_content_paths.contains(&claim_only_path),
        "claim-only overlap must not duplicate current-claim authority as GraphRAG content"
    );

    assert_claim_lane_content(&store, &source_path, &claim_only_path).await;

    let graph_content = store
        .related_doc_content(&source_path, 10, &[], None, None)
        .await
        .expect("related doc content with evidence");
    let graph_doc = graph_content
        .iter()
        .find(|doc| doc.doc.source_path == graph_path)
        .expect("graph related doc");
    assert_eq!(graph_doc.evidence.shared_count, 2);
    assert_eq!(
        usize::try_from(graph_doc.evidence.shared_count),
        Ok(graph_doc.evidence.shared_nodes.len()),
        "graph relation count should match distinct displayed graph nodes"
    );
    assert!(
        graph_doc
            .evidence
            .shared_nodes
            .iter()
            .any(|node| node == "relation lane concept")
    );
    assert!(
        graph_doc
            .evidence
            .shared_nodes
            .iter()
            .any(|node| node == "relation lane tool")
    );

    cleanup_docs(&store, &[&source_path, &claim_only_path, &graph_path]).await;
}

#[tokio::test]
async fn related_doc_content_respects_origin_project_and_internal_fixture_boundaries() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let fixture = prepare_related_boundary_fixture(&store).await;

    let all_paths = related_content_paths(&store, &fixture.source, &[], None).await;
    assert!(all_paths.contains(&fixture.personal));
    assert!(
        all_paths.contains(&fixture.legacy_personal),
        "legacy empty-origin related content should stay inside the personal boundary"
    );
    assert_no_cross_origin_candidate(&all_paths, &fixture.company);
    assert!(all_paths.contains(&fixture.other_project));
    assert!(
        !all_paths.contains(&fixture.eval),
        "internal eval fixtures must never feed generated relation context"
    );
    assert_no_generated_brief(&all_paths, &fixture.generated);

    let filtered_paths = related_content_paths(
        &store,
        &fixture.source,
        &["company".to_owned()],
        Some("related-alpha"),
    )
    .await;
    assert!(filtered_paths.contains(&fixture.personal));
    assert!(
        filtered_paths.contains(&fixture.legacy_personal),
        "legacy empty-origin related content should survive company exclusion"
    );
    assert!(!filtered_paths.contains(&fixture.company));
    assert!(!filtered_paths.contains(&fixture.other_project));
    assert!(!filtered_paths.contains(&fixture.eval));
    assert_no_generated_brief(&filtered_paths, &fixture.generated);

    let recent_paths = recent_doc_paths(&store, &[], Some("related-alpha")).await;
    assert_no_internal_eval_fixture(&recent_paths, &fixture.eval);
    assert_no_generated_brief(&recent_paths, &fixture.generated);

    let recent_personal_excluded =
        recent_doc_paths(&store, &["personal".to_owned()], Some("related-alpha")).await;
    assert!(
        !recent_personal_excluded.contains(&fixture.personal),
        "personal recent docs should be excluded"
    );
    assert!(
        !recent_personal_excluded.contains(&fixture.legacy_personal),
        "legacy empty-origin recent docs must not bypass personal exclusion"
    );
    assert!(
        recent_personal_excluded.contains(&fixture.company),
        "company recent docs should remain when only personal is excluded"
    );

    for paths in relation_candidate_paths(&store, &fixture.source).await {
        assert!(
            paths.contains(&fixture.personal),
            "same-origin relation candidates should remain eligible"
        );
        assert!(
            paths.contains(&fixture.legacy_personal),
            "legacy empty-origin candidates should be treated as personal"
        );
        assert_no_cross_origin_candidate(&paths, &fixture.company);
        assert_no_internal_eval_fixture(&paths, &fixture.eval);
        assert_no_generated_brief(&paths, &fixture.generated);
    }

    let query_embedding = vec![0.1_f32; 1024];
    let vector_paths = store
        .vector_search_filtered(&query_embedding, 20, &[], Some("related-alpha"), None)
        .await
        .expect("filtered vector search")
        .into_iter()
        .map(|hit| hit.source_path)
        .collect::<Vec<_>>();
    assert!(vector_paths.contains(&fixture.source));
    assert_no_generated_brief(&vector_paths, &fixture.generated);

    let text_paths = store
        .text_search_filtered(
            "generatedbriefingsentinel",
            20,
            &[],
            Some("related-alpha"),
            None,
        )
        .await
        .expect("filtered text search")
        .into_iter()
        .map(|hit| hit.source_path)
        .collect::<Vec<_>>();
    assert_no_generated_brief(&text_paths, &fixture.generated);

    let cleanup_paths = fixture.paths();
    cleanup_docs(&store, &cleanup_paths).await;
}

#[tokio::test]
async fn graph_entry_search_excludes_internal_eval_fixture_top_hits() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let source_path = unique_path("graph-entry-source");
    let eval_path = unique_path("eval-graph-entry");
    let source_front = dummy_frontmatter(&source_path);
    let eval_front = dummy_frontmatter(&eval_path);
    let sentinel = "graphevalboundary";

    let mut query = vec![0.0_f32; 1024];
    query[1023] = 1.0;
    let mut source_embedding = vec![0.0_f32; 1024];
    source_embedding[1023] = 0.9;
    source_embedding[0] = 0.1;
    let mut eval_embedding = vec![0.0_f32; 1024];
    eval_embedding[1023] = 1.0;

    for (front, content, embedding) in [
        (
            &source_front,
            "graphevalboundary source memory",
            source_embedding,
        ),
        (
            &eval_front,
            "graphevalboundary eval fixture",
            eval_embedding,
        ),
    ] {
        store
            .upsert_document(front, "sha-graph-entry-eval", SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: content.to_owned(),
                embedding,
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }

    let vector_hit = store
        .vector_search(&query, 1)
        .await
        .expect("graph entry vector search")
        .pop()
        .expect("source hit");
    assert_eq!(
        vector_hit.source_path, source_path,
        "graph entry search must not anchor neighbors on internal eval fixtures"
    );

    let text_paths = store
        .text_search(sentinel, 10)
        .await
        .expect("graph entry text search")
        .into_iter()
        .map(|hit| hit.source_path)
        .collect::<Vec<_>>();
    assert!(text_paths.contains(&source_path));
    assert_no_internal_eval_fixture(&text_paths, &eval_path);

    cleanup_docs(&store, &[&source_path, &eval_path]).await;
}

#[tokio::test]
async fn search_filters_excluded_origins_before_limit() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let project = format!(
        "search-origin-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let company_path = unique_path("search-origin-company");
    let personal_path = unique_path("search-origin-personal");
    let mut company_front = dummy_frontmatter(&company_path);
    company_front.origin = "company".to_owned();
    company_front.project.clone_from(&project);
    let mut personal_front = dummy_frontmatter(&personal_path);
    personal_front.origin = "personal".to_owned();
    personal_front.project.clone_from(&project);

    let mut company_embedding = vec![0.0_f32; 1024];
    company_embedding[0] = 1.0;
    let mut personal_embedding = vec![0.0_f32; 1024];
    personal_embedding[0] = 0.8;
    personal_embedding[1] = 0.2;

    for (front, content, embedding) in [
        (
            &company_front,
            "originboundarysentinel company closest match",
            company_embedding,
        ),
        (
            &personal_front,
            "originboundarysentinel personal allowed match",
            personal_embedding,
        ),
    ] {
        store
            .upsert_document(front, "sha-search-origin", SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: content.to_owned(),
                embedding,
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }

    let excluded_origins = vec!["company".to_owned()];
    let mut query_embedding = vec![0.0_f32; 1024];
    query_embedding[0] = 1.0;
    let vector_hit = store
        .vector_search_filtered(&query_embedding, 1, &excluded_origins, Some(&project), None)
        .await
        .expect("vector search")
        .pop()
        .expect("allowed vector hit");
    assert_eq!(vector_hit.source_path, personal_path);

    let text_hit = store
        .text_search_filtered(
            "originboundarysentinel",
            1,
            &excluded_origins,
            Some(&project),
            None,
        )
        .await
        .expect("text search")
        .pop()
        .expect("allowed text hit");
    assert_eq!(text_hit.source_path, personal_path);

    cleanup_docs(&store, &[&company_path, &personal_path]).await;
}

#[tokio::test]
async fn search_treats_missing_origin_as_personal_for_exclusion() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let project = format!(
        "search-legacy-origin-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let legacy_path = unique_path("search-legacy-empty-origin");
    let company_path = unique_path("search-legacy-company");
    let mut legacy_front = dummy_frontmatter(&legacy_path);
    legacy_front.origin.clear();
    legacy_front.project.clone_from(&project);
    let mut company_front = dummy_frontmatter(&company_path);
    company_front.origin = "company".to_owned();
    company_front.project.clone_from(&project);

    let mut legacy_embedding = vec![0.0_f32; 1024];
    legacy_embedding[0] = 1.0;
    let mut company_embedding = vec![0.0_f32; 1024];
    company_embedding[0] = 0.9;
    company_embedding[1] = 0.1;

    for (front, content, embedding) in [
        (
            &legacy_front,
            "originmissingpersonal originmissingpersonal originmissingpersonal",
            legacy_embedding,
        ),
        (
            &company_front,
            "originmissingpersonal company survivor",
            company_embedding,
        ),
    ] {
        store
            .upsert_document(front, "sha-search-legacy-origin", SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: content.to_owned(),
                embedding,
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }

    let excluded_origins = vec!["personal".to_owned()];
    let mut query_embedding = vec![0.0_f32; 1024];
    query_embedding[0] = 1.0;
    let vector_hit = store
        .vector_search_filtered(&query_embedding, 1, &excluded_origins, Some(&project), None)
        .await
        .expect("vector search")
        .pop()
        .expect("company vector hit");
    assert_eq!(
        vector_hit.source_path, company_path,
        "legacy empty-origin vector hit must not bypass personal exclusion"
    );

    let text_hit = store
        .text_search_filtered(
            "originmissingpersonal",
            1,
            &excluded_origins,
            Some(&project),
            None,
        )
        .await
        .expect("text search")
        .pop()
        .expect("company text hit");
    assert_eq!(
        text_hit.source_path, company_path,
        "legacy empty-origin text hit must not bypass personal exclusion"
    );

    cleanup_docs(&store, &[&legacy_path, &company_path]).await;
}

/// nearest_document returns the closest document only when within the distance threshold.
#[tokio::test]
async fn nearest_document_respects_threshold() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let a_path = unique_path("nearest-a");
    let b_path = unique_path("nearest-b");
    let generated_path = unique_path("nearest-generated-brief");
    let eval_path = unique_path("eval-nearest-candidate");
    let a_front = dummy_frontmatter(&a_path);
    let b_front = dummy_frontmatter(&b_path);
    let mut generated_front = dummy_frontmatter(&generated_path);
    generated_front.tags.push("daily-brief".to_owned());
    let eval_front = dummy_frontmatter(&eval_path);
    upsert_test_docs(
        &store,
        &[&a_front, &b_front, &generated_front, &eval_front],
        "sha-nearest",
    )
    .await;

    let mut emb_a = [0.0_f32; 1024];
    emb_a[0] = 1.0;
    let mut emb_b = [0.0_f32; 1024];
    emb_b[1] = 1.0;
    let mut query_near_a = [0.0_f32; 1024];
    query_near_a[0] = 0.9;
    query_near_a[1] = 0.1;

    for (front, content, embedding) in [
        (&a_front, "A note", emb_a.to_vec()),
        (&b_front, "B note", emb_b.to_vec()),
        (
            &generated_front,
            "generated brief should not be an internal candidate",
            query_near_a.to_vec(),
        ),
        (
            &eval_front,
            "eval fixture should not be an internal candidate",
            query_near_a.to_vec(),
        ),
    ] {
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: content.to_owned(),
                embedding,
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("chunk nearest candidate");
    }

    // Query close to A → should return A.
    let near = store
        .nearest_document(&query_near_a, 0.2)
        .await
        .expect("nearest")
        .map(|(p, _)| p);
    assert_eq!(near, Some(a_path.clone()), "query near A should return A");
    let near_many = store
        .nearest_documents(&query_near_a, 1.0, 2)
        .await
        .expect("nearest many")
        .into_iter()
        .map(|(p, _)| p)
        .collect::<Vec<_>>();
    assert_eq!(
        near_many,
        vec![a_path.clone(), b_path.clone()],
        "multi-candidate search should preserve distance ordering after generated/eval filtering"
    );
    let near_limited = store
        .nearest_documents(&query_near_a, 1.0, 1)
        .await
        .expect("nearest limited");
    assert_eq!(near_limited.len(), 1, "limit should cap candidates");

    // Distant query with tight threshold → none.
    let far = store
        .nearest_document(&[0.5_f32; 1024], 0.01)
        .await
        .expect("nearest far");
    assert!(far.is_none(), "distant query below threshold returns none");

    cleanup_docs(&store, &[&a_path, &b_path, &generated_path, &eval_path]).await;
}

#[tokio::test]
async fn duplicate_boundary_nearest_filters_candidates_before_limit() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let project = format!(
        "duplicate-boundary-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let company_path = unique_path("duplicate-boundary-company");
    let other_project_path = unique_path("duplicate-boundary-other-project");
    let generated_path = unique_path("duplicate-boundary-generated");
    let eval_path = unique_path("eval-duplicate-boundary");
    let empty_project_path = unique_path("duplicate-boundary-empty-project");
    let same_project_path = unique_path("duplicate-boundary-same-project");

    let mut company_front = dummy_frontmatter(&company_path);
    company_front.origin = "company".to_owned();
    company_front.project.clone_from(&project);
    let mut other_project_front = dummy_frontmatter(&other_project_path);
    other_project_front.project = format!("{project}-other");
    let mut generated_front = dummy_frontmatter(&generated_path);
    generated_front.project.clone_from(&project);
    generated_front.tags.push("daily-brief".to_owned());
    let mut eval_front = dummy_frontmatter(&eval_path);
    eval_front.project.clone_from(&project);
    let mut empty_project_front = dummy_frontmatter(&empty_project_path);
    empty_project_front.project.clear();
    let mut same_project_front = dummy_frontmatter(&same_project_path);
    same_project_front.project.clone_from(&project);

    let mut query = vec![0.0_f32; 1024];
    query[0] = 1.0;
    let mut company_embedding = vec![0.0_f32; 1024];
    company_embedding[0] = 1.0;
    let mut other_project_embedding = vec![0.0_f32; 1024];
    other_project_embedding[0] = 0.99;
    other_project_embedding[1] = 0.01;
    let mut generated_embedding = vec![0.0_f32; 1024];
    generated_embedding[0] = 0.98;
    generated_embedding[1] = 0.02;
    let mut eval_embedding = vec![0.0_f32; 1024];
    eval_embedding[0] = 0.975;
    eval_embedding[1] = 0.025;
    let mut empty_project_embedding = vec![0.0_f32; 1024];
    empty_project_embedding[0] = 0.97;
    empty_project_embedding[1] = 0.03;
    let mut same_project_embedding = vec![0.0_f32; 1024];
    same_project_embedding[0] = 0.96;
    same_project_embedding[1] = 0.04;

    for (front, embedding) in [
        (&company_front, company_embedding),
        (&other_project_front, other_project_embedding),
        (&generated_front, generated_embedding),
        (&eval_front, eval_embedding),
        (&empty_project_front, empty_project_embedding),
        (&same_project_front, same_project_embedding),
    ] {
        store
            .upsert_document(front, "sha-duplicate-boundary", SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: "duplicate boundary candidate".to_owned(),
                embedding,
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }

    let paths = store
        .nearest_documents_for_duplicate_boundary(&query, 1.0, 2, "personal", Some(&project))
        .await
        .expect("nearest duplicate candidates")
        .into_iter()
        .map(|(path, _)| path)
        .collect::<Vec<_>>();

    assert_eq!(
        paths,
        vec![empty_project_path.clone(), same_project_path.clone()],
        "incompatible origin/project/generated/eval candidates must not consume the duplicate candidate limit"
    );

    cleanup_docs(
        &store,
        &[
            &company_path,
            &other_project_path,
            &generated_path,
            &eval_path,
            &empty_project_path,
            &same_project_path,
        ],
    )
    .await;
}

#[tokio::test]
async fn duplicate_boundary_treats_missing_origin_as_personal() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let project = format!(
        "duplicate-legacy-origin-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let legacy_path = unique_path("duplicate-legacy-empty-origin");
    let company_path = unique_path("duplicate-legacy-company");
    let mut legacy_front = dummy_frontmatter(&legacy_path);
    legacy_front.origin.clear();
    legacy_front.project.clone_from(&project);
    let mut company_front = dummy_frontmatter(&company_path);
    company_front.origin = "company".to_owned();
    company_front.project.clone_from(&project);

    let mut query = vec![0.0_f32; 1024];
    query[0] = 1.0;
    let mut legacy_embedding = vec![0.0_f32; 1024];
    legacy_embedding[0] = 0.95;
    legacy_embedding[1] = 0.05;
    let mut company_embedding = vec![0.0_f32; 1024];
    company_embedding[0] = 1.0;

    for (front, embedding) in [
        (&legacy_front, legacy_embedding),
        (&company_front, company_embedding),
    ] {
        store
            .upsert_document(front, "sha-duplicate-legacy-origin", SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: "duplicate legacy origin candidate".to_owned(),
                embedding,
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }

    let paths = store
        .nearest_documents_for_duplicate_boundary(&query, 1.0, 1, "personal", Some(&project))
        .await
        .expect("nearest duplicate candidates")
        .into_iter()
        .map(|(path, _)| path)
        .collect::<Vec<_>>();

    assert_eq!(
        paths,
        vec![legacy_path.clone()],
        "legacy empty-origin notes should remain eligible personal duplicate candidates"
    );

    cleanup_docs(&store, &[&legacy_path, &company_path]).await;
}

/// Semantic projection candidates must be corroborated by project or deterministic graph evidence.
#[tokio::test]
async fn semantic_related_docs_requires_project_or_graph_evidence() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let source_path = unique_path("semantic-related-source");
    let same_project_path = unique_path("semantic-related-same-project");
    let cross_project_path = unique_path("semantic-related-cross-project");
    let graph_path = unique_path("semantic-related-graph");

    let mut source_front = dummy_frontmatter(&source_path);
    source_front.project = "semantic-alpha".to_owned();
    let mut same_project_front = dummy_frontmatter(&same_project_path);
    same_project_front.project = "semantic-alpha".to_owned();
    let mut cross_project_front = dummy_frontmatter(&cross_project_path);
    cross_project_front.project = "semantic-beta".to_owned();
    let mut graph_front = dummy_frontmatter(&graph_path);
    graph_front.project = "semantic-gamma".to_owned();

    for front in [
        &source_front,
        &same_project_front,
        &cross_project_front,
        &graph_front,
    ] {
        store
            .upsert_document(front, "sha-semantic-related", SystemTime::now())
            .await
            .expect("upsert doc");
    }

    for (front, offset, body) in [
        (&source_front, 0.0_f32, "source"),
        (&same_project_front, 0.01_f32, "same project"),
        (
            &cross_project_front,
            0.02_f32,
            "cross project embedding only",
        ),
        (&graph_front, 0.03_f32, "cross project graph evidence"),
    ] {
        let mut embedding = [0.0_f32; 1024];
        embedding[0] = 1.0 - offset;
        embedding[1] = offset;
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: body.to_owned(),
                embedding: embedding.to_vec(),
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }

    store
        .upsert_tool("semanticgate", "semantic gate")
        .await
        .expect("upsert tool");
    store
        .relate_doc_tool(&source_path, "semanticgate")
        .await
        .expect("source tool edge");
    store
        .relate_doc_tool(&graph_path, "semanticgate")
        .await
        .expect("graph tool edge");

    let related = store
        .semantic_related_docs(&source_path, 10, 0.2)
        .await
        .expect("semantic related docs");

    assert!(
        related.contains(&same_project_path),
        "same-project semantic neighbors should remain eligible"
    );
    assert!(
        related.contains(&graph_path),
        "cross-project semantic neighbors need graph corroboration"
    );
    assert!(
        !related.contains(&cross_project_path),
        "embedding-only cross-project neighbors should not become relates_to links"
    );

    for path in [
        &source_path,
        &same_project_path,
        &cross_project_path,
        &graph_path,
    ] {
        store.delete_document(path).await.expect("cleanup doc");
    }
}

/// Claims can carry kind/confidence and be filtered by kind.
#[tokio::test]
async fn claim_kind_and_confidence_round_trip() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-kind");
    let mut front = dummy_frontmatter(&path);
    front.project = "omb".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            "omb",
            "release-version",
            "0.2.0",
            &path,
            SystemTime::now(),
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert decision claim");
    store
        .upsert_claim(
            "omb",
            "auth-flow",
            "unverified",
            &path,
            SystemTime::now(),
            &emb,
            "risk",
            "likely",
        )
        .await
        .expect("upsert risk claim");

    let decisions = store
        .recent_claims(10, Some("omb"), Some(&["decision".to_owned()]), &[])
        .await
        .expect("recent decisions");
    assert_eq!(decisions.len(), 1);
    assert_eq!(decisions[0].kind(), "decision");
    assert_eq!(decisions[0].confidence(), "certain");
    let decision_records = store
        .recent_claim_records(10, Some("omb"), Some(&["decision".to_owned()]), &[])
        .await
        .expect("recent decision records");
    assert_eq!(decision_records.len(), 1);
    assert_eq!(decision_records[0].source_path.as_str(), path.as_str());
    assert_eq!(decision_records[0].claim.predicate, "release version");

    let risks = store
        .recent_claims(
            10,
            Some("omb"),
            Some(&[
                "risk".to_owned(),
                "assumption".to_owned(),
                "blocked".to_owned(),
            ]),
            &[],
        )
        .await
        .expect("recent risks");
    assert_eq!(risks.len(), 1);
    assert_eq!(risks[0].kind(), "risk");

    store.delete_document(&path).await.expect("cleanup");
}

/// `next` claims are stored and filterable alongside blockers.
#[tokio::test]
async fn next_claim_is_recallable() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-next");
    let mut front = dummy_frontmatter(&path);
    front.project = "omb".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            "omb",
            "follow-up",
            "add next_actions endpoint",
            &path,
            SystemTime::now(),
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("upsert next claim");

    let nexts = store
        .recent_claims(
            10,
            Some("omb"),
            Some(&["next".to_owned(), "blocked".to_owned()]),
            &[],
        )
        .await
        .expect("recent next actions");
    assert_eq!(nexts.len(), 1);
    assert_eq!(nexts[0].kind(), "next");
    // Predicates are stored in canonical claim-key form (punctuation → spaces).
    assert_eq!(nexts[0].predicate, "follow up");

    store.delete_document(&path).await.expect("cleanup");
}

/// Stalled backlog should respect the requested action kinds; old decisions stay in the decision register.
#[tokio::test]
async fn stalled_claims_honor_requested_kinds() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-stalled");
    let project = format!(
        "stalled-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let mut front = dummy_frontmatter(&path);
    front.project = project.clone();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let older = SystemTime::now()
        .checked_sub(Duration::from_hours(192))
        .expect("valid older timestamp");
    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            &project,
            "follow-up",
            "ship release checklist",
            &path,
            older,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("upsert next claim");
    store
        .upsert_claim(
            &project,
            "release-decision",
            "keep stable wiki ids",
            &path,
            older,
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert decision claim");

    let stalled = store
        .stalled_claims(
            10,
            Some(&project),
            Some(&["next".to_owned(), "blocked".to_owned()]),
            &[],
            7,
        )
        .await
        .expect("stalled claims");
    assert_eq!(stalled.len(), 1);
    assert_eq!(stalled[0].kind(), "next");
    // Predicates are stored in canonical claim-key form (punctuation → spaces).
    assert_eq!(stalled[0].predicate, "follow up");

    store.delete_document(&path).await.expect("cleanup");
}

/// k-hop graph traversal reaches documents that are not directly connected.
#[tokio::test]
async fn related_docs_khop_reaches_second_hop() {
    let Some(dsn) = test_dsn() else {
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path_a = unique_path("khop-a");
    let path_b = unique_path("khop-b");
    let path_c = unique_path("khop-c");

    let fronts = [
        dummy_frontmatter(&path_a),
        dummy_frontmatter(&path_b),
        dummy_frontmatter(&path_c),
    ];
    upsert_test_docs(&store, &fronts.iter().collect::<Vec<_>>(), "khop-sha").await;
    for (front, content) in [
        (&fronts[0], "doc a content"),
        (&fronts[1], "doc b content"),
        (&fronts[2], "doc c content"),
    ] {
        store
            .upsert_chunk(&Doc {
                id: format!("{}#0", front.source_path),
                content: content.to_owned(),
                embedding: vec![0.1; 1024],
                front: front.clone(),
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
    }

    // A ↔ B share two concepts; B ↔ C share two different concepts.
    for slug in ["khop-concept-a1", "khop-concept-a2"] {
        store.upsert_concept(slug, slug).await.expect("concept");
        store
            .relate_doc_concept(&path_a, slug)
            .await
            .expect("a concept");
        store
            .relate_doc_concept(&path_b, slug)
            .await
            .expect("b concept");
    }
    for slug in ["khop-concept-b1", "khop-concept-b2"] {
        store.upsert_concept(slug, slug).await.expect("concept");
        store
            .relate_doc_concept(&path_b, slug)
            .await
            .expect("b concept");
        store
            .relate_doc_concept(&path_c, slug)
            .await
            .expect("c concept");
    }

    let one_hop = store
        .related_docs_khop(&path_a, 1, 10)
        .await
        .expect("1-hop");
    assert!(
        one_hop.contains(&path_b),
        "1-hop should include direct neighbor B"
    );
    assert!(
        !one_hop.contains(&path_c),
        "1-hop should not include two-hop neighbor C"
    );

    let two_hop = store
        .related_docs_khop(&path_a, 2, 10)
        .await
        .expect("2-hop");
    assert!(
        two_hop.contains(&path_c),
        "2-hop should reach transitive neighbor C"
    );

    cleanup_docs(&store, &[&path_a, &path_b, &path_c]).await;
}

/// Graph rerank features correctly reflect shared tools/concepts and degree.
#[tokio::test]
async fn graph_rerank_features_detect_linked_candidates() {
    let Some(dsn) = test_dsn() else {
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path_top = unique_path("rr-top");
    let path_linked = unique_path("rr-linked");
    let path_unlinked = unique_path("rr-unlinked");

    let fronts = [
        dummy_frontmatter(&path_top),
        dummy_frontmatter(&path_linked),
        dummy_frontmatter(&path_unlinked),
    ];
    upsert_test_docs(&store, &fronts.iter().collect::<Vec<_>>(), "rr-sha").await;

    store
        .upsert_tool("rr-tool", "shared tool")
        .await
        .expect("tool");
    store
        .relate_doc_tool(&path_top, "rr-tool")
        .await
        .expect("top tool");
    store
        .relate_doc_tool(&path_linked, "rr-tool")
        .await
        .expect("linked tool");
    // unlinked gets a private concept to give it non-zero degree without sharing with top
    store
        .upsert_concept("rr-private", "private concept")
        .await
        .expect("concept");
    store
        .relate_doc_concept(&path_unlinked, "rr-private")
        .await
        .expect("unlinked concept");

    let top_for_features = Hit {
        id: path_top.clone(),
        source_path: path_top.clone(),
        origin: "personal".to_owned(),
        project: "test".to_owned(),
        content: "top".to_owned(),
        dist: 0.0,
        score: 1.0,
    };
    let candidates = vec![
        Hit {
            id: path_top.clone(),
            source_path: path_top.clone(),
            origin: "personal".to_owned(),
            project: "test".to_owned(),
            content: "top".to_owned(),
            dist: 0.0,
            score: 1.0,
        },
        Hit {
            id: path_linked.clone(),
            source_path: path_linked.clone(),
            origin: "personal".to_owned(),
            project: "test".to_owned(),
            content: "linked".to_owned(),
            dist: 0.0,
            score: 0.5,
        },
        Hit {
            id: path_unlinked.clone(),
            source_path: path_unlinked.clone(),
            origin: "personal".to_owned(),
            project: "test".to_owned(),
            content: "unlinked".to_owned(),
            dist: 0.0,
            score: 0.5,
        },
    ];

    let features = store
        .graph_rerank_features(&top_for_features, &candidates)
        .await
        .expect("features");
    assert_eq!(features.len(), 3);
    assert!(
        features[1].shared_tools >= 1,
        "linked candidate should share a tool with top"
    );
    assert_eq!(
        features[2].shared_tools, 0,
        "unlinked candidate should share no tools with top"
    );
    assert!(
        features[1].degree >= 1 && features[2].degree >= 1,
        "all candidates should have non-zero graph degree"
    );

    // Verify the rerank function actually boosts the linked candidate above the unlinked one.
    let reranked = drudge::retrieve::rerank_by_graph(candidates, &features, 1.0);
    assert_eq!(reranked[0].source_path, path_top);
    assert_eq!(reranked[1].source_path, path_linked);
    assert_eq!(reranked[2].source_path, path_unlinked);

    cleanup_docs(&store, &[&path_top, &path_linked, &path_unlinked]).await;
}

// ── Code-note lane (remember_code ↔ re-index) ────────────────────────────────
//
// The code graph is replaced wholesale by every `code-index` pass (full refresh), but
// doc→code `code_uses` edges written via `remember_code` must survive the wipe, and the
// gc must reclaim only the edges whose symbols were renamed away.

fn test_code_symbol(name: &str, source_path: &str) -> CodeSymbol {
    CodeSymbol {
        source_path: source_path.to_owned(),
        name: name.to_owned(),
        kind: CodeSymbolKind::Function,
        language: CodeLanguage::Rust,
        start_line: 1,
        end_line: 3,
        parent: String::new(),
        signature: format!("fn {name}()"),
    }
}

async fn upsert_code_note_doc(store: &Store, note_path: &str) -> FrontMatter {
    let front = dummy_frontmatter(note_path);
    upsert_test_docs(store, &[&front], "sha-code-note").await;
    store
        .upsert_chunk(&Doc {
            id: format!("{note_path}#0"),
            content: "code note body preview".to_owned(),
            embedding: vec![0.1; 1024],
            front: front.clone(),
            chunk_idx: 0,
        })
        .await
        .expect("upsert code note chunk");
    front
}

async fn cleanup_code_rows(db: &Client, note_path: &str, symbol_files: &[String]) {
    let doc_node = format!("doc:{note_path}");
    db.execute("DELETE FROM edge WHERE src = $1;", &[&doc_node])
        .await
        .expect("cleanup doc-code edges");
    for file in symbol_files {
        let pattern = format!("code:%:{file}:%");
        db.execute(
            "DELETE FROM edge WHERE src LIKE $1 OR dst LIKE $1;",
            &[&pattern],
        )
        .await
        .expect("cleanup code edges");
        db.execute("DELETE FROM node WHERE id LIKE $1;", &[&pattern])
            .await
            .expect("cleanup code nodes");
    }
}

#[tokio::test]
async fn code_graph_refresh_preserves_doc_note_edges() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    // Clean slate: these tests assert table-wide code-row counts (tests run serially).
    store
        .clear_code_graph_preserving_doc_edges()
        .await
        .expect("initial clear");
    db.execute(
        "DELETE FROM edge WHERE kind = 'code_uses' AND src LIKE 'doc:%';",
        &[],
    )
    .await
    .expect("initial code_uses cleanup");

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let file_a = format!("src/it-code-note-a-{ts}.rs");
    let file_b = format!("src/it-code-note-b-{ts}.rs");
    let note_path = unique_path("code-note-refresh");
    upsert_code_note_doc(&store, &note_path).await;

    let sym_a = test_code_symbol("alpha_fn", &file_a);
    let sym_b = test_code_symbol("beta_fn", &file_b);
    store.upsert_code_symbol(&sym_a).await.expect("symbol a");
    store.upsert_code_symbol(&sym_b).await.expect("symbol b");
    store
        .upsert_code_relation(&CodeRelation {
            from: sym_a.clone(),
            to: sym_b.clone(),
            kind: CodeRelationKind::Calls,
        })
        .await
        .expect("code relation");
    store
        .upsert_doc_code_relation(&note_path, &sym_a, CodeRelationKind::Uses)
        .await
        .expect("doc code relation");

    // Full-refresh wipe: code nodes + code↔code edges go away, the note edge survives.
    store
        .clear_code_graph_preserving_doc_edges()
        .await
        .expect("clear code graph");
    let code_nodes: i64 = db
        .query_one("SELECT count(*) FROM node WHERE id LIKE 'code:%';", &[])
        .await
        .expect("count code nodes")
        .get(0);
    assert_eq!(code_nodes, 0, "refresh must remove all code symbol nodes");
    let code_edges: i64 = db
        .query_one(
            "SELECT count(*) FROM edge WHERE kind = 'code_calls' AND src LIKE 'code:%';",
            &[],
        )
        .await
        .expect("count code edges")
        .get(0);
    assert_eq!(code_edges, 0, "refresh must remove code↔code edges");
    let note_edges: i64 = db
        .query_one(
            "SELECT count(*) FROM edge WHERE kind = 'code_uses' AND src = $1;",
            &[&format!("doc:{note_path}")],
        )
        .await
        .expect("count note edges")
        .get(0);
    assert_eq!(note_edges, 1, "doc→code note edge must survive the refresh");

    // Re-index brings symbol A back (deterministic id); gc must keep its note edge.
    store.upsert_code_symbol(&sym_a).await.expect("re-upsert a");
    let reclaimed = store
        .gc_dangling_code_note_edges()
        .await
        .expect("gc after re-index");
    assert_eq!(reclaimed, 0, "edge to a live symbol must not be reclaimed");
    let note_edges: i64 = db
        .query_one(
            "SELECT count(*) FROM edge WHERE kind = 'code_uses' AND src = $1;",
            &[&format!("doc:{note_path}")],
        )
        .await
        .expect("recount note edges")
        .get(0);
    assert_eq!(note_edges, 1);

    cleanup_code_rows(&db, &note_path, &[file_a, file_b]).await;
    cleanup_docs(&store, &[&note_path]).await;
}

#[tokio::test]
async fn code_notes_for_symbols_surfaces_note_and_gc_reclaims_dangling() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    // Clean slate: the gc count below is table-wide (tests run serially).
    store
        .clear_code_graph_preserving_doc_edges()
        .await
        .expect("initial clear");
    db.execute(
        "DELETE FROM edge WHERE kind = 'code_uses' AND src LIKE 'doc:%';",
        &[],
    )
    .await
    .expect("initial code_uses cleanup");

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let file_a = format!("src/it-code-note-gc-{ts}.rs");
    let note_path = unique_path("code-note-gc");
    upsert_code_note_doc(&store, &note_path).await;

    let sym_a = test_code_symbol("gamma_fn", &file_a);
    store.upsert_code_symbol(&sym_a).await.expect("symbol a");
    store
        .upsert_doc_code_relation(&note_path, &sym_a, CodeRelationKind::Uses)
        .await
        .expect("doc code relation");

    // The linked note rides along with the matched symbol (title + chunk-0 snippet).
    let notes = store
        .code_notes_for_symbols(&[sym_a.node_id()])
        .await
        .expect("code notes for symbol");
    assert_eq!(notes.len(), 1);
    assert_eq!(notes[0].source_path, note_path);
    assert_eq!(notes[0].title, "test note");
    assert_eq!(notes[0].snippet, "code note body preview");
    assert_eq!(notes[0].symbol_node_id, sym_a.node_id());

    // Symbol renamed away (never re-upserted) → gc reclaims the dangling edge.
    store
        .clear_code_graph_preserving_doc_edges()
        .await
        .expect("clear code graph");
    let reclaimed = store
        .gc_dangling_code_note_edges()
        .await
        .expect("gc dangling");
    assert_eq!(reclaimed, 1, "edge to a vanished symbol must be reclaimed");
    let notes = store
        .code_notes_for_symbols(&[sym_a.node_id()])
        .await
        .expect("code notes after gc");
    assert!(
        notes.is_empty(),
        "reclaimed edge must stop surfacing the note"
    );

    cleanup_code_rows(&db, &note_path, &[file_a]).await;
    cleanup_docs(&store, &[&note_path]).await;
}

#[tokio::test]
async fn remember_code_stub_never_clobbers_indexed_signature() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    store
        .clear_code_graph_preserving_doc_edges()
        .await
        .expect("initial clear");

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let file_a = format!("src/it-code-stub-{ts}.rs");
    let indexed = test_code_symbol("real_fn", &file_a); // signature "fn real_fn()"

    // Index path writes the parsed signature…
    store.upsert_code_symbol(&indexed).await.expect("upsert");
    // …then remember_code links a note with a signature-less stub — it must NOT clobber.
    let mut stub = indexed.clone();
    stub.signature = String::new();
    store.ensure_code_symbol_stub(&stub).await.expect("stub");

    let outcome: String = db
        .query_one(
            "SELECT COALESCE(outcome, '') FROM node WHERE id = $1;",
            &[&indexed.node_id()],
        )
        .await
        .expect("read outcome")
        .get(0);
    assert_eq!(
        outcome,
        format!("fn {}()", indexed.name),
        "stub insert must preserve the indexed signature"
    );

    // Stub for a not-yet-indexed symbol is created empty…
    let mut missing = test_code_symbol("later_fn", &file_a);
    missing.signature = String::new(); // remember_code knows only path+name+kind
    store
        .ensure_code_symbol_stub(&missing)
        .await
        .expect("stub missing");
    let outcome: String = db
        .query_one(
            "SELECT COALESCE(outcome, '') FROM node WHERE id = $1;",
            &[&missing.node_id()],
        )
        .await
        .expect("read missing outcome")
        .get(0);
    assert_eq!(outcome, "", "fresh stub carries no signature");
    // …and the next index pass fills it in.
    let parsed = test_code_symbol("later_fn", &file_a);
    store
        .upsert_code_symbol(&parsed)
        .await
        .expect("index refill");
    let outcome: String = db
        .query_one(
            "SELECT COALESCE(outcome, '') FROM node WHERE id = $1;",
            &[&missing.node_id()],
        )
        .await
        .expect("read refilled outcome")
        .get(0);
    assert_eq!(outcome, format!("fn {}()", missing.name));

    cleanup_code_rows(&db, "/vault/wiki/unused.md", &[file_a]).await;
}

#[tokio::test]
async fn code_indexed_files_lists_distinct_graph_files() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let db = connect(&dsn).await;
    store
        .clear_code_graph_preserving_doc_edges()
        .await
        .expect("initial clear");

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let file_a = format!("src/it-files-a-{ts}.rs");
    let file_b = format!("src/it-files-b-{ts}.rs");
    assert!(
        !store
            .code_indexed_files()
            .await
            .expect("empty files")
            .contains(&file_a),
        "empty graph must not list the file"
    );

    store
        .upsert_code_symbol(&test_code_symbol("one_fn", &file_a))
        .await
        .expect("symbol a");
    store
        .upsert_code_symbol(&test_code_symbol("two_fn", &file_a))
        .await
        .expect("symbol a2");
    store
        .upsert_code_symbol(&test_code_symbol("three_fn", &file_b))
        .await
        .expect("symbol b");

    let files = store.code_indexed_files().await.expect("files");
    assert_eq!(files.len(), 2, "distinct files, not symbols");
    assert!(files.contains(&file_a) && files.contains(&file_b));

    cleanup_code_rows(&db, "/vault/wiki/unused.md", &[file_a, file_b]).await;
}
