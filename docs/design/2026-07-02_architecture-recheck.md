<!-- Architecture recheck, 2026-07-02. Synthesis of three parallel primary-source research passes:
       docs/research/2026-07-02_framework-coverage.md      (10 agent frameworks + A2A/MCP/OTel)
       docs/research/2026-07-02_k8s-insertion-surfaces.md  (gateways, ext_proc, sidecar, eBPF, Tetragon)
       docs/research/2026-07-02_landscape-delta.md         (competitor/standards deltas since 06-13)
     Layers over docs/design/2026-06-13_v0.1-design.md (still canonical for the engine/CRD) and
     2026-06-13b_control-levels.md. This doc changes TARGET SURFACES and PRIORITIES, not the engine. -->

# Argus Architecture Recheck (2026-07-02)

**One-line verdict: the engine design (3-axis identity + armed principal backstop + one-Decide contract) is
_more_ correct than when written; what must change is the set of surfaces Argus targets and the build order.**

---

## 1. What the latest data CONFIRMS (do not touch)

| Design decision | Fresh evidence (2026-07-02) |
|---|---|
| 3-axis identity, content-derived conversation | **No framework except Claude Agent SDK emits conversation/run identity on the wire by default** (10 frameworks source-verified — LangGraph's `thread_id` never reaches the request). Derived identity is therefore the **primary** mechanism for ~7/10 frameworks, not a fallback. §2 of the v0.1 design only needs a rank note, no logic change. |
| Unforgeable principal backstop | Framework self-guards are *loosening*: LangGraph `recursion_limit` default went 25 → **1000**; Agents SDK allows `max_turns=None`; AG2's "TokenBudgetPolicy" is context-trimming, not a spend cap. Only Claude Agent SDK (`max_budget_usd`) and ADK (`max_llm_calls=500`) have real backstops. External declarative enforcement is the story. |
| GEP-713 policy attachment shape | agentgateway `AgentgatewayPolicy` and Solo `EnterpriseAgentgatewayPolicy` both converged on the same shape. |
| Niche | InferCost shipped nothing since v0.5.0; no budget/pattern CRD appeared in GAIE, Envoy AI GW v1.0, or agentgateway v1.3. The combination (CRD × behavioral invariants × pattern detection × dedup kill) is still unshipped anywhere. |
| Problem pressure | Anthropic **reversed** its June-15 agent-billing split; providers still ship no hard-kill budget APIs. |

## 2. What CHANGES (5 items)

### 2.1 Responses API parsing is table stakes — promote to P0
OpenAI Agents SDK, Pydantic AI v2 (GA 06-23), and MS Agent Framework all now **default to the Responses
API**. The proxy currently parses Chat Completions + Anthropic Messages only → it is blind to the default
path of the newest SDKs. Also: `previous_response_id` + `store=true` means the proxy sees only the
conversation *tail* — prefix-hash dedup breaks; a **response-id chain tracker** is required.
**This reverses Open Fork #7** (v0.1-design §10: "skip previous_response_id threading") — at minimum,
parse `conversation` / `previous_response_id` as Conversation-axis sources and keep the `{p:<P>}:resp:<rid>`
map from design §2.1 in scope for v0.1.

### 2.2 ext_proc: from "first cut" to "one server, three gateways"
All three major gateways expose the **same `envoy.service.ext_proc.v3` wire protocol**: Envoy AI Gateway
**v1.0.0 GA (06-23, v1beta1 APIs frozen)** via `EnvoyExtensionPolicy`; **agentgateway** via its extProc
traffic policy (explicitly Envoy-wire-compatible, failClosed default); **kgateway v2.4** adding extProc
`filterStage` control. One canonical Argus ext_proc server covers every gateway-fronted workload with zero
per-framework work. **Reverses the §11 scope-cut rule** ("if behind, drop ext_proc") — the cut order is now:
full-band Fuzzy similarity → RateSpike corpus tuning → LiteLLM tap. ext_proc stays the v0.2 headline;
for the v0.1 demo the standalone proxy remains first (it exists and works).

### 2.3 New deployment mode: webhook-injected native sidecar (the K8s-native answer for gateway-less pods)
A mutating webhook (opt-in namespace/pod label) injects the **existing standalone proxy** as a K8s 1.29+
native sidecar plus `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` env — 9/10 frameworks honor these
(source-verified). Full L7/token visibility, **no CA distribution** (ClusterTrustBundle is still beta in
1.35), 100 % code reuse. Ambient-mesh egress waypoints are explicitly rejected as a primary path: SNI-only
visibility without app scheme changes, and ambient's own docs say egress enforcement is not guaranteed.
MITM-CA mode (env-injected trust: `SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`) is an advanced option for pods
with hardcoded endpoints, not the default.
Demo line this buys: **"label the namespace → every agent framework in it is covered"** — the Tetragon
install-and-see moment, without a DaemonSet.

### 2.4 eBPF egress lockdown as the bypass backstop (ship manifests, not code)
Cilium `toFQDNs`/SNI policy (or plain NetworkPolicy) samples: **deny direct egress to LLM API FQDNs except
from the Argus proxy/gateway identity**. This converts taps from convention into enforcement — the honest
version of Tetragon's kernel-enforced posture. eBPF is never a primary tap (no token semantics inside TLS).

### 2.5 MCP plans rewritten
The **2026-07-28 MCP revision (RC live) deletes `Mcp-Session-Id` and the initialize handshake** — do not
architect on MCP session headers. The real MCP runaway vector is **server-driven sampling loops** (a
malicious/buggy MCP server pumps unbounded LLM calls through the host's credentials; on the wire it looks
like the agent's own traffic). Roadmap item (post-v0.1): "MCP-server-driven spend" as a distinct
attribution class. A2A v1.0.1 is the opposite: `contextId`/`taskId` are spec-blessed Conversation/RunID in
JSON bodies — the deferred A2A tap (Fork #8) gets cheaper and better-grounded; parser must handle 1.0
PascalCase methods (0.x `message/send` is gone).

## 3. Revised coverage model — one brain, 4+1 surfaces

```
                RunawayPolicy CRD (GEP-713) + controller
                                │ compiled engine.Config (ConfigMap → taps watch)
    ┌───────────────┬───────────┼──────────────────┬───────────────────┐
 Tap A: proxy     Tap A': same proxy,           Tap B: LiteLLM      Tap C: ext_proc
 (gateway-style   webhook-injected native       CustomLogger        (Envoy AI GW ·
  Deployment)     sidecar + env injection       callback            agentgateway ·
    ✅ built         🆕 webhook only              planned             kgateway)  v0.2
    └───────────────┴───────────┴──────────────────┴───────────────────┘
            + Cilium toFQDNs / NetworkPolicy egress lockdown samples 🆕
```

**Parser roadmap:** OpenAI Chat ✅ → Anthropic Messages ✅ → **Responses API (P0)** → Gemini
(P1 — unlocks Google ADK's default google-genai path, the single highest-coverage addition) → A2A
`contextId`/`taskId` (P2) → Bedrock dialects (later). Documented gaps: Agents SDK websocket transport,
in-process local models (smolagents Transformers/MLX/vLLM).

**Identity additions:**
- Native fast-path for Claude Agent SDK headers `x-claude-code-session-id` / `x-claude-code-agent-id` /
  `x-claude-code-parent-agent-id` (the only framework shipping 3-axis identity for free; SSE pass-through
  is mandatory on this path — never mutate bodies).
- Publish an **identity-injection convention** (`X-Argus-Session-Id`, already in the §2.2 header contract)
  with per-framework one-liners — every framework has a verified escape hatch (LangChain `default_headers`,
  Agents SDK `ModelSettings.extra_headers`, CrewAI `BaseInterceptor`, Pydantic AI injected client,
  Claude Code `ANTHROPIC_CUSTOM_HEADERS`, …).
- Read `traceparent`/`baggage` opportunistically; emit OTel `gen_ai.*` on Argus telemetry **pinned to a
  semconv version** (conventions are Development-stability and moved repos in June; `gen_ai.system` is gone,
  use `gen_ai.provider.name`).

## 4. Tetragon-feel surface decisions (ratified this session)

Borrow the operator surface, not the kernel: (1) **`argus getevents -o compact`** CLI — v0.1 = SSE client
over the existing demo event feed + formatter; (2) events always carry **policy name + invariant + observed
vs threshold** (add `Observed/Threshold/PolicyRef` to `Decision` — rides on the P0 wiring work);
(3) **K8s Event emitted on terminal kills** so `kubectl describe runawaypolicy` shows the kill;
(4) metrics named on the Tetragon pattern: `argus_events_total{policy,hook,namespace,pod}`,
`argus_runawaypolicy_loaded{state}` (verified against tetragon.io/docs/reference/metrics);
(5) **policy gallery** in `config/samples/` (observe-onboarding / guard-default / stop-the-47k-loop /
no-tool-loops / lockdown-untrusted). Do NOT borrow: DaemonSet-per-node model, kernel enforcement claims,
µs-latency comparisons. Tetragon v1.7.0 itself added a monitor-only mode — the observe→enforce posture
convergence continues; it still has zero LLM features, so "Tetragon for LLM traffic" stays uncontested.

## 5. Realism corrections carried from this session (pre-research review)

1. **Wiring before Redis.** Controller→tap policy delivery (compiled `engine.Config` → ConfigMap, proxy
   watches) is a prerequisite for the controlLevel demo; the Redis engine is not. Priority order in
   NEXT-STEPS is swapped accordingly.
2. **Backstop fail-closed cliff.** At Guard, backstop `failureMode: Closed` on every request path means a
   Redis outage ⇒ full 503 for all agent traffic — contradicting "adoption-safe." Add ONE mitigation to the
   design: time-bounded grace fail-open (e.g. 60 s with `argus_engine_degraded` alarm) OR per-replica local
   slack counters during outage. Unbounded-loss protection stays; the cliff goes.
3. **`decide.lua` ported incrementally**, not big-bang: pass 1 = tombstone + stepCap + budget + backstop
   (the minimum that stops the $47K loop); pass 2 = dedup/mint-rate/patterns. Formalize the MemoryEngine
   tests as a **conformance suite** run against both engines (incl. "counter past 3600 s does not reset").

## 6. Naming ⚠ (decide before any public launch)

LiteLLM v1.90.0 ships a first-class guardrail integration named **"Repello Argus"** — Repello AI's
commercial runtime-AI-guardrail product, i.e. a trademark/SEO collision in exactly this category.
Not a code blocker today, but the name soaks into CRD group (`policy.argus.dev`), binaries, and docs —
**decide by end of W2**. Criteria: no existing product/trademark in the AI-guardrail category; GitHub org +
domain + Helm chart name available; keeping a "watcher" metaphor eases the rebrand narrative.

## 7. Revised priorities (supersedes the order in NEXT-STEPS as of 06-21)

| Pri | Item | Status vs 06-13 plan |
|---|---|---|
| P0 | Controller→tap ConfigMap wiring (+ `Decision.Observed/Threshold/PolicyRef`) | order swapped up |
| P0 | **Responses API parser + response-id chain tracker** | 🆕 promoted (was Fork #7 "skip") |
| P0 | `decide.lua` core (tombstone/stepCap/budget/backstop) via conformance suite | scoped down (incremental) |
| P1 | **Sidecar-injection mutating webhook** (env-based, native sidecar) | 🆕 |
| P1 | `argus getevents` CLI + K8s Events on kills | 🆕 (Tetragon-feel) |
| P1 | Cilium/NetworkPolicy egress-lockdown sample manifests + docs | 🆕 (manifests, not code) |
| P1 | Streaming mid-stream abort → terminating SSE + tombstone | unchanged |
| P2 | LiteLLM tap; Gemini parser; policy gallery; metrics naming | adjusted |
| v0.2 | ext_proc server (one impl → Envoy AI GW + agentgateway + kgateway) | promoted from "first cut" |
| pre-launch | **Rename decision** (Repello Argus collision) | 🆕 |
| cut first | Full-band Fuzzy similarity; RateSpike corpus tuning; `previous_response_id` full threading beyond chain-tracking | replaces "drop ext_proc" |
