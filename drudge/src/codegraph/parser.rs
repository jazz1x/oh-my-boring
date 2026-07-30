//! Tree-sitter parsing of Rust / Python source files into code symbols + relations.
//!
//! Cross-reference: mod.rs (contract) · ENFORCEMENT.md §B (deterministic code lane, no LLM).
//!
//! Extraction contract:
//! - Rust symbols: `function_item`, `impl_item` methods, `struct_item`, `enum_item`,
//!   `trait_item`, `mod_item`, `use_declaration`, `const_item`, `static_item`.
//! - Python symbols: `function_definition`, `class_definition`, `import_statement`,
//!   `import_from_statement`, top-level UPPER_SNAKE `assignment` (constant convention).
//! - Relations: `code_calls` (enclosing fn/method → callee), `code_imports` (import decl →
//!   referenced path), `code_contains` (container → child), `code_inherits` (Rust
//!   `impl Trait for Type` / Python class bases).
//! - Cross-file references (callees, import targets, impl target types, base classes) are
//!   *placeholder* symbols with an empty `source_path` — they carry a name only, and the
//!   Phase 3 store upsert resolves them to real nodes. Placeholders are never pushed into
//!   the symbol list itself, only embedded in relations.
//! - Bounded: at most `max_symbols` symbols per file; signatures truncated to 120 chars.
use std::collections::HashSet;
use std::path::Path;

use anyhow::{Context, Result};
use tree_sitter::{Node, Parser};

use super::{CodeLanguage, CodeRelation, CodeRelationKind, CodeSymbol, CodeSymbolKind};

/// Signatures are bounded declaration snippets (whitespace-collapsed, no body) — never the
/// whole body. 120 chars keeps one line of terminal output readable.
const MAX_SIGNATURE_CHARS: usize = 120;

/// Parse one source file from disk. `path` is recorded verbatim (lossy) as `source_path`
/// on every extracted symbol — callers pass the repo-root-relative path for stable node ids.
pub fn parse_file(
    path: &Path,
    lang: CodeLanguage,
    max_symbols: usize,
) -> Result<(Vec<CodeSymbol>, Vec<CodeRelation>)> {
    let source = std::fs::read_to_string(path)
        .with_context(|| format!("read source file: {}", path.display()))?;
    parse_source(&source, &path.to_string_lossy(), lang, max_symbols)
}

/// Parse already-loaded source text. Split out from [`parse_file`] so the directory walk
/// (ingest) controls the recorded `source_path` label and tests need no temp files.
pub fn parse_source(
    source: &str,
    source_path: &str,
    lang: CodeLanguage,
    max_symbols: usize,
) -> Result<(Vec<CodeSymbol>, Vec<CodeRelation>)> {
    let language: tree_sitter::Language = match lang {
        CodeLanguage::Rust => tree_sitter_rust::LANGUAGE.into(),
        CodeLanguage::Python => tree_sitter_python::LANGUAGE.into(),
        // `.tsx` sources need the JSX-aware grammar; everything else uses the plain TS one.
        CodeLanguage::TypeScript => {
            if std::path::Path::new(source_path)
                .extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("tsx"))
            {
                tree_sitter_typescript::LANGUAGE_TSX.into()
            } else {
                tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()
            }
        }
        CodeLanguage::Kotlin => tree_sitter_kotlin_ng::LANGUAGE.into(),
    };
    let mut parser = Parser::new();
    parser
        .set_language(&language)
        .context("load tree-sitter language")?;
    // `parse` returns None only without a language or with a cancelled progress callback —
    // neither happens here, but stay on the Result rail anyway (ROP, no unwrap).
    let tree = parser
        .parse(source, None)
        .with_context(|| format!("tree-sitter returned no tree: {source_path}"))?;
    let mut ctx = Ctx {
        src: source.as_bytes(),
        path: source_path.to_owned(),
        lang,
        max_symbols,
        symbols: Vec::new(),
        relations: Vec::new(),
        seen_relations: HashSet::new(),
    };
    visit(tree.root_node(), None, None, &mut ctx);
    Ok((ctx.symbols, ctx.relations))
}

/// Mutable per-file extraction state threaded through the recursive walk.
struct Ctx<'a> {
    src: &'a [u8],
    path: String,
    lang: CodeLanguage,
    max_symbols: usize,
    symbols: Vec<CodeSymbol>,
    relations: Vec<CodeRelation>,
    /// (from node_id, to node_id, edge kind) — dedupes e.g. repeated calls to the same name.
    seen_relations: HashSet<(String, String, &'static str)>,
}

impl Ctx<'_> {
    fn at_cap(&self) -> bool {
        self.symbols.len() >= self.max_symbols
    }

    fn push_symbol(&mut self, scope: Option<&Scope>, sym: CodeSymbol) {
        if self.at_cap() {
            return;
        }
        if let Some(scope) = scope {
            self.push_relation(
                scope.symbol.clone(),
                sym.clone(),
                CodeRelationKind::Contains,
            );
        }
        self.symbols.push(sym);
    }

    fn push_relation(&mut self, from: CodeSymbol, to: CodeSymbol, kind: CodeRelationKind) {
        if self
            .seen_relations
            .insert((from.node_id(), to.node_id(), kind.as_str()))
        {
            self.relations.push(CodeRelation { from, to, kind });
        }
    }

    fn symbol(
        &self,
        node: Node,
        name: String,
        kind: CodeSymbolKind,
        scope: Option<&Scope>,
    ) -> CodeSymbol {
        CodeSymbol {
            source_path: self.path.clone(),
            name,
            kind,
            language: self.lang,
            start_line: line(node.start_position().row),
            end_line: line(node.end_position().row),
            parent: scope.map_or_else(String::new, |s| s.symbol.name.clone()),
            signature: signature_of(node, self.src),
        }
    }

    /// Unresolved name-only reference (external callee, import target, base class). The
    /// empty `source_path` marks it as a placeholder — Phase 3 resolves these by name.
    fn placeholder(&self, node: Node, name: String, kind: CodeSymbolKind) -> CodeSymbol {
        CodeSymbol {
            source_path: String::new(),
            name,
            kind,
            language: self.lang,
            start_line: line(node.start_position().row),
            end_line: line(node.end_position().row),
            parent: String::new(),
            signature: String::new(),
        }
    }
}

/// Enclosing container during the walk: drives the `parent` field, `code_contains` edges,
/// and function-vs-method classification.
struct Scope {
    symbol: CodeSymbol,
    /// Class/trait/impl bodies turn child functions into methods (vs plain functions).
    is_type: bool,
}

