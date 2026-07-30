//! Code graph — AST-parsed symbols and relations as deterministic graph data.
//!
//! Cross-reference: design decision D2 (deterministic graph) · ENFORCEMENT.md §B (one-way flow).
//! The code lane is separate from the semantic wiki lane: code symbols come from tree-sitter
//! parsing of source files, not from LLM extraction or agent-curated frontmatter.
//!
//! Node id convention: `code:<kind>:<source_path>:<name>`.
//! Edge kinds: `code_calls`, `code_imports`, `code_inherits`, `code_contains`, `code_uses`.
//!
//! SRP: `parser` extracts from files, `ingest` walks directories, `mod.rs` owns the contract.

use serde::{Deserialize, Serialize};

pub mod ingest;
pub mod parser;

/// Programming language supported by the code indexer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CodeLanguage {
    Rust,
    Python,
    /// Covers both `.ts` and `.tsx` (the parser picks the TSX grammar by file extension).
    TypeScript,
    /// Covers `.kt` and `.kts` (Kotlin scripts).
    Kotlin,
}

impl CodeLanguage {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Rust => "rust",
            Self::Python => "python",
            Self::TypeScript => "typescript",
            Self::Kotlin => "kotlin",
        }
    }

    #[must_use]
    pub fn from_extension(ext: &str) -> Option<Self> {
        match ext.to_ascii_lowercase().as_str() {
            "rs" => Some(Self::Rust),
            "py" | "pyi" => Some(Self::Python),
            "ts" | "tsx" => Some(Self::TypeScript),
            "kt" | "kts" => Some(Self::Kotlin),
            _ => None,
        }
    }
}

/// Kind of code symbol extracted from an AST.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeSymbolKind {
    Function,
    Method,
    Class,
    Struct,
    Enum,
    Trait,
    Module,
    Import,
    Constant,
    Variable,
}

impl CodeSymbolKind {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Function => "function",
            Self::Method => "method",
            Self::Class => "class",
            Self::Struct => "struct",
            Self::Enum => "enum",
            Self::Trait => "trait",
            Self::Module => "module",
            Self::Import => "import",
            Self::Constant => "constant",
            Self::Variable => "variable",
        }
    }
}

/// One AST-parsed code symbol. `source_path` is the file path relative to the indexed root.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeSymbol {
    pub source_path: String,
    pub name: String,
    pub kind: CodeSymbolKind,
    pub language: CodeLanguage,
    /// 1-based line where the symbol starts.
    pub start_line: u32,
    /// 1-based line where the symbol ends (inclusive).
    pub end_line: u32,
    /// Parent symbol name when nested (e.g. method inside a class). Empty for top-level.
    pub parent: String,
    /// Short signature or declaration snippet (bounded, never the whole body).
    pub signature: String,
}

impl CodeSymbol {
    /// Stable node id for the graph store.
    #[must_use]
    pub fn node_id(&self) -> String {
        format!(
            "code:{}:{}:{}",
            self.kind.as_str(),
            self.source_path,
            self.name
        )
    }

    /// Reconstruct a symbol from its node id, label, and optional signature.
    /// Used by the store when reading code nodes back out of Postgres.
    #[must_use]
    pub fn from_node_id(id: &str, label: &str, outcome: Option<String>) -> Option<Self> {
        let rest = id.strip_prefix("code:")?;
        let mut parts = rest.splitn(3, ':');
        let kind = match parts.next()? {
            "function" => CodeSymbolKind::Function,
            "method" => CodeSymbolKind::Method,
            "class" => CodeSymbolKind::Class,
            "struct" => CodeSymbolKind::Struct,
            "enum" => CodeSymbolKind::Enum,
            "trait" => CodeSymbolKind::Trait,
            "module" => CodeSymbolKind::Module,
            "import" => CodeSymbolKind::Import,
            "constant" => CodeSymbolKind::Constant,
            "variable" => CodeSymbolKind::Variable,
            _ => return None,
        };
        let source_path = parts.next()?.to_owned();
        let name = parts.next().unwrap_or(label).to_owned();
        let language = CodeLanguage::from_extension(
            std::path::Path::new(&source_path)
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or(""),
        )?;
        Some(Self {
            source_path,
            name,
            kind,
            language,
            start_line: 0,
            end_line: 0,
            parent: String::new(),
            signature: outcome.unwrap_or_default(),
        })
    }
}

