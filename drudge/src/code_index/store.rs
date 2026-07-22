use std::collections::HashMap;
use std::time::SystemTime;

use tokio_postgres::{Client, NoTls, Transaction};

use super::{
    CodeIndexError, CodeRelation, CodeSearchHit, CodeSymbolDetail, PreparedFile, RepositoryStatus,
    SyncReport, usize_to_i64,
};
use crate::config::CodeIndexSource;

/// PostgreSQL adapter dedicated to the `code_index` schema. It never reads or writes the memory
/// corpus (`document`, `chunk`, `node`, `edge`, `claim`).
pub struct CodeIndexStore {
    db: Client,
}

impl CodeIndexStore {
    /// Connect without creating or migrating schema. Read-only commands use this path.
    pub async fn connect(dsn: &str) -> Result<Self, CodeIndexError> {
        let (client, connection) = tokio_postgres::connect(dsn, NoTls).await?;
        tokio::spawn(async move {
            if let Err(error) = connection.await {
                eprintln!("code index postgres connection error: {error}");
            }
        });
        Ok(Self { db: client })
    }

    /// Initialize the isolated code-index schema immediately before a selected sync writes it.
    pub async fn initialize(&self) -> Result<(), CodeIndexError> {
        self.db.batch_execute(SCHEMA).await?;
        Ok(())
    }

    pub async fn existing_hashes(
        &self,
        repository_id: &str,
    ) -> Result<HashMap<String, String>, CodeIndexError> {
        let rows = self
            .db
            .query(
                "SELECT relative_path, sha256 FROM code_index.file WHERE repository_id = $1",
                &[&repository_id],
            )
            .await?;
        Ok(rows
            .into_iter()
            .map(|row| (row.get(0), row.get(1)))
            .collect())
    }

    pub(super) async fn replace_repository(
        &mut self,
        source: &CodeIndexSource,
        seen_paths: &[String],
        changed: &[PreparedFile],
    ) -> Result<SyncReport, CodeIndexError> {
        let root_path = source
            .root()
            .to_str()
            .ok_or_else(|| CodeIndexError::NonUtf8Path(source.root().to_path_buf()))?;
        let transaction = self.db.transaction().await?;
        transaction
            .execute(
                "INSERT INTO code_index.repository (id, name, root_path, language, last_synced_at)
                 VALUES ($1, $2, $3, $4, now())
                 ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    root_path = EXCLUDED.root_path,
                    language = EXCLUDED.language,
                    last_synced_at = EXCLUDED.last_synced_at",
                &[
                    &source.id(),
                    &source.name(),
                    &root_path,
                    &source.language().as_str(),
                ],
            )
            .await?;

        for file in changed {
            replace_file(&transaction, source.id(), file).await?;
        }

        let deleted = transaction
            .execute(
                "DELETE FROM code_index.file
                 WHERE repository_id = $1 AND NOT (relative_path = ANY($2))",
                &[&source.id(), &seen_paths],
            )
            .await?;

        let parse_errors = transaction
            .query_one(
                "SELECT coalesce(sum(error_count), 0)::bigint
                 FROM code_index.file
                 WHERE repository_id = $1",
                &[&source.id()],
            )
            .await?
            .get::<_, i64>(0);
        transaction.commit().await?;

        Ok(SyncReport {
            repository_id: source.id().to_owned(),
            scanned: seen_paths.len(),
            changed: changed.len(),
            unchanged: seen_paths.len() - changed.len(),
            deleted: usize::try_from(deleted).map_err(|_| CodeIndexError::NumericOverflow)?,
            parse_errors: count_to_usize(parse_errors)?,
        })
    }

    pub async fn status(
        &self,
        repository_id: Option<&str>,
    ) -> Result<Vec<RepositoryStatus>, CodeIndexError> {
        let rows = self
            .db
            .query(
                "SELECT r.id, r.name, r.root_path, r.language, r.last_synced_at,
                        (SELECT count(*) FROM code_index.file f WHERE f.repository_id = r.id)::bigint,
                        (SELECT count(*) FROM code_index.symbol s WHERE s.repository_id = r.id)::bigint,
                        (SELECT count(*) FROM code_index.relation rel WHERE rel.repository_id = r.id)::bigint,
                        (SELECT count(*) FROM code_index.file f WHERE f.repository_id = r.id AND f.error_count > 0)::bigint,
                        (SELECT coalesce(sum(f.error_count), 0) FROM code_index.file f WHERE f.repository_id = r.id)::bigint
                 FROM code_index.repository r
                 WHERE ($1::text IS NULL OR r.id = $1)
                 ORDER BY r.id",
                &[&repository_id],
            )
            .await?;
        rows.into_iter()
            .map(|row| {
                Ok(RepositoryStatus {
                    repository_id: row.get(0),
                    name: row.get(1),
                    root_path: row.get(2),
                    language: row.get(3),
                    last_synced_at: row.get::<_, SystemTime>(4),
                    files: count_to_usize(row.get(5))?,
                    symbols: count_to_usize(row.get(6))?,
                    relations: count_to_usize(row.get(7))?,
                    files_with_errors: count_to_usize(row.get(8))?,
                    parse_errors: count_to_usize(row.get(9))?,
                })
            })
            .collect()
    }