/// 0-based tree-sitter row → 1-based line, saturating instead of truncating on absurd input.
fn line(row: usize) -> u32 {
    u32::try_from(row + 1).unwrap_or(u32::MAX)
}

/// Node text as `&str`. The source was read as a `String` (valid UTF-8) and node boundaries
/// are token boundaries, so a UTF-8 error here is impossible — degrade to "" regardless.
fn text<'a>(node: Node, src: &'a [u8]) -> &'a str {
    node.utf8_text(src).unwrap_or("")
}

/// Declaration snippet without the body, whitespace-collapsed and char-truncated.
fn signature_of(node: Node, src: &[u8]) -> String {
    let end = node
        .child_by_field_name("body")
        .map_or_else(|| node.end_byte(), |body| body.start_byte());
    String::from_utf8_lossy(&src[node.start_byte()..end])
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(MAX_SIGNATURE_CHARS)
        .collect()
}

/// Whitespace-collapsed, truncated text of a node (used for use-paths and exotic types).
fn collapsed(node: Node, src: &[u8]) -> String {
    text(node, src)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(MAX_SIGNATURE_CHARS)
        .collect()
}

fn visit(node: Node, scope: Option<&Scope>, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    if ctx.at_cap() {
        return;
    }
    match ctx.lang {
        CodeLanguage::Rust => visit_rust(node, scope, caller, ctx),
        CodeLanguage::Python => visit_python(node, scope, caller, ctx),
        CodeLanguage::TypeScript => visit_typescript(node, scope, caller, ctx),
        CodeLanguage::Kotlin => visit_kotlin(node, scope, caller, ctx),
    }
}

fn visit_children(node: Node, scope: Option<&Scope>, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        visit(child, scope, caller, ctx);
    }
}

// ─────────────────────────────────────────────────────────────
// Rust
// ─────────────────────────────────────────────────────────────

fn visit_rust(node: Node, scope: Option<&Scope>, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    match node.kind() {
        // `function_signature_item` covers bodyless declarations (trait methods, extern fns).
        "function_item" | "function_signature_item" => rust_function(node, scope, ctx),
        "struct_item" | "enum_item" | "trait_item" | "mod_item" => {
            rust_container(node, scope, ctx);
        }
        "impl_item" => rust_impl(node, ctx),
        "use_declaration" => rust_use(node, scope, ctx),
        "const_item" | "static_item" => rust_const(node, scope, ctx),
        "call_expression" => {
            rust_call(node, caller, ctx);
            visit_children(node, scope, caller, ctx); // args may hold nested calls / closures
        }
        _ => visit_children(node, scope, caller, ctx),
    }
}

fn rust_function(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let kind = if scope.is_some_and(|s| s.is_type) {
        CodeSymbolKind::Method
    } else {
        CodeSymbolKind::Function
    };
    let sym = ctx.symbol(node, text(name_node, ctx.src).to_owned(), kind, scope);
    ctx.push_symbol(scope, sym.clone());
    let fn_scope = Scope {
        symbol: sym.clone(),
        is_type: false,
    };
    visit_children(node, Some(&fn_scope), Some(&sym), ctx);
}

fn rust_container(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let kind = match node.kind() {
        "struct_item" => CodeSymbolKind::Struct,
        "enum_item" => CodeSymbolKind::Enum,
        "trait_item" => CodeSymbolKind::Trait,
        _ => CodeSymbolKind::Module,
    };
    let sym = ctx.symbol(node, text(name_node, ctx.src).to_owned(), kind, scope);
    ctx.push_symbol(scope, sym.clone());
    let inner = Scope {
        symbol: sym,
        is_type: kind != CodeSymbolKind::Module,
    };
    visit_children(node, Some(&inner), None, ctx);
}

fn rust_impl(node: Node, ctx: &mut Ctx) {
    let Some(type_node) = node.child_by_field_name("type") else {
        return;
    };
    // The impl target may be defined elsewhere — placeholder by name (Phase 3 resolves).
    let type_sym = ctx.placeholder(
        type_node,
        rust_type_name(type_node, ctx.src),
        CodeSymbolKind::Struct,
    );
    if let Some(trait_node) = node.child_by_field_name("trait") {
        // `impl Trait for Type` — direction: the type gains the trait.
        let trait_sym = ctx.placeholder(
            trait_node,
            rust_type_name(trait_node, ctx.src),
            CodeSymbolKind::Trait,
        );
        ctx.push_relation(type_sym.clone(), trait_sym, CodeRelationKind::Inherits);
    }
    let impl_scope = Scope {
        symbol: type_sym,
        is_type: true,
    };
    visit_children(node, Some(&impl_scope), None, ctx);
}

/// Best-effort short name of a Rust type node (`Foo` for `Foo<T>`, `Bar` for `foo::Bar`).
fn rust_type_name(node: Node, src: &[u8]) -> String {
    match node.kind() {
        "generic_type" => node
            .child_by_field_name("type")
            .map_or_else(|| collapsed(node, src), |inner| rust_type_name(inner, src)),
        "scoped_type_identifier" => node
            .child_by_field_name("name")
            .map_or_else(|| collapsed(node, src), |n| text(n, src).to_owned()),
        _ => text(node, src).to_owned(),
    }
}

fn rust_use(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(arg) = node.child_by_field_name("argument") else {
        return;
    };
    let target = collapsed(arg, ctx.src);
    // Binding name: the alias if renamed, else the last path segment; brace lists
    // (`use a::{b, c}`) keep the whole clause as their name — one symbol per declaration.
    let name = match arg.kind() {
        "use_as_clause" => arg
            .child_by_field_name("alias")
            .map_or_else(|| target.clone(), |n| text(n, ctx.src).to_owned()),
        "scoped_identifier" => arg
            .child_by_field_name("name")
            .map_or_else(|| target.clone(), |n| text(n, ctx.src).to_owned()),
        _ => target.clone(),
    };
    let sym = ctx.symbol(node, name, CodeSymbolKind::Import, scope);
    ctx.push_symbol(scope, sym.clone());
    let to = ctx.placeholder(arg, target, CodeSymbolKind::Import);
    ctx.push_relation(sym, to, CodeRelationKind::Imports);
}

fn rust_const(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    // `const` → Constant; `static` is a global variable (it may be `static mut`).
    let kind = if node.kind() == "const_item" {
        CodeSymbolKind::Constant
    } else {
        CodeSymbolKind::Variable
    };
    let sym = ctx.symbol(node, text(name_node, ctx.src).to_owned(), kind, scope);
    ctx.push_symbol(scope, sym);
}

