/**
 * Code-lane eval fixture — known Kotlin symbols for the /code-search golden gate.
 * Indexed by `make code-index` like any other repo file; never shipped logic.
 */

/** Golden symbol: an interface the eval gate can look up by name. */
interface EvalFixtureRepo {
    fun eval_fixture_find(id: Long): String
}

/** Golden symbol: a class with a delegation (superclass + interface). */
class EvalFixtureService : EvalFixtureRepo {
    override fun eval_fixture_find(id: Long): String = id.toString()
}

/** Golden symbol: a top-level function the eval gate expects to find. */
fun eval_fixture_route(name: String): String = name.trim()
