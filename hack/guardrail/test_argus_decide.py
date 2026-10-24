#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""판정 엔진 단위 테스트 — 프록시 없이, 표준 라이브러리만으로 돈다.

    python3 hack/guardrail/test_argus_decide.py

가장 중요한 테스트는 차단이 되는지가 **아니라** 정상이 안 막히는지다.
오탐 하나가 남의 진짜 작업을 끊는다.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from argus_decide import ENFORCE, GUARD, OBSERVE, DecisionEngine, Policy, load_policy_file  # noqa: E402

# 실측에서 도출된 값 (hack/analyze/derive_thresholds.py, 129콜 3코호트)
MEASURED = dict(
    duplicate_calls_soft=2, duplicate_calls_hard=20,
    principal_calls_soft=11, principal_calls_hard=26,
    run_steps_soft=8, run_steps_hard=27,
    rotation_gap_soft=3.0, rotation_gap_hard=5.2,
)


def policy(level: str = ENFORCE, **kw) -> Policy:
    p = dict(
        control_level=level, window_s=60.0, cooldown_s=30.0,
        repeat_window=20, repeat_soft=MEASURED["duplicate_calls_soft"],
        repeat_hard=MEASURED["duplicate_calls_hard"],
        principal_calls_soft=MEASURED["principal_calls_soft"],
        principal_calls_hard=MEASURED["principal_calls_hard"],
        run_steps_soft=MEASURED["run_steps_soft"], run_steps_hard=MEASURED["run_steps_hard"],
        rotation_gap_soft=MEASURED["rotation_gap_soft"],
        rotation_gap_hard=MEASURED["rotation_gap_hard"],
        budget_usd_soft=0.0, budget_usd_hard=0.0,   # 정책 축 — 측정에서 켜지 않는다
        soft_escalation=False,
    )
    p.update(kw)
    return Policy(**p)


def sig(principal="p", run="r1", conv="c1", tool="read_file", args=None, cost=0.0):
    return {
        "principal": principal, "run_id": run, "conversation_hash": conv,
        "tool_name": tool, "tool_args_value_hashes": args if args is not None else {"path": "aaa"},
        "cost_usd": cost,
    }


class NoFalsePositives(unittest.TestCase):
    """정상 트래픽은 절대 막히지 않는다. 이게 깨지면 나머지는 의미 없다."""

    def test_measured_baseline_shape_is_never_blocked(self):
        """대조군 실측 형태 재현: principal 4명 · 각 2런 · 런당 3~5스텝 · 인자 매번 다름."""
        e = DecisionEngine(policy())
        t = 1000.0
        for p in range(4):
            for run in range(2):
                for step in range(5):
                    d = e.decide(sig(
                        principal=f"base{p}", run=f"base{p}-r{run}", conv=f"base{p}-c{run}",
                        args={"path": f"file-{p}-{run}-{step}"},   # 매 스텝 다른 파일 = 정상
                    ), now=t)
                    self.assertEqual(d.decision, "allow", f"정상이 차단됨: {d.reason}")
                    self.assertFalse(d.would_block, f"정상에 would_block: {d.reason}")
                    t += 2.5   # 대조군 실측 간격

    def test_single_soft_hit_never_blocks(self):
        """soft 하나로는 절대 안 막는다 — 기본 규칙."""
        e = DecisionEngine(policy())
        t = 1000.0
        # 같은 인자 2회 = duplicate_calls soft(2) 도달, 다른 축은 안 건드림
        for _ in range(2):
            d = e.decide(sig(args={"path": "same"}), now=t)
            t += 5
        self.assertEqual(d.decision, "allow")
        self.assertTrue(any(h["level"] == "soft" for h in d.policy_hit), "soft 히트는 기록돼야 한다")

    def test_two_soft_hits_do_not_block_by_default(self):
        """soft 2개도 기본값에서는 막지 않는다 — 대조군 표본이 작기 때문."""
        e = DecisionEngine(policy())
        t = 1000.0
        for i in range(12):        # principal_calls soft(11) 넘김
            d = e.decide(sig(args={"path": "same"}), now=t)   # duplicate soft 도 넘김
            t += 1
        softs = [h for h in d.policy_hit if h["level"] == "soft"]
        self.assertGreaterEqual(len(softs), 2, "soft 두 축이 걸린 상황이어야 한다")
        self.assertEqual(d.decision, "allow", "기본값에서는 soft 2개로 막지 않는다")

    def test_soft_escalation_opt_in_blocks(self):
        """스위치를 켜면 soft 2개로 막는다 — 대조군이 커진 뒤 쓰는 경로."""
        e = DecisionEngine(policy(soft_escalation=True))
        t = 1000.0
        for i in range(12):
            d = e.decide(sig(args={"path": "same"}), now=t)
            t += 1
        self.assertEqual(d.decision, "block")


