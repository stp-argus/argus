# SPDX-License-Identifier: Apache-2.0
"""대조군 — 실모델이 직접 루프를 도는 "정상 에이전트" 4종.

multi_tool_identity.py 와 결정적으로 다른 점: 저긴 메시지를 우리가 조작해 만들었고,
여긴 **모델이 진짜로 도구를 고른다.** 도구 결과만 가짜(정해진 답)를 먹인다.
그래서 스텝 수·도구 선택·인자·대화 리듬이 전부 모델의 것 = 정상 트래픽의 분포다.

목적은 탐지가 아니라 **오탐 0 의 증명**이다 — "폭주를 잡는다"는 어제 했고,
"정상을 안 잡는다"는 이게 처음이다. 정상 에이전트 화면이 조용한 것이 결과다.

    eval "$(bash hack/demo/provision_keys.sh)"        # KEY_A/B/C(+D 는 이 스크립트가 안내)
    python3 hack/demo/realistic_agents.py             # 비용 ≈ $0.005 (max_tokens·스텝 캡)

페르소나:
    code-reviewer  (agent-a·team-research)  read_file / grep / list_dir
    researcher     (agent-b·team-research)  web_search / fetch_url
    sre-oncall     (agent-c·team-research)  kubectl_get / read_logs
    data-analyst   (agent-d·team-data)      run_sql
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("ARGUS_BASE", "http://127.0.0.1:4400")
MODEL = os.environ.get("ARGUS_MODEL", "argus-real-gpt")
MAX_STEPS = 6           # 스텝 캡 = 이 스크립트의 유일한 브레이크 (러너 MAX_CALLS 와 같은 역할)
MAX_TOKENS = 200

# 도구 결과는 가짜지만 모양은 진짜처럼 — 스텝마다 조금씩 달라야 정상 분포다.
def _tool_result(name: str, args: dict, step: int) -> str:
    q = json.dumps(args, ensure_ascii=False)[:80]
    return {
        "read_file":  f"# handlers.py (step{step})\ndef handle(req):\n    total = 0\n    for i in range(len(req.items)):\n        total += req.items[i].price\n    return total  # TODO: tax?",
        "grep":       f"handlers.py:12: total += req.items[i].price\nbilling.py:44: total = apply_tax(total)  (query={q})",
        "list_dir":   "handlers.py  billing.py  models.py  tests/",
        "web_search": f"1. 'OTel GenAI semconv 1.27 발표' (otel.io)\n2. 'LLM 게이트웨이 비교 2026' (infoq)\n3. '에이전트 비용 사고 사례' (blog) — 검색어 {q}",
        "fetch_url":  f"본문(step{step}): OTel GenAI 시맨틱 컨벤션은 gen_ai.* 네임스페이스로 요청·응답·비용 속성을 표준화한다...",
        "kubectl_get": "NAME            READY  STATUS             RESTARTS\npayments-7d9f   0/1    CrashLoopBackOff   14\npayments-db     1/1    Running            0",
        "read_logs":  "OOMKilled: container payments exceeded memory limit 256Mi\nlast gc pause 1.2s, heap 251Mi",
        "run_sql":    f"rows=3 (step{step})\nteam-research | 42 | $0.00025\nteam-data     | 11 | $0.00008",
    }.get(name, f"(모르는 도구 {name})")


PERSONAS = [
    dict(key_env="KEY_A", name="code-reviewer", run="run-review",
         tools=["read_file", "grep", "list_dir"],
         task="handlers.py 의 handle 함수를 리뷰해줘. 파일을 읽고, 관련 코드를 grep 으로 찾아본 뒤, 버그와 개선점을 2줄로 보고해."),
    dict(key_env="KEY_B", name="researcher", run="run-research",
         tools=["web_search", "fetch_url"],
         task="OTel GenAI 시맨틱 컨벤션의 최신 동향을 조사해줘. 검색하고 가장 관련 있는 글 하나를 읽은 뒤 3줄로 요약해."),
    dict(key_env="KEY_C1", name="sre-oncall", run="run-oncall",
         tools=["kubectl_get", "read_logs"],
         task="payments 서비스가 재시작을 반복한다. 파드 상태를 확인하고 로그를 본 뒤 원인과 조치를 2줄로 보고해."),
    dict(key_env="KEY_D", name="data-analyst", run="run-analyst",
         tools=["run_sql"],
         task="팀별 LLM 사용량을 SQL 로 집계해서 어느 팀이 비용을 더 쓰는지 1줄로 답해."),
]


def run_agent(p: dict, key: str, round_no: int) -> tuple[int, bool]:
    tools = [{"type": "function",
              "function": {"name": t, "parameters": {
                  "type": "object", "properties": {"q": {"type": "string"}}}}}
             for t in p["tools"]]
    messages = [{"role": "user", "content": p["task"] + f" (요청 #{round_no})"}]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "X-Run-Id": f"{p['run']}-{round_no}",
               "x-claude-code-session-id": f"sess-{p['name']}-{round_no}"}
    for step in range(1, MAX_STEPS + 1):
        body = {"model": MODEL, "max_tokens": MAX_TOKENS,
                "messages": messages, "tools": tools}
        req = urllib.request.Request(f"{BASE}/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=90) as resp:
            out = json.loads(resp.read())
        msg = out["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if not calls:
            print(f"    {step}스텝에 완료 — {(msg.get('content') or '')[:70]!r}")
            return step, True
        messages.append(msg)
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            print(f"    step{step}: 모델이 {name}({json.dumps(args, ensure_ascii=False)[:50]}) 선택")
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": _tool_result(name, args, step)})
        time.sleep(1.0)   # 도구 실행 시간 흉내 — 대화 리듬을 실물처럼
    print(f"    스텝 캡({MAX_STEPS}) 도달 — 중단")
    return MAX_STEPS, False


def main() -> None:
    missing = [p["key_env"] for p in PERSONAS if not os.environ.get(p["key_env"])]
    if missing:
        sys.exit(f"환경변수 없음: {missing} — provision_keys.sh (+ KEY_D 는 README 부록) 먼저")
    total = 0
    for rnd in (1, 2):
        for p in PERSONAS:
            print(f"[{p['name']}] round {rnd}")
            steps, done = run_agent(p, os.environ[p["key_env"]], rnd)
            total += steps
            time.sleep(1.5)
    print(f"총 {total} 콜. 이제 레코드에서 would_block=0 을 확인하라 — 그게 이 스크립트의 결과다.")


if __name__ == "__main__":
    main()
