# SPDX-License-Identifier: Apache-2.0
"""spec 006f — 와이어 포맷 세 가지에서 탐지가 똑같이 도는지 고정한다.

이 파일이 지키는 불변식은 하나다:

    **같은 도구 호출은 어느 와이어로 오든 같은 지문을 만든다.**

깨지면 증상이 조용하다. 도구를 못 뽑으면 `tool_name` 이 None 이 되고, 판정 엔진의
`_fingerprint()` 가 None 을 돌려주고, `duplicate_calls` 축이 **평가 자체를 안 한다.**
에러도 로그도 없고 레코드는 정상으로 보인다. 실제로 그 상태로 dev 에 배포돼 있었다.

본문 모양은 추측이 아니라 실측이다 (`hack/analyze/wire_probe.py`, 2026-08-16):
Claude Code 2.1.220 을 실물로 통과시켜 얻은 것과 같은 구조를 쓴다.
"""

from __future__ import annotations

import json
import sys
import unittest


import argus_decide as ad  # noqa: E402
import argus_signals as g  # noqa: E402

TOOL = "read_file"
ARGS = {"path": "config.yaml", "attempt": 3}

# ── 같은 도구 호출을 세 와이어로 표현한 것 ──────────────────────────────────
CHAT = {
    "model": "m",
    "messages": [
        {"role": "user", "content": "read config.yaml"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": TOOL, "arguments": json.dumps(ARGS)}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "port: 8080"},
    ],
}

# Claude Code 실측 구조: system 은 최상위, 도구 호출은 content[] 의 tool_use 블록.
ANTHROPIC = {
    "model": "m",
    "system": "You are a helpful assistant.",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "read config.yaml"}]},
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": "t1", "name": TOOL, "input": ARGS}]},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "port: 8080"}]},
    ],
}

# Codex / OpenAI Agents SDK: input[] 배열에 function_call 아이템.
RESPONSES = {
    "model": "m",
    "input": [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "read config.yaml"}]},
        {"type": "function_call", "call_id": "c1", "name": TOOL, "arguments": json.dumps(ARGS)},
        {"type": "function_call_output", "call_id": "c1", "output": "port: 8080"},
    ],
}

WIRES = [
    ("chat", "acompletion", CHAT),
    ("anthropic", "aanthropic_messages", ANTHROPIC),
    ("responses", "aresponses", RESPONSES),
]