class CatchesRunaways(unittest.TestCase):
    def test_retry_storm_blocked(self):
        """동일 (tool, args) 반복 — hard(20) 에서 걸린다."""
        e = DecisionEngine(policy())
        t, blocked_at = 1000.0, None
        for i in range(1, 51):
            d = e.decide(sig(principal="retry", run="run-01", args={"path": "config.yaml"}), now=t)
            if d.decision == "block" and blocked_at is None:
                blocked_at = i
            t += 0.2
        self.assertIsNotNone(blocked_at, "재시도 폭풍이 안 막혔다")
        self.assertLessEqual(blocked_at, 30, f"너무 늦게 막힘: {blocked_at}콜")

    def test_pingpong_blocked_by_principal_not_by_run(self):
        """⭐ 이 제품의 논지. run 을 회전시키면 per-run 축은 눈이 멀고,
        principal 축만 잡는다. 실측에서 run_steps 의 핑퐁 탐지율이 0% 였다."""
        e = DecisionEngine(policy())
        t, blocked_at, first = 1000.0, None, None
        for i in range(1, 51):
            run = f"run-{(i - 1) // 5 + 1:02d}"      # 5콜마다 회전 (실측과 동일)
            d = e.decide(sig(principal="pp", run=run, conv=f"pp-{run}",
                             args={"path": f"f{i}"}), now=t)
            if d.decision == "block" and blocked_at is None:
                blocked_at = i
                first = [h["axis"] for h in d.policy_hit if h["level"] == "hard"]
            t += 0.2
        self.assertIsNotNone(blocked_at, "핑퐁이 안 막혔다")
        self.assertNotIn("run_steps", first or [],
                         "run_steps 가 핑퐁을 잡으면 안 된다 — 회전이 그 축을 무력화한다")
        self.assertTrue({"principal_calls", "rotation_gap"} & set(first or []),
                        f"principal 축이 잡아야 한다. 실제: {first}")

    def test_run_steps_axis_is_blind_to_rotation(self):
        """회전하면 run 당 스텝은 5 를 넘지 않는다 — soft(8) 에도 못 미친다."""
        e = DecisionEngine(policy(principal_calls_soft=0, principal_calls_hard=0,
                                  rotation_gap_soft=0, rotation_gap_hard=0,
                                  repeat_soft=0, repeat_hard=0))
        t = 1000.0
        for i in range(1, 51):
            d = e.decide(sig(principal="pp", run=f"run-{(i - 1) // 5 + 1:02d}",
                             args={"path": f"f{i}"}), now=t)
            t += 0.2
        self.assertEqual(d.decision, "allow", "run_steps 만으로는 핑퐁을 못 잡아야 한다(사실)")


