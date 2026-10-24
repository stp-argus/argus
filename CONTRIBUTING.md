# Contributing to ARGUS

Issues, design feedback, and PRs are welcome.

## Before you start

- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and the invariants in [AGENTS.md](AGENTS.md).
- For anything larger than a fix, open an issue first so we can agree on the shape.
- Changes to the record/header/metric shape update
  [docs/decision-event-contract.md](docs/decision-event-contract.md) in the same PR.

## Development

Requirements: Go 1.26, Python 3.12+, Docker, kind, kubectl, helm.

```bash
make manifests generate                                        # after editing api/
make test                                                      # engine + controller (envtest)
python3 -m unittest discover -s hack/guardrail -p 'test_*.py'  # guardrail
```

End-to-end on a local kind cluster: follow [QUICKSTART.md](QUICKSTART.md) with an operator image built from this tree
(see its troubleshooting table, `ImagePullBackOff` row).

## Pull requests

- Branch off `dev` (`feat/…`, `fix/…`, `docs/…`); PRs target `dev`.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- **Sign off** every commit (DCO): `git commit -s`. AI-assisted commits carry a `Co-Authored-By:`
  trailer naming the model.
- Every source file carries the Apache-2.0 SPDX header.
- Green CI + at least one review. Add tests for behavior changes.

## License

By contributing you agree your contributions are licensed under Apache-2.0.
