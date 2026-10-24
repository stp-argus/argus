# SPDX-License-Identifier: Apache-2.0
"""업스트림 실패 레코드 — 실패 이벤트로 error 표시, 유휴 스위퍼가 내린다, 재시도 성공이면 성공이 이긴다.

    python3 -m unittest discover -s hack/guardrail -p 'test_*.py'
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_argus_fail_closed  # noqa: E402,F401 — litellm·fastapi 스텁 + 임시 RECORDS_PATH
import argus_guardrail as ag  # noqa: E402
import argus_signals as sg  # noqa: E402


def _lines() -> list[dict]:
    p = Path(sg.RECORDS_PATH)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


class FailureEvent(unittest.TestCase):
    def setUp(self):
        Path(sg.RECORDS_PATH).write_text("")
        sg._pending.clear()
        self.g = object.__new__(ag.ArgusObserve)

    def _pend(self, cid):
        sg._pending[cid] = (time.time() + 60, {"status": "observed", "principal": "p"})

    def test_failure_is_written_by_sweep_after_grace(self):
        self._pend("c1")
        asyncio.run(self.g.async_log_failure_event(
            {"litellm_call_id": "c1", "exception": RuntimeError("upstream 502")}, None, None, None))
        sg._sweep(time.time())                       # 유예 안 — 아직 안 쓴다
        self.assertEqual(_lines(), [])
        sg._sweep(time.time() + ag.FAILURE_GRACE_S + 0.1)
        (r,) = _lines()
        self.assertEqual((r["status"], r["usage_source"]), ("error", "failure_event"))
        self.assertIn("upstream 502", r["error"])

    def test_retry_success_wins_over_earlier_failure(self):
        self._pend("c2")
        asyncio.run(self.g.async_log_failure_event(
            {"litellm_call_id": "c2", "exception": RuntimeError("first attempt")}, None, None, None))
        asyncio.run(self.g.async_log_success_event({"litellm_call_id": "c2"}, {"usage": {}}, None, None))
        sg._sweep(time.time() + 3600)
        (r,) = _lines()
        self.assertEqual((r["status"], r["usage_source"]), ("observed", "success_event"))
        self.assertIn("first attempt", r["retried_after_error"])

    def test_blocked_call_is_not_rewritten(self):
        # 차단은 _flush_blocked 가 이미 pending 에서 뺐다 — 실패 이벤트가 와도 아무것도 안 쓴다
        asyncio.run(self.g.async_log_failure_event({"litellm_call_id": "gone"}, None, None, None))
        sg._sweep(time.time() + 3600)
        self.assertEqual(_lines(), [])

    def test_sweeper_thread_flushes_without_a_new_request(self):
        sg._pending["c3"] = (0.0, {"status": "observed", "principal": "p"})   # 이미 마감 지남
        sg.SWEEP_EVERY_S = 0.05
        sg._sweeper_started = False
        sg._start_sweeper()
        for _ in range(40):
            if _lines():
                break
            time.sleep(0.05)
        self.assertEqual(_lines()[0]["usage_source"], "missing")


if __name__ == "__main__":
    unittest.main()