class ToolCallsAreFoundOnEveryWire(unittest.TestCase):
    def test_wire_is_identified(self):
        for want, call_type, body in WIRES:
            with self.subTest(want):
                self.assertEqual(g._normalize(call_type, body, {})["wire"], want)

    def test_wire_is_identified_without_call_type(self):
        """call_type 을 못 믿는 상황에서도 본문 모양으로 판별된다.

        LiteLLM 이 타입을 늘렸을 때 우리가 조용히 unknown 으로 떨어지지 않게 하는 안전망.
        """
        for want, _ct, body in WIRES:
            with self.subTest(want):
                norm = g._normalize("some_new_call_type", body, {})
                self.assertEqual(norm["wire"], want)
                self.assertEqual(norm["wire_source"], "shape")

    def test_tool_name_and_args_extracted_everywhere(self):
        for want, call_type, body in WIRES:
            with self.subTest(want):
                norm = g._normalize(call_type, body, {})
                name, hashes = g._tool_args_value_hashes(norm["tool_name"], norm["raw_args"])
                self.assertEqual(name, TOOL, f"{want}: 도구 이름을 못 뽑았다")
                self.assertEqual(sorted(hashes or {}), ["attempt", "path"])

    @staticmethod
    def _append(body, *turns):
        import copy
        b = copy.deepcopy(body)
        (b["input"] if isinstance(b.get("input"), list) else b["messages"]).extend(turns)
        return b

    @staticmethod
    def _turn(body, role, text):
        if "input" in body:
            return {"type": "message", "role": role, "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}
        return {"role": role, "content": text}

    def test_tool_loop_ends_when_the_model_answers_in_text(self):
        """K1 (2026-10-03) — 모델이 글로 답한 뒤 이어지는 평범한 질문엔 옛 도구 호출을 지문으로 내지 않는다.

        안 그러면 도구를 한 번 쓴 대화의 질문마다 같은 지문이 쌓여 20턴째 duplicate_calls hard 로 정상 대화가 막힌다.
        """
        for want, call_type, body in WIRES:
            with self.subTest(want):
                b = self._append(body, self._turn(body, "assistant", "port is 8080"),
                                 self._turn(body, "user", "thanks — and the host?"))
                self.assertIsNone(g._normalize(call_type, b, {})["tool_name"], f"{want}: 끝난 도구 루프의 호출이 지문으로 남았다")

    def test_user_nudge_inside_a_tool_loop_keeps_the_call(self):
        """재촉 루프 — 매 스텝 user 메시지를 덧붙이는 오케스트레이터(샘플앱 포함)의 도구 호출은 그대로 잡는다.

        2026-10-04 K1 첫 수정이 user 턴을 경계로 삼아 이 루프의 tool_name 이 전부 None 이 됐고
        dev 실모델에서 duplicate_calls 가 통째로 꺼졌다. 그 회귀를 막는다.
        """
        for want, call_type, body in WIRES:
            with self.subTest(want):
                b = self._append(body, self._turn(body, "user", "Step 2: call the tool again"))
                self.assertEqual(g._normalize(call_type, b, {})["tool_name"], TOOL, f"{want}: 재촉 루프의 도구 호출을 놓쳤다")

    def test_same_call_gives_the_same_fingerprint_on_all_wires(self):
        """⭐ 이 파일의 핵심. 와이어가 달라도 판정이 같아야 한다."""
        prints = set()
        for _want, call_type, body in WIRES:
            norm = g._normalize(call_type, body, {})
            name, hashes = g._tool_args_value_hashes(norm["tool_name"], norm["raw_args"])
            prints.add(ad._fingerprint({"tool_name": name, "tool_args_value_hashes": hashes}))
        self.assertEqual(len(prints), 1, f"와이어마다 지문이 달라졌다: {prints}")
        self.assertNotIn(None, prints)

    def test_unknown_wire_extracts_nothing_but_does_not_raise(self):
        """모르는 모양이 와도 호출 경로로 예외가 새지 않는다 (NFR-001)."""
        for body in ({}, {"foo": "bar"}, {"messages": "not-a-list"}, {"input": 42}):
            with self.subTest(str(body)[:30]):
                norm = g._normalize("mystery", body, {})
                self.assertIsNone(norm["tool_name"])
                self.assertIsInstance(norm["turn_bytes"], int)


class MessageSizeCountsWhatIsActuallySent(unittest.TestCase):
    def test_anthropic_system_prompt_is_counted(self):
        """Anthropic 은 system 이 최상위 필드다.

        안 세면 6KB 짜리 시스템 프롬프트가 통째로 안 보인다 — Claude Code 실측 6.4KB.
        """
        with_system = g._normalize("aanthropic_messages", ANTHROPIC, {})["turn_bytes"]
        without = dict(ANTHROPIC)
        without.pop("system")
        self.assertGreater(with_system, g._normalize("aanthropic_messages", without, {})["turn_bytes"])


class ConversationKeyLadder(unittest.TestCase):
    """client > protocol > derived > unavailable."""

    def test_claude_code_session_id_wins(self):
        h = {"x-claude-code-session-id": "4d5417df-25ea-437c-8487-f82d96c4ebcc"}
        norm = g._normalize("aanthropic_messages", ANTHROPIC, h)
        key, src = g._conversation(ANTHROPIC, h, norm, "p1")
        self.assertEqual(src, "client")
        self.assertTrue(key)

    def test_session_id_is_stable_across_turns(self):
        """실측: Claude Code 의 세션 ID 는 호출 간 유지된다 — 대화 축을 공짜로 준다."""
        h = {"x-claude-code-session-id": "sess-1"}
        later = dict(ANTHROPIC)
        later["messages"] = ANTHROPIC["messages"] + [
            {"role": "assistant", "content": [{"type": "text", "text": "the port is 8080"}]}]
        a = g._conversation(ANTHROPIC, h, g._normalize("aanthropic_messages", ANTHROPIC, h), "p1")
        b = g._conversation(later, h, g._normalize("aanthropic_messages", later, h), "p1")
        self.assertEqual(a, b)

    def test_protocol_conversation_object_is_used(self):
        body = dict(RESPONSES, conversation="conv_abc123")
        norm = g._normalize("aresponses", body, {})
        self.assertEqual(g._conversation(body, {}, norm, "p1")[1], "protocol")

    def test_falls_back_to_derived(self):
        for _want, call_type, body in WIRES:
            with self.subTest(_want):
                norm = g._normalize(call_type, body, {})
                self.assertEqual(g._conversation(body, {}, norm, "p1")[1], "derived")

    def test_truncated_tail_is_unavailable_not_a_wrong_guess(self):
        """previous_response_id + store 면 꼬리만 온다 — 파생 키가 턴마다 달라진다.

        틀린 키를 만드느니 모른다고 말한다. 그래야 엔진이 대화 축을 건너뛴다.
        """
        body = dict(RESPONSES, previous_response_id="resp_abc")
        norm = g._normalize("aresponses", body, {})
        self.assertEqual(g._conversation(body, {}, norm, "p1")[1], "unavailable")


class DominantClientsGetTheirDocumentedSessionAxis(unittest.TestCase):
    """두 지배적 클라이언트는 세션을 **공식 계약으로** 준다. 파생 해시를 쓸 이유가 없다.

    Claude Code  게이트웨이 프로토콜 문서 — `x-claude-code-session-id`:
        "Use it to aggregate all requests from one session without parsing request bodies"
    Codex CLI    codex-rs/codex-api/src/requests/headers.rs — `session-id` / `thread-id`
    """

    CLAUDE = {"user-agent": "claude-cli/2.1.220 (external, sdk-cli)",
              "x-claude-code-session-id": "sess-abc"}
    CODEX = {"user-agent": "codex_cli_rs/0.5.0", "originator": "codex_cli_rs",
             "session-id": "sess-codex", "thread-id": "thr-9"}

    def test_both_reach_client_grade(self):
        for name, h, ct, body in (("claude-code", self.CLAUDE, "aanthropic_messages", ANTHROPIC),
                                  ("codex", self.CODEX, "aresponses", RESPONSES)):
            with self.subTest(name):
                src = g._conversation(body, h, g._normalize(ct, body, h), "p1")[1]
                self.assertEqual(src, "client", f"{name} 가 파생 해시로 떨어졌다")

    def test_claude_code_session_wins_over_codex_headers(self):
        """둘 다 있으면 더 구체적인 쪽. 순서는 _SESSION_HEADERS 하나가 정한다."""
        h = {**self.CLAUDE, **self.CODEX}
        self.assertEqual(g._client_identity(h, {})["client_session_header"],
                         "x-claude-code-session-id")

    def test_subagent_headers_are_captured(self):
        """서브에이전트는 아직 판정 축이 아니지만 **지금 안 남기면 나중에 못 만든다.**"""
        claude = g._client_identity({**self.CLAUDE, "x-claude-code-agent-id": "a1",
                                     "x-claude-code-parent-agent-id": "a0"}, {})
        self.assertEqual(sorted(claude["subagent"]), ["agent", "parent"])
        # Codex 는 ID 가 아니라 종류를 준다 — 열거형이라 해시하지 않는다
        codex = g._client_identity({**self.CODEX, "x-openai-subagent": "collab_spawn"}, {})
        self.assertEqual(codex["subagent"], {"kind": "collab_spawn"})

    def test_agent_id_is_not_used_as_principal(self):
        """게이트웨이 문서가 못박는다 — "identifies an agent, not a person or a device"."""
        ident = g._client_identity({**self.CLAUDE, "x-claude-code-agent-id": "a1"}, {})
        self.assertNotIn("protocol_user", ident)
        self.assertNotEqual(ident.get("client_session"), ident["subagent"]["agent"])

    def test_unknown_claude_code_headers_are_still_captured(self):
        """문서가 명시적으로 요구한다 — "Treat the headers as open lists, not closed ones."

        고정 목록만 보면 다음 릴리스가 주는 것을 놓친다.
        """
        h = {**self.CLAUDE, "x-claude-code-something-new": "v"}
        self.assertIn("x-claude-code-something-new", g._client_identity(h, {})["headers"])

    def test_secrets_are_never_captured_even_with_prefix_rule(self):
        h = {**self.CLAUDE, "authorization": "Bearer sk-real", "x-api-key": "sk-real"}
        blob = json.dumps(g._client_identity(h, {}))
        self.assertNotIn("sk-real", blob)
        self.assertNotIn("authorization", blob)


class UnknownConversationIsNotCountedIntoAFakeBucket(unittest.TestCase):
    """예전 버그의 회귀 방지.

    대화 키가 없으면 `principal|run_id` 로 버킷을 만들었다. 그러면 대화를 식별 못 하는
    와이어에서 **무관한 작업이 서로의 반복으로 세어진다** = 오탐.
    """

    def _engine(self):
        return ad.DecisionEngine(ad.Policy(
            control_level=ad.OBSERVE, repeat_soft=2, repeat_hard=3, window_s=60))

    def test_duplicate_axis_is_skipped_when_conversation_is_unknown(self):
        e = self._engine()
        sig = {"principal": "p1", "run_id": "r1", "conversation_hash": None,
               "conversation_source": "unavailable",
               "tool_name": TOOL, "tool_args_value_hashes": {"path": "abc"}}
        for _ in range(6):
            d = e.decide(dict(sig))
        self.assertNotIn("duplicate_calls", d.observations,
                         "대화를 모르는데 반복을 셌다 — 무관한 작업이 서로의 반복이 된다")
        self.assertFalse([h for h in d.policy_hit if h["axis"] == "duplicate_calls"])

    def test_duplicate_axis_runs_when_conversation_is_known(self):
        e = self._engine()
        sig = {"principal": "p1", "run_id": "r1", "conversation_hash": "conv-1",
               "conversation_source": "client",
               "tool_name": TOOL, "tool_args_value_hashes": {"path": "abc"}}
        seen = [e.decide(dict(sig)) for _ in range(4)]
        # 1·2번째는 세기만 하고, 3번째(hard=3)에서 걸린다. 그 뒤는 쿨다운이 유지한다.
        self.assertEqual([d.observations.get("duplicate_calls") for d in seen[:3]], [1, 2, 3])
        self.assertTrue(seen[2].would_block, "반복이 임계에 닿았는데 안 걸렸다")
        self.assertEqual(seen[3].policy_hit[0]["axis"], "cooldown")

    def test_two_unknown_conversations_do_not_pollute_each_other(self):
        """서로 다른 작업이 같은 principal 로 와도 반복으로 합산되지 않는다."""
        e = self._engine()
        base = {"principal": "p1", "conversation_hash": None,
                "conversation_source": "unavailable", "tool_args_value_hashes": {"path": "abc"}}
        for i in range(10):
            d = e.decide({**base, "run_id": f"r{i}", "tool_name": f"tool_{i % 2}"})
        self.assertNotIn("duplicate_calls", d.observations)


if __name__ == "__main__":
    unittest.main()
