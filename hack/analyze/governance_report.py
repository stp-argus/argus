#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""거버넌스 리포트 — 누가(팀·주체) 얼마를 썼고, 어떤 이상(비용·행동·사용량·신원)이 있었나.

Prometheus 가 없어도 레코드 파일만으로 본다. 회전본(.gz)도 여러 개 받는다.

    python3 hack/analyze/governance_report.py records.jsonl [records-2026-09-2*.jsonl.gz ...]
    python3 hack/analyze/governance_report.py --json records.jsonl     # 기계용

범주는 레코드의 `categories` 를 쓴다 (계약 §9). 필드가 없는 옛 레코드는 policy_hit 축에서
같은 표(argus_decide.CATEGORY_BY_AXIS)로 다시 만든다 — 그래서 과거 파일에도 쓸 수 있다.
신원은 레코드 그대로 해시다. 사람 이름으로 보려면 LiteLLM 의 키·팀 목록과 맞춰 본다.
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "guardrail"))
from argus_decide import category  # noqa: E402 — 범주 표는 한 곳(엔진)에만 둔다

LABEL = {"cost": "이상 비용", "behavior": "이상 행동", "volume": "이상 사용량", "identity": "신원 위조", "other": "기타"}


def read(paths: list[str]) -> list[dict]:
    rows = []
    for p in paths:
        opener = gzip.open if p.endswith(".gz") else open
        with opener(p, "rt", encoding="utf-8") as f:
            rows += [json.loads(l) for l in f if l.strip()]
    return rows


def categories(r: dict) -> list[str]:
    if "categories" in r:
        return list(r["categories"] or [])
    cats = {category(h.get("axis", "")) for h in r.get("policy_hit") or [] if h.get("axis") != "cooldown"}
    if (r.get("client_identity") or {}).get("claimed_conflicts_with_resolved"):
        cats.add("identity")
    return sorted(cats)


def summarize(rows: list[dict]) -> dict:
    def empty():
        return {"calls": 0, "cost_usd": 0.0, "would_block": 0, "blocked": 0, "anomaly": Counter()}
    teams: dict[str, dict] = defaultdict(empty)
    principals: dict[str, dict] = defaultdict(empty)
    total = empty()
    for r in rows:
        cost = float(r.get("cost_usd") or 0.0)
        wb, bl = bool(r.get("would_block")), r.get("status") == "blocked"
        cats = categories(r)
        for agg in (teams[r.get("team") or "(none)"], principals[r.get("principal") or "(none)"], total):
            agg["calls"] += 1
            agg["cost_usd"] += cost
            agg["would_block"] += wb
            agg["blocked"] += bl
            agg["anomaly"].update(cats)
    first = min((r.get("ts", "") for r in rows), default="")
    last = max((r.get("ts", "") for r in rows), default="")
    return {"range": [first, last], "total": total, "teams": teams, "principals": principals}


def _fmt(agg: dict) -> str:
    a = " ".join(f"{LABEL.get(k, k)}={v}" for k, v in sorted(agg["anomaly"].items())) or "-"
    return (f"{agg['calls']:>6}콜  ${agg['cost_usd']:>10.4f}  탐지 {agg['would_block']:>4}  "
            f"차단 {agg['blocked']:>4}  │ {a}")


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    paths = [a for a in argv if a != "--json"] or ["records.jsonl"]
    rows = read(paths)
    if not rows:
        print("레코드 없음", file=sys.stderr)
        return 1
    s = summarize(rows)
    if as_json:
        def plain(d):
            return {k: (dict(v) if isinstance(v, Counter) else v) for k, v in d.items()}
        print(json.dumps({"range": s["range"], "total": plain(s["total"]),
                          "teams": {k: plain(v) for k, v in s["teams"].items()},
                          "principals": {k: plain(v) for k, v in s["principals"].items()}},
                         ensure_ascii=False, indent=2))
        return 0
    print("=" * 96)
    print(f"거버넌스 리포트  ·  {s['range'][0]} ~ {s['range'][1]}  ·  파일 {len(paths)}개")
    print("=" * 96)
    print("범례: 탐지 = 폭주 판정(would_block) 수 · 차단 = 403 으로 끊은 수 · │ 뒤 = 그 범주의 임계(soft 포함)를 넘은 호출 수")
    print("전체   " + _fmt(s["total"]))
    print("\n── 팀별 (지출 순) " + "─" * 70)
    for t, agg in sorted(s["teams"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"{t:<18} " + _fmt(agg))
    print("\n── 이상이 있었던 주체 (탐지 순) " + "─" * 56)
    flagged = [(p, a) for p, a in s["principals"].items() if a["anomaly"]]
    for p, agg in sorted(flagged, key=lambda kv: (-kv[1]["would_block"], -kv[1]["cost_usd"]))[:20]:
        print(f"{p:<18} " + _fmt(agg))
    if not flagged:
        print("(없음)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
