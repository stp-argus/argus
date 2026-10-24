#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""정상 에이전트 — 데모의 1막이자 임계값 도출의 대조군.

**폭주만 측정하면 모든 숫자가 폭주처럼 보인다.** 정상이 얼마인지 모르는 채 정한 임계는
다시 임의값이다 (1차 검증 지적). 그래서 이 생성기가 있다.

정상의 정의는 추상적으로 세우지 않았다 — **폭주의 각 특징을 하나씩 뒤집었다**:

  | | 정상 (여기)              | 폭주 (샘플앱 시나리오)   |
  |-|--------------------------|--------------------------|
  | principal | 여러 명이 각자      | 하나가 run 을 회전       |
  | run 길이  | 3~5 스텝에 종료     | 하드스톱까지             |
  | 도구 인자 | 매 스텝 다름        | 같은 인자 반복           |
  | 도구 결과 | 성공                | 실패 반복                |

대화 이력은 스크립트가 만든다. 샘플앱 러너도 assistant 턴을 스크립트하고,
모델의 실제 답변 내용은 기록 **형태**에 영향이 없으면서 비용만 늘리기 때문이다.

⚠️ 호출 간격(GAP_S)은 **설계값이지 측정값이 아니다.** 이 값에서 도출되는 축
(principal_calls, rotation_gap)의 임계는 부분적으로 설계다 — 근거표에 그렇게 표기했다.

    python3 hack/demo/normal_agent.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("LITELLM_BASE", os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:4000"))
KEY = os.getenv("LITELLM_MASTER_KEY") or os.getenv("LLM_API_KEY") or "sk-argus-lab-1234"
MODEL = os.getenv("LLM_MODEL", "argus-real-gpt")
GAP_S = float(os.getenv("GAP_S", "2.5"))

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file at the given path.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    },
}]

# run 별 (principal, 작업 지시, 스텝마다 건드리는 서로 다른 파일들)
WORK = [
    ("normal-p01", "Summarize the service's retry configuration.",
     ["config/retry.yaml", "config/timeouts.yaml", "docs/retry.md"]),
    ("normal-p01", "Check which ports the gateway exposes.",
     ["deploy/gateway.yaml", "deploy/service.yaml", "README.md", "docs/ports.md"]),
    ("normal-p02", "Find where the database URL is configured.",
     ["config/db.yaml", "src/db/client.ts", ".env.example", "docs/db.md", "migrations/001.sql"]),
    ("normal-p03", "List the CI steps that run on pull requests.",
     ["ci/pr.yaml", "ci/shared.yaml", "Makefile"]),
    ("normal-p03", "Explain the cache eviction policy.",
     ["src/cache/policy.ts", "src/cache/index.ts", "docs/cache.md", "test/cache.test.ts"]),
    ("normal-p04", "Locate the health check endpoint.",
     ["src/server/health.ts", "src/server/routes.ts", "deploy/probe.yaml"]),
    ("normal-p04", "Report the log retention window.",
     ["config/logging.yaml", "docs/observability.md", "deploy/fluentbit.yaml", "README.md"]),
    ("normal-p02", "Describe how secrets are injected.",
     ["deploy/secrets.yaml", "docs/secrets.md", "src/config/env.ts"]),
]


def call(principal: str, run_id: str, messages: list) -> dict | None:
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS,
                       "metadata": {"run_id": run_id}}).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body, headers={
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "User-Agent": "argus-normal-agent/1.0",
        "X-Principal-Id": principal,
        "X-Run-Id": run_id,
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # ⚠️ **429 를 곧바로 "Argus 차단"으로 읽으면 안 된다.**
        # 업스트림(OpenRouter 등)도 429 를 준다. 실제로 이 스크립트가 처음에 그렇게 오인해
        # "오탐 3건" 이라고 잘못 보고했다 — 알고 보니 전부 provider 레이트리밋이었다.
        # 진짜 Argus 차단은 본문에 decision_id 를 실어 보낸다 (block-contract.md).
        raw = exc.read().decode(errors="replace")
        if _is_argus_block(raw):
            print(f"    \033[31m✖ Argus 가 차단했다 (HTTP {exc.code}) — 정상 트래픽이므로 오탐\033[0m",
                  flush=True)
            return "BLOCKED"  # type: ignore[return-value]
        print(f"    \033[33m· 업스트림 오류 HTTP {exc.code} (Argus 아님)\033[0m", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"    실패: {exc}", flush=True)
    return None


def _is_argus_block(raw: str) -> bool:
    """Argus 차단인지 업스트림 오류인지 가른다.

    LiteLLM 이 가드레일 예외를 자기 오류 봉투로 감싸므로 decision_id 가 최상위에 오지 않는다
    (error.provider_specific_fields.decision_id). 탭 구현마다 봉투가 다를 수 있어
    경로를 못 박지 않고 마커의 존재만 본다.
    """
    if "decision_id" not in raw:
        return False
    try:
        return "argus_runaway_blocked" in raw or bool(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return True


def main() -> int:
    calls = cost = 0
    blocked = 0
    started = time.time()
    for i, (principal, task, paths) in enumerate(WORK, 1):
        run_id = f"normal-{i:02d}"
        print(f"  [{i}/{len(WORK)}] {principal} · {run_id} · {len(paths)}스텝 — {task}", flush=True)
        messages: list = [{"role": "system", "content": "You are a code assistant. Answer briefly."},
                          {"role": "user", "content": task}]
        for step, path in enumerate(paths, 1):
            out = call(principal, run_id, messages)
            calls += 1
            if out == "BLOCKED":
                blocked += 1          # **Argus 가 막은 것만** 오탐으로 센다
            elif out:
                cost += (out.get("usage") or {}).get("cost") or 0.0
            # 다음 턴 이력: 어시스턴트가 **다른 파일**을 읽고 **성공**한다.
            # path 가 매번 달라지는 것이 정상 패턴의 핵심 — 재시도 폭풍의 반대다.
            messages = messages + [
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call_{i}_{step}", "type": "function",
                    "function": {"name": "read_file",
                                 "arguments": json.dumps({"path": path})}}]},
                {"role": "tool", "tool_call_id": f"call_{i}_{step}",
                 "content": f"(contents of {path})"},
            ]
            time.sleep(GAP_S)
    ok = "\033[32m차단 0 — 정상\033[0m" if not blocked else f"\033[31m차단 {blocked}건 — 오탐!\033[0m"
    print(f"\n  총 {calls}콜 · {time.time()-started:.0f}초 · provider 실측 ${cost:.6f} · {ok}")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