class ControlLevels(unittest.TestCase):
    def test_observe_records_but_never_blocks(self):
        e = DecisionEngine(policy(OBSERVE))
        t = 1000.0
        for _ in range(50):
            d = e.decide(sig(args={"path": "same"}), now=t)
            t += 0.2
        self.assertEqual(d.decision, "allow", "Observe 는 절대 차단하지 않는다")
        self.assertTrue(d.would_block, "그러나 would_block 은 기록돼야 한다")
        self.assertIsNotNone(d.decision_id, "판정 식별자는 Observe 에서도 발급된다")

    def test_guard_records_behavior_trips_but_does_not_block(self):
        # 행동 축(duplicate_calls hard 20)만 넘고 사용량 축(principal_calls hard 26)은 안 넘는다
        e = DecisionEngine(policy(GUARD))
        t = 1000.0
        for _ in range(22):
            d = e.decide(sig(args={"path": "same"}), now=t)
            t += 0.2
        self.assertEqual(d.decision, "allow")
        self.assertTrue(d.would_block)

    def test_guard_blocks_volume_backstop(self):
        # 인자를 매번 바꿔 행동 축은 피하고 principal_calls 만 26 을 넘긴다
        e = DecisionEngine(policy(GUARD, run_steps_hard=0, rotation_gap_hard=0))   # 행동 축을 꺼서 사용량 축만
        t = 1000.0
        for i in range(26):
            d = e.decide(sig(run=f"r{i}", args={"path": str(i)}), now=t)
            t += 0.2
        self.assertEqual(d.decision, "block")
        # 쿨다운 동안도 막는다
        self.assertEqual(e.decide(sig(run="rx", args={"path": "x"}), now=t + 1).decision, "block")

    def test_guard_blocks_volume_even_during_behavior_cooldown(self):
        # 행동 축으로 먼저 트립(20번째) → 쿨다운 → 그 사이 principal_calls 26 도달 → 막는다
        e = DecisionEngine(policy(GUARD))
        t = 1000.0
        ds = []
        for _ in range(27):
            ds.append(e.decide(sig(args={"path": "same"}), now=t).decision)
            t += 0.2
        self.assertEqual(ds[19], "allow")    # 행동 축 트립 — Guard 는 기록만
        self.assertEqual(ds[-1], "block")    # 사용량 상한 — Guard 도 막는다
        # 막은 이유가 레코드에 남는다 — cooldown 옆에 principal_calls hard
        last = e.decide(sig(args={"path": "same"}), now=t)
        self.assertIn(("principal_calls", "hard"), {(h["axis"], h["level"]) for h in last.policy_hit})

    def test_guard_blocks_budget(self):
        e = DecisionEngine(policy(GUARD, budget_usd_hard=1.0))
        d = e.decide(sig(cost=1.5), now=1000.0)
        self.assertEqual(d.decision, "block")


class EnvContract(unittest.TestCase):
    """도출기가 만드는 env 이름과 엔진이 읽는 env 이름이 같아야 한다.

    이 짝이 어긋나면 **축이 조용히 꺼진 채로 배포된다.** 실제로 겪었다 —
    도출기는 ARGUS_DUPLICATE_CALLS_* 를 뱉는데 엔진은 ARGUS_REPEAT_* 를 읽고 있어서
    재시도 폭풍 축이 랩에서 한 번도 발화하지 않았다. 단위 테스트는 전부 통과했고
    (임계를 인자로 직접 넘기니까) 관통 테스트에서야 드러났다.
    """

    def test_env_names_match_axis_names(self):
        import os as _os
        import importlib
        axes = ["principal_calls", "duplicate_calls", "run_steps", "rotation_gap", "budget_usd",
                "msg_bytes", "conv_growth", "team_calls"]
        saved = {k: v for k, v in _os.environ.items() if k.startswith("ARGUS_")}
        try:
            for k in list(_os.environ):
                if k.startswith("ARGUS_"):
                    del _os.environ[k]
            for i, axis in enumerate(axes, start=1):
                # 축마다 서로 다른 값을 넣어 어느 필드로 들어갔는지 구별한다
                _os.environ[f"ARGUS_{axis.upper()}_SOFT"] = str(100 + i)
                _os.environ[f"ARGUS_{axis.upper()}_HARD"] = str(200 + i)
            mod = importlib.reload(importlib.import_module("argus_decide"))
            p = mod.Policy()
            got = {
                "principal_calls": (p.principal_calls_soft, p.principal_calls_hard),
                "duplicate_calls": (p.repeat_soft, p.repeat_hard),
                "run_steps": (p.run_steps_soft, p.run_steps_hard),
                "rotation_gap": (p.rotation_gap_soft, p.rotation_gap_hard),
                "budget_usd": (p.budget_usd_soft, p.budget_usd_hard),
                "msg_bytes": (p.msg_bytes_soft, p.msg_bytes_hard),
                "conv_growth": (p.conv_growth_soft, p.conv_growth_hard),
                "team_calls": (p.team_calls_soft, p.team_calls_hard),
            }
            for i, axis in enumerate(axes, start=1):
                self.assertEqual(
                    got[axis], (100 + i, 200 + i),
                    f"{axis}: ARGUS_{axis.upper()}_SOFT/HARD 가 엔진에 도달하지 않는다",
                )
        finally:
            for k in list(_os.environ):
                if k.startswith("ARGUS_"):
                    del _os.environ[k]
            _os.environ.update(saved)
            importlib.reload(importlib.import_module("argus_decide"))


