<!-- Decision: 2026-06-13. Song chose the 4-level model with default = Guard.
     This addendum layers over docs/design/2026-06-13_v0.1-design.md §6 (the canonical CRD).
     controlLevel sets the posture defaults; any explicitly-set field overrides (PSA-style level + exceptions). -->

# Argus Control Levels (v0.1)

> **The idea.** "How hard do I control this agent?" is answered by ONE named level, not 11 knobs.
> Direct precedent: Kubernetes Pod Security Admission (Privileged / Baseline / Restricted). Argus's axis is
> *control aggressiveness* — higher level = more invariants block, fewer fall back to shadow, availability is
> traded for safety. **Default = `Guard`.**

## The four levels

| Level | One-line posture | Use case |
|---|---|---|
| **Observe** (L0) | Global shadow. Detect + emit metrics/events, **block nothing** (`enforcement: Dryrun`). | Onboarding; measure the baseline / prove the runaway is happening before you arm anything. |
| **Guard** (L1, **default**) | Block the unambiguous, cheap-to-be-sure controls; **shadow** the fuzzy ones. The armed per-principal backstop is on and fails **closed**. | Adoption-safe production default. Stops the $47K loop without false-killing healthy agents. |
| **Enforce** (L2) | Block the fuzzy controls too (dedup, RateSpike); refuse unauthenticated principals. | Trusted prod agents you want held strictly. |
| **Lockdown** (L3) | Global **fail-closed**; any ambiguity rejects. | Untrusted / high-risk agents; availability deliberately sacrificed for control. |

## What each level sets (posture matrix)

`controlLevel` controls **action + failure mode + onMissing** — the *posture*. Numeric thresholds
(`stepCap`, `budgetUSD`, windows…) are independent knobs with their own defaults; a level does not change them
(except where noted). This mirrors PSA: levels are qualitative, exceptions are explicit.

| Knob | Observe | **Guard** | Enforce | Lockdown |
|---|---|---|---|---|
| `enforcement` (global) | `Dryrun` | `Enforce` | `Enforce` | `Enforce` |
| stepCap / budgetUSD (run+conv) | detect | **block** | block | block |
| ⭐ `backstop` (principal, armed) | detect | **block · fail-closed** | block · fail-closed | block · fail-closed |
| `inputHashDedup.action` | flag | **flag** | block | block |
| `patterns.recursionLoop.action` | warn | **block** | block | block |
| `patterns.retryStorm.action` | warn | **block** | block | block |
| `patterns.rateSpike.action` | warn | **warn** | block | block |
| global `failureMode` | open | **open** | open | **closed** |
| `identity.principal.onMissing` | degradeToConn | **degradeToConn** | **reject** | reject |
| evasion guards (mint-rate, coalesce) | on (observe) | on | on | on (tightest) |

**Design invariants baked in (the "control is the core" rules):**
1. **The backstop fails *closed* from Guard up — always.** It is the one axis where fail-open = unbounded loss, so
   the load-bearing control never yields, even while everything else stays fail-open for adoption.
2. **Ambiguous controls (dedup, RateSpike) shadow at low levels, block at high levels** — false-kill risk is managed
   *by the level*, not by asking every operator to reason about it.
3. `Observe` is global `Dryrun`: every per-action setting is moot; nothing blocks. The counters/tombstone logic still
   runs so the operator sees exactly what *would* have been blocked.

## CRD integration

```yaml
spec:
  controlLevel: Guard          # Observe | Guard | Enforce | Lockdown  (default Guard)
  # --- any explicitly-set field OVERRIDES the level's posture default (PSA-style targeted exception) ---
  budgetUSD: "10.00"
  inputHashDedup:
    action: block              # e.g. opt THIS policy into blocking dedup while staying otherwise Guard
```

- **Precedence (controller resolves at reconcile):** explicit field  >  `controlLevel` posture default  >  built-in default.
- The controller writes the **effective resolved posture** into `status` so an operator can see what Guard actually
  expanded to (no guessing).
- `controlLevel` is **not** a `backstop` override: the backstop is always armed; the level only flips its *enforcement*
  (detect at Observe → block from Guard).

## How it resolves the three ratification forks

| Fork (design doc §10) | Resolved by Guard default | Escalate via |
|---|---|---|
| dedup `flag` vs `block` | **flag** (shadow) | set `controlLevel: Enforce` or override `inputHashDedup.action: block` |
| backstop armed + independent fail-closed | **yes, armed + fail-closed** | (always on from Guard) |
| `principal.onMissing` reject | **degradeToConn** (adoption-safe) | `controlLevel: Enforce`/`Lockdown` or override `onMissing: reject` |

## Demo narrative (free win)

1. `controlLevel: Observe` — run the 4-agent ping-pong; dashboards show the runaway climbing, **nothing blocked**.
2. `kubectl patch` to `controlLevel: Guard` — the per-principal backstop trips, the loop **halts**, tombstone denies
   the retries. One field, dialed up. That *is* "controlling the agent."

## Open (tune in W5 against the corpus)

- The exact **numeric** defaults per knob (windows/thresholds) are tuned against the benign-agent + $47K-replay corpus;
  levels only set posture. A future refinement could let `Lockdown` also tighten thresholds — deferred past v0.1.
