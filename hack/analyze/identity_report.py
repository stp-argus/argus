#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""신원 포착 판독 — 도구별로 무엇을 얻을 수 있는가.

`capture_probe.py` 가 모은 것을 클라이언트별로 접어서, **협조 없이 어느 축을 채울 수
있는지**를 보여준다. 빈칸이 곧 우리가 자기신고 헤더에 의존해야 하는 자리다.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

# 채우고 싶은 축과, 그것을 어디서 얻는지
AXES = [
    ("도구 이름",      lambda i, r: i.get("client")),
    ("도구 버전",      lambda i, r: i.get("client_version")),
    ("런타임",         lambda i, r: (i.get("headers", {}).get("x-stainless-lang"),
                                     i.get("headers", {}).get("x-stainless-runtime"))),
    ("세션 식별자",    lambda i, r: i.get("client_session")),
    ("재시도 횟수",    lambda i, r: i.get("headers", {}).get("x-stainless-retry-count")),
    ("trace 상관키",   lambda i, r: i.get("trace_id")),
    ("프로토콜 사용자", lambda i, r: i.get("protocol_user")),
    ("선언 도구 수",   lambda i, r: r.get("tool_count") or None),
    ("인증 헤더",      lambda i, r: r.get("auth_header")),
]


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "capture.jsonl"
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r.get("model") or r.get("msg_count")]   # GET 탐색 제외

    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[(r.get("client_identity") or {}).get("client") or "(미상)"].append(r)

    print("=" * 78)
    print("신원 포착 판독  ·  협조 없이 무엇을 얻는가")
    print("=" * 78)
    print(f"\n포착 {len(rows)}건 · 클라이언트 {len(by)}종\n")

    clients = list(by)
    w = 17
    print(f"  {'축':<15}" + "".join(f"{c[:w-1]:<{w}}" for c in clients))
    print("  " + "─" * (15 + w * len(clients)))
    for label, get in AXES:
        line = f"  {label:<15}"
        for c in clients:
            r = by[c][-1]
            v = get(r.get("client_identity") or {}, r)
            if isinstance(v, tuple):
                v = "/".join(x for x in v if x) or None
            if isinstance(v, dict):
                v = ",".join(v)
            line += f"{('—' if not v else str(v))[:w-1]:<{w}}"
        print(line)

    # 세션 식별자가 호출 간에 유지되는가 = 대화 축으로 쓸 수 있는가
    print("\n세션 식별자 안정성 (대화 축으로 쓸 수 있나)")
    print("  " + "─" * 62)
    for c, rs in by.items():
        ses = [(r.get("client_identity") or {}).get("client_session") for r in rs]
        ses = [s for s in ses if s]
        if not ses:
            print(f"  {c:<16} 없음 — 대화 축을 우리가 파생해야 한다")
            continue
        uniq = len(set(ses))
        verdict = ("호출 간 유지됨 → 대화 축으로 쓸 수 있다" if uniq < len(ses)
                   else "호출마다 새로 생김 → 대화 축으로 못 쓴다")
        print(f"  {c:<16} {len(ses)}건 중 고유 {uniq}개 — {verdict}")

    # 목록에 없어서 값을 못 남긴 헤더 = 다음에 무엇을 넣을지
    unknown: dict[str, set] = defaultdict(set)
    for r in rows:
        for k in (r.get("client_identity") or {}).get("unknown_header_keys") or []:
            unknown[k].add((r.get("client_identity") or {}).get("client") or "?")
    if unknown:
        print("\n미지 헤더 (키 이름만 기록됨 — 목록 추가 후보)")
        print("  " + "─" * 62)
        for k, cs in sorted(unknown.items()):
            print(f"  {k:<48} {','.join(sorted(cs))}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