/// Kind of directed relation between two code symbols.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeRelationKind {
    Calls,
    Imports,
    Inherits,
    Contains,
    Uses,
}

impl CodeRelationKind {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Calls => "code_calls",
            Self::Imports => "code_imports",
            Self::Inherits => "code_inherits",
            Self::Contains => "code_contains",
            Self::Uses => "code_uses",
        }
    }
}

/// One directed relation between two code symbols.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeRelation {
    pub from: CodeSymbol,
    pub to: CodeSymbol,
    pub kind: CodeRelationKind,
}

/// Stats returned by a code-indexing pass. Phase 2 is parse-only: the extracted
/// symbols/relations ride along for the Phase 3 store upsert (nothing written yet).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct CodeIndexStats {
    pub files_parsed: usize,
    pub symbols_upserted: usize,
    pub relations_upserted: usize,
    pub files_skipped: usize,
    pub parse_errors: usize,
    /// All symbols extracted this pass (`symbols_upserted` is this vec's length).
    pub symbols: Vec<CodeSymbol>,
    /// All relations extracted this pass (`relations_upserted` is this vec's length).
    pub relations: Vec<CodeRelation>,
}

/// Configuration for the code indexer (loaded from `boring.json`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CodeIndexConfig {
    /// Master switch. When false, code indexing is skipped entirely.
    pub enabled: bool,
    /// Languages to index. Empty = all supported languages.
    pub languages: Vec<String>,
    /// Cap on symbols per file to keep the graph bounded.
    pub max_symbols_per_file: usize,
    /// Excluded from indexing. Entries without `/` match any path component at any depth
    /// (`target` excludes `crates/foo/target/...`); entries with `/` are root-relative
    /// prefixes (`src/gen` excludes only `<root>/src/gen/...`).
    pub exclude_paths: Vec<String>,
}

impl Default for CodeIndexConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            languages: vec![
                "rust".to_owned(),
                "python".to_owned(),
                "typescript".to_owned(),
                "kotlin".to_owned(),
            ],
            max_symbols_per_file: 200,
            exclude_paths: vec![
                "target".to_owned(),
                "node_modules".to_owned(),
                "__pycache__".to_owned(),
                ".git".to_owned(),
            ],
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn language_from_extension() {
        assert_eq!(CodeLanguage::from_extension("rs"), Some(CodeLanguage::Rust));
        assert_eq!(
            CodeLanguage::from_extension("py"),
            Some(CodeLanguage::Python)
        );
        assert_eq!(
            CodeLanguage::from_extension("pyi"),
            Some(CodeLanguage::Python)
        );
        assert_eq!(
            CodeLanguage::from_extension("ts"),
            Some(CodeLanguage::TypeScript)
        );
        assert_eq!(
            CodeLanguage::from_extension("tsx"),
            Some(CodeLanguage::TypeScript)
        );
        assert_eq!(
            CodeLanguage::from_extension("kt"),
            Some(CodeLanguage::Kotlin)
        );
        assert_eq!(
            CodeLanguage::from_extension("kts"),
            Some(CodeLanguage::Kotlin)
        );
        assert_eq!(CodeLanguage::from_extension("go"), None);
    }

    #[test]
    fn symbol_node_id_is_stable() {
        let sym = CodeSymbol {
            source_path: "src/lib.rs".to_owned(),
            name: "parse".to_owned(),
            kind: CodeSymbolKind::Function,
            language: CodeLanguage::Rust,
            start_line: 1,
            end_line: 10,
            parent: String::new(),
            signature: "fn parse() {}".to_owned(),
        };
        assert_eq!(sym.node_id(), "code:function:src/lib.rs:parse");
    }

    #[test]
    fn relation_kind_uses_code_prefix() {
        assert_eq!(CodeRelationKind::Calls.as_str(), "code_calls");
        assert_eq!(CodeRelationKind::Uses.as_str(), "code_uses");
    }
}
