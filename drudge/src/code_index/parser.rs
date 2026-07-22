use std::collections::HashMap;

use tree_sitter::{Node, Parser};

use super::{
    CodeIndexError, ParseStatus, ParsedFile, Relation, RelationKind, Symbol, SymbolKind, stable_id,
};

pub(super) fn parse_rust(
    repository_id: &str,
    relative_path: &str,
    _file_id: &str,
    source: &str,
) -> Result<ParsedFile, CodeIndexError> {
    let mut parser = Parser::new();
    parser.set_language(&tree_sitter_rust::LANGUAGE.into())?;
    let tree = parser
        .parse(source, None)
        .ok_or(CodeIndexError::ParserCancelled)?;
    let root = tree.root_node();
    let error_count = count_errors(root);
    let status = if error_count == 0 {
        ParseStatus::Parsed
    } else {
        ParseStatus::ParsedWithErrors
    };
    let mut output = ParsedFile {
        status,
        error_count,
        symbols: Vec::new(),
        relations: Vec::new(),
    };
    let mut symbol_occurrences = HashMap::new();
    let mut relation_occurrences = HashMap::new();
    walk(
        root,
        source.as_bytes(),
        repository_id,
        relative_path,
        None,
        "",
        &mut symbol_occurrences,
        &mut relation_occurrences,
        &mut output,
    );
    Ok(output)
}

#[allow(clippy::too_many_arguments)]
fn walk(
    node: Node<'_>,
    source: &[u8],
    repository_id: &str,
    relative_path: &str,
    parent_symbol_id: Option<&str>,
    parent_qualified_name: &str,
    symbol_occurrences: &mut HashMap<String, usize>,
    relation_occurrences: &mut HashMap<String, usize>,
    output: &mut ParsedFile,
) {
    let impl_scope = (node.kind() == "impl_item")
        .then(|| impl_name(node, source))
        .flatten()
        .map(|name| qualify(parent_qualified_name, &name));
    let definition = symbol_definition(node, source);
    let (scope_symbol_id, scope_qualified_name) = if let Some((kind, name)) = definition {
        let qualified_name = if parent_qualified_name.is_empty() {
            name.clone()
        } else {
            format!("{parent_qualified_name}::{name}")
        };
        let signature = definition_signature(node, source);
        let identity = format!("{}\0{qualified_name}\0{signature}", kind.as_str());
        let occurrence = symbol_occurrences.entry(identity).or_default();
        let ordinal = occurrence.to_string();
        *occurrence += 1;
        let symbol_id = stable_id(&[
            "symbol",
            repository_id,
            relative_path,
            kind.as_str(),
            &qualified_name,
            &signature,
            &ordinal,
        ]);
        output.symbols.push(Symbol {
            id: symbol_id.clone(),
            kind,
            name,
            qualified_name: qualified_name.clone(),
            start_byte: node.start_byte(),
            end_byte: node.end_byte(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
        });
        push_relation(
            output,
            relation_occurrences,
            repository_id,
            relative_path,
            parent_symbol_id,
            RelationKind::Contains,
            Some(&symbol_id),
            None,
            node,
            source,
        );
        (Some(symbol_id), qualified_name)
    } else if let Some(qualified_name) = impl_scope {
        (parent_symbol_id.map(str::to_owned), qualified_name)
    } else {
        (
            parent_symbol_id.map(str::to_owned),
            parent_qualified_name.to_owned(),
        )
    };

    collect_relation(
        node,
        source,
        repository_id,
        relative_path,
        scope_symbol_id.as_deref(),
        relation_occurrences,
        output,
    );

    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        walk(
            child,
            source,
            repository_id,
            relative_path,
            scope_symbol_id.as_deref(),
            &scope_qualified_name,
            symbol_occurrences,
            relation_occurrences,
            output,
        );
    }
}

fn qualify(parent: &str, name: &str) -> String {
    if parent.is_empty() {
        name.to_owned()
    } else {
        format!("{parent}::{name}")
    }
}

fn symbol_definition(node: Node<'_>, source: &[u8]) -> Option<(SymbolKind, String)> {
    let kind = match node.kind() {
        "function_item" | "function_signature_item" => SymbolKind::Function,
        "struct_item" => SymbolKind::Struct,
        "enum_item" => SymbolKind::Enum,
        "union_item" => SymbolKind::Union,
        "trait_item" => SymbolKind::Trait,
        "type_item" => SymbolKind::TypeAlias,
        "const_item" => SymbolKind::Constant,
        "static_item" => SymbolKind::Static,
        "mod_item" => SymbolKind::Module,
        "macro_definition" => SymbolKind::Macro,
        _ => return None,
    };
    node.child_by_field_name("name")
        .and_then(|name| node_text(name, source))
        .map(|name| (kind, name))
}

fn definition_signature(node: Node<'_>, source: &[u8]) -> String {
    let declaration_end = ["body", "value"]
        .into_iter()
        .filter_map(|field| node.child_by_field_name(field))
        .map(|child| child.start_byte())
        .min()
        .unwrap_or_else(|| node.end_byte());
    without_ascii_whitespace(&source[node.start_byte()..declaration_end])
}

