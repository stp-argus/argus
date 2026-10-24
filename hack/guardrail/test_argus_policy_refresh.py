# SPDX-License-Identifier: Apache-2.0
"""원격 판정이 정상인 동안에도 CR 의 identity 블록 변경이 가드레일에 반영된다.

    python3 -m unittest discover -s hack/guardrail -p 'test_*.py'
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from argus_decide import DecisionEngine  # noqa: E402


def _write(path: str, headers: list[str], mtime: float) -> None:
    with open(path, "w") as f:
        json.dump({"control_level": "Observe", "identity": {"conversation_headers": headers}}, f)
    os.utime(path, (mtime, mtime))


class RefreshWithoutDecide(unittest.TestCase):
    def test_identity_change_is_picked_up_without_decide(self):
        path = os.path.join(tempfile.mkdtemp(), "policy.json")
        os.environ["ARGUS_POLICY_FILE"] = path
        try:
            _write(path, ["x-a"], 1000)
            e = DecisionEngine()
            self.assertEqual(e.policy.conversation_headers, ("x-a",))
            _write(path, ["x-b"], 2000)
            e._next_reload = 0          # 5초 간격 대기를 건너뛴다
            e.refresh_policy()          # decide() 는 한 번도 부르지 않는다
            self.assertEqual(e.policy.conversation_headers, ("x-b",))
        finally:
            os.environ.pop("ARGUS_POLICY_FILE")


class GuardrailCallsRefresh(unittest.TestCase):
    def test_record_path_refreshes_before_reading_identity(self):
        src = (Path(__file__).parent / "argus_guardrail.py").read_text()   # litellm 없이 본다
        self.assertLess(src.index("self._engine.refresh_policy()"),
                        src.index("ident = self._identity_policy()"))


if __name__ == "__main__":
    unittest.main()