fn rust_call(node: Node, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    if let Some(caller) = caller
        && let Some((name, kind)) = node
            .child_by_field_name("function")
            .and_then(|f| rust_callee(f, ctx.src))
    {
        let to = ctx.placeholder(node, name, kind);
        ctx.push_relation(caller.clone(), to, CodeRelationKind::Calls);
    }
}

/// Callee name + kind from a Rust call target (`function` field of `call_expression`).
fn rust_callee(node: Node, src: &[u8]) -> Option<(String, CodeSymbolKind)> {
    match node.kind() {
        "identifier" => Some((text(node, src).to_owned(), CodeSymbolKind::Function)),
        "scoped_identifier" => node
            .child_by_field_name("name")
            .map(|n| (text(n, src).to_owned(), CodeSymbolKind::Function)),
        "field_expression" => node
            .child_by_field_name("field")
            .map(|n| (text(n, src).to_owned(), CodeSymbolKind::Method)),
        // `foo::<T>()` wraps the callee in a `generic_function` node.
        "generic_function" => node
            .child_by_field_name("function")
            .and_then(|inner| rust_callee(inner, src)),
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────
// Python
// ─────────────────────────────────────────────────────────────

fn visit_python(node: Node, scope: Option<&Scope>, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    match node.kind() {
        "function_definition" => py_function(node, scope, ctx),
        "class_definition" => py_class(node, scope, ctx),
        "import_statement" => py_import(node, scope, ctx),
        "import_from_statement" => py_import_from(node, scope, ctx),
        "assignment" => py_assignment(node, scope, ctx),
        "call" => {
            py_call(node, caller, ctx);
            visit_children(node, scope, caller, ctx); // args may hold nested calls
        }
        // `decorated_definition` falls through: its `definition` child is visited next level.
        _ => visit_children(node, scope, caller, ctx),
    }
}

fn py_function(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let kind = if scope.is_some_and(|s| s.is_type) {
        CodeSymbolKind::Method
    } else {
        CodeSymbolKind::Function
    };
    let sym = ctx.symbol(node, text(name_node, ctx.src).to_owned(), kind, scope);
    ctx.push_symbol(scope, sym.clone());
    let fn_scope = Scope {
        symbol: sym.clone(),
        is_type: false,
    };
    visit_children(node, Some(&fn_scope), Some(&sym), ctx);
}

fn py_class(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let sym = ctx.symbol(
        node,
        text(name_node, ctx.src).to_owned(),
        CodeSymbolKind::Class,
        scope,
    );
    ctx.push_symbol(scope, sym.clone());
    if let Some(bases) = node.child_by_field_name("superclasses") {
        let mut cursor = bases.walk();
        for base in bases.named_children(&mut cursor) {
            if let Some(name) = py_base_name(base, ctx.src) {
                let to = ctx.placeholder(base, name, CodeSymbolKind::Class);
                ctx.push_relation(sym.clone(), to, CodeRelationKind::Inherits);
            }
        }
    }
    let class_scope = Scope {
        symbol: sym,
        is_type: true,
    };
    visit_children(node, Some(&class_scope), None, ctx);
}

/// Base-class name from a `superclasses` argument (`Base`, `pkg.Base`, `Generic[T]`).
/// `keyword_argument` (e.g. `metaclass=...`) is not a base and returns None.
fn py_base_name(node: Node, src: &[u8]) -> Option<String> {
    match node.kind() {
        "identifier" => Some(text(node, src).to_owned()),
        "attribute" => node
            .child_by_field_name("attribute")
            .map(|n| text(n, src).to_owned()),
        "subscript" => node
            .child_by_field_name("value")
            .and_then(|v| py_base_name(v, src)),
        _ => None,
    }
}

/// `import a.b` / `import a.b as c` — comma-separated bindings become one symbol each.
fn py_import(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if ctx.at_cap() {
            break;
        }
        let (name, target) = match child.kind() {
            "dotted_name" => (
                last_segment(text(child, ctx.src)),
                text(child, ctx.src).to_owned(),
            ),
            "aliased_import" => {
                let target = child
                    .child_by_field_name("name")
                    .map_or_else(String::new, |n| text(n, ctx.src).to_owned());
                let alias = child
                    .child_by_field_name("alias")
                    .map_or_else(|| target.clone(), |n| text(n, ctx.src).to_owned());
                (alias, target)
            }
            _ => continue,
        };
        push_import(ctx, scope, child, name, target);
    }
}

/// `from x import a, b as c` / `from x import *` — targets are `x.a`, `x.b`, `x.*`.
fn py_import_from(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let module = node
        .child_by_field_name("module_name")
        .map_or_else(String::new, |n| {
            text(n, ctx.src).trim_end_matches('.').to_owned()
        });
    let mut cursor = node.walk();
    for child in node.children_by_field_name("name", &mut cursor) {
        if ctx.at_cap() {
            break;
        }
        let (name, item) = match child.kind() {
            "dotted_name" => {
                let item = text(child, ctx.src).to_owned();
                (last_segment(&item), item)
            }
            "aliased_import" => {
                let item = child
                    .child_by_field_name("name")
                    .map_or_else(String::new, |n| text(n, ctx.src).to_owned());
                let alias = child
                    .child_by_field_name("alias")
                    .map_or_else(|| item.clone(), |n| text(n, ctx.src).to_owned());
                (alias, item)
            }
            "wildcard_import" => ("*".to_owned(), "*".to_owned()),
            _ => continue,
        };
        let target = if module.is_empty() {
            item
        } else {
            format!("{module}.{item}")
        };
        push_import(ctx, scope, child, name, target);
    }
}

/// One import binding: symbol (local name) + `code_imports` edge to the referenced path.
fn push_import(ctx: &mut Ctx, scope: Option<&Scope>, node: Node, name: String, target: String) {
    if name.is_empty() || target.is_empty() {
        return;
    }
    let sym = ctx.symbol(node, name, CodeSymbolKind::Import, scope);
    ctx.push_symbol(scope, sym.clone());
    let to = ctx.placeholder(node, target, CodeSymbolKind::Import);
    ctx.push_relation(sym, to, CodeRelationKind::Imports);
}

/// `a.b.c` → `c` (the local binding name of a dotted import).
fn last_segment(dotted: &str) -> String {
    dotted.rsplit('.').next().unwrap_or(dotted).to_owned()
}

/// Top-level `CONSTANT = ...`. UPPER_SNAKE only — the Python constant convention; plain
/// module-level variables are not graph-worthy symbols.
fn py_assignment(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    if scope.is_some() {
        return; // module-level only, not class attributes or locals
    }
    let Some(left) = node.child_by_field_name("left") else {
        return;
    };
    if left.kind() != "identifier" {
        return;
    }
    let name = text(left, ctx.src);
    if !is_upper_snake(name) {
        return;
    }
    let sym = ctx.symbol(node, name.to_owned(), CodeSymbolKind::Constant, None);
    ctx.push_symbol(None, sym);
}

/// UPPER_SNAKE_CASE: starts with an ASCII uppercase letter, then only upper/digit/`_`.
fn is_upper_snake(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    first.is_ascii_uppercase()
        && chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
}

fn py_call(node: Node, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    if let Some(caller) = caller
        && let Some((name, kind)) = node
            .child_by_field_name("function")
            .and_then(|f| py_callee(f, ctx.src))
    {
        let to = ctx.placeholder(node, name, kind);
        ctx.push_relation(caller.clone(), to, CodeRelationKind::Calls);
    }
}

/// Callee name + kind from a Python call target (`function` field of `call`).
fn py_callee(node: Node, src: &[u8]) -> Option<(String, CodeSymbolKind)> {
    match node.kind() {
        "identifier" => Some((text(node, src).to_owned(), CodeSymbolKind::Function)),
        "attribute" => node
            .child_by_field_name("attribute")
            .map(|n| (text(n, src).to_owned(), CodeSymbolKind::Method)),
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────
// TypeScript
// ─────────────────────────────────────────────────────────────

fn visit_typescript(node: Node, scope: Option<&Scope>, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    match node.kind() {
        // `method_signature` covers bodyless interface members.
        "function_declaration"
        | "generator_function_declaration"
        | "method_definition"
        | "method_signature" => ts_function(node, scope, ctx),
        "class_declaration" | "abstract_class_declaration" => ts_class(node, scope, ctx),
        "interface_declaration" => ts_interface(node, scope, ctx),
        "enum_declaration" => ts_container(node, scope, ctx, CodeSymbolKind::Enum),
        "internal_module" => ts_container(node, scope, ctx, CodeSymbolKind::Module),
        "import_statement" => ts_import(node, scope, ctx),
        "lexical_declaration" | "variable_declaration" => ts_vars(node, scope, ctx),
        "call_expression" => {
            ts_call(node, caller, ctx);
            visit_children(node, scope, caller, ctx); // args may hold nested calls
        }
        _ => visit_children(node, scope, caller, ctx),
    }
}

fn ts_function(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let kind = if scope.is_some_and(|s| s.is_type) {
        CodeSymbolKind::Method
    } else {
        CodeSymbolKind::Function
    };
    let sym = ctx.symbol(node, text(name_node, ctx.src).to_owned(), kind, scope);
    ctx.push_symbol(scope, sym.clone());
    let fn_scope = Scope {
        symbol: sym.clone(),
        is_type: false,
    };
    visit_children(node, Some(&fn_scope), Some(&sym), ctx);
}

fn ts_class(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let sym = ctx.symbol(
        node,
        text(name_node, ctx.src).to_owned(),
        CodeSymbolKind::Class,
        scope,
    );
    ctx.push_symbol(scope, sym.clone());
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() != "class_heritage" {
            continue;
        }
        let mut hc = child.walk();
        for clause in child.named_children(&mut hc) {
            match clause.kind() {
                // `extends` — the parent class.
                "extends_clause" => {
                    if let Some(value) = clause.child_by_field_name("value")
                        && let Some(name) = ts_type_name(value, ctx.src)
                    {
                        let to = ctx.placeholder(value, name, CodeSymbolKind::Class);
                        ctx.push_relation(sym.clone(), to, CodeRelationKind::Inherits);
                    }
                }
                // `implements` — interfaces, mapped onto the Trait kind.
                "implements_clause" => {
                    let mut ic = clause.walk();
                    for ty in clause.named_children(&mut ic) {
                        if let Some(name) = ts_type_name(ty, ctx.src) {
                            let to = ctx.placeholder(ty, name, CodeSymbolKind::Trait);
                            ctx.push_relation(sym.clone(), to, CodeRelationKind::Inherits);
                        }
                    }
                }
                _ => {}
            }
        }
    }
    let class_scope = Scope {
        symbol: sym,
        is_type: true,
    };
    visit_children(node, Some(&class_scope), None, ctx);
}

fn ts_interface(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let sym = ctx.symbol(
        node,
        text(name_node, ctx.src).to_owned(),
        CodeSymbolKind::Trait,
        scope,
    );
    ctx.push_symbol(scope, sym.clone());
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() != "extends_type_clause" {
            continue;
        }
        let mut ec = child.walk();
        for ty in child.named_children(&mut ec) {
            if let Some(name) = ts_type_name(ty, ctx.src) {
                let to = ctx.placeholder(ty, name, CodeSymbolKind::Trait);
                ctx.push_relation(sym.clone(), to, CodeRelationKind::Inherits);
            }
        }
    }
    let iface_scope = Scope {
        symbol: sym,
        is_type: true,
    };
    visit_children(node, Some(&iface_scope), None, ctx);
}

/// Enum / namespace containers: symbol + scope for their children.
fn ts_container(node: Node, scope: Option<&Scope>, ctx: &mut Ctx, kind: CodeSymbolKind) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let sym = ctx.symbol(node, text(name_node, ctx.src).to_owned(), kind, scope);
    ctx.push_symbol(scope, sym.clone());
    let inner = Scope {
        symbol: sym,
        is_type: false,
    };
    visit_children(node, Some(&inner), None, ctx);
}

