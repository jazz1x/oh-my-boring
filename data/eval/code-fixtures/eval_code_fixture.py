"""Code-lane eval fixture — known symbols for the /code-search golden gate.

Indexed by `make code-index` like any other repo file; never shipped logic.
"""


class EvalFixtureHandler:
    """Golden symbol: a class the eval gate can look up by name."""

    def eval_fixture_handle(self, payload: dict) -> dict:
        """Golden symbol: a method nested under the class (parent linkage)."""
        return {"ok": bool(payload)}


def eval_fixture_normalize(text: str) -> str:
    """Golden symbol: a top-level function the eval gate expects to find."""
    return " ".join(text.split()).lower()
