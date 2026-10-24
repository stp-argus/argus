# SPDX-License-Identifier: Apache-2.0
"""한화 시나리오 재현 — 멈춘 세션이 같은 요청을 재시도하며 돈을 태운다.

레퍼런스: 한화솔루션 LLM Gateway 사례 (클리핑 볼트 · docs/research/2026-08-18_hanwha-*).
한 세션이 12,388턴을 돌며 Haiku 만으로 $79 — 발견은 **한 달 뒤 SpendLogs 사후 분석**,
대응은 기능 비활성화 + 교육이었다. "보는 것 ≠ 막는 것"의 공개 실증.

여기서는 같은 병을 축소 재현한다: 클라이언트 재시도 핸들러가 고장나 **동일 요청**을
같은 세션으로 계속 다시 보낸다. 매 재시도가 실모델 비용이다. duplicate_calls 축이
셌을 때 Observe 는 would_block 만 적고(한화의 현실), Enforce 는 그 자리에서 끊는다.

    python3 hack/demo/stuck_session.py --calls 12          # 1국면: Observe 에서 태우기
    kubectl patch runawaypolicy ... controlLevel=Enforce   # 폭주 도중 선언 전환
    python3 hack/demo/stuck_session.py --calls 8 --cont    # 2국면: 차단 확인 (429+decision_id)

--cont 는 같은 대화를 이어간다 (윈도우 60초 안에 이어 돌려야 카운터가 이어진다).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("ARGUS_BASE", "http://127.0.0.1:4400")

# 매 콜 완전히 동일한 본문 — 고장난 재시도 루프의 서명.
# 도구 호출을 품은 동일 요청 — 에이전트의 재시도 폭풍 모양 (한화 Cowork 도 에이전틱이었다).
# ⚠️ 도구 없는 순수 챗 반복은 duplicate_calls 축의 사각이다 (2026-08-25 실측:
#    이 축은 tool_args 해시를 세므로 tool_calls 가 없으면 평가 자체가 빠진다).
#    그 모양은 principal_calls/run_steps hard 가 백스톱으로 잡아야 하고,
#    챗 전용 중복 축은 MOC "③ prefix 중복 축 재정의" 항목이다.
BODY = {
    "model": "argus-real-gpt",
    "max_tokens": 40,
    "messages": [
        {"role": "user", "content": "사내 복지 포털 비밀번호 재설정 방법 알려줘"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_retry", "type": "function",
            "function": {"name": "search_kb", "arguments": "{\"q\": \"password reset\"}"}}]},
        {"role": "tool", "tool_call_id": "call_retry", "content": "문서 없음 (timeout)"},
    ],
    "tools": [{"type": "function", "function": {"name": "search_kb", "parameters": {
        "type": "object", "properties": {"q": {"type": "string"}}}}}],
}
HEADERS_BASE = {
    "Content-Type": "application/json",
    "X-Run-Id": "run-stuck-1",
    "x-claude-code-session-id": "sess-support-stuck",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=12)
    ap.add_argument("--cont", action="store_true", help="이전 국면에서 이어감 (배너 생략)")
    args = ap.parse_args()
    key = os.environ.get("KEY_E") or sys.exit("KEY_E 필요 (support-bot 가상 키)")
    headers = {**HEADERS_BASE, "Authorization": f"Bearer {key}"}

    if not args.cont:
        print("고장난 재시도 루프 시작 — 매 콜이 동일 요청, 매 콜이 실비용이다.")
    passed = blocked = 0
    spent_calls = 0
    for i in range(1, args.calls + 1):
        req = urllib.request.Request(f"{BASE}/v1/chat/completions",
                                     data=json.dumps(BODY).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
                passed += 1
                spent_calls += 1
                print(f"  #{i:2d}  200 통과 — 비용 발생 (누적 통과 {spent_calls})")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            blocked += 1
            # 계약 실측 두 갈래: 봉투 속 바디 vs 응답 헤더 (헤더가 살아남는지가 실험 대상)
            hdr = e.headers.get("x-argus-decision-id", "")
            did = ""
            try:
                j = json.loads(body)
                did = (j.get("decision_id")
                       or j.get("error", {}).get("provider_specific_fields", {}).get("decision_id")
                       or "")
            except ValueError:
                pass
            print(f"  #{i:2d}  {e.code} 차단 ★  헤더={hdr or '없음'}  바디={did or '(봉투 속)'}")
            if blocked == 1:
                print(f"      ── 첫 차단 응답 헤더: {dict(e.headers)}")
                print(f"      ── 첫 차단 응답 바디: {body[:500]}")
        time.sleep(0.2)
    print(f"통과 {passed} · 차단 {blocked}")


if __name__ == "__main__":
    main()
