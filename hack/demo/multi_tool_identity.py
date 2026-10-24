# SPDX-License-Identifier: Apache-2.0
"""신원 사다리 key 티어 검증 드라이버 — "여러 도구를 불러도 같은 에이전트인가".

2026-08-19 재설계의 핵심 주장을 트래픽으로 증명한다:

  국면 1  에이전트 A(가상 키)가 도구 3종을 번갈아 부른다
          → 레코드의 principal 이 전부 같아야 한다 (도구·인자와 무관)
  국면 2  에이전트 B(다른 가상 키)가 같은 도구들을 부른다
          → A 와 다른 principal 로 갈라져야 한다
  국면 3  A 가 X-Principal-Id 를 위조해 B 행세를 한다
          → key 티어가 header 를 이기므로 여전히 A 다 (위조 불가의 증명)
  국면 4  에이전트 C 가 키 두 개를 번갈아 쓴다 (핑퐁 재귀의 신원 회피 수법)
          → user_id 가 같으므로 principal 누적이 유지되고 principal_calls 가
            임계를 넘는다. Observe 라 would_block 만 기록된다

사용:
    ARGUS_BASE=http://127.0.0.1:4400 \
    KEY_A=sk-... KEY_B=sk-... KEY_C1=sk-... KEY_C2=sk-... \
    python3 hack/demo/multi_tool_identity.py

키 발급은 provision_keys.sh 가 한다. 업스트림은 mock 이라 비용이 없다.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("ARGUS_BASE", "http://127.0.0.1:4400")
TOOLS = ["web_search", "file_read", "code_exec"]


def call(key: str, tool: str, arg: str, run_id: str, session: str,
         extra_headers: dict | None = None) -> int:
    """에이전트 루프 한 스텝 모양의 요청 — 직전 도구 호출이 messages 에 실려 온다."""
    body = {
        "model": "argus-mock-gpt",
        "messages": [
            {"role": "user", "content": f"task for {run_id}"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_demo", "type": "function",
                "function": {"name": tool, "arguments": json.dumps({"q": arg})},
            }]},
            {"role": "tool", "tool_call_id": "call_demo", "content": "result"},
        ],
        "tools": [{"type": "function", "function": {"name": t, "parameters": {}}}
                  for t in TOOLS],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Run-Id": run_id,
        "x-claude-code-session-id": session,
    }
    headers.update(extra_headers or {})
    req = urllib.request.Request(f"{BASE}/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def main() -> None:
    keys = {name: os.environ.get(name) for name in ("KEY_A", "KEY_B", "KEY_C1", "KEY_C2")}
    missing = [k for k, v in keys.items() if not v]
    if missing:
        sys.exit(f"환경변수 없음: {missing} — provision_keys.sh 를 먼저 돌려라")

    print("국면 1 — 에이전트 A, 도구 3종 x 2바퀴 (한 대화)")
    for i in range(6):
        tool = TOOLS[i % 3]
        st = call(keys["KEY_A"], tool, f"query-{i}", "run-a-1", "sess-agent-a")
        print(f"  A {tool:<10} arg=query-{i} -> {st}")
        time.sleep(0.3)

    print("국면 2 — 에이전트 B, 같은 도구들 (다른 주체로 갈라져야 함)")
    for i in range(3):
        tool = TOOLS[i % 3]
        st = call(keys["KEY_B"], tool, f"other-{i}", "run-b-1", "sess-agent-b")
        print(f"  B {tool:<10} arg=other-{i} -> {st}")
        time.sleep(0.3)

    print("국면 3 — 에이전트 A 가 X-Principal-Id 로 B 행세 (key 가 header 를 이겨야 함)")
    st = call(keys["KEY_A"], "web_search", "spoof-attempt", "run-a-2", "sess-agent-a",
              extra_headers={"X-Principal-Id": "agent-b"})
    print(f"  A(위조 헤더) web_search -> {st}")

    print("국면 4 — 에이전트 C, 키 2개 회전 x 28회 (principal_calls hard=26 돌파)")
    blocked = 0
    for i in range(28):
        key = keys["KEY_C1"] if i % 2 == 0 else keys["KEY_C2"]
        st = call(key, TOOLS[i % 3], f"pingpong-{i}", f"run-c-{i}", f"sess-c-{i}")
        if st == 429:
            blocked += 1
        if i % 7 == 6:
            print(f"  C {i + 1}/28 (최근 status={st})")
        time.sleep(0.2)
    print(f"  429 응답 {blocked}건 (Observe 면 0 이 정상 — would_block 은 레코드에만)")
    print("끝. records.jsonl 을 분석하라 — analyze 커맨드는 README 참고.")


if __name__ == "__main__":
    main()