fn impl_name(node: Node<'_>, source: &[u8]) -> Option<String> {
    let target = node
        .child_by_field_name("type")
        .map(|child| without_ascii_whitespace(&source[child.start_byte()..child.end_byte()]))?;
    let implemented_trait = node
        .child_by_field_name("trait")
        .map(|child| without_ascii_whitespace(&source[child.start_byte()..child.end_byte()]));
    Some(implemented_trait.map_or_else(
        || format!("impl {target}"),
        |trait_name| format!("impl {trait_name} for {target}"),
    ))
}

fn collect_relation(
    node: Node<'_>,
    source: &[u8],
    repository_id: &str,
    relative_path: &str,
    source_symbol_id: Option<&str>,
    relation_occurrences: &mut HashMap<String, usize>,
    output: &mut ParsedFile,
) {
    let relation = match node.kind() {
        "use_declaration" => node
            .child_by_field_name("argument")
            .and_then(|argument| node_text(argument, source))
            .map(|target| (RelationKind::Imports, target)),
        "call_expression" => node
            .child_by_field_name("function")
            .and_then(|function| node_text(function, source))
            .map(|target| (RelationKind::Calls, target)),
        "type_identifier" | "identifier" if is_reference(node) => {
            node_text(node, source).map(|target| (RelationKind::References, target))
        }
        _ => None,
    };
    if let Some((kind, target_name)) = relation {
        push_relation(
            output,
            relation_occurrences,
            repository_id,
            relative_path,
            source_symbol_id,
            kind,
            None,
            Some(&target_name),
            node,
            source,
        );
    }
}

#[allow(clippy::too_many_arguments)]
fn push_relation(
    output: &mut ParsedFile,
    occurrences: &mut HashMap<String, usize>,
    repository_id: &str,
    relative_path: &str,
    source_symbol_id: Option<&str>,
    kind: RelationKind,
    target_symbol_id: Option<&str>,
    target_name: Option<&str>,
    node: Node<'_>,
    source: &[u8],
) {
    let syntax = if kind == RelationKind::Contains {
        String::new()
    } else {
        without_ascii_whitespace(&source[node.start_byte()..node.end_byte()])
    };
    let identity = [
        source_symbol_id.unwrap_or_default(),
        kind.as_str(),
        target_symbol_id.unwrap_or_default(),
        target_name.unwrap_or_default(),
        &syntax,
    ]
    .join("\0");
    let occurrence = occurrences.entry(identity).or_default();
    let ordinal = occurrence.to_string();
    *occurrence += 1;
    output.relations.push(Relation {
        id: stable_id(&[
            "relation",
            repository_id,
            relative_path,
            source_symbol_id.unwrap_or_default(),
            kind.as_str(),
            target_symbol_id.unwrap_or_default(),
            target_name.unwrap_or_default(),
            &syntax,
            &ordinal,
        ]),
        source_symbol_id: source_symbol_id.map(str::to_owned),
        kind,
        target_symbol_id: target_symbol_id.map(str::to_owned),
        target_name: target_name.map(str::to_owned),
        start_byte: node.start_byte(),
        end_byte: node.end_byte(),
    });
}

fn without_ascii_whitespace(bytes: &[u8]) -> String {
    String::from_utf8_lossy(
        &bytes
            .iter()
            .copied()
            .filter(|byte| !byte.is_ascii_whitespace())
            .collect::<Vec<_>>(),
    )
    .into_owned()
}

fn is_reference(node: Node<'_>) -> bool {
    let Some(parent) = node.parent() else {
        return false;
    };
    if parent
        .child_by_field_name("name")
        .is_some_and(|name| name.id() == node.id())
    {
        return false;
    }
    if parent
        .child_by_field_name("function")
        .is_some_and(|function| function.id() == node.id())
    {
        return false;
    }
    if parent
        .child_by_field_name("field")
        .is_some_and(|field| field.id() == node.id())
    {
        return false;
    }
    !matches!(
        parent.kind(),
        "use_declaration" | "scoped_identifier" | "scoped_type_identifier" | "use_as_clause"
    )
}

fn node_text(node: Node<'_>, source: &[u8]) -> Option<String> {
    node.utf8_text(source).ok().map(str::to_owned)
}

