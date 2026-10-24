# ARGUS — Claude Code

@AGENTS.md

Claude-specific notes:

- **gofmt hook**: edited `.go` files are auto-formatted by a PostToolUse hook (`.claude/hooks/gofmt.py`);
  a no-op when Go isn't installed.
- **Permissions**: `.claude/settings.json` allowlists go/make/kubebuilder/kubectl/kind/helm/docker and
  denies reading `.env*`/`*.pem`/`*.key`.
- **Commits**: only when asked; branch off `dev` first; Conventional Commits + `git commit -s`; end the
  message with a `Co-Authored-By:` trailer naming the model you are.
