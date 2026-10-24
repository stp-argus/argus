#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""인자가 **변하는** 폭주 — 지금 우리 탐지가 못 잡는 트래픽.

1차 검증 피드백:

    "실제 폭주는 매번 조금씩 다른 요청으로 나타납니다. 시각이나 시도 횟수, 직전 실패
     메시지 같은 값이 섞여 들어가기 때문입니다. **완전히 동일한 요청만 잡는 방식으로는
     거의 걸러내지 못합니다.**"

샘플앱 시나리오는 도구 인자가 `{path: "config.yaml"}` 로 **고정**이라 이 경우를
한 번도 만들어내지 못했다. 그래서 우리 `duplicate_calls` 축이 98% 탐지로 보였다.

여기서 만드는 변형 넷 + **혼동군 하나**가 이 문제의 전부다:

    A. attempt 증가        {path: 고정,  attempt: 1,2,3...}
    B. 오류 메시지 변동     {path: 고정,  last_error: "...(1)","...(2)"}
    C. 타임스탬프 변동      {path: 고정,  ts: ISO8601}
    D. 셋 다 섞임          위 전부

    N. **혼동군** — 정상인데 인자가 변한다  {path: 매번 다른 파일}
       ← 이게 없으면 "변동 키는 무시" 라는 규칙이 정상 작업을 뭉개는 걸 못 본다.
         정규화의 진짜 어려움은 A~D 를 잡는 게 아니라 **N 을 안 잡는 것**이다.

판정을 바꾸지 않는다. 트래픽만 만든다. 무엇을 정규화해야 하는지는 이 데이터를 보고 정한다.
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
STEPS = int(os.getenv("STEPS", "22"))
GAP_S = float(os.getenv("GAP_S", "0.4"))

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file at the given path.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    },
}]

FILES = ["config/a.yaml", "src/b.ts", "docs/c.md", "deploy/d.yaml", "test/e.test.ts",
         "ci/f.yaml", "src/g.ts", "docs/h.md", "config/i.yaml", "src/j.ts"]


def variants(step: int) -> dict[str, dict]:
    """코호트 이름 → 그 스텝의 도구 인자."""
    return {
        "vary-attempt": {"path": "config.yaml", "attempt": step},
        "vary-error":   {"path": "config.yaml", "last_error": f"permission denied ({step})"},
        "vary-ts":      {"path": "config.yaml", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
        "vary-all":     {"path": "config.yaml", "attempt": step,
                         "last_error": f"denied ({step})",
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
        # 혼동군: 인자가 변하지만 **정상**이다. 매번 다른 파일을 읽는다.
        "normal-vary":  {"path": FILES[(step - 1) % len(FILES)]},
    }


def call(cohort: str, step: int, args: dict) -> str:
    """대화 이력을 스크립트로 만들어 보낸다 — 샘플앱 러너와 같은 방식."""
    msgs: list = [{"role": "system", "content": "You are a code assistant. Answer briefly."}]
    for i in range(1, step + 1):
        msgs.append({"role": "user", "content": f"Step {i}: read the file."})
        prev = variants(i)[cohort]
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps(prev)}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": "(contents)" if cohort == "normal-vary"
                                else f"Error: permission denied (attempt {i})"})

    body = json.dumps({"model": MODEL, "messages": msgs, "tools": TOOLS}).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body, headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
        "User-Agent": "argus-varying-runaway/1.0",
        "X-Principal-Id": f"vary-{cohort}", "X-Run-Id": f"{cohort}-run-01",
    })
    try:
        urllib.request.urlopen(req, timeout=90).read()
        return "ok"
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        return "BLOCKED" if "decision_id" in raw else f"http{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:30]


def main() -> int:
    cohorts = list(variants(1))
    print(f"인자 변동 폭주 4종 + 혼동군 1종 · 각 {STEPS}스텝 · 모델 {MODEL}\n")
    for c in cohorts:
        tag = "정상(혼동군)" if c.startswith("normal") else "폭주"
        blocked_at = None
        for step in range(1, STEPS + 1):
            r = call(c, step, variants(step)[c])
            if r == "BLOCKED" and blocked_at is None:
                blocked_at = step
            time.sleep(GAP_S)
        mark = f"\033[31m{blocked_at}콜에서 차단\033[0m" if blocked_at else "차단 없음"
        good = (blocked_at is None) if c.startswith("normal") else (blocked_at is not None)
        print(f"  {'✅' if good else '❌'} {c:<14} [{tag}]  {mark}")
    print("\n  정상(혼동군)이 차단되면 오탐이다. 폭주가 안 잡히면 미탐이다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
