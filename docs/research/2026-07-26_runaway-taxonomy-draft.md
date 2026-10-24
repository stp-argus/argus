# "What Is a Runaway?" — Draft Taxonomy

Written 2026-07-26, as discussion material for the team's re-opened question about the definition
of "runaway." Grounded in the empirical findings of
`2026-07-26_litellm-proxy-investigation.md` §d — that column is the load-bearing one below.
Headline: **threshold-shaped runaways are already well-served by LiteLLM. Argus exists for the
pattern-shaped ones and the principal backstop.**

## Why this table is needed

So far "runaway" has been treated implicitly as "call count exceeds N." But:
- Plain high-volume legitimate use (e.g. an intentional overnight batch job) can't be told apart
  from a runaway by count alone.
- LiteLLM already ships numeric caps (`max_budget`, `max_iterations`, `rpm_limit`, ...), so "what
  Argus catches" needs to be redefined by **pattern**, not raw numbers, for the product to have a
  reason to exist.

## Table

| Pattern | One-line definition | Distinguishing it from legitimate use | Detection axis | Caught by LiteLLM out of the box? | Sample-app scenario |
|---|---|---|---|---|---|
| **Budget overrun** | Cumulative spend crosses a fixed ceiling ($X) | The ceiling itself *is* the policy — no distinction needed, pure number | spend counter | ✅ **yes** (`max_budget` + `budget_duration`) | none (LiteLLM already covers it, low priority) |
| **Step-count overrun** | A single run/session's call count crosses a cap | The cap itself is the policy | counter | ✅ **yes** (`max_iterations`, `rpm_limit`) | none (same) |
| **Rate spike** | Call rate jumps sharply relative to baseline | Judged as a multiple of a rolling baseline (legitimate use can spike too — needs to be distinguished from "slow ramp") | sliding window | △ partial (`rpm_limit` is an absolute ceiling, not a "spike vs. baseline" judgment) | none → expansion candidate |
| **Retry storm** | Failed calls are retried at a very tight interval, repeatedly | Distinguished from normal retry (exponential backoff) by interval + count | pattern matcher | ❌ **no** — looks like ordinary calls that just push the counter up | ✅ **have it** (`retry-storm`) |
| **Ping-pong recursion** | Agents A↔B call each other indefinitely; rotating run-ids make each individual run look harmless | Per-run caps can't catch it — only cumulative-per-principal can | **principal backstop** | ❌ **no** — LiteLLM's team/user aggregation sums a manually-assigned key group, not "the same actor keeps rotating keys" | ✅ **have it** (`pingpong-recursion`) |
| **Duplicate-input repetition (dedup)** | The same (or near-identical) prompt repeats at short intervals | Hard to distinguish from intentional repetition (e.g. batch reprocessing) by "the interval is meaningless — the response is never used before the next identical call" — needs further design | input-hash comparison | ❌ **no** | none → expansion candidate (07-25 direction doc job B) |

## Three jobs this table does

1. **Team discussion material for "what is a runaway."** The "distinguishing from legitimate use"
   column is the contentious one — e.g. rate spike and dedup both carry false-positive risk and
   need threshold/window design in the engine spec. Item for the engine-core stream.
2. **Sample-app scenario roadmap** (07-25 direction doc job B). Rows marked ❌ under "Sample-app
   scenario" are the next scenarios to add. Priority: **dedup before rate spike** — dedup is the
   clearer "LiteLLM literally cannot do this" story for a demo.
3. **Demo/pitch explainer.** Answers "why not just use LiteLLM?" in one table: the two green (✅)
   rows are LiteLLM's job; the three red (❌) rows are what the Argus judgment engine builds.

## Open questions (need engine-core + team agreement)

- How should "rate spike" establish its baseline — per-user historical average? A fixed
  multiplier?
- What counts as "near-identical" for dedup — exact match? Embedding similarity?
- What exactly should the principal backstop accumulate across runs — call count? Spend? Both?
  This is a core design decision for the engine spec.
