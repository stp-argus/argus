#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""와이어 포맷 프로브 — 시장 지배적 클라이언트가 **도구 호출을 어떤 모양으로** 보내는가.

`capture_probe.py`(006d)는 "신원 재료가 무엇인가"를 쟀다. 이건 다른 질문을 잰다:

    우리 파서가 이 클라이언트의 도구 호출을 실제로 뽑아내는가?

왜 필요한가 — 가드레일의 `_tool_args_value_hashes` 는 `msg["tool_calls"]`(OpenAI 모양)만
찾는다. 그런데 LiteLLM 의 CallTypes 에는 `anthropic_messages` 와 `responses` 가 **별개
호출 타입**으로 있다. 못 뽑으면 `tool_name` 이 None 이 되고, 그러면 판정 엔진의
`_fingerprint()` 가 None 을 돌려주어 **`duplicate_calls` 축이 평가 자체를 안 한다.**
에러도 로그도 없다. 조용한 미탐이다.

## 이 프로브가 하는 일

세 와이어를 전부 받고, **1턴에 도구 호출을 돌려준다.** 클라이언트는 그 도구를 실행하려
시도하고 결과(또는 실패)를 담아 다시 요청한다 — **그 두 번째 요청이 우리가 보려던 모양이다.**

도구 이름은 일부러 존재하지 않는 `argus_probe_noop` 이다. 클라이언트가 찾지 못해 오류를
담아 되돌려주므로 **아무것도 실행되지 않는다.** 우리가 원하는 건 실행 결과가 아니라 모양이다.

매 요청마다 **현행 가드레일 파서를 그대로 돌려** 무엇을 뽑았는지 같이 기록한다.
따로 구현하면 여기서 본 것과 운영에서 남는 것이 갈라진다.

    python3 hack/analyze/wire_probe.py wire.jsonl 4101

    # Claude Code (Anthropic Messages · SSE 필수)
    ANTHROPIC_BASE_URL=http://127.0.0.1:4101 ANTHROPIC_API_KEY=probe \
      claude -p "read config.yaml"

    # Codex (OpenAI)
    OPENAI_BASE_URL=http://127.0.0.1:4101/v1 OPENAI_API_KEY=probe \
      codex exec "read config.yaml"

    python3 hack/analyze/wire_report.py wire.jsonl
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "guardrail"))

# ── 신호 추출부를 그대로 import 한다 ────────────────────────────────────────
# 예전엔 소스를 텍스트로 잘라 exec 했는데, 그러면 함수 시그니처가 바뀔 때 프로브가
# 조용히 옛 코드를 재는 상태가 된다. 측정 도구가 측정 대상과 갈라지는 건 최악이다.
#
# argus_signals 는 프록시에 묶이지 않아 **litellm 스텁도 필요 없다.**
import argus_signals as _g  # noqa: E402

_client_identity = _g._client_identity

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "wire.jsonl")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4101

PROBE_TOOL = "argus_probe_noop"
PROBE_ARGS = {"path": "config.yaml", "attempt": 1}

# 대화별 턴 수. 1턴엔 도구 호출, 2턴부터는 평문으로 끝낸다 (무한 루프 방지).
_turns: dict[str, int] = {}


# ── 참조 파서 — "제대로 뽑으면 무엇이 나오는가"의 기준선 ──────────────────────
# 현행 파서와 나란히 기록해서 격차를 숫자로 만든다. 이게 나중에 정규화기의 사양이 된다.
def _reference_tool(body: dict) -> tuple[str | None, dict | None, str]:
    """와이어를 가리지 않고 마지막 도구 호출을 찾는다. (이름, 인자, 찾은 위치)"""
    # ① OpenAI Chat Completions — assistant.tool_calls[]
    for msg in reversed(body.get("messages") or []):
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            return fn.get("name"), _loads(fn.get("arguments")), "messages[].tool_calls"
    # ② Anthropic Messages — assistant.content[] 의 tool_use 블록
    for msg in reversed(body.get("messages") or []):
        content = msg.get("content")
        if isinstance(content, list):
            for blk in reversed(content):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    return blk.get("name"), blk.get("input"), "messages[].content[].tool_use"
    # ③ OpenAI Responses — input[] 의 function_call 아이템
    inp = body.get("input")
    if isinstance(inp, list):
        for item in reversed(inp):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                return item.get("name"), _loads(item.get("arguments")), "input[].function_call"
            # Responses 도 메시지 아이템 안에 블록을 담을 수 있다
            content = item.get("content")
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") in ("tool_use", "function_call"):
                        return (blk.get("name"), _loads(blk.get("arguments") or blk.get("input")),
                                "input[].content[]")
    return None, None, "(없음)"


