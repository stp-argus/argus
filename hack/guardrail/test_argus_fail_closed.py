# SPDX-License-Identifier: Apache-2.0
"""판정 불능 시 backstop.failureMode (STP-40) — Closed + Enforce 만 막고 나머지는 fail-open.

    python3 -m unittest discover -s hack/guardrail -p 'test_*.py'

litellm·fastapi 없이 돌도록 두 모듈을 최소 스텁으로 꽂고 가드레일 훅을 그대로 부른다.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["ARGUS_RECORDS_PATH"] = os.path.join(tempfile.mkdtemp(), "records.jsonl")
os.environ.pop("ARGUS_DECIDE_URL", None)


class _HTTPException(Exception):
    def __init__(self, status_code, detail=None, headers=None):
        super().__init__(status_code)
        self.status_code, self.detail, self.headers = status_code, detail, headers


def _stub(name: str, **attrs) -> None:
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules.setdefault(name, m)


for pkg in ("litellm", "litellm.caching", "litellm.integrations", "litellm.proxy"):
    _stub(pkg)
_stub("litellm.caching.caching", DualCache=object)
_stub("litellm.integrations.custom_guardrail", CustomGuardrail=object)
_stub("litellm.proxy._types", UserAPIKeyAuth=object)
_stub("fastapi", HTTPException=_HTTPException)

import argus_guardrail as ag  # noqa: E402
from argus_decide import Policy  # noqa: E402


class _BrokenEngine:
    def __init__(self, policy):
        self.policy = policy

    def decide(self, _record):
        raise RuntimeError("engine down")


def _run(policy):
    g = object.__new__(ag.ArgusObserve)
    g._engine = _BrokenEngine(policy)
    g._record_request = lambda *_: {"principal": "p", "run_id": "r"}
    return asyncio.run(g.async_pre_call_hook(None, None, {"litellm_call_id": "c1"}, "completion"))


class FailureMode(unittest.TestCase):
    def test_closed_enforce_blocks(self):
        with self.assertRaises(_HTTPException) as cm:
            _run(Policy(control_level="Enforce", failure_mode="Closed", source="crd:test"))
        self.assertTrue(cm.exception.headers["x-argus-decision-id"].startswith("fc-"))

    def test_closed_guard_blocks(self):
        with self.assertRaises(_HTTPException):
            _run(Policy(control_level="Guard", failure_mode="Closed", source="crd:test"))

    def test_open_or_observe_passes(self):
        for p in (Policy(control_level="Enforce", failure_mode="Open", source="crd:test"),
                  Policy(control_level="Observe", failure_mode="Closed", source="crd:test")):
            self.assertEqual(_run(p), {"litellm_call_id": "c1"}, p)

    def test_remote_outage_falls_back_within_grace_then_blocks(self):
        from argus_decide import DecisionEngine
        g = object.__new__(ag.ArgusObserve)
        g._engine = DecisionEngine(Policy(control_level="Enforce", failure_mode="Closed", source="crd:test"))
        g._decide_remote = staticmethod(lambda *_: (_ for _ in ()).throw(OSError("refused")))
        os.environ["ARGUS_DECIDE_URL"] = "http://argus.invalid/v1/decide"
        ag.ArgusObserve._remote_down_since = None
        try:
            rec = {"principal": "p", "run_id": "r"}
            asyncio.run(g._decide(rec))
            self.assertEqual(rec["decide_source"], "local")          # 유예 안 — 로컬 폴백
            ag.ArgusObserve._remote_down_since -= ag.FAIL_CLOSED_GRACE_S
            rec = {"principal": "p", "run_id": "r"}
            d = asyncio.run(g._decide(rec))
            self.assertEqual((rec["decide_source"], d.decision), ("fail_closed", "block"))
            g._engine = DecisionEngine(Policy(control_level="Enforce", failure_mode="Open", source="crd:test"))
            rec = {"principal": "p", "run_id": "r"}
            asyncio.run(g._decide(rec))
            self.assertEqual(rec["decide_source"], "local")          # Open 은 유예가 지나도 폴백
        finally:
            os.environ.pop("ARGUS_DECIDE_URL")
            ag.ArgusObserve._remote_down_since = None

    def test_learns_failure_mode_from_operator_without_policy_file(self):
        # 다른 네임스페이스의 LiteLLM — 정책 파일이 없어 로컬 정책은 env 기본(Observe/Open)이다.
        # 오퍼레이터가 살아 있을 때 받은 failure_mode=Closed·Enforce 를 기억했다가, 사라지면 막는다.
        from argus_decide import DecisionEngine
        g = object.__new__(ag.ArgusObserve)
        g._engine = DecisionEngine(Policy())                     # source="env"
        answers = [{"decision": "allow", "control_level": "Enforce", "failure_mode": "Closed"}]
        def remote(*_):
            if answers:
                return answers.pop()
            raise OSError("operator gone")
        g._decide_remote = staticmethod(remote)
        os.environ["ARGUS_DECIDE_URL"] = "http://argus.invalid/v1/decide"
        ag.ArgusObserve._remote_down_since = None
        ag.ArgusObserve._learned_fail = ("Open", None)
        try:
            rec = {"principal": "p", "run_id": "r"}
            asyncio.run(g._decide(rec))
            self.assertEqual(rec["decide_source"], "operator")
            rec = {"principal": "p", "run_id": "r"}
            asyncio.run(g._decide(rec))                           # 유예 안 — 로컬
            ag.ArgusObserve._remote_down_since -= ag.FAIL_CLOSED_GRACE_S
            rec = {"principal": "p", "run_id": "r"}
            d = asyncio.run(g._decide(rec))
            self.assertEqual((rec["decide_source"], d.decision), ("fail_closed", "block"))
        finally:
            os.environ.pop("ARGUS_DECIDE_URL")
            ag.ArgusObserve._remote_down_since = None
            ag.ArgusObserve._learned_fail = ("Open", None)

    def test_learns_identity_from_operator_without_policy_file(self):
        from argus_decide import DecisionEngine
        g = object.__new__(ag.ArgusObserve)
        g._engine = DecisionEngine(Policy())                     # 정책 파일 없음
        ag.ArgusObserve._learned_identity = None
        self.assertEqual(g._identity_policy().conversation_headers, ())
        g._decide_remote = staticmethod(lambda *_: {"decision": "allow", "control_level": "Observe",
                                                    "identity": {"conversation_headers": ["x-agent-session"],
                                                                 "trust_gateway_session": False,
                                                                 "on_unknown_conversation": "Derive"}})
        os.environ["ARGUS_DECIDE_URL"] = "http://argus.invalid/v1/decide"
        try:
            asyncio.run(g._decide({"principal": "p"}))
            ip = g._identity_policy()
            self.assertEqual((ip.conversation_headers, ip.trust_gateway_session, ip.on_unknown_conversation),
                             (("x-agent-session",), False, "Derive"))
            g._engine = DecisionEngine(Policy(conversation_headers=("x-file",), source="crd:test"))
            self.assertEqual(g._identity_policy().conversation_headers, ("x-file",))   # 파일이 있으면 파일이 이긴다
        finally:
            os.environ.pop("ARGUS_DECIDE_URL")
            ag.ArgusObserve._learned_identity = None

    def test_policy_file_carries_failure_mode(self):
        import json
        import argus_decide as ad
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"control_level": "Enforce", "backstop_failure_mode": "Closed"}, f)
        os.environ["ARGUS_POLICY_FILE"] = f.name
        try:
            self.assertEqual(ad.load_policy_file().failure_mode, "Closed")
        finally:
            os.environ.pop("ARGUS_POLICY_FILE")


class DecideAuth(unittest.TestCase):
    """오퍼레이터가 TokenReview 로 확인할 SA 토큰을 원격 판정 요청에 싣는다."""

    def _serve(self):
        import http.server
        import json
        import threading
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen["auth"] = self.headers.get("Authorization")
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({"decision": "allow"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}/v1/decide", seen

    def test_sends_current_token_from_file(self):
        url, seen = self._serve()
        tok = os.path.join(tempfile.mkdtemp(), "token")
        os.environ["ARGUS_DECIDE_TOKEN_FILE"] = tok
        self.addCleanup(os.environ.pop, "ARGUS_DECIDE_TOKEN_FILE")
        for value in ("first", "rotated"):
            with open(tok, "w", encoding="utf-8") as f:
                f.write(value + "\n")
            ag.ArgusObserve._decide_remote(url, {"principal": "p"})
            self.assertEqual(seen["auth"], f"Bearer {value}", "kubelet 이 회전한 토큰을 그대로 따라가야 한다")

    def test_missing_token_file_names_the_cause(self):
        url, _ = self._serve()
        os.environ["ARGUS_DECIDE_TOKEN_FILE"] = os.path.join(tempfile.mkdtemp(), "absent")
        self.addCleanup(os.environ.pop, "ARGUS_DECIDE_TOKEN_FILE")
        with self.assertRaisesRegex(RuntimeError, "토큰 파일"):
            ag.ArgusObserve._decide_remote(url, {"principal": "p"})

    def test_no_token_file_no_header(self):
        url, seen = self._serve()
        os.environ.pop("ARGUS_DECIDE_TOKEN_FILE", None)
        ag.ArgusObserve._decide_remote(url, {"principal": "p"})
        self.assertIsNone(seen["auth"])


if __name__ == "__main__":
    unittest.main()