/// Best-effort short name of a TS type node (`Foo` for `Foo<T>`, `Bar` for `ns.Bar`).
fn ts_type_name(node: Node, src: &[u8]) -> Option<String> {
    match node.kind() {
        "identifier" | "type_identifier" => Some(text(node, src).to_owned()),
        "member_expression" => node
            .child_by_field_name("property")
            .map(|n| text(n, src).to_owned()),
        "generic_type" => node
            .child_by_field_name("name")
            .and_then(|n| ts_type_name(n, src)),
        _ => None,
    }
}

/// `import … from 'mod'` — one Import symbol per binding, `code_imports` edge to the
/// module path (named bindings target `mod.name` like Python's `from x import a`).
fn ts_import(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let module = ts_module_string(node, ctx.src);
    let mut cursor = node.walk();
    let Some(clause) = node
        .named_children(&mut cursor)
        .find(|c| c.kind() == "import_clause")
    else {
        return; // side-effect import (`import 'mod'`) — no local binding, nothing to graph
    };
    let mut cc = clause.walk();
    for child in clause.named_children(&mut cc) {
        if ctx.at_cap() {
            break;
        }
        match child.kind() {
            // default import: `import Foo from 'mod'`
            "identifier" => {
                push_import(
                    ctx,
                    scope,
                    child,
                    text(child, ctx.src).to_owned(),
                    module.clone(),
                );
            }
            // namespace import: `import * as ns from 'mod'` (the binding is a plain
            // `identifier` child — the grammar gives it no field name)
            "namespace_import" => {
                let mut nc = child.walk();
                if let Some(name) = child
                    .named_children(&mut nc)
                    .find(|c| c.kind() == "identifier")
                {
                    push_import(
                        ctx,
                        scope,
                        name,
                        text(name, ctx.src).to_owned(),
                        module.clone(),
                    );
                }
            }
            // named imports: `import { a, b as c } from 'mod'`
            "named_imports" => {
                let mut nc = child.walk();
                for spec in child.named_children(&mut nc) {
                    if ctx.at_cap() {
                        break;
                    }
                    if spec.kind() != "import_specifier" {
                        continue;
                    }
                    let Some(name_node) = spec.child_by_field_name("name") else {
                        continue;
                    };
                    let item = text(name_node, ctx.src).to_owned();
                    let local = spec
                        .child_by_field_name("alias")
                        .map_or_else(|| item.clone(), |n| text(n, ctx.src).to_owned());
                    let target = if module.is_empty() {
                        item
                    } else {
                        format!("{module}.{item}")
                    };
                    push_import(ctx, scope, spec, local, target);
                }
            }
            _ => {}
        }
    }
}

