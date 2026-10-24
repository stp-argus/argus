<!-- Research agent output, 2026-07-02 (parallel 3-agent recheck; see docs/design/2026-07-02_architecture-recheck.md for the synthesis).
     Deltas ONLY vs docs/research/2026-06-13_landscape-refresh.md. Claims tagged VERIFIED / REPORTED. -->

# Argus Landscape Delta Report — 2026-06-13 → 2026-07-02

## 1. InferCost — VERDICT: NO CHANGE

- **Releases page (VERIFIED, fetched 2026-07-02):** latest release is still **v0.5.0 (2026-06-04)**. No releases after the baseline date. https://github.com/defilantech/infercost/releases
- **README/roadmap (VERIFIED):** TokenBudget still listed under "Alert" phase as **"coming soon"**; **Enforce still "Planned"** (rate-limit over-budget teams, graceful model degradation). Repo active (51 commits main, 6 open PRs) but nothing enforcement-shaped has landed. https://github.com/defilantech/infercost
- Structural collision remains **potential, not actual**: InferCost is still observe/report-only. Its Enforce concept remains rate-limiting/degradation, not kill semantics.

## 2. LiteLLM — VERDICT: CHANGED (incremental, plus a naming landmine)

- **v1.90.0 (2026-06-26/27) (VERIFIED via docs.litellm.ai/release_notes):**
  - Budget robustness fixes that mirror Argus's reserve-then-settle mechanics: *enforce budgets against authoritative DB spend when cross-pod counter is stale*, *release budget reservation when a request is cancelled mid-flight*, *record partial spend on interrupted streams*, *recompute budget_reset_at on budget_duration change*. https://docs.litellm.ai/release_notes/v1.90.0/v1-90-0
  - **No new features found** for loop detection, input-hash dedup, max_iterations extensions, pattern detection, or Kubernetes CRDs in v1.90.0. v1.91.0-rc.1 (2026-06-28) has no notable notes yet. https://github.com/BerriAI/litellm/releases
