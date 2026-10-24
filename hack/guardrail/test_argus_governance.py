"""거버넌스 신호 — 범주 표(Go 와 동일)·알림 트리거·중복 억제·신원 노출 단계.

    python3 -m unittest discover -s hack/guardrail -p 'test_*.py'
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import argus_signals as g  # noqa: E402
from argus_decide import category, hit_category  # noqa: E402

# Go internal/decide/governance_test.go 의 categoryCases 와 **같은 표**다.
CATEGORY_CASES = {
    "budget_usd": "cost", "duplicate_calls": "behavior", "run_steps": "behavior",
    "conv_growth": "behavior", "msg_bytes": "behavior", "rotation_gap": "behavior",
    "principal_calls": "volume", "team_calls": "volume", "cooldown": "other", "새축": "other",
}


class D:  # 판정 대역
    def __init__(self, hits, decision="allow", would_block=False, level="Observe"):
        self.policy_hit = [dict(h, category=category(h["axis"])) for h in hits]
        self.decision, self.would_block, self.control_level = decision, would_block, level
        self.decision_id, self.policy_source = "d-test", "crd:argus-system/observe-default"


def hit(axis, level, observed=20.0, threshold=20.0):
    return {"axis": axis, "level": level, "observed": observed, "threshold": threshold, "detail": ""}


REC = {"principal": "p1", "principal_source": "key:user_id", "team": "t1", "run_id": "run-01",
       "model": "argus-mock-gpt", "ts": "2026-09-24T00:00:00.000Z"}
IDENT = {"user_id": "alice@example.com", "team_id": "team-research", "key_alias": "agent-a-key",
         "api_key": "sk-SHOULD-NEVER-LEAK"}


class Categories(unittest.TestCase):
    def test_matches_go_table(self):
        for axis, want in CATEGORY_CASES.items():
            self.assertEqual(category(axis), want, axis)

    def test_cooldown_hit_takes_the_tripped_axis_category(self):
        cool = {"axis": "cooldown", "level": "hard", "detail": "principal_calls=19>=19"}
        self.assertEqual(hit_category(cool), "volume")
        self.assertEqual(hit_category(dict(cool, detail="duplicate_calls=20>=20 · run_steps=18>=18")), "behavior")
        self.assertEqual(hit_category(dict(cool, detail="budget_usd=1.2>=1")), "cost")

    def test_cooldown_hit_without_known_reason_stays_other(self):
        for detail in ("", None, "failureMode=Closed", "새축=3>=2"):
            self.assertEqual(hit_category({"axis": "cooldown", "detail": detail}), "other", detail)

    def test_non_cooldown_hit_uses_its_own_axis(self):
        self.assertEqual(hit_category({"axis": "run_steps", "detail": "principal_calls=19>=19"}), "behavior")


class AlertEvents(unittest.TestCase):
    def ev(self, d, rec=REC):
        return [(e["event"], e["category"], e["severity"]) for e in g._alert_events(rec, d)]

    def test_observe_trip_is_detected_not_blocked(self):
        d = D([hit("principal_calls", "hard"), hit("duplicate_calls", "soft")], "allow", True)
        self.assertEqual(self.ev(d), [("runaway_detected", "volume", "critical")])

    def test_enforce_trip_is_blocked(self):
        d = D([hit("run_steps", "hard")], "block", True, "Enforce")
        self.assertEqual(self.ev(d), [("runaway_blocked", "behavior", "critical")])

    def test_cooldown_is_silent(self):
        """같은 폭주를 쿨다운(30초)마다 다시 알리면 채널이 죽는다."""
        d = D([hit("cooldown", "hard", 25, 0)], "block", True, "Enforce")
        self.assertIn("other", g.ALERT_CATEGORIES)  # 범주 필터가 가려주지 않는 상태에서 시험한다
        self.assertEqual(self.ev(d), [])

    def test_unmapped_axis_trip_still_alerts(self):
        """새 축을 추가하고 범주 표를 잊어도 트립은 알린다 (other)."""
        d = D([hit("brand_new_axis", "hard")], "allow", True)
        self.assertEqual(self.ev(d), [("runaway_detected", "other", "critical")])

    def test_soft_cost_warns_but_soft_behavior_does_not(self):
        self.assertEqual(self.ev(D([hit("budget_usd", "soft", 0.8, 0.75)])), [("cost_warning", "cost", "warning")])
        self.assertEqual(self.ev(D([hit("duplicate_calls", "soft", 6, 5)])), [])

    def test_spoof_attempt_alerts_identity(self):
        rec = dict(REC, client_identity={"claimed_conflicts_with_resolved": True})
        self.assertEqual(self.ev(D([]), rec), [("identity_spoof_attempt", "identity", "warning")])

    def test_soft_escalation_trip_alerts(self):
        """soft 2개로 트립(soft_escalation) — hard 가 없어도 트립이다. cost 를 warning 으로 부르면 안 된다."""
        d = D([hit("budget_usd", "soft", 0.8, 0.7), hit("run_steps", "soft", 10, 9)], "block", True, "Enforce")
        self.assertEqual(sorted(self.ev(d)), [("runaway_blocked", "behavior", "critical"),
                                              ("runaway_blocked", "cost", "critical")])

    def test_multiple_categories_one_event_each(self):
        d = D([hit("budget_usd", "hard", 1.2, 1), hit("run_steps", "hard"), hit("principal_calls", "hard")],
              "block", True, "Enforce")
        self.assertEqual(sorted(c for _, c, _ in self.ev(d)), ["behavior", "cost", "volume"])


class Notify(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig = (g.ALERT_URL, g._send_alert, g.ALERT_IDENTITY)
        g.ALERT_URL = "http://receiver.example/hook"
        g._send_alert = lambda url, payload: self.sent.append((url, payload))
        g._alert_last.clear()

    def tearDown(self):
        g.ALERT_URL, g._send_alert, g.ALERT_IDENTITY = self._orig

    def test_no_url_no_alert(self):
        g.ALERT_URL = ""
        self.assertEqual(g._notify(REC, D([hit("run_steps", "hard")], "allow", True), IDENT), [])

    def test_dedup_within_window_then_again_after(self):
        d = D([hit("run_steps", "hard")], "allow", True)
        g._notify(REC, d, IDENT, now=1000)
        g._notify(REC, d, IDENT, now=1000 + g.ALERT_DEDUP_S - 1)
        g._notify(REC, d, IDENT, now=1000 + g.ALERT_DEDUP_S + 1)
        self.assertEqual(len(self.sent), 2)

    def test_identity_modes_and_never_api_key(self):
        d = D([hit("run_steps", "hard")], "allow", True)
        for mode, has_labels, has_user in (("hash", False, False), ("labels", True, False), ("full", True, True)):
            g.ALERT_IDENTITY = mode
            g._alert_last.clear()
            self.sent.clear()
            g._notify(REC, d, IDENT, now=1)
            p = self.sent[0][1]
            self.assertEqual("key_alias" in p, has_labels, mode)
            self.assertEqual("user_id" in p, has_user, mode)
            self.assertNotIn("sk-SHOULD-NEVER-LEAK", str(p), mode)
            self.assertEqual(p["principal"], "p1")

    def test_hash_mode_message_has_no_labels(self):
        """hash 모드면 Slack 본문(event_message)에도 key_alias·team_id·user_id 가 없어야 한다."""
        g.ALERT_IDENTITY = "hash"
        d = D([hit("run_steps", "hard")], "allow", True)
        p = g._notify(REC, d, IDENT, now=1)[0]
        for v in ("agent-a-key", "team-research", "alice@example.com"):
            self.assertNotIn(v, p["event_message"])
        self.assertIn("p1", p["event_message"])

    def test_cost_is_sticky_deduped_for_a_day(self):
        """누적 지출은 줄지 않아 쿨다운마다 재트립한다 — 비용 알림은 하루 창."""
        g.ALERT_IDENTITY = "labels"
        d = D([hit("budget_usd", "hard", 1.5, 1)], "allow", True)
        for t in (0, 301, 3600, 80000):
            g._notify(REC, d, IDENT, now=1000 + t)
        self.assertEqual(len(self.sent), 1)
        g._notify(REC, d, IDENT, now=1000 + g.ALERT_DEDUP_STICKY_S + 1)
        self.assertEqual(len(self.sent), 2)

    def test_payload_shape_and_message(self):
        g.ALERT_IDENTITY = "labels"
        d = D([hit("principal_calls", "hard", 19, 19)], "block", True, "Enforce")
        p = g._notify(REC, d, IDENT, now=1)[0]
        for k in ("event", "event_group", "event_message", "category", "severity", "control_level", "decision_id"):
            self.assertIn(k, p)
        self.assertEqual(p["event_group"], "argus")
        self.assertIn("이상 사용량", p["event_message"])
        self.assertIn("principal_calls=19>=19", p["event_message"])
        self.assertIn("agent-a-key", p["event_message"])


class SlackBody(unittest.TestCase):
    def test_slack_url_gets_text_only(self):
        import urllib.request
        captured = {}
        orig = urllib.request.urlopen

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=None):
            captured["body"] = req.data
            return R()
        urllib.request.urlopen = fake
        try:
            g._send_alert("https://hooks.slack.com/services/X", {"event_message": "hi", "x": 1})
            import time
            for _ in range(50):
                if "body" in captured:
                    break
                time.sleep(0.01)
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(captured["body"], b'{"text": "hi"}')


if __name__ == "__main__":
    unittest.main()