/// Module path of an `import_statement` (the `string` child, quotes stripped).
fn ts_module_string(node: Node, src: &[u8]) -> String {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "string" {
            let mut sc = child.walk();
            if let Some(frag) = child
                .named_children(&mut sc)
                .find(|c| c.kind() == "string_fragment")
            {
                return text(frag, src).to_owned();
            }
            return text(child, src).trim_matches(['\'', '"']).to_owned();
        }
    }
    String::new()
}

/// Top-level `const`/`let`/`var` declarators. An arrow/function initializer makes the
/// declarator a Function symbol (the React-component pattern); plain initializers become
/// Constant (`const`) or Variable (`let`/`var`). Module-level only, like `py_assignment`.
fn ts_vars(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    if scope.is_some() {
        return; // module-level only, not class fields or locals
    }
    let is_const = node.kind() == "lexical_declaration"
        && node
            .child_by_field_name("kind")
            .is_some_and(|k| text(k, ctx.src) == "const");
    let mut cursor = node.walk();
    for declarator in node.named_children(&mut cursor) {
        if ctx.at_cap() {
            break;
        }
        if declarator.kind() != "variable_declarator" {
            continue;
        }
        let Some(name_node) = declarator.child_by_field_name("name") else {
            continue;
        };
        if name_node.kind() != "identifier" {
            continue; // destructuring patterns are not single symbols
        }
        let name = text(name_node, ctx.src).to_owned();
        let value = declarator.child_by_field_name("value");
        if value.is_some_and(|v| {
            matches!(
                v.kind(),
                "arrow_function" | "function_expression" | "generator_function"
            )
        }) {
            let sym = ctx.symbol(declarator, name, CodeSymbolKind::Function, None);
            ctx.push_symbol(None, sym.clone());
            let fn_scope = Scope {
                symbol: sym.clone(),
                is_type: false,
            };
            visit_children(declarator, Some(&fn_scope), Some(&sym), ctx);
            continue;
        }
        let kind = if is_const {
            CodeSymbolKind::Constant
        } else {
            CodeSymbolKind::Variable
        };
        let sym = ctx.symbol(declarator, name, kind, None);
        ctx.push_symbol(None, sym);
    }
}

fn ts_call(node: Node, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    if let Some(caller) = caller
        && let Some(f) = node.child_by_field_name("function")
        && let Some((name, kind)) = ts_callee(f, ctx.src)
    {
        let to = ctx.placeholder(node, name, kind);
        ctx.push_relation(caller.clone(), to, CodeRelationKind::Calls);
    }
}

/// Callee name + kind from a TS call target (`function` field of `call_expression`).
fn ts_callee(node: Node, src: &[u8]) -> Option<(String, CodeSymbolKind)> {
    match node.kind() {
        "identifier" => Some((text(node, src).to_owned(), CodeSymbolKind::Function)),
        "member_expression" => node
            .child_by_field_name("property")
            .map(|n| (text(n, src).to_owned(), CodeSymbolKind::Method)),
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────
// Kotlin
// ─────────────────────────────────────────────────────────────

fn visit_kotlin(node: Node, scope: Option<&Scope>, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    match node.kind() {
        "function_declaration" => kt_function(node, scope, ctx),
        "class_declaration" => kt_class(node, scope, ctx),
        "object_declaration" => kt_object(node, scope, ctx),
        "import" => kt_import(node, scope, ctx),
        "property_declaration" => kt_property(node, scope, ctx),
        "call_expression" => {
            kt_call(node, caller, ctx);
            visit_children(node, scope, caller, ctx); // args may hold nested calls
        }
        _ => visit_children(node, scope, caller, ctx),
    }
}

/// Text of the first direct named child of `kind` (the Kotlin grammar uses plain
/// `identifier` children rather than `name` fields).
fn kt_child_text(node: Node, kind: &str, src: &[u8]) -> Option<String> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor)
        .find(|c| c.kind() == kind)
        .map(|n| text(n, src).to_owned())
}

fn kt_function(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name) = kt_child_text(node, "identifier", ctx.src) else {
        return;
    };
    let kind = if scope.is_some_and(|s| s.is_type) {
        CodeSymbolKind::Method
    } else {
        CodeSymbolKind::Function
    };
    let sym = ctx.symbol(node, name, kind, scope);
    ctx.push_symbol(scope, sym.clone());
    let fn_scope = Scope {
        symbol: sym.clone(),
        is_type: false,
    };
    visit_children(node, Some(&fn_scope), Some(&sym), ctx);
}

/// True when a direct (unnamed keyword) child of `kind` exists.
fn kt_has_keyword(node: Node, kind: &str) -> bool {
    let mut cursor = node.walk();
    node.children(&mut cursor)
        .any(|c| !c.is_named() && c.kind() == kind)
}

