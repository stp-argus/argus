#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""판정 결과 판독 — 실제로 무엇이 막혔고 무엇이 안 막혔는가.

`observe_report.py` 가 원재료(신호)를 보여준다면 이것은 **판정 결과**를 본다.
가장 중요한 줄은 마지막의 오탐 수치다. 폭주를 잘 막는 것보다 정상을 안 막는 것이 어렵다.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "records.jsonl"
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    if not rows:
        print("레코드 없음", file=sys.stderr)
        return 1

    # 코호트는 run_id 접두로 가른다. 폭주 시나리오는 principal 이 하나뿐이라
    # 여기서는 run_id 로 충분하다 (핑퐁 run-NN, 재시도 run-01, 대조군 baseline-NN).
    def cohort(r: dict) -> str:
        rid = str(r.get("run_id") or "")
        # 대조군 접두는 둘 다 받는다 — 생성기를 스크래치패드(baseline-)에서
        # hack/demo/normal_agent.py(normal-)로 옮기면서 이름이 바뀌었다.
        if rid.startswith(("baseline-", "normal-")):
            return "정상"
        if rid.startswith(("run-", "retry-", "pingpong-")):
            return "폭주"
        return "기타"

    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[cohort(r)].append(r)

    print("=" * 72)
    print(f"판정 결과 판독  ·  {path}")
    print("=" * 72)
    print(f"\n제어 수준: {Counter(r.get('control_level') for r in rows).most_common()}")

    print(f"\n  {'코호트':<6}{'콜':>5}{'차단':>6}{'would_block':>13}{'비용':>12}{'판정오류':>9}")
    print("  " + "─" * 62)
    for name in ("정상", "폭주", "기타"):
        rs = by.get(name)
        if not rs:
            continue
        blocked = sum(1 for r in rs if r.get("status") == "blocked")
        would = sum(1 for r in rs if r.get("would_block"))
        cost = sum(r.get("cost_usd") or 0 for r in rs)
        errs = sum(1 for r in rs if r.get("decide_error"))
        print(f"  {name:<6}{len(rs):>5}{blocked:>6}{would:>13}${cost:>11.6f}{errs:>9}")

    # 어느 축이 실제로 방아쇠를 당겼나 — hard 히트만 센다
    trig = Counter()
    for r in rows:
        if r.get("status") != "blocked":
            continue
        for h in r.get("policy_hit") or []:
            if h.get("level") == "hard":
                trig[h["axis"]] += 1
    if trig:
        print("\n차단을 발동시킨 축 (hard)")
        print("  " + "─" * 40)
        for axis, n in trig.most_common():
            print(f"  {axis:<20} {n:>4}회")

    fp = sum(1 for r in by.get("정상", []) if r.get("status") == "blocked")
    fp_would = sum(1 for r in by.get("정상", []) if r.get("would_block"))
    n_norm = len(by.get("정상", []))
    print("\n" + "=" * 72)
    if n_norm == 0:
        print("⚠️  대조군이 없다 — 오탐을 측정하지 못했다. 이 판정은 신뢰할 수 없다.")
    elif fp == 0 and fp_would == 0:
        print(f"✅ 오탐 0  ·  정상 {n_norm}콜 중 차단 0, would_block 0")
    else:
        print(f"❌ 오탐 발생  ·  정상 {n_norm}콜 중 차단 {fp}, would_block {fp_would}")
        for r in by["정상"]:
            if r.get("status") == "blocked" or r.get("would_block"):
                print(f"   {r.get('run_id')}  {r.get('decide_reason')}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
