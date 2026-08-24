//! Live PostgreSQL guardrails for the isolated code-index corpus.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

use drudge::code_index::{CodeIndexStore, sync_repository};
use drudge::config::{CodeIndexSource, CodeLanguage};
use tempfile::tempdir;
use tokio_postgres::{Client, NoTls};

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn unique_id(prefix: &str) -> String {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{timestamp}")
}

fn source(id: &str, root: &std::path::Path) -> CodeIndexSource {
    CodeIndexSource::new(
        id,
        format!("Test {id}"),
        root.to_path_buf(),
        CodeLanguage::Rust,
        true,
    )
    .expect("valid test source")
}

async fn connect(dsn: &str) -> Client {
    let (client, connection) = tokio_postgres::connect(dsn, NoTls)
        .await
        .expect("connect to test Postgres");
    tokio::spawn(connection);
    client
}

async fn initialized_store(dsn: &str) -> CodeIndexStore {
    let store = CodeIndexStore::connect(dsn).expect("connect code index");
    store.initialize().await.expect("initialize code index");
    store
}

async fn repository_counts(db: &Client, repository_id: &str) -> (i64, i64) {
    let row = db
        .query_one(
            "SELECT
                (SELECT count(*) FROM code_index.symbol WHERE repository_id = $1)::bigint,
                (SELECT count(*) FROM code_index.relation WHERE repository_id = $1)::bigint",
            &[&repository_id],
        )
        .await
        .unwrap();
    (row.get(0), row.get(1))
}

#[tokio::test]
async fn sha_skip_missing_root_and_prune_preserve_repository_ownership() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let first_root = tempdir().unwrap();
    let second_root = tempdir().unwrap();
    fs::write(first_root.path().join("lib.rs"), "pub fn first() {}\n").unwrap();
    fs::write(second_root.path().join("lib.rs"), "pub fn second() {}\n").unwrap();
    let first_id = unique_id("code-owner-a");
    let second_id = unique_id("code-owner-b");
    let first = source(&first_id, first_root.path());
    let second = source(&second_id, second_root.path());
    let mut store = initialized_store(&dsn).await;

    let initial = sync_repository(&mut store, &first)
        .await
        .expect("sync first");
    assert_eq!(initial.changed, 1);
    let unchanged = sync_repository(&mut store, &first)
        .await
        .expect("SHA skip first");
    assert_eq!(unchanged.changed, 0);
    assert_eq!(unchanged.unchanged, 1);
    sync_repository(&mut store, &second)
        .await
        .expect("sync second");

    let missing = source(&first_id, &first_root.path().join("missing"));
    assert!(sync_repository(&mut store, &missing).await.is_err());
    assert_eq!(store.status(Some(&first_id)).await.unwrap()[0].files, 1);

    fs::remove_file(first_root.path().join("lib.rs")).unwrap();
    let pruned = sync_repository(&mut store, &first)
        .await
        .expect("prune first");
    assert_eq!(pruned.deleted, 1);
    assert_eq!(store.status(Some(&first_id)).await.unwrap()[0].files, 0);
    assert_eq!(store.status(Some(&second_id)).await.unwrap()[0].files, 1);

    let db = connect(&dsn).await;
    db.execute(
        "DELETE FROM code_index.repository WHERE id = ANY($1)",
        &[&vec![first_id, second_id]],
    )
    .await
    .expect("clean up test repositories");
}