class ConversationSize(unittest.TestCase):
    """G3/G4 (STP-19) — 호출 수는 적은데 대화가 부푸는 폭주. 임계 0 이면 관측만 한다."""

    def test_growth_is_relative_to_first_call_and_soft_only_never_blocks(self):
        e = DecisionEngine(policy(conv_growth_soft=3.0, msg_bytes_soft=5000))
        t = 1000.0
        d = None
        for i, b in enumerate((1000, 1500, 2200, 3300, 5000, 7500)):
            d = e.decide({**sig(args={"path": str(i)}), "msg_bytes": b}, now=t + i)
        self.assertEqual(d.observations["conv_growth"], 7.5)
        self.assertEqual(d.observations["msg_bytes"], 7500)
        self.assertEqual({(h["axis"], h["level"]) for h in d.policy_hit},
                         {("conv_growth", "soft"), ("msg_bytes", "soft")})
        self.assertEqual(d.decision, "allow", "soft 만으로는 절대 막지 않는다")

    def test_growth_not_evaluated_without_conversation_or_size(self):
        e = DecisionEngine(policy(conv_growth_soft=2.0))
        d1 = e.decide({**sig(), "msg_bytes": 100}, now=1000.0)
        self.assertNotIn("conv_growth", d1.observations, "첫 호출엔 분모가 없다")
        d2 = e.decide({**sig(conv=None, args={"path": "b"}), "conversation_source": "unavailable",
                       "msg_bytes": 9999}, now=1001.0)
        self.assertNotIn("conv_growth", d2.observations, "대화를 모르면 평가하지 않는다")
        self.assertEqual(d2.observations["msg_bytes"], 9999, "단일 크기는 대화 없이도 관측한다")


def _facets(**kv):
    """테스트용 facet — 값 해시·모양·변화부호. 실제 값은 argus_signals._tool_arg_facets 가 만든다."""
    out = {}
    for k, (v, shape, d) in kv.items():
        out[k] = {"h": str(v), "s": shape, "d": d}
    return out


class ArgNormalization(unittest.TestCase):
    """spec 006e "파라미터 제외" (STP-20) — 매번 조금씩 다른 요청으로 나타나는 폭주를 soft 로 잡되,
    매번 다른 파일을 읽는 정상 작업은 반복으로 뭉개지 않는다."""

    def test_retry_with_attempt_counter_is_soft_caught_but_never_hard(self):
        e = DecisionEngine(policy(repeat_soft=5, repeat_hard=20))
        d = None
        for i in range(1, 13):
            sig_i = {**sig(args={"path": "cfg", "attempt": str(i)}),
                     "tool_arg_facets": _facets(path=("cfg", "a.a", None), attempt=(i, "<int>", "+1"))}
            d = e.decide(sig_i, now=1000.0 + i)
        self.assertEqual(d.observations["duplicate_calls"], 1, "완전일치는 매번 다르다")
        self.assertGreaterEqual(d.observations["duplicate_calls_norm"], 5)
        dup = [h["level"] for h in d.policy_hit if h["axis"] == "duplicate_calls"]
        self.assertEqual(dup, ["soft"])
        self.assertEqual(d.decision, "allow", "정규화만으로는 절대 hard 가 아니다")

    def test_reading_different_files_is_not_counted_as_repeat(self):
        e = DecisionEngine(policy(repeat_soft=3, repeat_hard=20))
        d = None
        for i in range(1, 13):
            sig_i = {**sig(args={"path": f"f{i}"}),
                     "tool_arg_facets": _facets(path=(f"f{i}", "a.a", None))}
            d = e.decide(sig_i, now=1000.0 + i)
        self.assertNotIn("duplicate_calls_norm", d.observations, "남는 정체성 키가 없으면 세지 않는다")
        self.assertEqual([h for h in d.policy_hit if h["axis"] == "duplicate_calls"], [])


