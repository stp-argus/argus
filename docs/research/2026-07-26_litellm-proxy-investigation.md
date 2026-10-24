# LiteLLM Proxy Investigation

Written 2026-07-26, in response to the 2026-07-25 direction pivot: instead of building our own
tap proxy, LiteLLM Proxy handles the data plane and Argus builds only the judgment API + a
LiteLLM guardrail hook. Nobody on the team had run LiteLLM before this — everything below was
verified hands-on against a local `docker compose` stack, not read from docs alone.

Reproducible environment: `docs/research/litellm-lab/` in this repo (`docker-compose.yml`,
`config.yaml`, `guardrails/argus_guardrail.py`). No real LLM key required — it registers
`argus-sample-app`'s mock upstream as an OpenAI-compatible backend.

## Reproduce it yourself

```bash
# 1) start argus-sample-app's mock upstream (no real LLM key needed)
cd argus-sample-app && npm run mock          # :18080

# 2) start LiteLLM from this repo
cd argus/docs/research/litellm-lab && docker compose up -d   # :4000

# 3) call it through LiteLLM
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-argus-lab-1234" \
  -H "Content-Type: application/json" \
  -d '{"model":"argus-mock-gpt","messages":[{"role":"user","content":"hi"}]}'
```

## a. Model registration / routing

`config.yaml`'s `model_list` maps a caller-facing alias (`model_name`) to a real backend
(`litellm_params.model`). Adding `api_base` lets you point at **any** OpenAI-compatible server —
we used this to register our own mock upstream with no real LLM key:

```yaml
model_list:
  - model_name: argus-mock-gpt
    litellm_params:
      model: openai/gpt-mock-4o
      api_base: http://host.docker.internal:18080/v1
      api_key: "sk-fake-not-checked-by-mock"
```

The caller only needs to know `model: "argus-mock-gpt"`; LiteLLM hides where it actually goes.
Responses are always normalized to OpenAI ChatCompletion shape (it re-wrapped our mock's response,
which was already that shape, adding `provider_specific_fields`).

## b. Guardrail hooks — confirmed signature and timing

```python
async def async_pre_call_hook(
    self,
    user_api_key_dict: UserAPIKeyAuth,   # caller identity, see (c)
    cache: DualCache,
    data: dict,                            # request body — can be mutated here
    call_type: str,                        # e.g. "acompletion"
) -> Optional[Union[Exception, str, dict]]:
```

- **`pre_call`** runs **before** the real LLM API is called. Raising
  `HTTPException(status_code=429, detail={...})` inside it returns that error to the caller
  immediately and **the upstream LLM is never called** — exactly the shape we need (a block means
  the spend never happens).
- Verified hands-on: our dummy guardrail (below) blocks from the 3rd call onward; confirmed via
  curl (`HTTP 429`), and LiteLLM auto-attaches `guardrail_name`/`guardrail_mode` to the error body
  — useful for logging which guardrail fired.
- `during_call` (parallel, input-only) and `post_call` (after the response, cannot block
  streaming) also exist. For blocking the call itself, `pre_call` is the right mode.
- Registration: under `config.yaml`'s `guardrails:`, `guardrail: <module.path>.<ClassName>`,
  `mode: "pre_call"`. **`default_on: true` is the key field** — once an admin sets it, the caller
  cannot opt out.

## c. Virtual key / team / user structure — what identity reaches the hook

**Calling with the raw master key**, `user_api_key_dict.api_key` is LiteLLM's own internal token
value (e.g. `"litellm_prox..."`), and `team_id=None`, `key_alias=None`,
`user_id="default_user_id"` (a generic placeholder). **The master key alone does not distinguish
callers.**

Issuing a real per-caller **virtual key** via `/key/generate` fails without a database:

```
{"error":{"message":"No connected db.","type":"no_db_connection", ...}}
```

**Virtual key issuance/verification is bound to Postgres.** Confirmed hands-on, matching what the
07-25 direction doc said:
- **Blocking itself works with zero DB** (our PoC proves it).
- **Trustworthy per-caller identity (virtual key/team) requires the DB.**
- Wiring Argus's `principal` axis for real means Postgres has to be added at the cluster-deploy
  stage (stage 3 per the direction doc) — re-confirmed empirically here.

## d. ⭐ Exact scope of LiteLLM's built-in budget/rate-limit features (most important finding)

| Feature | Field(s) | Scope |
|---|---|---|
| Spend cap | `max_budget` + `budget_duration` ("30s"/"30m"/"30h"/"30d") | proxy-wide · team · user · virtual key · per-model (`model_max_budget`) |
| Rate limit | `tpm_limit` (tokens/min) · `rpm_limit` (requests/min) | team · user · key · per-model |
| Concurrency limit | `max_parallel_requests` | per key |
| **Agent session controls** | `max_iterations` (per-session step cap!) · `max_budget_per_session` | per agent |
| On exceed | returns 429 | all of the above |

**Decisive fact, quoted directly from the docs**: all of the above is **"purely threshold-based."**
There is **no detection of loops, retries, or duplicate-input patterns.**

→ **This is the exact boundary of our differentiation.** What LiteLLM already does: step caps
(`max_iterations` is conceptually near-identical to our stepCap), spend caps (`max_budget`). **What
it can't do, and why Argus exists:**
1. **A per-principal backstop that survives run-id rotation.** LiteLLM's team/user aggregation
   sums keys an admin has *manually* grouped into a team — it is not "automatically notice the
   same actor rotating keys."
2. **Duplicate-input (dedup) detection.** Not present.
3. **Recursion/ping-pong pattern detection.** Not present. (This is exactly the gap the 07-25
   direction doc pointed at with "run-scoped loop/recursion pattern detection.")
4. **Declaring policy as a K8s CRD.** LiteLLM is configured via API/UI, no CRD.

## e. Helm chart · architecture support

- Official Helm charts exist: `oci://ghcr.io/berriai/litellm-helm` (monolithic) or
  `oci://ghcr.io/berriai/litellm/chart/litellm` (microservices, version-pin recommended).
- The image itself (`docker.litellm.ai/berriai/litellm:latest`) verified via
  `docker manifest inspect`: **amd64 + arm64** multi-arch — compatible with both dev-argus
  (amd64) and an arm64 test box.

## Summary — what to tell the team

> LiteLLM does exactly "call-count / budget caps" (and does it well — `max_iterations` is
> conceptually close to our stepCap). **Pattern detection (loop/recursion/retry/duplicate-input)
> and a principal backstop that survives key rotation do not exist in LiteLLM — that is precisely
> where our judgment engine lives.** The `pre_call` guardrail hook definitely works and can call
> our judgment API before the upstream call, cutting it off with a 429 — reproduced and verified,
> not just read about. Virtual keys (real per-caller identity) need Postgres at stage 3, but the
> guardrail logic itself can be built and tested without a DB before then.