#[tokio::test]
async fn parser_status_and_relations_persist_in_the_isolated_schema() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let root = tempdir().unwrap();
    fs::write(
        root.path().join("lib.rs"),
        "use crate::Widget;\nstruct Widget;\nfn helper() {}\nfn run(_: Widget) { helper(); }\n",
    )
    .unwrap();
    fs::write(root.path().join("broken.rs"), "fn broken( {").unwrap();
    let repository_id = unique_id("code-parser");
    let configured = source(&repository_id, root.path());
    let mut store = initialized_store(&dsn).await;
    let report = sync_repository(&mut store, &configured)
        .await
        .expect("sync parser fixture");
    assert_eq!(report.scanned, 2);
    assert!(report.parse_errors > 0);

    let status = &store.status(Some(&repository_id)).await.unwrap()[0];
    assert_eq!(status.files, 2);
    assert_eq!(status.files_with_errors, 1);
    assert!(status.symbols >= 3);
    assert!(status.relations >= 4);
    let total_parse_errors = status.parse_errors;

    fs::write(
        root.path().join("lib.rs"),
        "use crate::Widget;\nstruct Widget;\nfn helper() {}\nfn run(_: Widget) { helper(); helper(); }\n",
    )
    .unwrap();
    let changed_valid_file = sync_repository(&mut store, &configured)
        .await
        .expect("resync with unchanged parse-error file");
    assert_eq!(changed_valid_file.changed, 1);
    assert_eq!(changed_valid_file.unchanged, 1);
    assert_eq!(changed_valid_file.parse_errors, total_parse_errors);

    let search_hits = store
        .search("run", Some(&repository_id))
        .await
        .expect("search indexed symbols");
    let run = search_hits
        .iter()
        .find(|hit| hit.name == "run")
        .expect("find run symbol");
    let detail = store
        .symbol(&run.id)
        .await
        .expect("read symbol detail")
        .expect("indexed symbol exists");
    assert!(detail.relations.iter().any(|relation| {
        relation.kind == "calls"
            && relation.target_name.as_deref() == Some("helper")
            && relation.target_symbol_id.is_none()
    }));

    let db = connect(&dsn).await;
    let syntactic_calls_with_target_ids: i64 = db
        .query_one(
            "SELECT count(*) FROM code_index.relation
             WHERE repository_id = $1 AND kind = 'calls'
               AND target_name = 'helper' AND target_symbol_id IS NOT NULL",
            &[&repository_id],
        )
        .await
        .expect("query resolved call")
        .get(0);
    assert_eq!(syntactic_calls_with_target_ids, 0);
    let public_memory_tables_touched: i64 = db
        .query_one(
            "SELECT count(*) FROM information_schema.tables
             WHERE table_schema = 'code_index' AND table_name IN ('document', 'chunk', 'node', 'edge', 'claim')",
            &[],
        )
        .await
        .expect("query schema separation")
        .get(0);
    assert_eq!(public_memory_tables_touched, 0);

    db.execute(
        "DELETE FROM code_index.repository WHERE id = $1",
        &[&repository_id],
    )
    .await
    .expect("clean up parser repository");
}

#[tokio::test]
async fn composite_foreign_keys_reject_cross_repository_ownership() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let first_root = tempdir().unwrap();
    let second_root = tempdir().unwrap();
    fs::write(first_root.path().join("lib.rs"), "fn first() {}\n").unwrap();
    fs::write(second_root.path().join("lib.rs"), "fn second() {}\n").unwrap();
    let first_id = unique_id("code-fk-a");
    let second_id = unique_id("code-fk-b");
    let mut store = initialized_store(&dsn).await;
    sync_repository(&mut store, &source(&first_id, first_root.path()))
        .await
        .unwrap();
    sync_repository(&mut store, &source(&second_id, second_root.path()))
        .await
        .unwrap();

    let db = connect(&dsn).await;
    let first_file: String = db
        .query_one(
            "SELECT id FROM code_index.file WHERE repository_id = $1",
            &[&first_id],
        )
        .await
        .unwrap()
        .get(0);
    let second_file: String = db
        .query_one(
            "SELECT id FROM code_index.file WHERE repository_id = $1",
            &[&second_id],
        )
        .await
        .unwrap()
        .get(0);
    let first_symbol: String = db
        .query_one(
            "SELECT id FROM code_index.symbol WHERE repository_id = $1",
            &[&first_id],
        )
        .await
        .unwrap()
        .get(0);
    let second_symbol: String = db
        .query_one(
            "SELECT id FROM code_index.symbol WHERE repository_id = $1",
            &[&second_id],
        )
        .await
        .unwrap()
        .get(0);
    let original_counts = repository_counts(&db, &first_id).await;

    let cross_file = db
        .execute(
            "INSERT INTO code_index.symbol
                (id, repository_id, file_id, kind, name, qualified_name,
                 start_byte, end_byte, start_line, end_line)
             VALUES ($1, $2, $3, 'function', 'cross', 'cross', 0, 1, 0, 0)",
            &[&unique_id("cross-file"), &first_id, &second_file],
        )
        .await;
    assert!(cross_file.is_err());

    let cross_source = db
        .execute(
            "INSERT INTO code_index.relation
                (id, repository_id, file_id, source_symbol_id, kind, target_name,
                 start_byte, end_byte)
             VALUES ($1, $2, $3, $4, 'calls', 'first', 0, 1)",
            &[
                &unique_id("cross-source"),
                &first_id,
                &first_file,
                &second_symbol,
            ],
        )
        .await;
    assert!(cross_source.is_err());

    let cross_target = db
        .execute(
            "INSERT INTO code_index.relation
                (id, repository_id, file_id, source_symbol_id, kind, target_symbol_id,
                 start_byte, end_byte)
             VALUES ($1, $2, $3, $4, 'contains', $5, 0, 1)",
            &[
                &unique_id("cross-target"),
                &first_id,
                &first_file,
                &first_symbol,
                &second_symbol,
            ],
        )
        .await;
    assert!(cross_target.is_err());

    let retained_counts = repository_counts(&db, &first_id).await;
    assert_eq!(retained_counts, original_counts);

    db.execute(
        "DELETE FROM code_index.repository WHERE id = ANY($1)",
        &[&vec![first_id, second_id]],
    )
    .await
    .unwrap();
}
