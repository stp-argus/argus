# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Use GitHub's private
vulnerability reporting: the **Security** tab → **Report a vulnerability**. We aim to
acknowledge within 5 business days.

## Scope

Argus sits in the request path of LLM/agent traffic and is a control plane for agent
behavior, so its own attack surface matters. The v0.1 threat model — including evasion
vectors (run-id rotation, header spoofing, multi-tenant isolation, the engine as a SPOF,
fail-open vs fail-closed) and their mitigations — is documented in
`docs/design/2026-06-13_v0.1-design.md` §8.3.

## Supported versions

Pre-1.0: only the latest tagged release is supported. This will be revisited at v1.0.