    pub async fn search(
        &self,
        query: &str,
        repository_id: Option<&str>,
    ) -> Result<Vec<CodeSearchHit>, CodeIndexError> {
        let rows = self
            .db
            .query(
                "SELECT s.id, s.repository_id, f.relative_path, s.kind, s.name,
                        s.qualified_name, s.start_line, s.end_line
                 FROM code_index.symbol s
                 JOIN code_index.file f
                   ON f.repository_id = s.repository_id AND f.id = s.file_id
                 WHERE ($2::text IS NULL OR s.repository_id = $2)
                   AND (strpos(lower(s.name), lower($1)) > 0
                        OR strpos(lower(s.qualified_name), lower($1)) > 0
                        OR strpos(lower(f.relative_path), lower($1)) > 0)
                 ORDER BY CASE
                            WHEN lower(s.name) = lower($1) THEN 0
                            WHEN strpos(lower(s.name), lower($1)) = 1 THEN 1
                            WHEN strpos(lower(s.qualified_name), lower($1)) = 1 THEN 2
                            ELSE 3
                          END,
                          s.qualified_name, f.relative_path
                 LIMIT 20",
                &[&query, &repository_id],
            )
            .await?;
        rows.iter().map(search_hit_from_row).collect()
    }

    pub async fn symbol(&self, id: &str) -> Result<Option<CodeSymbolDetail>, CodeIndexError> {
        let Some(row) = self
            .db
            .query_opt(
                "SELECT s.id, s.repository_id, f.relative_path, s.kind, s.name,
                        s.qualified_name, s.start_line, s.end_line
                 FROM code_index.symbol s
                 JOIN code_index.file f
                   ON f.repository_id = s.repository_id AND f.id = s.file_id
                 WHERE s.id = $1",
                &[&id],
            )
            .await?
        else {
            return Ok(None);
        };
        let symbol = search_hit_from_row(&row)?;
        let relations = self
            .db
            .query(
                "SELECT kind, target_symbol_id, target_name, start_byte, end_byte
                 FROM code_index.relation
                 WHERE repository_id = $1 AND source_symbol_id = $2
                 ORDER BY start_byte, kind, target_name",
                &[&symbol.repository_id, &symbol.id],
            )
            .await?
            .into_iter()
            .map(|relation| {
                Ok(CodeRelation {
                    kind: relation.get(0),
                    target_symbol_id: relation.get(1),
                    target_name: relation.get(2),
                    start_byte: count_to_usize(relation.get(3))?,
                    end_byte: count_to_usize(relation.get(4))?,
                })
            })
            .collect::<Result<Vec<_>, CodeIndexError>>()?;
        Ok(Some(CodeSymbolDetail { symbol, relations }))
    }
}

fn search_hit_from_row(row: &tokio_postgres::Row) -> Result<CodeSearchHit, CodeIndexError> {
    Ok(CodeSearchHit {
        id: row.get(0),
        repository_id: row.get(1),
        relative_path: row.get(2),
        kind: row.get(3),
        name: row.get(4),
        qualified_name: row.get(5),
        start_line: count_to_usize(row.get(6))?,
        end_line: count_to_usize(row.get(7))?,
    })
}

