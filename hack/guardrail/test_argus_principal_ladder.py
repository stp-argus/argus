# SPDX-License-Identifier: Apache-2.0
"""주체 사다리의 순서와 해시 분리를 고정한다 (2026-08-19 재설계).

이 시험이 지키는 불변식 세 가지:

  · **순서** — key(인증 산출물) > header(자기신고) > api_key(폴백) > unavailable.
    순서가 바뀌면 위조 가능한 값이 위조 불가한 값을 덮는다.
  · **동일 주체 안정성** — 같은 재료면 도구·모델·본문이 무엇이든 같은 시드가 나온다.
    "여러 도구를 호출해도 같은 에이전트로 탐지된다"가 여기서 결정된다.
  · **티어 간 해시 분리** — 다른 티어에서 같은 문자열이 와도 시드가 갈라진다.
    갈라지지 않으면 "어떻게 알아냈는가"(신뢰 수준)가 해시에서 사라진다.
"""

from __future__ import annotations

import unittest

from argus_decide import ENFORCE, DecisionEngine, Policy
from argus_signals import _claimed_principal_conflict, _principal_ladder, _sha, _team_seed

H = "x-principal-id"


class PrincipalLadder(unittest.TestCase):
    def test_key_tier_beats_header(self):
        seed, src = _principal_ladder({H: "self-declared"}, H, user_id="agent-a")
        self.assertEqual(src, "key:user_id")
        self.assertIn("agent-a", seed)

    def test_key_tier_internal_order(self):
        _, src = _principal_ladder({}, H, user_id="u", team_id="t", key_alias="a")
        self.assertEqual(src, "key:user_id")
        _, src = _principal_ladder({}, H, team_id="t", key_alias="a")
        self.assertEqual(src, "key:team_id")
        _, src = _principal_ladder({}, H, key_alias="a")
        self.assertEqual(src, "key:alias")

    def test_header_then_api_key_then_unavailable(self):
        _, src = _principal_ladder({H: "self"}, H, api_key="sk-x")
        self.assertEqual(src, "header")
        _, src = _principal_ladder({}, H, api_key="sk-x")
        self.assertEqual(src, "api_key")
        seed, src = _principal_ladder({}, H)
        self.assertIsNone(seed)
        self.assertEqual(src, "unavailable")

    def test_master_key_placeholder_user_id_is_absent(self):
        # LiteLLM 이 master key 인증에 채우는 자리표시자가 key 티어를 자칭하면 안 된다.
        _, src = _principal_ladder({}, H, user_id="default_user_id", api_key="sk-master")
        self.assertEqual(src, "api_key")

    def test_same_agent_same_seed_regardless_of_call(self):
        # 사다리는 요청 본문을 보지 않는다 — 도구가 무엇이든 시드는 재료만으로 정해진다.
        a = _principal_ladder({H: "ignored"}, H, user_id="agent-a")
        b = _principal_ladder({}, H, user_id="agent-a")
        self.assertEqual(a[0], b[0])

    def test_cross_tier_hash_separation(self):
        key_seed, _ = _principal_ladder({}, H, user_id="agent-a")
        hdr_seed, _ = _principal_ladder({H: "agent-a"}, H)
        self.assertNotEqual(key_seed, hdr_seed)
        self.assertNotEqual(_sha(key_seed, 16), _sha(hdr_seed, 16))


class ClaimedPrincipalConflict(unittest.TestCase):
    """위조 시도는 레코드에 남아야 한다 (STP-32) — 값 원문이 아니라 해시로, 어긋날 때만."""

    def test_forged_header_under_key_tier_is_recorded(self):
        headers = {H: "agent-b"}
        _, src = _principal_ladder(headers, H, user_id="agent-a")
        out = _claimed_principal_conflict(headers, H, src)
        self.assertTrue(out["claimed_conflicts_with_resolved"])
        # header 티어였다면 됐을 principal 과 대조 가능해야 한다
        hdr_seed, _ = _principal_ladder(headers, H)
        self.assertEqual(out["claimed_principal"], _sha(hdr_seed, 16))

    def test_forged_header_with_seed_still_recorded(self):
        headers = {H: "agent-b"}
        seed, src = _principal_ladder(headers, H, user_id="agent-a")
        self.assertTrue(_claimed_principal_conflict(headers, H, src, seed)["claimed_conflicts_with_resolved"])

    def test_header_matching_key_tier_is_not_a_conflict(self):
        # STP-41 — 자기 user_id(또는 team_id·alias)를 헤더로도 붙인 협조적 에이전트는 위조가 아니다
        for kw in ({"user_id": "agent-a"}, {"team_id": "agent-a"}, {"key_alias": "agent-a"}):
            headers = {H: "agent-a"}
            seed, src = _principal_ladder(headers, H, **kw)
            self.assertEqual(_claimed_principal_conflict(headers, H, src, seed), {}, kw)

    def test_no_header_or_header_tier_leaves_no_trace(self):
        self.assertEqual(_claimed_principal_conflict({}, H, "key:user_id"), {})
        self.assertEqual(_claimed_principal_conflict({H: "self"}, H, "header"), {})


class HeaderRotation(unittest.TestCase):
    """header 티어는 자기신고다. 같은 키로 헤더만 돌리면 주체는 갈라져도 팀 backstop 에서 다시 모인다."""

    def _run(self, key_kwargs):
        e = DecisionEngine(Policy(control_level=ENFORCE, team_calls_hard=10, source="test"))
        d = None
        for i in range(12):
            seed, src = _principal_ladder({H: f"agent-{i}"}, H, api_key="sk-shared", **key_kwargs)
            team = _team_seed(key_kwargs.get("team_id"), src, "sk-shared")
            d = e.decide({"principal": _sha(seed, 16), "run_id": f"r{i}", "team": _sha(team, 16) if team else None},
                         now=1000.0 + i)
        return d

    def test_rotating_header_on_one_key_trips_team_backstop(self):
        d = self._run({})
        self.assertEqual(d.decision, "block", "헤더를 돌려 창을 새로 받았다")
        self.assertEqual([h["axis"] for h in d.policy_hit if h["level"] == "hard"], ["team_calls"])

    def test_key_tier_keeps_team_semantics(self):
        # key 티어(user_id)는 주체가 이미 위조 불가다 — 팀이 없으면 키로 묶지 않는다(같은 키의 여러 user_id 를 합치지 않게).
        self.assertIsNone(_team_seed(None, "key:user_id", "sk-shared"))
        self.assertEqual(_team_seed("t1", "header", "sk-shared"), "team:t1")


if __name__ == "__main__":
    unittest.main()
