# ARGUS — Agent Guide

Orientation for any AI coding agent (Claude Code, Copilot, Codex, Cursor, …) and for humans.
`CLAUDE.md` is a thin pointer here.

## What this repo is

The ARGUS control plane: a Kubernetes operator that renders `RunawayPolicy` CRs, the Go decision engine
behind `POST /v1/decide`, and the LiteLLM guardrail that asks it before every upstream call.
The Helm chart is published as `oci://ghcr.io/stp-argus/charts/argus` (install: QUICKSTART.md).

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first — components, call flow, failure behavior.

## Invariants — break these and something silently goes wrong

- **Detection logic lives in the engine only** (`internal/decide/`). The guardrail extracts signals and
  renders the response; the controller renders policy. Neither decides.
- **The Python engine (`hack/guardrail/argus_decide.py`) is the local fallback and must match Go.**
  `internal/decide/python_parity_test.go` pins them together. Change one → change both.
- **The axis → category table exists twice** (`internal/decide/category.go`,
  `argus_decide.CATEGORY_BY_AXIS`); `governance_test.go` and `test_argus_governance.py` pin the same cases.
- **Key-tier identity outranks the caller header.** The principal comes from `user_api_key_dict`
  (user_id > team_id > key_alias); the `X-Principal-Id` header is used only when the key carries none of
  those. A header that differs from the key-tier identity is recorded as `identity`; a matching one is not.
- **Clients detect a block by header** (`x-argus-decision-id`), not body — LiteLLM wraps the body.
- **Fail-open by default.** A remote decide failure falls back to the local Python engine (`decide_source=local`);
  if that fails too, the call passes and the record carries `decide_error`. `backstop.failureMode: Closed`
  (Enforce only) blocks once the operator has been unreachable longer than `ARGUS_FAIL_CLOSED_GRACE_S`.
- Record/header/metric shapes are a contract with the dashboard and alerting —
  update [docs/decision-event-contract.md](docs/decision-event-contract.md) in the same PR.

## Conventions

- Go 1.26 · kubebuilder v4 · controller-runtime v0.23. Images build for amd64 + arm64.
- After editing `api/**/*_types.go`: `make manifests generate`. Never hand-edit `zz_generated.*`,
  `config/crd/bases/*`, `config/rbac/role.yaml`, `PROJECT`. The CRD is copied into argus-helm by the
  release workflow.
- API group `argus.stp.io/v1alpha1`; never break a served version.
- New source files carry `SPDX-License-Identifier: Apache-2.0`.
- Conventional Commits + DCO (`git commit -s`); branch first, PR to `dev`. AI-assisted commits carry a
  `Co-Authored-By:` trailer naming the model.
- Code comments and docs here are mostly Korean; either language is fine, match the file you're in.
- Secrets never committed; `.env*`, `*.pem`, `*.key` are gitignored and read-denied.

## Tests

```bash
make test                                                        # engine + controller (envtest)
python3 -m unittest discover -s hack/guardrail -p 'test_*.py'    # guardrail
```
End-to-end on kind: argus-helm `hack/kind-up.sh && hack/e2e.sh`.

## Don't

- Don't put detection logic in the guardrail, the controller, or a dashboard.
- Don't cite the $47K story as a corporate incident — it is a community anecdote.