async fn replace_file(
    transaction: &Transaction<'_>,
    repository_id: &str,
    file: &PreparedFile,
) -> Result<(), CodeIndexError> {
    transaction
        .execute(
            "DELETE FROM code_index.file WHERE repository_id = $1 AND relative_path = $2",
            &[&repository_id, &file.collected.relative_path],
        )
        .await?;
    transaction
        .execute(
            "INSERT INTO code_index.file
                (id, repository_id, relative_path, sha256, parse_status, error_count, updated_at)
             VALUES ($1, $2, $3, $4, $5, $6, now())",
            &[
                &file.collected.id,
                &repository_id,
                &file.collected.relative_path,
                &file.collected.sha256,
                &file.parsed.status.as_str(),
                &usize_to_i64(file.parsed.error_count)?,
            ],
        )
        .await?;

    for symbol in &file.parsed.symbols {
        transaction
            .execute(
                "INSERT INTO code_index.symbol
                    (id, repository_id, file_id, kind, name, qualified_name,
                     start_byte, end_byte, start_line, end_line)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                &[
                    &symbol.id,
                    &repository_id,
                    &file.collected.id,
                    &symbol.kind.as_str(),
                    &symbol.name,
                    &symbol.qualified_name,
                    &usize_to_i64(symbol.start_byte)?,
                    &usize_to_i64(symbol.end_byte)?,
                    &usize_to_i64(symbol.start_line)?,
                    &usize_to_i64(symbol.end_line)?,
                ],
            )
            .await?;
    }

    for relation in &file.parsed.relations {
        transaction
            .execute(
                "INSERT INTO code_index.relation
                    (id, repository_id, file_id, source_symbol_id, kind, target_symbol_id,
                     target_name, start_byte, end_byte)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                &[
                    &relation.id,
                    &repository_id,
                    &file.collected.id,
                    &relation.source_symbol_id,
                    &relation.kind.as_str(),
                    &relation.target_symbol_id,
                    &relation.target_name,
                    &usize_to_i64(relation.start_byte)?,
                    &usize_to_i64(relation.end_byte)?,
                ],
            )
            .await?;
    }
    Ok(())
}

fn count_to_usize(value: i64) -> Result<usize, CodeIndexError> {
    usize::try_from(value).map_err(|_| CodeIndexError::NumericOverflow)
}

const SCHEMA: &str = r"
CREATE SCHEMA IF NOT EXISTS code_index;