/// True when the declaration's `modifiers` hold a `modifier_kind` entry whose keyword is
/// `keyword` (e.g. `class_modifier` → `enum`, `property_modifier` → `const`).
fn kt_has_modifier_keyword(node: Node, modifier_kind: &str, keyword: &str) -> bool {
    let mut cursor = node.walk();
    node.named_children(&mut cursor)
        .filter(|c| c.kind() == "modifiers")
        .any(|m| {
            let mut mc = m.walk();
            m.named_children(&mut mc)
                .filter(|c| c.kind() == modifier_kind)
                .any(|entry| kt_has_keyword(entry, keyword))
        })
}

fn kt_class(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name) = kt_child_text(node, "identifier", ctx.src) else {
        return;
    };
    // `interface X` has an `interface` keyword child; `enum class X` carries the
    // `enum` keyword inside modifiers → class_modifier.
    let kind = if kt_has_keyword(node, "interface") {
        CodeSymbolKind::Trait
    } else if kt_has_modifier_keyword(node, "class_modifier", "enum") {
        CodeSymbolKind::Enum
    } else {
        CodeSymbolKind::Class
    };
    let sym = ctx.symbol(node, name, kind, scope);
    ctx.push_symbol(scope, sym.clone());
    // Supertypes: `constructor_invocation` = superclass (Class), plain `user_type` = interface (Trait).
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() != "delegation_specifiers" {
            continue;
        }
        let mut dc = child.walk();
        for spec in child.named_children(&mut dc) {
            if spec.kind() != "delegation_specifier" {
                continue;
            }
            let mut sc = spec.walk();
            let invocation = spec
                .named_children(&mut sc)
                .find(|c| c.kind() == "constructor_invocation");
            let (holder, kind) = invocation.map_or_else(
                || (spec, CodeSymbolKind::Trait),
                |inv| (inv, CodeSymbolKind::Class),
            );
            if let Some(name) = kt_child_text(holder, "user_type", ctx.src) {
                let to = ctx.placeholder(spec, name, kind);
                ctx.push_relation(sym.clone(), to, CodeRelationKind::Inherits);
            }
        }
    }
    let class_scope = Scope {
        symbol: sym,
        is_type: true,
    };
    visit_children(node, Some(&class_scope), None, ctx);
}

/// `object Foo` — a Kotlin singleton; mapped onto the Class kind.
fn kt_object(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(name) = kt_child_text(node, "identifier", ctx.src) else {
        return;
    };
    let sym = ctx.symbol(node, name, CodeSymbolKind::Class, scope);
    ctx.push_symbol(scope, sym.clone());
    let obj_scope = Scope {
        symbol: sym,
        is_type: true,
    };
    visit_children(node, Some(&obj_scope), None, ctx);
}

/// `import a.b.C` / `import a.b.C as D` — one Import symbol per header (no field names;
/// the alias is the `identifier` child after the `as` keyword).
fn kt_import(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    let Some(path) = kt_child_text(node, "qualified_identifier", ctx.src) else {
        return;
    };
    let alias = if kt_has_keyword(node, "as") {
        // the second identifier-ish child is the alias (the first is the qualified path)
        let mut cursor = node.walk();
        node.named_children(&mut cursor)
            .filter(|c| c.kind() == "identifier")
            .last()
            .map(|n| text(n, ctx.src).to_owned())
    } else {
        None
    };
    let name = alias.unwrap_or_else(|| last_segment(&path));
    push_import(ctx, scope, node, name, path);
}

/// Top-level `const val` → Constant; plain `val`/`var` → Variable. Module-level only.
fn kt_property(node: Node, scope: Option<&Scope>, ctx: &mut Ctx) {
    if scope.is_some() {
        return; // module-level only, not class members or locals
    }
    let is_const = kt_has_modifier_keyword(node, "property_modifier", "const");
    let Some(decl) = ({
        let mut cursor = node.walk();
        node.named_children(&mut cursor)
            .find(|c| c.kind() == "variable_declaration")
    }) else {
        return;
    };
    let Some(name) = kt_child_text(decl, "identifier", ctx.src) else {
        return;
    };
    let kind = if is_const {
        CodeSymbolKind::Constant
    } else {
        CodeSymbolKind::Variable
    };
    let sym = ctx.symbol(node, name, kind, None);
    ctx.push_symbol(None, sym);
}

fn kt_call(node: Node, caller: Option<&CodeSymbol>, ctx: &mut Ctx) {
    let Some(caller) = caller else {
        return;
    };
    let mut cursor = node.walk();
    let Some(callee_node) = node.named_children(&mut cursor).next() else {
        return;
    };
    if let Some((name, kind)) = kt_callee(callee_node, ctx.src) {
        let to = ctx.placeholder(node, name, kind);
        ctx.push_relation(caller.clone(), to, CodeRelationKind::Calls);
    }
}