class TeamBackstop(unittest.TestCase):
    """G2 identity exhaustion 의 부분 해소 (STP-16) — 주체가 갈라져도 팀은 하나다."""

    def test_many_principals_one_team_trip_team_backstop(self):
        e = DecisionEngine(policy(team_calls_hard=10))
        d = None
        for i in range(12):
            d = e.decide({**sig(principal=f"p{i}", run=f"r{i}", conv=f"c{i}", args={"path": str(i)}),
                          "team": "team-x"}, now=1000.0 + i)
        # 10번째부터 막히고, 막힌 호출은 팀 창에 남지 않아 관측은 10 에 머문다.
        self.assertEqual(d.observations["team_calls"], 10)
        self.assertEqual([h["axis"] for h in d.policy_hit if h["level"] == "hard"], ["team_calls"])
        self.assertEqual(d.decision, "block")

    def test_no_team_no_axis(self):
        e = DecisionEngine(policy(team_calls_hard=1))
        d = e.decide(sig(), now=1000.0)
        self.assertNotIn("team_calls", d.observations)
        self.assertEqual(d.decision, "allow")


class Mechanics(unittest.TestCase):
    def test_budget_observes_absolute_spend_not_sum(self):
        # cost_usd 는 principal 누적 지출의 절대값이다 — 같은 값을 두 번 보내도 두 배가 되지 않고 (STP-28),
        # 영속 출처가 아니면(탭 정산) 줄어든 값은 무시한다. 영속 출처는 test_budget_reset_unblocks_after_cooldown.
        e = DecisionEngine(policy(budget_usd_soft=0.0, budget_usd_hard=1.0))
        t = 1000.0
        e.decide(sig(cost=0.5), t); d = e.decide(sig(cost=0.5), t + 1)
        self.assertEqual(d.observations["budget_usd"], 0.5)
        d = e.decide(sig(cost=0.7), t + 2)
        self.assertEqual(d.observations["budget_usd"], 0.7)
        d = e.decide(sig(cost=0.1), t + 3)
        self.assertEqual(d.observations["budget_usd"], 0.7)

    def test_budget_reset_unblocks_after_cooldown(self):
        # engine_test.go TestBudgetResetUnblocksAfterCooldown 과 같은 케이스
        p = policy(budget_usd_hard=1.0)
        for authoritative, want in ((True, "allow"), (False, "block")):
            e = DecisionEngine(p)
            first = {**sig(principal="reset", cost=1.2), "cost_authoritative": authoritative}
            self.assertEqual(e.decide(first, now=1000.0).decision, "block")
            later = {**sig(principal="reset", cost=0.05), "cost_authoritative": authoritative}
            d = e.decide(later, now=1000.0 + p.cooldown_s + 1)
            self.assertEqual(d.decision, want, f"authoritative={authoritative}: {d.reason}")

    def test_retrying_during_cooldown_does_not_extend_block(self):
        # engine_test.go TestRetryingDuringCooldownDoesNotExtendBlock 과 같은 케이스
        e = DecisionEngine(policy(principal_calls_soft=0, principal_calls_hard=5))
        saw_block, freed_at = False, None
        for i in range(91):
            now = 1000.0 + i
            d = e.decide(sig(principal="retry", args={"path": str(now)}), now=now)
            saw_block = saw_block or d.decision == "block"
            if saw_block and d.decision == "allow":
                freed_at = now
                break
        self.assertTrue(saw_block, "선행 조건: 5번째 호출에서 막혀야 한다")
        self.assertIsNotNone(freed_at, "재시도가 차단을 연장했다")
        self.assertLessEqual(freed_at, 1065.0)

    def test_sliding_window_forgets(self):
        """윈도우 밖의 호출은 잊는다 — 고정 윈도우가 아니다."""
        e = DecisionEngine(policy())
        t = 1000.0
        for _ in range(25):
            e.decide(sig(principal="w", args={"path": f"{t}"}), now=t)
            t += 1
        d = e.decide(sig(principal="w", args={"path": "x"}), now=t + 3600)
        self.assertEqual(d.observations["principal_calls"], 1, "윈도우가 안 미끄러졌다")

    def test_cooldown_survives_principal_eviction(self):
        """차단당한 주체가 잠깐 조용한 사이 다른 주체들이 LRU 를 넘겨 들어와도 쿨다운은 남는다 (STP-24)."""
        e = DecisionEngine(policy(max_principals=3))
        t = 1000.0
        for _ in range(30):
            d = e.decide(sig(principal="victim", args={"path": "same"}), now=t)
            t += 0.2
        self.assertEqual(d.decision, "block")
        for i in range(20):
            e.decide(sig(principal=f"noise{i}", conv="c", args={"path": str(i)}), now=t)
            t += 0.1
        self.assertGreater(e.evicted, 0)
        d2 = e.decide(sig(principal="victim", run="r2", conv="c2", args={"path": "brand-new"}), now=t)
        self.assertEqual(d2.decision, "block", f"쿨다운 중인데 축출로 풀렸다 (evicted={e.evicted})")

    def test_budget_boundary_rounds_to_8_places(self):
        # engine_test.go TestBudgetRoundsLikePython 과 같은 케이스
        d = DecisionEngine(policy(budget_usd_hard=1.0)).decide(sig(principal="b", cost=0.999999999), now=1000.0)
        self.assertEqual(d.decision, "block")

    def test_no_run_id_does_not_evaluate_run_steps(self):
        # run 헤더가 없는 에이전트 — 주체 호출 수가 run_steps 임계를 넘어도 run_steps 로 트립하지 않는다
        e = DecisionEngine(policy(run_steps_hard=5, principal_calls_hard=0, rotation_gap_hard=0))
        for i in range(8):
            d = e.decide({**sig(args={"path": str(i)}), "run_id": None}, now=1000.0 + i)
        self.assertFalse(d.would_block)
        self.assertNotIn("run_steps", d.observations)

    def test_cooldown_keeps_accumulating_observations(self):
        """쿨다운 중에도 나간 호출은 계속 쌓인다 — 임계가 낮은 축 하나가 나머지 축의 눈을 가리면 안 된다.

        Observe 라 호출이 나간다. Enforce 처럼 막힌 호출은 쌓지 않는다 (test_retrying_during_cooldown_does_not_extend_block).
        """
        e = DecisionEngine(policy(OBSERVE, budget_usd_hard=0.001))
        t = 1000.0
        d = e.decide(sig(principal="b", cost=0.5), now=t)          # 예산이 먼저 걸린다
        self.assertTrue(d.would_block)
        for i in range(5):
            d = e.decide(sig(principal="b", args={"path": str(i)}), now=t + 1 + i)
        self.assertEqual([h["axis"] for h in d.policy_hit], ["cooldown"])
        self.assertEqual(d.observations["principal_calls"], 6, "쿨다운 중 누적이 멈췄다")

    def test_cooldown_holds_without_reevaluating(self):
        """서킷 브레이커식 히스테리시스 — 걸린 뒤 윈도우가 미끄러져도 플래핑하지 않는다."""
        e = DecisionEngine(policy())
        t = 1000.0
        for _ in range(30):
            d = e.decide(sig(principal="cd", args={"path": "same"}), now=t)
            t += 0.2
        self.assertEqual(d.decision, "block")
        d2 = e.decide(sig(principal="cd", args={"path": "brand-new"}), now=t + 1)
        self.assertEqual(d2.decision, "block", "쿨다운 중에는 유지돼야 한다")
        d3 = e.decide(sig(principal="cd", args={"path": "brand-new"}), now=t + 3600)
        self.assertEqual(d3.decision, "allow", "쿨다운이 끝나면 풀려야 한다")

    def test_growing_conversation_does_not_defeat_fingerprint(self):
        """폭주 대화는 스텝마다 자란다. 대화 전체를 해싱하면 매번 달라져 영영 못 잡는다.
        도구 지문은 그 성장에 오염되지 않아야 한다."""
        e = DecisionEngine(policy())
        t = 1000.0
        for i in range(1, 31):
            d = e.decide({**sig(principal="g", args={"path": "config.yaml"}),
                          "msg_count": i * 3, "msg_bytes": i * 400}, now=t)
            t += 0.2
        self.assertEqual(d.decision, "block", "대화가 자라도 도구 반복은 잡혀야 한다")

    def test_no_tool_call_yields_no_duplicate_axis(self):
        e = DecisionEngine(policy())
        d = e.decide(sig(tool=None, args=None), now=1000.0)
        self.assertNotIn("duplicate_calls", d.observations)

    def test_principal_state_is_bounded(self):
        """principal 이 무한히 늘면 게이트웨이가 죽는다 — LRU 상한이 있어야 한다."""
        e = DecisionEngine(policy(max_principals=50))
        for i in range(500):
            e.decide(sig(principal=f"p{i}"), now=1000.0 + i)
        self.assertLessEqual(len(e._principals), 50)
        self.assertGreater(e.evicted, 0, "축출이 기록돼야 한다")

    def test_decision_id_is_stable_and_present_only_when_tripped(self):
        e = DecisionEngine(policy())
        d = e.decide(sig(args={"path": "one"}), now=1000.0)
        self.assertIsNone(d.decision_id, "안 걸렸으면 판정 식별자가 없다")

    def test_single_run_has_no_rotation_signal(self):
        """run 이 하나뿐이면 격차는 정의상 1.0 — 신호로 쓰면 안 된다."""
        e = DecisionEngine(policy())
        t = 1000.0
        for _ in range(20):
            d = e.decide(sig(principal="one", run="only", args={"path": f"{t}"}), now=t)
            t += 0.5
        self.assertNotIn("rotation_gap", d.observations)




