//! Data integrity torture tests — malformed input, oversized lists, sync idempotency.
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::config::BoringConfig;
use drudge::frontmatter::{FrontMatter, GENERATED_BRIEF_TAG};
use drudge::ingest::{Stats, ingest_file};
use drudge::llm::Llm;
use drudge::store::{Doc, Store};

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn unique_path(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("/tmp/{prefix}-{ts}")
}

fn dummy_frontmatter(path: &str) -> FrontMatter {
    FrontMatter {
        source_path: path.to_owned(),
        origin: "personal".to_owned(),
        project: "omb".to_owned(),
        title: Some("t".to_owned()),
        kind: "note".to_owned(),
        tags: vec![],
        ..Default::default()
    }
}

fn now() -> SystemTime {
    SystemTime::now()
}

fn emb() -> [f32; 1024] {
    [0.1_f32; 1024]
}

#[tokio::test]
async fn sync_is_idempotent() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    // Re-ingest the same document and claim twice; counts must stay stable.
    let path = unique_path("idempotent");
    let front = dummy_frontmatter(&path);
    store
        .upsert_document(&front, "sha-idem", now())
        .await
        .expect("upsert doc");

    for _ in 0..2 {
        store
            .upsert_claim(
                "omb",
                "idempotent-test",
                "v1",
                &path,
                now(),
                &emb(),
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }

    let claims = store
        .recent_claims(10, Some("omb"), Some(&["fact".to_owned()]), &[])
        .await
        .expect("claims")
        .into_iter()
        .filter(|c| c.predicate == "idempotent test")
        .count();
    assert_eq!(
        claims, 1,
        "duplicate upsert should keep a single current claim"
    );

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn oversized_claim_list_is_ingested_without_panic() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("oversized-claims");
    let front = dummy_frontmatter(&path);
    store
        .upsert_document(&front, "sha-big", now())
        .await
        .expect("upsert doc");

    for i in 0..75 {
        store
            .upsert_claim(
                "omb",
                &format!("claim-{i}"),
                &format!("value-{i}").repeat(20),
                &path,
                now(),
                &emb(),
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }

    let count = store
        .recent_claims(200, Some("omb"), Some(&["fact".to_owned()]), &[])
        .await
        .expect("claims")
        .into_iter()
        .filter(|c| c.subject == "omb" && c.predicate.starts_with("claim "))
        .count();
    assert_eq!(count, 75, "all oversized claims should be stored");

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn generated_brief_ingest_prunes_stale_db_artifact() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("daily-brief-2026-07-02.md");
    let path_str = path.to_string_lossy().to_string();
    std::fs::write(
        &path,
        format!(
            "---\ntitle: Daily Brief\norigin: personal\nproject: omb\ntags: [{GENERATED_BRIEF_TAG}]\n---\nloop contract summary"
        ),
    )
    .expect("write generated brief");

    let mut front = dummy_frontmatter(&path_str);
    front.tags.push(GENERATED_BRIEF_TAG.to_owned());
    store
        .upsert_document(&front, "stale-generated-brief", now())
        .await
        .expect("upsert stale doc");
    store
        .upsert_chunk(&Doc {
            id: format!("{path_str}#0"),
            content: "loop contract summary".to_owned(),
            embedding: emb().to_vec(),
            front,
            chunk_idx: 0,
        })
        .await
        .expect("upsert stale chunk");

    let cfg = BoringConfig::default();
    let llm = Llm::from_config(&cfg);
    let mut stats = Stats::default();
    let outcome = ingest_file(&store, &llm, &cfg, &path_str, &mut stats)
        .await
        .expect("ingest generated brief");

    assert!(matches!(outcome, drudge::ingest::FileOutcome::Skipped));
    assert_eq!(stats.skipped, 1);
    assert_eq!(stats.deleted, 1);
    assert!(
        store
            .get_doc_sha(&path_str)
            .await
            .expect("get doc sha")
            .is_none(),
        "generated brief must not remain as a DB-derived source artifact"
    );
}
