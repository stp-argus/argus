# SPDX-License-Identifier: Apache-2.0
"""레코드의 OTel GenAI 표준 뷰(`gen_ai` 블록)가 표준 규칙을 지키는지 고정한다.

표준(semantic-conventions-genai)이 **문장으로** 강제하는 것을 테스트로 옮긴 것이다:

    conversation.id — "식별자가 없으면 채우지 말라. 새 UUID, trace id,
    request content 의 해시를 폴백으로 쓰지 말라."
    conversation.compacted — "확실할 때만 true 로. false 는 설정하지 말라."

우리 `derived` 등급이 정확히 그 금지된 "request content 해시"라서, 이 규칙이
깨지면 표준 위반이 조용히 밖으로 새어 나간다 — 그래서 회귀 테스트가 필요하다.
"""

from __future__ import annotations

import unittest

import argus_signals as g


def _record(**over) -> dict:
    base = {
        "call_type": "acompletion",
        "model": "argus-mock-gpt",
        "provider": None,
        "conversation_hash": "abc123",
        "conversation_source": "client",
        "tool_name": None,
        "stream": False,
        "truncated": False,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_cached": None,
        "tokens_reasoning": None,
    }
    base.update(over)
    return base


class ConversationIdFollowsTheStandardsFallbackBan(unittest.TestCase):
    """표준 주석 [3]: 합성 폴백을 conversation.id 로 내보내지 않는다."""

    def test_exportable_tiers_carry_the_id(self):
        for source in ("gateway", "client", "protocol"):
            with self.subTest(source=source):
                view = g._genai_view(_record(conversation_source=source))
                self.assertEqual(view.get("conversation.id"), "abc123")

    def test_derived_hash_is_never_exported(self):
        # derived = sha(principal|model|첫 user 해시) — 표준이 명시적으로 금지하는
        # "request content 의 해시" 그 자체다.
        view = g._genai_view(_record(conversation_source="derived"))
        self.assertNotIn("conversation.id", view)

    def test_unavailable_exports_nothing(self):
        view = g._genai_view(_record(conversation_source="unavailable",
                                     conversation_hash=None))
        self.assertNotIn("conversation.id", view)


class CompactedIsPositiveOnly(unittest.TestCase):
    """표준 주석 [2]: true 만 설정, false 는 아예 없어야 한다."""

    def test_truncated_maps_to_true(self):
        self.assertIs(g._genai_view(_record(truncated=True))["conversation.compacted"], True)

    def test_not_truncated_leaves_the_key_unset(self):
        self.assertNotIn("conversation.compacted", g._genai_view(_record(truncated=False)))


class OperationNameCollapsesWiresIntoStandardValues(unittest.TestCase):
    """와이어가 달라도 연산은 같다 — anthropic/responses 도 chat 이다."""

    def test_all_chat_wires_map_to_chat(self):
        for ct in ("completion", "acompletion", "anthropic_messages",
                   "aanthropic_messages", "responses", "aresponses"):
            with self.subTest(call_type=ct):
                self.assertEqual(g._genai_view(_record(call_type=ct))["operation.name"], "chat")

    def test_embeddings_and_text_completion_keep_their_own_values(self):
        self.assertEqual(g._genai_view(_record(call_type="aembedding"))["operation.name"],
                         "embeddings")
        self.assertEqual(g._genai_view(_record(call_type="text_completion"))["operation.name"],
                         "text_completion")

    def test_unknown_call_type_emits_no_operation(self):
        # 모르는 값을 chat 으로 우기면 표준 소비자가 잘못 집계한다 — 비워 둔다.
        self.assertNotIn("operation.name", g._genai_view(_record(call_type="mystery")))


class UsageMapsOneToOne(unittest.TestCase):
    def test_filled_tokens_are_translated(self):
        view = g._genai_view(_record(tokens_in=1204, tokens_out=87,
                                     tokens_cached=900, tokens_reasoning=40))
        self.assertEqual(view["usage.input_tokens"], 1204)
        self.assertEqual(view["usage.output_tokens"], 87)
        self.assertEqual(view["usage.cache_read.input_tokens"], 900)
        self.assertEqual(view["usage.reasoning.output_tokens"], 40)

    def test_missing_usage_stays_absent_not_zero(self):
        # 요청측 시점(성공 이벤트 전)의 레코드 — 0 으로 지어내지 않는다.
        view = g._genai_view(_record())
        for k in ("usage.input_tokens", "usage.output_tokens",
                  "usage.cache_read.input_tokens", "usage.reasoning.output_tokens"):
            self.assertNotIn(k, view)


class WriteInjectsTheViewOnEveryPath(unittest.TestCase):
    """_write 가 단일 관문 — 차단·성공·sweep 어느 경로로 나가든 블록이 붙는다."""

    def test_write_attaches_gen_ai_block(self):
        record = _record(tool_name="read_file", provider="openai", tokens_in=10)
        old = g.RECORDS_PATH
        try:
            import os, tempfile
            g.RECORDS_PATH = os.path.join(tempfile.mkdtemp(), "records.jsonl")
            g._write(record)
        finally:
            g.RECORDS_PATH = old
        self.assertEqual(record["gen_ai"]["tool.name"], "read_file")
        self.assertEqual(record["gen_ai"]["provider.name"], "openai")
        self.assertEqual(record["gen_ai"]["usage.input_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