CREATE TABLE IF NOT EXISTS code_index.repository (
    id text PRIMARY KEY,
    name text NOT NULL,
    root_path text NOT NULL,
    language text NOT NULL CHECK (language IN ('rust')),
    last_synced_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS code_index.file (
    id text PRIMARY KEY,
    repository_id text NOT NULL REFERENCES code_index.repository(id) ON DELETE CASCADE,
    relative_path text NOT NULL,
    sha256 text NOT NULL,
    parse_status text NOT NULL CHECK (parse_status IN ('parsed', 'parsed-with-errors')),
    error_count bigint NOT NULL CHECK (error_count >= 0),
    updated_at timestamptz NOT NULL,
    UNIQUE (repository_id, relative_path),
    CONSTRAINT file_repository_id_id_key UNIQUE (repository_id, id)
);
CREATE INDEX IF NOT EXISTS code_index_file_repository ON code_index.file(repository_id);

CREATE TABLE IF NOT EXISTS code_index.symbol (
    id text PRIMARY KEY,
    repository_id text NOT NULL REFERENCES code_index.repository(id) ON DELETE CASCADE,
    file_id text NOT NULL REFERENCES code_index.file(id) ON DELETE CASCADE,
    kind text NOT NULL,
    name text NOT NULL,
    qualified_name text NOT NULL,
    start_byte bigint NOT NULL,
    end_byte bigint NOT NULL,
    start_line bigint NOT NULL,
    end_line bigint NOT NULL,
    CONSTRAINT symbol_repository_id_id_key UNIQUE (repository_id, id),
    CONSTRAINT symbol_repository_file_fkey FOREIGN KEY (repository_id, file_id)
        REFERENCES code_index.file(repository_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS code_index_symbol_repository_name
    ON code_index.symbol(repository_id, name);

CREATE TABLE IF NOT EXISTS code_index.relation (
    id text PRIMARY KEY,
    repository_id text NOT NULL REFERENCES code_index.repository(id) ON DELETE CASCADE,
    file_id text NOT NULL REFERENCES code_index.file(id) ON DELETE CASCADE,
    source_symbol_id text REFERENCES code_index.symbol(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('contains', 'imports', 'calls', 'references')),
    target_symbol_id text REFERENCES code_index.symbol(id) ON DELETE SET NULL,
    target_name text,
    start_byte bigint NOT NULL,
    end_byte bigint NOT NULL,
    CHECK (target_symbol_id IS NOT NULL OR target_name IS NOT NULL),
    CONSTRAINT relation_repository_id_id_key UNIQUE (repository_id, id),
    CONSTRAINT relation_repository_file_fkey FOREIGN KEY (repository_id, file_id)
        REFERENCES code_index.file(repository_id, id) ON DELETE CASCADE,
    CONSTRAINT relation_repository_source_symbol_fkey FOREIGN KEY (repository_id, source_symbol_id)
        REFERENCES code_index.symbol(repository_id, id) ON DELETE CASCADE,
    CONSTRAINT relation_repository_target_symbol_fkey FOREIGN KEY (repository_id, target_symbol_id)
        REFERENCES code_index.symbol(repository_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS code_index_relation_repository_kind
    ON code_index.relation(repository_id, kind);

-- Idempotently upgrade the unreleased local-dev schema created by earlier AST prototypes.
ALTER TABLE code_index.relation ALTER COLUMN id DROP DEFAULT;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'code_index'
          AND table_name = 'relation'
          AND column_name = 'id'
          AND data_type = 'bigint'
    ) THEN
        ALTER TABLE code_index.relation ALTER COLUMN id TYPE text USING id::text;
    END IF;
END
$$;

UPDATE code_index.relation
SET target_symbol_id = NULL
WHERE kind <> 'contains' AND target_symbol_id IS NOT NULL;

WITH deterministic_relation_ids AS (
    SELECT id AS old_id,
           md5(concat_ws(E'\x1f',
               'relation-migrated', repository_id, file_id,
               coalesce(source_symbol_id, ''), kind,
               coalesce(target_symbol_id, ''), coalesce(target_name, ''),
               start_byte::text, end_byte::text,
               row_number() OVER (
                   PARTITION BY repository_id, file_id, source_symbol_id, kind,
                                target_symbol_id, target_name, start_byte, end_byte
                   ORDER BY id
               )::text
           )) || md5(concat_ws(E'\x1f',
               'relation-migrated-v1', repository_id, file_id,
               coalesce(source_symbol_id, ''), kind,
               coalesce(target_symbol_id, ''), coalesce(target_name, ''),
               start_byte::text, end_byte::text,
               row_number() OVER (
                   PARTITION BY repository_id, file_id, source_symbol_id, kind,
                                target_symbol_id, target_name, start_byte, end_byte
                   ORDER BY id
               )::text
           )) AS new_id
    FROM code_index.relation
    WHERE id !~ '^[0-9a-f]{64}$'
)
UPDATE code_index.relation relation
SET id = deterministic_relation_ids.new_id
FROM deterministic_relation_ids
WHERE relation.id = deterministic_relation_ids.old_id;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'file_repository_id_id_key' AND conrelid = 'code_index.file'::regclass) THEN
        ALTER TABLE code_index.file ADD CONSTRAINT file_repository_id_id_key UNIQUE (repository_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'symbol_repository_id_id_key' AND conrelid = 'code_index.symbol'::regclass) THEN
        ALTER TABLE code_index.symbol ADD CONSTRAINT symbol_repository_id_id_key UNIQUE (repository_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'relation_repository_id_id_key' AND conrelid = 'code_index.relation'::regclass) THEN
        ALTER TABLE code_index.relation ADD CONSTRAINT relation_repository_id_id_key UNIQUE (repository_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'symbol_repository_file_fkey' AND conrelid = 'code_index.symbol'::regclass) THEN
        ALTER TABLE code_index.symbol ADD CONSTRAINT symbol_repository_file_fkey
            FOREIGN KEY (repository_id, file_id) REFERENCES code_index.file(repository_id, id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'relation_repository_file_fkey' AND conrelid = 'code_index.relation'::regclass) THEN
        ALTER TABLE code_index.relation ADD CONSTRAINT relation_repository_file_fkey
            FOREIGN KEY (repository_id, file_id) REFERENCES code_index.file(repository_id, id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'relation_repository_source_symbol_fkey' AND conrelid = 'code_index.relation'::regclass) THEN
        ALTER TABLE code_index.relation ADD CONSTRAINT relation_repository_source_symbol_fkey
            FOREIGN KEY (repository_id, source_symbol_id) REFERENCES code_index.symbol(repository_id, id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'relation_repository_target_symbol_fkey' AND conrelid = 'code_index.relation'::regclass) THEN
        ALTER TABLE code_index.relation ADD CONSTRAINT relation_repository_target_symbol_fkey
            FOREIGN KEY (repository_id, target_symbol_id) REFERENCES code_index.symbol(repository_id, id) ON DELETE CASCADE;
    END IF;
END
$$;
";
