/**
 * Code-lane eval fixture — known TypeScript symbols for the /code-search golden gate.
 * Indexed by `make code-index` like any other repo file; never shipped logic.
 */

/** Golden symbol: an interface the eval gate can look up by name. */
export interface EvalFixtureStore {
  readonly maxItems: number;
}

/** Golden symbol: a class implementing the fixture interface. */
export class EvalFixtureCache implements EvalFixtureStore {
  readonly maxItems = 5;

  /** Golden symbol: a method nested under the class (parent linkage). */
  eval_fixture_get(key: string): string {
    return key.trim();
  }
}

/** Golden symbol: an arrow-function component/utility the eval gate expects to find. */
export const eval_fixture_render = (value: string): string => `<b>${value}</b>`;
