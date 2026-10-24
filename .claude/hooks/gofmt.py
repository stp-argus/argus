#!/usr/bin/env python3
"""PostToolUse hook: run `gofmt -w` on a Go file right after Claude edits it.

Reads the hook payload from stdin (JSON with tool_input.file_path), and formats
the file in place when it is a .go file. Safe no-op until the Go toolchain is
installed — if `gofmt` is not on PATH, it exits quietly. Always exits 0 so it
never blocks an edit.
"""
import json
import shutil
import subprocess
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path", "")
    if not isinstance(path, str) or not path.endswith(".go"):
        return

    gofmt = shutil.which("gofmt")
    if not gofmt:
        return

    try:
        subprocess.run([gofmt, "-w", path], check=False, timeout=15)
    except Exception:
        pass


if __name__ == "__main__":
    main()