def _loads(raw):
    if raw is None or isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"_unparsed": str(raw)[:80]}


# 프로브는 HTTP 경로로 와이어를 알지만 가드레일은 LiteLLM 의 call_type 으로 안다.
# 운영과 같은 경로를 태우려고 되돌려 준다.
_CALL_TYPE_BY_WIRE = {
    "chat": "acompletion",
    "anthropic": "aanthropic_messages",
    "responses": "aresponses",
}


def _wire_of(path: str, body: dict) -> str:
    if "/messages" in path or "anthropic" in path:
        return "anthropic"
    if "/responses" in path:
        return "responses"
    if "/chat/completions" in path:
        return "chat"
    return "unknown"


def _conv_keys(headers: dict, body: dict) -> dict:
    """대화 축으로 쓸 수 있는 것이 요청에 실려 오는가."""
    return {k: v for k, v in {
        "x-claude-code-session-id": headers.get("x-claude-code-session-id"),
        "x-claude-code-agent-id": headers.get("x-claude-code-agent-id"),
        "x-claude-code-parent-agent-id": headers.get("x-claude-code-parent-agent-id"),
        "session_id": headers.get("session_id") or headers.get("x-session-id"),
        "originator": headers.get("originator"),
        "body.conversation": body.get("conversation"),
        "body.previous_response_id": body.get("previous_response_id"),
        "body.store": body.get("store"),
        "body.prompt_cache_key": body.get("prompt_cache_key"),
        "body.safety_identifier": body.get("safety_identifier"),
        "body.metadata": bool(body.get("metadata")),
    }.items() if v not in (None, False)}


# ── 응답 생성 ────────────────────────────────────────────────────────────────
def _anthropic_tool_call() -> dict:
    return {
        "id": "msg_probe", "type": "message", "role": "assistant", "model": "probe",
        "content": [{"type": "tool_use", "id": "toolu_probe",
                     "name": PROBE_TOOL, "input": PROBE_ARGS}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _anthropic_text() -> dict:
    return {
        "id": "msg_probe", "type": "message", "role": "assistant", "model": "probe",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


def _chat_reply(model: str, tool: bool) -> dict:
    msg = ({"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_probe", "type": "function",
                            "function": {"name": PROBE_TOOL,
                                         "arguments": json.dumps(PROBE_ARGS)}}]}
           if tool else {"role": "assistant", "content": "ok"})
    return {"id": "chatcmpl-probe", "object": "chat.completion", "created": 0,
            "model": model or "probe",
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _responses_reply(model: str, tool: bool) -> dict:
    output = ([{"type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                "name": PROBE_TOOL, "arguments": json.dumps(PROBE_ARGS), "status": "completed"}]
              if tool else
              [{"type": "message", "id": "msg_probe", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}]}])
    return {"id": "resp_probe", "object": "response", "created_at": 0, "status": "completed",
            "model": model or "probe", "output": output, "parallel_tool_calls": True,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}


def _anthropic_sse(payload: dict) -> bytes:
    """Claude Code 는 SSE 로만 받는다 — 논스트리밍으로 주면 붙지 않는다."""
    blk = payload["content"][0]
    ev: list[tuple[str, dict]] = [
        ("message_start", {"type": "message_start",
                           "message": {**payload, "content": [],
                                       "usage": {"input_tokens": 10, "output_tokens": 0}}}),
    ]
    if blk["type"] == "tool_use":
        ev += [
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "tool_use", "id": blk["id"],
                                                       "name": blk["name"], "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": json.dumps(blk["input"])}}),
        ]
    else:
        ev += [
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": blk["text"]}}),
        ]
    ev += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": payload["stop_reason"], "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in ev)


def _openai_sse(payload: dict, kind: str) -> bytes:
    """Chat/Responses 스트리밍은 한 덩어리로 흘려도 SDK 가 받는다."""
    if kind == "responses":
        chunks = [{"type": "response.completed", "response": payload}]
    else:
        ch = payload["choices"][0]["message"]
        delta = {"role": "assistant"}
        delta.update({"tool_calls": [{"index": 0, **tc} for tc in ch["tool_calls"]]}
                     if ch.get("tool_calls") else {"content": ch.get("content")})
        chunks = [{"id": payload["id"], "object": "chat.completion.chunk", "created": 0,
                   "model": payload["model"],
                   "choices": [{"index": 0, "delta": delta,
                                "finish_reason": payload["choices"][0]["finish_reason"]}]}]
    out = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    return out + b"data: [DONE]\n\n"


