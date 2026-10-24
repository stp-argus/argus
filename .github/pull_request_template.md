## What

<!-- One paragraph: what this PR does and why. Link the issue if there is one. -->

## Checklist

- [ ] Conventional Commit messages + DCO sign-off (`git commit -s`)
- [ ] Tests for behavior changes (`make test`, `python3 -m unittest discover -s hack/guardrail -p 'test_*.py'`)
- [ ] Touches `api/`? → `make manifests` run and the CRD diff is committed
- [ ] Touches the decision path (`internal/decide/`, `hack/guardrail/`)? → [`docs/decision-event-contract.md`](../docs/decision-event-contract.md) still true
