"""governance_report — 범주 집계, 옛 레코드(categories 없음) 재구성, 팀·주체 합계."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analyze"))
import governance_report as gr  # noqa: E402

ROWS = [
    {"ts": "t1", "team": "A", "principal": "p1", "cost_usd": 0.5, "categories": []},
    {"ts": "t2", "team": "A", "principal": "p1", "cost_usd": 0.25, "would_block": True, "categories": ["behavior"]},
    {"ts": "t3", "team": "A", "principal": "p1", "status": "blocked", "would_block": True, "categories": ["volume"]},
    # 옛 레코드 — categories 필드가 없다. policy_hit·client_identity 로 재구성해야 한다
    {"ts": "t4", "team": "B", "principal": "p2", "cost_usd": 1.0,
     "policy_hit": [{"axis": "budget_usd", "level": "hard"}, {"axis": "cooldown", "level": "hard"}],
     "client_identity": {"claimed_conflicts_with_resolved": True}},
]


class Report(unittest.TestCase):
    def test_legacy_record_categories_rebuilt(self):
        self.assertEqual(gr.categories(ROWS[3]), ["cost", "identity"])

    def test_team_and_principal_totals(self):
        s = gr.summarize(ROWS)
        a = s["teams"]["A"]
        self.assertEqual((a["calls"], round(a["cost_usd"], 4), a["would_block"], a["blocked"]), (3, 0.75, 2, 1))
        self.assertEqual(dict(a["anomaly"]), {"behavior": 1, "volume": 1})
        self.assertEqual(dict(s["principals"]["p2"]["anomaly"]), {"cost": 1, "identity": 1})
        self.assertEqual(s["total"]["calls"], 4)
        self.assertEqual(s["range"], ["t1", "t4"])


if __name__ == "__main__":
    unittest.main()