class Probe(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _read_body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def _capture(self, body: dict, wire: str) -> dict:
        headers = {k.lower(): v for k, v in self.headers.items()}
        ident = _client_identity(headers, body)

        # ⭐ 핵심 — 운영 파이프라인(정규화기)이 뽑는 것 vs 참조 파서가 뽑는 것.
        # 둘이 갈라지면 그 와이어에서 탐지가 조용히 꺼진다.
        norm = _g._normalize(_CALL_TYPE_BY_WIRE.get(wire, ""), body, headers)
        cur_name, cur_args = _g._tool_args_value_hashes(norm["tool_name"], norm["raw_args"])
        conv_hash, conv_source = _g._conversation(body, headers, norm, "probe-principal")
        ref_name, ref_args, ref_where = _reference_tool(body)

        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "path": self.path,
            "wire": wire,
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "body_keys": sorted(body.keys()),
            "msg_count": len(body.get("messages") or []),
            "input_kind": type(body.get("input")).__name__ if "input" in body else None,
            "input_len": len(body["input"]) if isinstance(body.get("input"), list) else None,
            "tool_count": len(body.get("tools") or []),
            "client": ident.get("client"),
            "client_version": ident.get("client_version"),
            "conv_keys": _conv_keys(headers, body),
            # 운영 파이프라인이 뽑은 것
            "detected_wire": norm["wire"],
            "wire_source": norm["wire_source"],
            "conversation_source": conv_source,
            "current_tool": cur_name,
            "current_arg_keys": sorted(cur_args) if cur_args else None,
            # 제대로 뽑으면 나오는 것
            "reference_tool": ref_name,
            "reference_arg_keys": sorted(ref_args) if isinstance(ref_args, dict) else None,
            "reference_where": ref_where,
            # 이 한 줄이 이 측정의 결론이다
            "gap": bool(ref_name and not cur_name),
        }
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        mark = "  ❌ 현행 파서가 못 뽑음" if rec["gap"] else ("  ✅" if cur_name else "")
        print(f"  [{(rec['client'] or '?'):<12}] {wire:<9} {self.path:<24} "
              f"tools={rec['tool_count']:<3} 도구={ref_name or '-'} conv={conv_source}{mark}", flush=True)
        return rec

    def _send(self, blob: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        body = self._read_body()
        wire = _wire_of(self.path, body)
        self._capture(body, wire)

        # 대화 키가 없으면 클라이언트별로 하나로 본다 — 턴 세기용일 뿐이다
        key = (self.headers.get("x-claude-code-session-id")
               or self.headers.get("user-agent") or "anon")
        _turns[key] = _turns.get(key, 0) + 1
        first = _turns[key] == 1

        if wire == "anthropic":
            payload = _anthropic_tool_call() if first else _anthropic_text()
            if body.get("stream"):
                return self._send(_anthropic_sse(payload), "text/event-stream")
            return self._send(json.dumps(payload).encode(), "application/json")

        if wire == "responses":
            payload = _responses_reply(body.get("model"), first)
            if body.get("stream"):
                return self._send(_openai_sse(payload, "responses"), "text/event-stream")
            return self._send(json.dumps(payload).encode(), "application/json")

        payload = _chat_reply(body.get("model"), first)
        if body.get("stream"):
            return self._send(_openai_sse(payload, "chat"), "text/event-stream")
        return self._send(json.dumps(payload).encode(), "application/json")

    def do_GET(self):
        self._capture({}, _wire_of(self.path, {}))
        self._send(json.dumps({"object": "list", "data": [{"id": "probe", "object": "model"}]})
                   .encode(), "application/json")

    do_HEAD = do_GET


if __name__ == "__main__":
    print(f"와이어 프로브 :{PORT}  →  {OUT}\n"
          f"  1턴에 도구 호출({PROBE_TOOL})을 돌려준다. 존재하지 않는 도구라 실행되지 않는다.\n"
          f"  되돌아온 요청의 모양이 우리가 보려던 것이다.\n", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Probe).serve_forever()
