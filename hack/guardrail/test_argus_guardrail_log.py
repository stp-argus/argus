# SPDX-License-Identifier: Apache-2.0
"""탐지 로그의 억제 규칙을 고정한다.

로그는 "있으면 좋은 것"이 아니라 이 사이클의 **관측 창구**다. 다만 규칙이 두 방향으로
동시에 틀릴 수 있어서 시험이 필요하다:

  · 너무 조용하면 — 탐지가 돌아도 아무도 모른다 (레코드를 꺼내 읽어야만 보인다)
  · 너무 시끄러우면 — 진짜 신호가 묻힌다. dev 실측에서 90콜 중 soft 히트가 36건,
    차단 20건 중 18건이 쿨다운 연장이었다. 그대로 찍으면 로그가 도배된다
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout

# 가드레일은 LiteLLM 을 import 한다. 로그 함수 하나를 시험하려고 게이트웨이를 깔 수는 없다.

import argus_signals as g  # noqa: E402


class FakeDecision:
    def __init__(self, hits, decision, would_block, level="Observe"):
        self.policy_hit = hits
        self.decision = decision
        self.would_block = would_block
        self.control_level = level
        self.decision_id = "d-test"
        self.policy_source = "crd:argus-system/observe-default"


def hit(axis, level, observed=20, threshold=20, detail=""):
    return {"axis": axis, "level": level, "observed": observed,
            "threshold": threshold, "detail": detail}


RECORD = {"principal": "p-1", "run_id": "run-01", "client_identity": {"client": "sample-app"}}


def capture(decision, record=None) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        g._log_decision(record or RECORD, decision)
    return buf.getvalue()


class WhatGetsLogged(unittest.TestCase):
    def setUp(self):
        g._cooldown_seen.clear()

    def test_hard_hit_is_logged_even_when_nothing_was_blocked(self):
        """Observe 의 would_block 이야말로 이 사이클의 산출물이다. 조용하면 안 된다."""
        out = capture(FakeDecision([hit("duplicate_calls", "hard")], "allow", True))
        self.assertIn("탐지", out)
        self.assertIn("duplicate_calls/hard=20>=20", out)
        self.assertIn("Observe", out)

    def test_block_says_it_blocked(self):
        out = capture(FakeDecision([hit("principal_calls", "hard", 27, 26)], "block", True, "Enforce"))
        self.assertIn("차단", out)
        self.assertNotIn("통과", out)

    def test_soft_only_is_quiet_by_default(self):
        """soft 는 정상 트래픽도 자주 스친다 — 기본으로 찍으면 진짜 신호가 묻힌다."""
        self.assertEqual("", capture(FakeDecision([hit("run_steps", "soft", 9, 8)], "allow", False)))

    def test_no_hits_is_quiet(self):
        self.assertEqual("", capture(FakeDecision([], "allow", False)))

    def test_reason_and_identity_are_in_the_line(self):
        """'탐지됐다' 만으로는 쓸모가 없다 — 누가·무슨 축이·얼마나 넘겼는가가 같이 있어야 한다."""
        out = capture(FakeDecision(
            [hit("rotation_gap", "hard", 6.0, 5.2, "누적 6 ÷ run당최대 1")], "block", True, "Enforce"))
        for expected in ("p-1", "run-01", "sample-app", "rotation_gap/hard=6>=5.2",
                         "누적 6 ÷ run당최대 1", "d-test", "crd:argus-system/observe-default"):
            self.assertIn(expected, out, f"{expected!r} 가 로그에 없다: {out!r}")


class CooldownIsRateLimited(unittest.TestCase):
    def setUp(self):
        g._cooldown_seen.clear()

    def test_first_cooldown_line_is_logged_then_every_nth(self):
        d = FakeDecision([hit("cooldown", "hard", 22.9, 0, "duplicate_calls=20>20")], "block", True, "Enforce")
        lines = [capture(d) for _ in range(12)]
        logged = [i + 1 for i, l in enumerate(lines) if l]
        self.assertEqual(logged, [1, 10], f"쿨다운 줄이 {logged} 에서 찍혔다 — 기대 [1, 10]")

    def test_a_fresh_trip_resets_the_cooldown_counter(self):
        """새 폭주는 이전 폭주의 억제 카운터를 물려받으면 안 된다 — 첫 줄을 놓치게 된다."""
        cool = FakeDecision([hit("cooldown", "hard", 22.9, 0)], "block", True, "Enforce")
        for _ in range(5):
            capture(cool)
        capture(FakeDecision([hit("duplicate_calls", "hard")], "block", True, "Enforce"))
        self.assertNotEqual("", capture(cool), "쿨다운 첫 줄이 억제됐다")


class NeverBreaksTheCall(unittest.TestCase):
    def test_a_malformed_decision_does_not_raise(self):
        """로그 한 줄 때문에 남의 LLM 호출이 깨지는 일은 없어야 한다 (NFR-002)."""
        class Broken:
            policy_hit = [{"axis": "x"}]  # level·observed 없음

            def __getattr__(self, _):
                raise RuntimeError("boom")

        self.assertEqual("", capture(Broken()))


if __name__ == "__main__":
    unittest.main()