/// Callee name + kind from a Kotlin call target (the first child of `call_expression`):
/// `foo()` → Function, `a.b.foo()` → Method (last identifier of the navigation chain).
fn kt_callee(node: Node, src: &[u8]) -> Option<(String, CodeSymbolKind)> {
    match node.kind() {
        "identifier" => Some((text(node, src).to_owned(), CodeSymbolKind::Function)),
        "navigation_expression" => {
            let mut cursor = node.walk();
            node.named_children(&mut cursor)
                .filter(|c| c.kind() == "identifier")
                .last()
                .map(|n| (text(n, src).to_owned(), CodeSymbolKind::Method))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    const MAX: usize = 200;

    fn rust(src: &str) -> (Vec<CodeSymbol>, Vec<CodeRelation>) {
        parse_source(src, "src/lib.rs", CodeLanguage::Rust, MAX).unwrap()
    }

    fn python(src: &str) -> (Vec<CodeSymbol>, Vec<CodeRelation>) {
        parse_source(src, "pkg/mod.py", CodeLanguage::Python, MAX).unwrap()
    }

    fn typescript(src: &str) -> (Vec<CodeSymbol>, Vec<CodeRelation>) {
        parse_source(src, "src/app.ts", CodeLanguage::TypeScript, MAX).unwrap()
    }

    fn kotlin(src: &str) -> (Vec<CodeSymbol>, Vec<CodeRelation>) {
        parse_source(src, "src/App.kt", CodeLanguage::Kotlin, MAX).unwrap()
    }

    fn symbols_of(symbols: &[CodeSymbol], kind: CodeSymbolKind) -> Vec<&str> {
        symbols
            .iter()
            .filter(|s| s.kind == kind)
            .map(|s| s.name.as_str())
            .collect()
    }

    fn relations_of(relations: &[CodeRelation], kind: CodeRelationKind) -> Vec<(String, String)> {
        relations
            .iter()
            .filter(|r| r.kind == kind)
            .map(|r| (r.from.name.clone(), r.to.name.clone()))
            .collect()
    }

    #[test]
    fn rust_extracts_symbol_kinds() {
        let (symbols, _) = rust(
            r"
use std::collections::HashMap;
use crate::util as helper;

const LIMIT: usize = 10;
static COUNTER: u64 = 0;

pub struct Store { map: HashMap<String, u64> }
enum Mode { Fast, Slow }
trait Repo { fn find(&self, id: u64) -> String; }
mod inner { fn nested() {} }

fn top_level(x: u64) -> u64 { x }

impl Store {
    pub fn get(&self, key: &str) -> u64 { 0 }
    fn put(&mut self) {}
}
",
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Function),
            ["nested", "top_level"]
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Method),
            ["find", "get", "put"]
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Struct), ["Store"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Enum), ["Mode"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Trait), ["Repo"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Module), ["inner"]);
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Import),
            ["HashMap", "helper"]
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Constant), ["LIMIT"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Variable), ["COUNTER"]);
        // every symbol records its source path and 1-based lines
        let store = symbols.iter().find(|s| s.name == "Store").unwrap();
        assert_eq!(store.source_path, "src/lib.rs");
        assert!(store.start_line > 1 && store.end_line >= store.start_line);
    }

    #[test]
    fn rust_method_parent_and_contains_edges() {
        let (symbols, relations) = rust(
            r"
struct S;
impl S {
    fn m(&self) {}
    fn n(&self) {}
}
mod outer { fn inside() {} }
",
        );
        let m = symbols.iter().find(|s| s.name == "m").unwrap();
        assert_eq!(m.parent, "S");
        assert_eq!(m.kind, CodeSymbolKind::Method);
        let contains = relations_of(&relations, CodeRelationKind::Contains);
        assert!(contains.contains(&("S".to_owned(), "m".to_owned())));
        assert!(contains.contains(&("S".to_owned(), "n".to_owned())));
        assert!(contains.contains(&("outer".to_owned(), "inside".to_owned())));
    }

    #[test]
    fn rust_calls_and_inherits_relations() {
        let (symbols, relations) = rust(
            r"
fn helper() {}
fn main_fn() {
    helper();
    helper(); // duplicate call — deduped
    let v = vec![1];
    v.iter().map(f).collect::<Vec<_>>();
    String::new();
}
trait T {}
struct S;
impl T for S {}
",
        );
        // callees are placeholders (empty source_path), never symbols
        assert!(
            !symbols
                .iter()
                .any(|s| s.name == "helper" && s.source_path.is_empty())
        );
        let calls = relations_of(&relations, CodeRelationKind::Calls);
        assert_eq!(
            calls
                .iter()
                .filter(|(f, t)| f == "main_fn" && t == "helper")
                .count(),
            1
        );
        assert!(calls.contains(&("main_fn".to_owned(), "map".to_owned())));
        assert!(calls.contains(&("main_fn".to_owned(), "new".to_owned())));
        let inherits = relations_of(&relations, CodeRelationKind::Inherits);
        assert!(inherits.contains(&("S".to_owned(), "T".to_owned())));
    }

    #[test]
    fn rust_import_edges_point_at_full_path() {
        let (_, relations) = rust("use std::collections::HashMap;\n");
        let imports = relations_of(&relations, CodeRelationKind::Imports);
        assert_eq!(
            imports,
            [("HashMap".to_owned(), "std::collections::HashMap".to_owned())]
        );
    }

    #[test]
    fn python_extracts_symbol_kinds() {
        let (symbols, _) = python(
            r"
import os
import collections as col
from typing import List, Optional
from . import sibling

MAX_RETRIES = 3
lowercase_var = 1

def top_level(x):
    return x

class Base:
    pass

class Child(Base, metaclass=type):
    def method(self):
        pass

    @staticmethod
    def static_m():
        pass
",
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Function),
            ["top_level"]
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Method),
            ["method", "static_m"]
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Class),
            ["Base", "Child"]
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Constant),
            ["MAX_RETRIES"]
        );
        // lowercase module-level assignment is not a constant — skipped
        assert!(!symbols.iter().any(|s| s.name == "lowercase_var"));
        let mut imports = symbols_of(&symbols, CodeSymbolKind::Import);
        imports.sort_unstable();
        assert_eq!(imports, ["List", "Optional", "col", "os", "sibling"]);
    }

    #[test]
    fn python_relations() {
        let (symbols, relations) = python(
            r"
from pkg.base import Base

def helper():
    pass

class Child(Base):
    def run(self):
        helper()
        self.close()
",
        );
        let run = symbols.iter().find(|s| s.name == "run").unwrap();
        assert_eq!(run.parent, "Child");
        assert_eq!(run.kind, CodeSymbolKind::Method);
        let contains = relations_of(&relations, CodeRelationKind::Contains);
        assert!(contains.contains(&("Child".to_owned(), "run".to_owned())));
        let calls = relations_of(&relations, CodeRelationKind::Calls);
        assert!(calls.contains(&("run".to_owned(), "helper".to_owned())));
        assert!(calls.contains(&("run".to_owned(), "close".to_owned())));
        let inherits = relations_of(&relations, CodeRelationKind::Inherits);
        assert!(inherits.contains(&("Child".to_owned(), "Base".to_owned())));
        let imports = relations_of(&relations, CodeRelationKind::Imports);
        assert_eq!(imports, [("Base".to_owned(), "pkg.base.Base".to_owned())]);
    }

    #[test]
    fn python_decorated_and_async_functions_are_found() {
        let (symbols, _) = python(
            r#"
@app.route("/x")
async def handler(req):
    return req
"#,
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Function), ["handler"]);
    }

    #[test]
    fn symbol_cap_is_respected() {
        use std::fmt::Write as _;
        let mut src = String::new();
        for i in 0..10 {
            writeln!(src, "fn f{i}() {{}}").unwrap();
        }
        let (symbols, _) = rust(&src);
        assert_eq!(symbols.len(), 10);
        let (symbols, _) = parse_source(&src, "x.rs", CodeLanguage::Rust, 3).unwrap();
        assert_eq!(symbols.len(), 3);
    }

    #[test]
    fn signature_is_bounded_and_bodyless() {
        let long_params = (0..30)
            .map(|i| format!("param_{i}: SomeVeryLongTypeName{i}"))
            .collect::<Vec<_>>()
            .join(", ");
        let src = format!("fn long_fn({long_params}) {{ let body = 1; }}\n");
        let (symbols, _) = rust(&src);
        let sig = &symbols[0].signature;
        assert!(
            sig.chars().count() <= 120,
            "sig len {}",
            sig.chars().count()
        );
        assert!(
            !sig.contains("body"),
            "signature must not contain the body: {sig}"
        );
    }

    #[test]
    fn garbage_source_does_not_fail() {
        // tree-sitter is error-tolerant: unparseable input yields few/no symbols, not an Err.
        let (symbols, relations) = rust("fn {{ broken @@##\n let = = =");
        assert!(symbols.len() <= 2);
        assert!(relations.is_empty());
        let (symbols, _) = python("\x00\x01 not python at all }{");
        assert!(symbols.is_empty());
    }

    #[test]
    fn max_symbols_zero_yields_nothing() {
        let (symbols, relations) =
            parse_source("fn a() {}\n", "x.rs", CodeLanguage::Rust, 0).unwrap();
        assert!(symbols.is_empty());
        assert!(relations.is_empty());
    }

    // ── TypeScript ────────────────────────────────────────────

    #[test]
    fn ts_extracts_symbol_kinds() {
        let (symbols, _) = typescript(
            r"
import React from 'react';
import * as utils from './utils';
import { fetchUser, Cache as C } from './api';

const LIMIT: number = 10;
let counter = 0;

const TopLevel = (x: number): number => x + 1;

enum Mode { Fast, Slow }

interface Repo extends BaseRepo {
  lookup(id: number): string;
}

namespace inner {
  export function nested() {}
}

function top_level(x: number): number {
  return x;
}

class Store extends BaseStore implements Repo {
  get(key: string): number { return 0; }
  find(id: number): string { return ''; }
}
",
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Function),
            ["TopLevel", "nested", "top_level"]
        );
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Method),
            ["lookup", "get", "find"]
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Class), ["Store"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Trait), ["Repo"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Enum), ["Mode"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Module), ["inner"]);
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Import),
            ["React", "utils", "fetchUser", "C"]
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Constant), ["LIMIT"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Variable), ["counter"]);
        let store = symbols.iter().find(|s| s.name == "Store").unwrap();
        assert_eq!(store.source_path, "src/app.ts");
        assert_eq!(store.language, CodeLanguage::TypeScript);
    }

    #[test]
    fn ts_class_heritage_and_contains_edges() {
        let (symbols, relations) = typescript(
            r"
interface Repo { lookup(id: number): string; }
class Store extends BaseStore implements Repo {
  get(key: string): number { return 0; }
}
",
        );
        let get = symbols.iter().find(|s| s.name == "get").unwrap();
        assert_eq!(get.parent, "Store");
        assert_eq!(get.kind, CodeSymbolKind::Method);
        let contains = relations_of(&relations, CodeRelationKind::Contains);
        assert!(contains.contains(&("Store".to_owned(), "get".to_owned())));
        let inherits = relations_of(&relations, CodeRelationKind::Inherits);
        assert!(inherits.contains(&("Store".to_owned(), "BaseStore".to_owned())));
        assert!(inherits.contains(&("Store".to_owned(), "Repo".to_owned())));
    }

    #[test]
    fn ts_call_edges_use_placeholders() {
        let (_, relations) = typescript(
            r"
function helper(): void {}
function boot(): void {
  helper();
  api.fetchUser(2);
}
",
        );
        let calls = relations_of(&relations, CodeRelationKind::Calls);
        assert!(calls.contains(&("boot".to_owned(), "helper".to_owned())));
        assert!(calls.contains(&("boot".to_owned(), "fetchUser".to_owned())));
    }

    #[test]
    fn tsx_parses_jsx_components() {
        let (symbols, _) = parse_source(
            r#"
const Card = ({ title }: { title: string }) => {
  return <div className="card">{title}</div>;
};

export function Page(): JSX.Element {
  return <Card title="hi" />;
}
"#,
            "src/page.tsx",
            CodeLanguage::TypeScript,
            MAX,
        )
        .unwrap();
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Function),
            ["Card", "Page"]
        );
    }

    // ── Kotlin ────────────────────────────────────────────────

    #[test]
    fn kt_extracts_symbol_kinds() {
        let (symbols, _) = kotlin(
            r#"
import kotlin.collections.List
import com.foo.Bar as B

const val LIMIT: Int = 10
var counter = 0
val label = "x"

interface Repo {
    fun lookup(id: Long): String
}

enum class Mode { FAST, SLOW }

object Singleton {
    fun create(): Store = Store()
}

class Store : BaseStore(), Repo {
    override fun find(id: Long): String = ""
    fun get(key: String): Int { return 0 }
}

fun topLevel(x: Int): Int { return x }
"#,
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Function), ["topLevel"]);
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Method),
            ["lookup", "create", "find", "get"]
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Trait), ["Repo"]);
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Class),
            ["Singleton", "Store"]
        );
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Enum), ["Mode"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Import), ["List", "B"]);
        assert_eq!(symbols_of(&symbols, CodeSymbolKind::Constant), ["LIMIT"]);
        assert_eq!(
            symbols_of(&symbols, CodeSymbolKind::Variable),
            ["counter", "label"]
        );
        let store = symbols.iter().find(|s| s.name == "Store").unwrap();
        assert_eq!(store.source_path, "src/App.kt");
        assert_eq!(store.language, CodeLanguage::Kotlin);
    }

    #[test]
    fn kt_class_delegation_and_contains_edges() {
        let (symbols, relations) = kotlin(
            r"
interface Repo { fun lookup(id: Long): String }
class Store : BaseStore(), Repo {
    fun get(key: String): Int { return 0 }
}
",
        );
        let get = symbols.iter().find(|s| s.name == "get").unwrap();
        assert_eq!(get.parent, "Store");
        assert_eq!(get.kind, CodeSymbolKind::Method);
        let contains = relations_of(&relations, CodeRelationKind::Contains);
        assert!(contains.contains(&("Store".to_owned(), "get".to_owned())));
        let inherits = relations_of(&relations, CodeRelationKind::Inherits);
        // constructor_invocation → superclass (Class), plain user_type → interface (Trait)
        assert!(inherits.contains(&("Store".to_owned(), "BaseStore".to_owned())));
        assert!(inherits.contains(&("Store".to_owned(), "Repo".to_owned())));
    }

    #[test]
    fn kt_call_edges_use_placeholders() {
        let (_, relations) = kotlin(
            r"
fun helper(): Int { return 0 }
fun boot(): Int {
    helper()
    Singleton.create()
    return 0
}
",
        );
        let calls = relations_of(&relations, CodeRelationKind::Calls);
        assert!(calls.contains(&("boot".to_owned(), "helper".to_owned())));
        assert!(calls.contains(&("boot".to_owned(), "create".to_owned())));
    }
}
