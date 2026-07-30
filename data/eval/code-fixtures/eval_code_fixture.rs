//! Code-lane eval fixture — known symbols for the /code-search golden gate.
//! Indexed by `make code-index` like any other repo file; never shipped logic.

/// Golden symbol: the eval gate expects `eval_fixture_parse` to be findable.
pub fn eval_fixture_parse(input: &str) -> Vec<String> {
    input.split_whitespace().map(str::to_owned).collect()
}

/// Golden symbol: a struct the eval gate can look up by name.
pub struct EvalFixtureConfig {
    pub max_items: usize,
    pub strict: bool,
}

impl EvalFixtureConfig {
    /// Golden symbol: a method nested under the struct (parent linkage).
    pub fn eval_fixture_validate(&self) -> bool {
        self.strict && self.max_items > 0
    }
}
