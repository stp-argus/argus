# SPDX-License-Identifier: Apache-2.0
"""Bedrock pass-through(`/bedrock/model/{id}/converse`·`/invoke`) 본문 해석.

    python3 -m unittest discover -s hack/guardrail -p 'test_*.py'

모양은 litellm 1.96.0 bedrock_llm_proxy_route 가 가드레일에 넘기는 그대로다:
{"method", "endpoint", "data": <Bedrock 원문 본문>, "custom_llm_provider": "bedrock"(, "model")}.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from argus_signals import _normalize, _tool_args_value_hashes, model_of  # noqa: E402

CONVERSE = {
    "system": [{"text": "you are a refund agent " * 20}],
    "messages": [
        {"role": "user", "content": [{"text": "refund order 42"}]},
        {"role": "assistant", "content": [{"text": "checking"},
                                          {"toolUse": {"toolUseId": "t1", "name": "get_order", "input": {"id": 42}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "EIO"}]}}]},
    ],
}


def wrap(body, endpoint="model/us.anthropic.claude-sonnet-5/converse"):
    return {"method": "POST", "endpoint": endpoint, "data": body, "custom_llm_provider": "bedrock"}


class BedrockPassthrough(unittest.TestCase):
    def test_converse_tool_use_and_text(self):
        n = _normalize("allm_passthrough_route", wrap(CONVERSE), {})
        self.assertEqual((n["wire"], n["wire_source"]), ("bedrock", "passthrough"))
        self.assertEqual(n["tool_name"], "get_order")
        self.assertEqual(n["raw_args"], {"id": 42})
        self.assertEqual(n["first_user"], "refund order 42")
        self.assertEqual(n["turn_count"], 3)
        self.assertGreater(n["turn_bytes"], 400, "system 프롬프트도 크기에 들어가야 한다")

    def test_same_tool_same_args_hash_identically(self):
        # duplicate_calls 가 설 수 있어야 한다 — 같은 도구·인자면 같은 지문
        a = _normalize("allm_passthrough_route", wrap(CONVERSE), {})
        b = _normalize("pass_through_endpoint", wrap(CONVERSE), {})
        self.assertEqual(_tool_args_value_hashes(a["tool_name"], a["raw_args"]),
                         _tool_args_value_hashes(b["tool_name"], b["raw_args"]))

    def test_invoke_anthropic_body(self):
        body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"},
                             {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "search", "input": {"q": "x"}}]}]}
        n = _normalize("allm_passthrough_route", wrap(body, "model/anthropic.claude-sonnet-5/invoke"), {})
        self.assertEqual((n["wire"], n["tool_name"]), ("anthropic", "search"))

    def test_model_from_endpoint_when_not_router_model(self):
        self.assertEqual(model_of(wrap(CONVERSE)), "us.anthropic.claude-sonnet-5")
        self.assertEqual(model_of({**wrap(CONVERSE), "model": "claude"}), "claude")

    def test_non_passthrough_untouched(self):
        n = _normalize("acompletion", {"messages": [{"role": "user", "content": "hi"}], "data": {"x": 1}}, {})
        self.assertEqual((n["wire"], n["wire_source"]), ("chat", "call_type"))


if __name__ == "__main__":
    unittest.main()