- **⚠ Naming collision (VERIFIED existence, REPORTED details):** v1.90.0 ships a new guardrail integration named **"Repello Argus"** (PR #30465) — Repello AI's commercial product **Argus** is a "runtime AI security & guardrails" layer (usage monitoring, abuse detection, policy enforcement). https://repello.ai/argus , https://docs.litellm.ai/docs/guardrail_providers . A commercial "Argus" now exists inside LiteLLM's guardrail namespace in the runtime-AI-guardrail category — direct trademark/SEO collision for the project name.
- Net: LiteLLM continues hardening proxy-level budgets (reservation semantics now match Argus's design) but still no K8s CRD, no dedup-kill, no pattern taxonomy.

## 3. Tokenomics Foundation — VERDICT: UNCLEAR (launch imminent/just occurred; scope confirmed non-enforcement)

- Intent announcement was **2026-06-03** (pre-baseline). Formal launch was slated for July; as of 2026-07-02 unclear if formally launched.
- **tokeneconomics.com (VERIFIED, fetched):** site live; scope = three pillars — **token production ("token factory effectiveness"), consumption ("FinOps for AI"), monetization ("AI value")**. No working groups/roadmap published yet. https://www.tokeneconomics.com/
- HPCwire headline says "Linux Foundation **Launches** Tokenomics Foundation" but page 403'd (REPORTED only). https://www.hpcwire.com/off-the-wire/linux-foundation-launches-tokenomics-foundation-to-standardize-ai-infrastructure-economics/
- Founding supporters (REPORTED, LF press): Accenture, Booking.com, Flexera, Google Cloud, IBM, JPMorganChase, KPMG, Microsoft, Oracle, Salesforce, SAP, ServiceNow; Technical Committee to fund FOCUS spec expansion into token spend models. https://www.linuxfoundation.org/press/linux-foundation-announces-the-intent-to-launch-the-tokenomics-foundation-to-establish-open-standards-for-ai-cost-management
- **Key finding: scope is measurement/benchmarking/accounting (FOCUS-style specs), not enforcement.** It standardizes the *ledger*, not the *breaker* — complementary to Argus (a future FOCUS-token export from Argus's settle ledger could be a checkbox feature).

## 4. New OSS in the niche — VERDICT: CHANGED (one real new entrant: Faramesh)

- **CNCF sandbox issues created after 2026-06-13 (VERIFIED, fetched filtered issue list):** #496 kbind (Jun 17), #498 CNMSQL (Jun 27), **#500 Faramesh (Jun 27)**, #501 K2s (Jun 29). Only Faramesh is relevant.
- **Faramesh (#500, VERIFIED both issue and repo):** "deterministic runtime policy and evidence layer for AI agent tool calls" — intercepts tool calls pre-execution, permit/defer/deny via its own policy language (FPL), **includes budget caps (`daily { max $20 on_exceed defer }`) and per-tool rate limits**. Go, Apache-2.0, ~93 stars, v0.2.0 (2026-05-24), 253 commits, SDK shims/MCP proxies for 13+ frameworks incl. LangChain/CrewAI/Claude Agents SDK. **Critically: local-daemon architecture, no Kubernetes CRDs** — deployable "as sidecars" but not operator/CRD-shaped. It sits at the *tool-call action boundary*, not the LLM-spend boundary. https://github.com/cncf/sandbox/issues/500 , https://github.com/faramesh/faramesh-core
  - Overlap with Argus: budget-based defer/deny per agent. Divergence: no USD reserve-then-settle, no pattern detection, no CRD/policy-as-K8s-resource. Closest new competitor, but on an adjacent axis (authorization vs runaway detection). Watch its sandbox review outcome.
- Also noted (REPORTED, likely pre-baseline): openkruise/agents (agent-sandbox lifecycle CRDs — provisioning, not enforcement); kubernetes-sigs/agent-sandbox (isolation, not cost).

## 5. agentgateway / Solo.io — VERDICT: CHANGED (commercial productization confirmed)

- **Solo Enterprise for agentgateway now documents "Budget and spend limits" as a shipped enterprise feature (VERIFIED, fetched docs):** token-bucket budgets per user/API key/route, **429 hard rejection when budget exhausted**, cost conversion from provider pricing, Prometheus metrics, configured via **`EnterpriseAgentgatewayPolicy` CRD** targeting HTTPRoute/Gateway. Docs at 2.2.x, latest enterprise version **2026.6.3** (June 2026 patch train). Limitation in their own docs: local rate limiting counts *requests not tokens* — token budgets require the global rate-limit server. https://docs.solo.io/agentgateway/2.2.x/llm/budget-limits/
- The 2026-06-08 Solo blog "Building Real-Time AI Cost Controls with agentgateway" (VERIFIED) is the baseline's reference impl — explicitly a **tutorial/reference architecture, not a product, no CRDs**. https://www.solo.io/blog/building-real-time-ai-cost-controls-with-agentgateway
- No new OSS agentgateway release with budget-kill built-in; kgateway v2.2 (AgentgatewayPolicy API) dates to 2026-02-11, pre-baseline.
- Delta significance: **dollar-ish budget enforcement via K8s CRD at the gateway is now a paid Solo Enterprise feature** — token-bucket quota (429), no USD reserve-then-settle, no dedup, no pattern detection, no per-principal armed backstop. "Budget kill via CRD" is no longer only a blog post. Argus's answer: "the open-source, semantics-rich version of what Solo charges for."

## 6. New named runaway incidents since June 13 — VERDICT: NO CHANGE

- All headline incidents predate baseline: $47K LangChain loop (dev.to, anonymized/illustrative), $4,200/63-hour 429-replan loop (Medium, low-confidence), Uber budget blowout / Microsoft Claude Code license revocation / "$500M Claude bill" (TechCrunch 2026-06-05). https://techcrunch.com/2026/06/05/the-token-bill-comes-due-inside-the-industry-scramble-to-manage-ais-runaway-costs/
- New-ish (REPORTED, marginal): "**loop engineering**" popularized June 2026 by Addy Osmani — termination-condition discipline entering mainstream discourse. No new named postmortem in the window.

## 7. Provider-side spend/loop guards — VERDICT: CHANGED (in Argus's favor — a containment measure was REVERSED)

- **Anthropic paused/cancelled the June 15 billing split (REPORTED, multiple independent sources):** the announced move of Agent SDK / `claude -p` / third-party app usage to a separate capped monthly credit pool was confirmed 2026-06-15 as **no longer happening** — programmatic agent loops continue drawing from interactive subscription limits. https://www.digitalapplied.com/blog/anthropic-claude-credit-overhaul-june-15-2026 , https://codersera.com/blog/anthropic-june-2026-billing-change-claude-code/
- OpenAI: no new hard-budget API found; monthly budget remains a soft threshold (REPORTED). https://blog.alephant.io/openai-spend-limit-how-to-cap-your-api-bill-2026/
- Google: nothing new found. No provider announced org-level hard-kill budget APIs or agent-SDK loop guards in the window.

---

## Net impact on Argus positioning

The 19-day window contains no fatal news and two genuinely favorable developments. Favorable: (a) InferCost — the closest structural collision — shipped nothing; the K8s-CRD enforcement seat remains empty in OSS; (b) Anthropic *reversing* its agent-billing containment means provider-side loop-cost discipline went backward, keeping third-party enforcement necessary; (c) Tokenomics Foundation's confirmed scope (FOCUS-style accounting, explicitly not enforcement) makes it an alignment opportunity (emit FOCUS-compatible spend records from the settle ledger) rather than a competitor. Unfavorable-but-manageable: Solo Enterprise now sells CRD-configured token-budget enforcement at the gateway — the concept is commercially validated, but it's paid, token-bucket-based, and lacks USD reserve-then-settle, dedup kill, and pattern detection. Watch closely: **Faramesh** (CNCF sandbox #500) — first new OSS entrant with per-agent dollar budget caps at the tool-call boundary; daemon-based, no CRDs today, converges on Argus only if it pivots K8s-native. Finally, a real naming problem: **Repello AI's commercial "Argus"** runtime-guardrail product is now a first-class LiteLLM guardrail integration in exactly Argus's category — evaluate a rename or explicit disambiguation before public launch.