class PolicyFile(unittest.TestCase):
    def test_policy_file_carries_budget_soft_and_hard(self):
        # 컨트롤러가 렌더한 policy.json 의 budget_usd_soft/hard 가 그대로 엔진 정책이 된다 (STP-14).
        import tempfile
        doc = {"control_level": "Observe", "axes": {}, "budget_usd_soft": "0.5", "budget_usd_hard": "1",
               "source_policies": ["argus-system/p"]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(doc, f)
        p = load_policy_file(f.name)
        self.assertEqual((p.budget_usd_soft, p.budget_usd_hard), (0.5, 1.0))
        self.assertEqual(p.source, "crd:argus-system/p")



# Go 판본 internal/decide/reason_format_test.go 의 reasonFormatCases 와 **같은 표**다.
# parity 테스트는 reason 을 안 보므로, 문장이 갈라지는 건 이 두 표만 막는다.
REASON_FORMAT_CASES = [
    (20, "20"), (0, "0"), (96.98, "96.98"), (130.2, "130.2"),
    (0.0023826, "0.0023826"),  # 센트 미만 예산 — 예전 Go %.2f 는 "0" 으로 뭉갰다
    (25.806577444076538, "25.8066"), (400, "400"), (1e-7, "1e-07"),
]


class ReasonText(unittest.TestCase):
    def test_number_format_matches_go(self):
        for v, want in REASON_FORMAT_CASES:
            self.assertEqual(f"{float(v):.6g}", want, v)

    def test_reason_uses_greater_or_equal(self):
        """판정은 observed >= threshold. 문장이 '>' 면 '20>20' — 틀린 식으로 읽힌다."""
        e = DecisionEngine(policy(run_steps_soft=0, run_steps_hard=3,
                                  principal_calls_soft=0, principal_calls_hard=0,
                                  repeat_soft=0, repeat_hard=0))
        d = None
        for i in range(3):
            d = e.decide(sig(tool=None, args={}), now=1000.0 + i)
        self.assertEqual(d.reason, "run_steps=3>=3")

    def test_fractional_reason_matches_go(self):
        """정수가 아닌 값이 엔진 reason 을 실제로 통과하는지 — 위 표는 f-string 만 본다.
        Go reason_format_test.go TestReasonFractionalBudget 과 같은 기대 문장.
        0.123456789 는 날것·.3g·.6g 가 전부 다르게 찍힌다 (엔진이 round(,8) 해도 살아남는다)."""
        e = DecisionEngine(policy(budget_usd_hard=0.1))
        d = e.decide(sig(tool=None, args={}, cost=0.123456789), now=1000.0)
        self.assertEqual(d.reason, "budget_usd=0.123457>=0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
