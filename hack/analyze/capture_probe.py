#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""신원 포착 프로브 — 실제 AI 도구가 게이트웨이에 무엇을 들고 오는지 실측한다.

LiteLLM 앞에서 재보려 했더니 **인증 계층이 가드레일보다 앞**이라, 인증에서 떨어지는
클라이언트는 우리 훅에 아예 도달하지 않았다 (Claude Code 가 그랬다). 그 자체가 발견이지만,
"무엇을 보내는가"를 재려면 인증·라우팅이 방해가 된다.

그래서 이 프로브는 **아무것도 검사하지 않는다.** 어떤 경로·어떤 키로 와도 200 을 준다.
목적은 오직 하나 — 협조 없이 얻을 수 있는 신원 재료가 무엇인지 확인하는 것.

추출 로직은 가드레일의 `_client_identity` **그대로** 쓴다. 따로 구현하면 여기서 본 것과
운영에서 남는 것이 갈라진다.

    python3 hack/analyze/capture_probe.py            # :4100 에서 대기
    ANTHROPIC_BASE_URL=http://127.0.0.1:4100 claude -p "say ok"
    OPENAI_BASE_URL=http://127.0.0.1:4100/v1 codex exec "say ok"
"""
from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "guardrail"))

# 가드레일의 신원 추출을 **그대로** import 한다. 따로 구현하면 여기서 본 것과
# 운영에서 남는 것이 갈라진다. argus_signals 는 프록시 무관이라 litellm 이 필요 없다.
import argus_signals as _g  # noqa: E402

_client_identity = _g._client_identity

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "capture.jsonl")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4100


def _anthropic_reply() -> dict:
    return {"id": "msg_probe", "type": "message", "role": "assistant",
            "model": "probe", "stop_reason": "end_turn", "stop_sequence": None,
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 2}}


def _openai_reply(model: str) -> dict:
    return {"id": "chatcmpl-probe", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}


class Probe(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 기본 접근 로그를 끈다 — 우리가 직접 찍는다
        pass

    def _read_body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def _capture(self, body: dict) -> dict:
        headers = {k.lower(): v for k, v in self.headers.items()}
        ident = _client_identity(headers, body)
        msgs = body.get("messages") or []
        tools = body.get("tools") or []
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": self.path,
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "msg_count": len(msgs),
            "msg_bytes": len(json.dumps(msgs, default=str)),
            "system_bytes": len(json.dumps(body.get("system") or "", default=str)),
            "tool_count": len(tools),
            "tool_names": sorted(
                (t.get("function", {}).get("name") or t.get("name") or "?")
                for t in tools if isinstance(t, dict))[:40],
            "body_keys": sorted(body.keys()),
            "client_identity": ident,
            # 인증을 **어떤 헤더로** 보내는지가 중요하다 (값은 절대 안 남긴다).
            # LiteLLM 이 x-api-key 와 Authorization 을 다르게 다뤄서 실패가 갈렸다.
            "auth_header": next((h for h in ("authorization", "x-api-key", "api-key")
                                 if h in headers), None),
        }
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        c = ident.get("client") or "?"
        print(f"  [{c:<14}] {self.path:<34} model={rec['model']} "
              f"msgs={rec['msg_count']} tools={rec['tool_count']} auth={rec['auth_header']}",
              flush=True)
        return rec

    def _send(self, payload: dict) -> None:
        blob = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        body = self._read_body()
        rec = self._capture(body)
        if "messages" in self.path or "anthropic" in self.path:
            self._send(_anthropic_reply())
        else:
            self._send(_openai_reply(rec.get("model") or "probe"))

    def do_GET(self):
        # /v1/models 같은 탐색 요청도 신원 재료를 들고 온다
        self._capture({})
        self._send({"object": "list", "data": [{"id": "probe", "object": "model"}]})

    do_HEAD = do_GET


if __name__ == "__main__":
    print(f"신원 포착 프로브 :{PORT}  →  {OUT}\n"
          f"  아무것도 검사하지 않는다. 무엇이 오는지만 본다.\n", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Probe).serve_forever()