fn count_errors(node: Node<'_>) -> usize {
    let own = usize::from(node.is_error() || node.is_missing());
    let mut cursor = node.walk();
    own + node
        .named_children(&mut cursor)
        .map(count_errors)
        .sum::<usize>()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::parse_rust;
    use crate::code_index::{ParseStatus, RelationKind, SymbolKind};

    #[test]
    fn rust_parser_extracts_symbols_and_typed_relations() {
        let source = r"
            use crate::service::Widget;
            struct App;
            impl App {
                fn run(&self, widget: Widget) { widget.execute(); helper(); }
            }
            fn helper() {}
        ";
        let parsed = parse_rust("repo", "src/lib.rs", "file-id", source).unwrap();
        assert_eq!(parsed.status, ParseStatus::Parsed);
        assert_eq!(parsed.error_count, 0);
        assert!(parsed.symbols.iter().any(|symbol| {
            symbol.kind == SymbolKind::Function && symbol.qualified_name == "impl App::run"
        }));
        assert!(parsed.relations.iter().any(|relation| {
            relation.kind == RelationKind::Imports
                && relation.target_name.as_deref() == Some("crate::service::Widget")
        }));
        assert!(parsed.relations.iter().any(|relation| {
            relation.kind == RelationKind::Calls
                && relation.target_name.as_deref() == Some("widget.execute")
        }));
        assert!(parsed.relations.iter().any(|relation| {
            relation.kind == RelationKind::References
                && relation.target_name.as_deref() == Some("Widget")
        }));
        assert!(parsed.relations.iter().all(|relation| {
            relation.kind == RelationKind::Contains || relation.target_symbol_id.is_none()
        }));
    }

    #[test]
    fn preserves_scoped_call_syntax_and_trait_signatures() {
        let parsed = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "trait Runner { fn run(&self); }\nfn invoke() { crate::service::run(); }\n",
        )
        .unwrap();
        assert!(parsed.symbols.iter().any(|symbol| {
            symbol.kind == SymbolKind::Function && symbol.qualified_name == "Runner::run"
        }));
        assert!(parsed.relations.iter().any(|relation| {
            relation.kind == RelationKind::Calls
                && relation.target_name.as_deref() == Some("crate::service::run")
                && relation.target_symbol_id.is_none()
        }));
    }

    #[test]
    fn ids_are_stable_across_line_shifts_and_parse_errors_are_explicit() {
        let before = parse_rust("repo", "src/lib.rs", "file-id", "fn stable() {}\n").unwrap();
        let after = parse_rust("repo", "src/lib.rs", "file-id", "\n\nfn stable() {}\n").unwrap();
        assert_eq!(before.symbols[0].id, after.symbols[0].id);

        let broken = parse_rust("repo", "src/broken.rs", "broken", "fn broken( {").unwrap();
        assert_eq!(broken.status, ParseStatus::ParsedWithErrors);
        assert!(broken.error_count > 0);
    }

    #[test]
    fn symbol_ids_survive_body_only_edits() {
        let before = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "fn stable(value: u8) -> u8 { value + 1 }\n",
        )
        .unwrap();
        let after = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "fn stable( value : u8 ) -> u8 { value.saturating_add(2) }\n",
        )
        .unwrap();
        assert_eq!(before.symbols[0].id, after.symbols[0].id);
    }

    #[test]
    fn method_ids_use_impl_scopes_and_survive_line_shifts_and_multiple_blocks() {
        let before = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "struct App;\nimpl App { fn first(&self) {} }\nimpl App { fn stable(&self) {} }\n",
        )
        .unwrap();
        let after = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "\nstruct App;\n\nimpl App { fn first(&self) {} }\n\nimpl App { fn stable(&self) {} }\n",
        )
        .unwrap();
        let method_id = |parsed: &crate::code_index::ParsedFile| {
            parsed
                .symbols
                .iter()
                .find(|symbol| symbol.qualified_name == "impl App::stable")
                .unwrap()
                .id
                .clone()
        };
        assert_eq!(method_id(&before), method_id(&after));
        assert_eq!(
            before
                .symbols
                .iter()
                .filter(|symbol| symbol.qualified_name.starts_with("impl App::"))
                .count(),
            2
        );
    }

    #[test]
    fn duplicate_definitions_do_not_renumber_existing_symbol_ids() {
        let before = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "fn duplicate() { let retained = 1; }\n",
        )
        .unwrap();
        let retained_id = before.symbols[0].id.clone();
        let after = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "fn duplicate(_: u8) {}\nfn duplicate() { let retained = 1; }\n",
        )
        .unwrap();
        assert!(after.symbols.iter().any(|symbol| symbol.id == retained_id));
    }

    #[test]
    fn relation_ids_are_deterministic_across_line_shifts() {
        let before = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "fn helper() {}\nfn run() { helper(); helper(); }\n",
        )
        .unwrap();
        let after = parse_rust(
            "repo",
            "src/lib.rs",
            "file-id",
            "\n\nfn helper() {}\n\nfn run() { helper(); helper(); }\n",
        )
        .unwrap();
        let relation_ids = |parsed: &crate::code_index::ParsedFile| {
            let mut ids: Vec<_> = parsed
                .relations
                .iter()
                .map(|relation| relation.id.clone())
                .collect();
            ids.sort_unstable();
            ids
        };
        assert_eq!(relation_ids(&before), relation_ids(&after));
        assert_eq!(
            before
                .relations
                .iter()
                .filter(|relation| relation.kind == RelationKind::Calls)
                .map(|relation| relation.id.as_str())
                .collect::<std::collections::HashSet<_>>()
                .len(),
            2
        );
    }
}
