<!-- Research agent output, 2026-07-02 (parallel 3-agent recheck; see docs/design/2026-07-02_architecture-recheck.md for the synthesis).
     Method: primary-source WebFetch verification; claims tagged VERIFIED / REPORTED. -->

# Argus data-plane insertion research — state of the ecosystem as of 2026-07-02

Verification legend: **VERIFIED** = read from primary source (GitHub API/releases, official docs). **REPORTED** = from search-result summaries/secondary sources, not independently confirmed.

---

## 1. Gateway API Inference Extension (GAIE)

- **Latest release: still v1.5.0, published 2026-04-19** (v1.5.0-rc.2 2026-04-16, v1.4.0 2026-03-20). No v1.5.x patch or v1.6 tag exists as of today. **VERIFIED** via GitHub API — https://api.github.com/repos/kubernetes-sigs/gateway-api-inference-extension/releases
- **PR #2637 is merged (2026-03-26) and therefore shipped in v1.5.0.** It converts BBR/plugin errors into Envoy `ImmediateResponse` with mapped HTTP codes (adds `Unauthorized`/401, `Forbidden`/403, `NotFound`/404), explicitly motivated by "security plugins like guardrails" being able to actively block instead of pass-through-on-error. **VERIFIED** — https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2637
- **No official policy/budget/guardrail CRD.** The proposals directory (`docs/proposals`) contains nothing cost/budget/guardrail-shaped; the relevant extension points remain `1964-pluggable-bbr-framework` and the EPP plugin framework (v1.5.0 added a "Pluggable Parser Framework for EPP payload processing," response-path plugin mirroring, and gRPC trailer support). Flow Control remains load/saturation-based — still not cost/token-budget-based. **VERIFIED** (proposals listing + release notes) — https://github.com/kubernetes-sigs/gateway-api-inference-extension/tree/main/docs/proposals
- The EPP protocol mandates `ImmediateResponse` 503/429 semantics (proposal 004), so blocking is first-class in the protocol. **VERIFIED** — https://github.com/kubernetes-sigs/gateway-api-inference-extension/tree/main/docs/proposals/004-endpoint-picker-protocol
- **Viability as an Argus tap:** technically yes — a BBR plugin gets buffered bodies + can now block — **but GAIE/EPP sits in front of an `InferencePool` of self-hosted model servers**. It never sees egress to api.openai.com/api.anthropic.com. Verdict: riding EPP/BBR is a good *fourth* tap for the self-hosted-vLLM slice of the market (and it's Go, same plugin SDK), but it cannot replace a generic ext_proc tap. Plain ext_proc remains the more universal surface.

## 2. Envoy AI Gateway

- **v1.0.0 GA published 2026-06-23** (rc1 same day; previous: v0.7.0 2026-06-06, v0.6.0 2026-05-05). **VERIFIED** via GitHub API — https://github.com/envoyproxy/ai-gateway/releases
- v1.0.0: core CRDs (`AIGatewayRoute`, `AIServiceBackend`, `BackendSecurityPolicy`, `GatewayConfig`, `MCPRoute`) **declared stable at `v1beta1` for the 1.x series**, drop-in upgrade from v0.7, no migration. 16 providers behind one OpenAI-compatible endpoint, MCP gateway with fine-grained authz, token/quota-aware rate limiting, OTel/OpenInference tracing. Built on Envoy Gateway v1.8.1 / Envoy v1.38.1. **VERIFIED**
- **Extension surface:** Envoy AI Gateway is layered on Envoy Gateway, so third parties attach via Envoy Gateway's `EnvoyExtensionPolicy` (ext_proc, Lua, Wasm) targeting the same Gateway/HTTPRoutes — no new AI-Gateway-specific policy hook appeared in v1.0.0. **REPORTED** (https://gateway.envoyproxy.io/docs/tasks/extensibility/ext-proc/ ; https://gateway.envoyproxy.io/contributions/design/envoy-extension-policy/). Argus's planned `EnvoyExtensionPolicy` ext_proc tap is exactly the sanctioned path, and v1.0.0's API freeze makes it a stable target.

## 3. agentgateway + kgateway

- **agentgateway v1.3.1, 2026-06-22** (v1.3.0 2026-06-18; v1.2.x May 2026). **VERIFIED** — https://github.com/agentgateway/agentgateway/releases
  - v1.3.0: **native token + dollar cost tracking** surfaced in logs/traces/metrics/`agctl`/UI; virtual models (weighted/failover/conditional routing); 13 new providers; **request body buffering so policies can see full bodies before backend selection**; MCP enhancements (Okta auth, resource subscribe). v1.2.0: **CEL-conditional policy execution, label-selector policy targets**, `agctl` CLI. **VERIFIED** (release notes)
  - **Third-party hooks: agentgateway supports both ExtAuthz and ExtProc as traffic policies, wire-compatible with Envoy's `envoy.service.ext_proc.v3` API**, with `failOpen`/`failClosed` modes (failClosed default). Policy order: JWT → basic/API-key → ExtAuthz → ExtProc → transformation. **REPORTED** (https://agentgateway.dev/docs/configuration/traffic-management/extproc/ , https://agentgateway.dev/docs/standalone/latest/configuration/security/external-authz/). One Argus ext_proc server can attach to agentgateway too.
  - **Kubernetes mode:** the **`AgentgatewayPolicy` CRD** (kgateway PR #12723) targets Gateways/HTTPRoutes via `targetRefs`/`targetSelectors` (GEP-713 shape), carries promptGuard/frontend/traffic/backend policy phases, translated by a Go controller and pushed over xDS to the Rust data plane. **REPORTED** — https://github.com/kgateway-dev/kgateway/pull/12723
  - A2A/MCP awareness is native (it's the AAIF "agentic proxy"); MCP authz + cost/$-tracking are first-party. **VERIFIED** (release notes) + https://aaif.io/blog/designing-agentgateway-a-unified-high-performance-gateway-for-ai-and-api-traffic/
- **kgateway: stable line v2.3.5 (2026-06-25); v2.4.0-alpha.2 (2026-06-25).** **VERIFIED** via GitHub API. v2.4.0-alpha.1 (2026-06-09) added **gateway-level TrafficPolicy application to routes** and **ExtProc `filterStage` configuration**; alpha.2 added `requestAttributes` on GatewayExtension ext_proc. **VERIFIED** (release notes). kgateway's ext_proc attachment story for third parties is actively improving in v2.4.

## 4. LiteLLM

- **Latest stable: v1.90.0, 2026-06-27** (v1.91.0-rc.1 2026-06-28; v1.92.0-dev.1 2026-06-30; baseline v1.89.x now in backport-only mode, v1.89.4 2026-06-25). **VERIFIED** via GitHub API — https://github.com/BerriAI/litellm/releases
- Relevant movement since mid-June: v1.90.0 adds **Cisco AI Defense guardrails integration**, rate-limit error standardization, Postgres partitioning for spend logs; v1.91.0-rc.1 adds **guardrail pipeline matching** and durable-worker spend logging; v1.92.0-dev.1 notes **budget enforcement hardening** and further guardrail enhancements. **VERIFIED** (release list)
- **CustomLogger / CustomGuardrail hook API is unchanged in shape**: `async_pre_call_hook` (pre-LLM, can mutate/reject), post-call output validation hooks, per-hook guardrail modes remain the documented extension path; new metric `litellm_overhead_with_guardrails_latency_metric` exposes guardrail overhead. **REPORTED** — https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail
- No breaking change to the CustomLogger surface found → **Argus's LiteLLM tap remains valid against v1.90.0**; retest against the guardrail-pipeline-matching change when v1.91.0 goes stable. No native "iteration budget" (loop-count) primitive found beyond the existing max_iterations — pattern detection differentiation preserved.

## 5. Egress interception for non-gateway pods

### (a) Istio ambient / ztunnel + egress waypoint
- Model: `ServiceEntry` for the external host, labeled `istio.io/use-waypoint` (or namespace `--enroll-namespace`), ztunnel steers matching traffic to a waypoint that applies L7 policy. Auto-allocated 240.240.x.x service IPs with `PILOT_ENABLE_IP_AUTOALLOCATE=true`. **REPORTED** — https://ambientmesh.io/docs/traffic/mesh-egress/ , https://istio.io/latest/docs/ambient/usage/waypoint/
- **Istio blog 2026-04-09 (Istio ~1.30.2): wildcard/dynamic-DNS egress** — wildcard `ServiceEntry` + `DYNAMIC_DNS` resolution builds a dynamic-forward-proxy cluster keyed on **SNI inspected from the client TLS handshake**. **VERIFIED** (fetched post) — https://istio.io/latest/blog/2026/egress-dynamic-dns/
- **TLS reality for api.openai.com / api.anthropic.com:** agents dial HTTPS directly, so the waypoint sees **SNI only (passthrough)** — enough for allow/deny + connection metering, **not enough for token counting or prompt inspection**. Full L7 visibility requires **TLS origination**: the app must send plaintext HTTP in-mesh (base-URL scheme change) and a `DestinationRule` (`tls.mode: SIMPLE`) upgrades at the waypoint — i.e., an app-config change after all. ambientmesh.io explicitly warns wildcard hostnames are unsupported in that guide's flow and that **"ambient mesh cannot strictly guarantee egress policies are enforced"** (bypass possible without NetworkPolicy backstop). **REPORTED**

### (b) Native sidecar (K8s 1.29+ `initContainers` + `restartPolicy: Always`) as transparent egress proxy
- The 2026-practical pattern: mutating webhook injects (1) an init container that installs iptables/nftables REDIRECT for 443/80 egress, (2) a native-sidecar proxy container. TLS options:
  1. **MITM with an injected CA** (mitmproxy/Squid ssl-bump style, or a purpose-built Go proxy): clients must trust the CA — practical because OpenAI/Anthropic SDKs honor standard env (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) which the same webhook can inject. Documented pattern: mitmproxy sidecar with shared cert volume — https://medium.com/@gavin.sinai/intercepting-outbound-kubernetes-traffic-using-mitmproxy-96e3d1ed716 . **REPORTED**
  2. **Explicit proxy via `HTTPS_PROXY` + CONNECT**: no iptables needed, but CONNECT tunnels are opaque unless you also MITM — alone it gives destination control only.
  3. **Base-URL redirection** (`OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL` → `http://127.0.0.1:<sidecar>`): sidecar terminates plaintext on localhost and originates real TLS upstream. **Zero CA trust games, full L7 visibility** — the "app change" collapses to two env vars a webhook can inject. Least-friction full-visibility option; matches Argus's existing standalone-proxy tap.
- **Trust distribution status: `ClusterTrustBundle` is still beta (off by default) in K8s 1.35** (`certificates.k8s.io/v1beta1`; beta since 1.33) — cert-manager `trust-manager` ConfigMap/Secret distribution remains the production path for MITM CA rollout. **REPORTED** — https://kubespec.dev/kubernetes/certificates.k8s.io/v1beta1/ClusterTrustBundle

### (c) eBPF (Cilium / Tetragon)
- **Cilium `toFQDNs` policies** (DNS-proxy-driven eBPF allowlists with TTL expiry) + **SNI-based TLS policy** give per-pod *allow/deny* of api.openai.com egress without touching the app; Cilium's TLS-visibility feature can decrypt but requires cert provisioning per policy (same MITM trust problem). **REPORTED** — https://docs.cilium.io/en/latest/security/dns/ , https://docs.cilium.io/en/stable/security/tls-visibility/
- **Verdict for eBPF:** kernel-level redirect/deny is a superb *bypass-prevention backstop* ("only the Argus proxy identity may reach LLM API FQDNs") and hard kill-switch, but eBPF alone never yields token/cost/loop semantics from inside TLS. Not a primary tap.

## 6. Cilium Tetragon (UX donor)

- **Latest: v1.7.0, 2026-04-29** (v1.6.1 2026-03-31, v1.6.0 2025-10-23). **VERIFIED** — https://github.com/cilium/tetragon/releases
- v1.7.0 highlights: **CEL expression evaluation in eBPF**, fentry sensor, `matchParentBinaries` selector, env-var retrieval, kernel+user stack traces merged into `ProcessKprobe` events, **monitor-only mode introduced** (reinforces observe→enforce posture), default gRPC server moved to a **unix socket**. **VERIFIED** (release notes)
- Architecture: **agent DaemonSet in `kube-system` + `tetragon-operator` (CRD management) + `tetragon-config` ConfigMap**, Helm chart `cilium/tetragon`; gRPC address configurable via `tetragon.grpc.address`. **VERIFIED** — https://tetragon.io/docs/installation/kubernetes/
- **TracingPolicy CRD**: cluster-scoped `TracingPolicy` (+ `TracingPolicyNamespaced`); spec hooks = `kprobes`/`tracepoints`/`uprobes` (+ LSM), each with **selectors** (`matchArgs`, `matchBinaries`, `matchActions`, now `matchParentBinaries`) doing in-kernel filtering; actions include Sigkill/Override/Post. **VERIFIED** — https://tetragon.io/docs/concepts/tracing-policy/
- **Metrics**: `tetragon_` prefix; e.g. `tetragon_events_total{type,pod,namespace,node_name,binary,workload}`, `tetragon_policy_events_total{policy,hook,namespace,pod}`, `tetragon_tracingpolicy_loaded{state}`, `tetragon_bpf_missed_events_total`. **VERIFIED** — https://tetragon.io/docs/reference/metrics/ . Argus analog: `argus_events_total{policy,hook,namespace,pod}` + `argus_runawaypolicy_loaded{state}` would be a faithful borrow.
- CLI UX: `tetra getevents -o compact` streaming remains the flagship interaction. **No AI/LLM-related features in Tetragon** — "Tetragon for LLM traffic" positioning stays uncontested.

## 7. Topology comparison for full-coverage LLM egress

| Topology | Sees external SaaS LLM egress? | L7/token visibility | App changes | Bypass resistance | Frameworks covered |
|---|---|---|---|---|---|
| **AI/agent gateway (agentgateway, Envoy AI GW, kgateway, LiteLLM)** | Only traffic routed to it | Full (it terminates the API) | Base-URL must point at gateway | None by itself | Any framework that honors base-URL |
| **Injected native sidecar (env base-URL or MITM-CA)** | Yes, per-pod | Full | Env-var injection only (webhook) | Good + NetworkPolicy lock | All (SDK env vars are universal) |
| **Ambient egress waypoint** | Yes, if mesh-enrolled | SNI-only unless TLS origination (scheme change) | None (SNI) / scheme change (full) | Weak alone (documented) | All, but shallow |
| **eBPF (Cilium FQDN/SNI, Tetragon)** | Yes | None inside TLS | None | Strongest (kernel) | All, but no semantics |

2026 practitioner writeups converge on gateway-centric governance for agent traffic (CNCF: https://www.cncf.io/blog/2026/03/23/cloud-native-agentic-standards/ , https://www.cncf.io/blog/2026/06/17/why-cloud-native-belongs-at-the-heart-of-agentic-ai-lessons-from-building-a-multi-agent-security-platform-on-kubernetes/ — the latter notably chose **Cilium identities + per-agent mTLS, no mesh**, calling it "materially simpler than a mesh"; also https://jimmysong.io/blog/ai-gateway-in-depth/). **REPORTED**

## Recommended insertion topology for maximal agent coverage (ranked)

1. **Primary tap: one Envoy-ext_proc-v3 gRPC server, attached three ways.** The single most leveraged Argus investment is a canonical `ext_proc` implementation, because **all three major gateways now expose the same wire protocol**: Envoy AI Gateway/Envoy Gateway via `EnvoyExtensionPolicy` (v1.0.0 GA, frozen v1beta1 APIs), kgateway via GatewayExtension/TrafficPolicy extProc (with `filterStage` control landing in v2.4), and agentgateway via its extProc policy (explicitly `envoy.service.ext_proc.v3`-compatible, failClosed default). RunawayPolicy (GEP-713 shape) reconciles into the right attachment per gateway. This covers every workload already fronted by a gateway with zero per-framework work.
2. **Coverage tap for gateway-less pods: webhook-injected native sidecar with env-based redirection.** Mutating webhook (opt-in label) injects the existing Argus standalone proxy as a native sidecar + `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`/`HTTPS_PROXY` env. Full token-level visibility, no CA distribution (ClusterTrustBundle is still beta in 1.35), works for LangGraph/kagent/custom containers alike. Offer MITM-CA mode (trust via `SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS` + trust-manager) only as an advanced option for pods with hardcoded endpoints.
3. **Bypass backstop: Cilium `toFQDNs`/SNI policy (or plain NetworkPolicy) denying direct egress to LLM API FQDNs except from the Argus proxy/gateway identity.** This is what makes taps 1–2 *enforcement* rather than convention, mirroring Tetragon's kernel-enforced posture without pretending eBPF can read tokens.
4. **Situational taps, keep but don't lead with:** LiteLLM CustomLogger/CustomGuardrail hook (unchanged API through v1.90.0; large installed base), and a GAIE BBR/EPP plugin (post-PR-#2637 blocking is first-class) for self-hosted InferencePool traffic that never leaves the cluster.
5. **Avoid as primary:** ambient egress waypoints — SNI-only visibility without app scheme changes, documented non-guaranteed enforcement, and a heavy prerequisite (mesh adoption) for less data than option 2 yields.

Net: **gateway-attached ext_proc first, injected native-sidecar proxy for the long tail, eBPF FQDN lockdown as the guarantee** — all three driven by the same RunawayPolicy CRD. No competing "runaway/budget CRD" emerged in GAIE, Envoy AI Gateway v1.0.0, or agentgateway v1.3 as of 2026-07-02; the nearest adjacents are $-cost *tracking* (agentgateway v1.3.0) and quota rate limiting (Envoy AI GW), neither of which does loop/iteration-pattern detection.
