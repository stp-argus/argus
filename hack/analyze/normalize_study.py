#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""정규화 연구 — 무엇을 빼고 비교해야 하는가를 데이터로 정한다.

1차 검증 피드백이 "이 제품에서 가장 어려운 부분"이라고 한 지점이다. 규칙을 지어내지
않고, `tool_arg_facets`(해시·모양·변화부호)의 분포를 재서 근거를 만든다.

세 가지를 본다:

  ① 키 프로파일   키마다 값이 얼마나 변하나 · 모양은 고정인가 · 단조 증가인가
  ② 정규화 시뮬   후보 규칙별로 폭주를 잡고 정상을 안 잡는지 **동시에** 검사
  ③ 실패 사례     각 규칙이 어디서 틀리는지

**②가 핵심이다.** 폭주를 잡는 규칙은 쉽다. 정상을 안 잡으면서 잡는 규칙이 어렵다.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict


def load(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def cohort_of(r: dict) -> str:
    return str(r.get("run_id") or "").rsplit("-run-", 1)[0] or "?"


# ── 정규화 후보 규칙 ────────────────────────────────────────────────────────
#
# 각 규칙은 facets → 지문 문자열. 무엇을 **빼는가** 가 규칙의 내용이다.

def fp_exact(tool: str, f: dict) -> str:
    """현재 방식. 모든 키의 값 해시. 감사 가능하지만 값 하나만 변해도 못 잡는다."""
    return tool + "|" + "|".join(f"{k}={f[k]['h']}" for k in sorted(f))


def fp_shape_only(tool: str, f: dict) -> str:
    """값을 버리고 모양만. 가장 공격적 — 정상까지 뭉갤 위험이 크다."""
    return tool + "|" + "|".join(f"{k}={f[k]['s']}" for k in sorted(f))


def fp_drop_monotone(tool: str, f: dict) -> str:
    """**단조 증가하는 수치 키만** 뺀다. 재시도 카운터를 정확히 겨냥한다."""
    keep = {k: v for k, v in f.items() if v.get("d") not in ("+1", "+")}
    return tool + "|" + "|".join(f"{k}={keep[k]['h']}" for k in sorted(keep))


def fp_keys_only(tool: str, f: dict) -> str:
    """키 이름 집합만. '같은 도구를 같은 인자 구성으로 부른다' 수준."""
    return tool + "|" + ",".join(sorted(f))


class OnlineProfile:
    """대화별로 키를 관찰하며 **파라미터인지 정체성 키인지** 분류한다.

    런타임에 쓸 수 있어야 하므로 코호트를 미리 아는 척하지 않는다.
    (도구, 키) 마다 고유 값 수·고유 모양 수·단조 여부만 들고 있으면 된다 — 상태가 작다.
    """

    MIN_OBS = 4          # 이만큼은 봐야 "변한다"고 말할 수 있다

    def __init__(self) -> None:
        self.s: dict = defaultdict(lambda: {"n": 0, "h": set(), "sh": set(), "mono": 0})

    def observe(self, tool: str, f: dict) -> None:
        for k, v in f.items():
            a = self.s[(tool, k)]
            a["n"] += 1
            a["h"].add(v.get("h"))
            a["sh"].add(v.get("s"))
            if v.get("d") in ("+1", "+"):
                a["mono"] += 1

    def is_param(self, tool: str, k: str) -> bool:
        """파라미터 = 값은 매번 변하는데 **모양은 고정**이거나 **단조 증가**하는 키."""
        a = self.s.get((tool, k))
        if not a or a["n"] < self.MIN_OBS:
            return False
        volatile = len(a["h"]) >= a["n"] * 0.9
        if a["mono"] >= a["n"] - 1:
            return True                       # 단조 카운터. 가장 확실한 파라미터
        return volatile and len(a["sh"]) == 1  # 값만 변하고 형식은 그대로


def fp_drop_params(tool: str, f: dict, prof: OnlineProfile) -> str | None:
    """**이 연구의 결론이 되는 규칙.**

    파라미터로 분류된 키를 빼고 남은 것으로 지문을 만든다.
    단 — **남는 정체성 키가 없으면 반복으로 세지 않는다(None).**

    이 단서가 핵심이다. 정상 에이전트가 매번 다른 파일을 읽으면 `path` 가 파라미터로
    분류될 수 있는데, 그때 남는 키가 없다. 그걸 "같은 호출"로 뭉개면 **정상 작업이
    통째로 반복으로 보인다** — 모양만/키집합만 규칙이 정확히 그 실패를 한다.
    """
    keep = {k: v for k, v in f.items() if not prof.is_param(tool, k)}
    if not keep:
        return None
    return tool + "|" + "|".join(f"{k}={keep[k]['h']}" for k in sorted(keep))


RULES = {
    "exact (현재)": fp_exact,
    "단조키 제외": fp_drop_monotone,
    "모양만": fp_shape_only,
    "키집합만": fp_keys_only,
    "파라미터 제외 ⭐": fp_drop_params,      # 프로파일을 받는다 — 아래에서 특별 처리
}


def profile(rows: list[dict]) -> dict:
    """① 키 프로파일 — (코호트, 도구, 키) 별 변동률·모양안정성·단조성."""
    acc: dict = defaultdict(lambda: {"n": 0, "h": set(), "s": set(), "d": defaultdict(int)})
    for r in rows:
        f = r.get("tool_arg_facets")
        if not f:
            continue
        c, t = cohort_of(r), r.get("tool_name") or "?"
        for k, v in f.items():
            a = acc[(c, t, k)]
            a["n"] += 1
            a["h"].add(v.get("h"))
            a["s"].add(v.get("s"))
            a["d"][v.get("d")] += 1
    return acc


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "records.jsonl"
    rows = [r for r in load(path) if r.get("tool_arg_facets")]
    if not rows:
        print("tool_arg_facets 가 있는 레코드가 없다 — 가드레일 006e 배포 후 다시 재라.",
              file=sys.stderr)
        return 1

    by_cohort: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cohort[cohort_of(r)].append(r)

    print("=" * 84)
    print("정규화 연구  ·  무엇을 빼고 비교할 것인가")
    print("=" * 84)
    print(f"\n{len(rows)}콜 · 코호트 {len(by_cohort)}종\n")

    # ① 키 프로파일
    print("① 키 프로파일  (변동률 = 고유값÷호출수)")
    print("─" * 84)
    print(f"  {'코호트':<15}{'키':<12}{'호출':>5}{'변동률':>8}{'모양':>10}  {'변화부호':<14} 판단")
    acc = profile(rows)
    for (c, t, k), a in sorted(acc.items()):
        rate = (len(a["h"]) - 1) / (a["n"] - 1) * 100 if a["n"] > 1 else 0
        shape_stable = len(a["s"]) == 1
        mono = a["d"].get("+1", 0) >= max(1, a["n"] - 1)
        if mono:
            verdict = "단조 카운터 → 제외 후보"
        elif rate > 90 and shape_stable:
            verdict = "값만 변동·모양 고정 → 파라미터 의심"
        elif rate == 0:
            verdict = "고정 → 정체성 키"
        else:
            verdict = "부분 변동"
        sh = list(a["s"])[0] if shape_stable else f"{len(a['s'])}종"
        dsign = ",".join(f"{k2}×{v2}" for k2, v2 in sorted(a["d"].items(), key=lambda x: -x[1])[:2])
        print(f"  {c:<15}{k:<12}{a['n']:>5}{rate:>7.0f}%{sh:>10}  {dsign:<14} {verdict}")

    # ② 정규화 시뮬레이션
    print("\n② 정규화 시뮬레이션  ·  최대 반복 횟수 (최근 20콜 창)")
    print("─" * 84)
    cohorts = sorted(by_cohort)
    print(f"  {'규칙':<16}" + "".join(f"{c[:13]:>15}" for c in cohorts))
    print("  " + "─" * (16 + 15 * len(cohorts)))

    results: dict[str, dict[str, int]] = {}
    for name, fn in RULES.items():
        line = f"  {name:<16}"
        results[name] = {}
        for c in cohorts:
            prof = OnlineProfile()          # 대화(코호트)별로 새로 학습한다
            seen: list[str] = []
            best = 0
            for r in by_cohort[c]:
                f = r["tool_arg_facets"]
                tool = r.get("tool_name") or "?"
                prof.observe(tool, f)
                fp = fn(tool, f, prof) if fn is fp_drop_params else fn(tool, f)
                if fp is None:              # 정체성 키가 안 남았다 = 반복으로 안 센다
                    continue
                seen.append(fp)
                del seen[:-20]
                best = max(best, sum(1 for x in seen if x == fp))
            results[name][c] = best
            line += f"{best:>15}"
        print(line)

    # ③ 판정 — 폭주는 잡고 정상은 안 잡는 규칙만 쓸 수 있다
    runaway = [c for c in cohorts if not c.startswith("normal")]
    normal = [c for c in cohorts if c.startswith("normal")]
    print("\n③ 판정  ·  **규칙마다 임계를 따로 도출한다**")
    print("─" * 84)
    print("  고정 임계 20 을 모든 규칙에 들이대면 안 된다 — 그건 exact 규칙에서 나온 값이다.")
    print("  thresholds.md 와 같은 규칙을 쓴다: 정상이 도달한 값은 임계가 될 수 없다.\n")
    print(f"  {'규칙':<16}{'정상 최대':>9}{'폭주 최소':>9}{'분리폭':>8}{'도출 임계':>10}   판단")
    best_rule, best_sep = None, -1
    for name in RULES:
        nmax = max((results[name][c] for c in normal), default=0)
        rmin = min((results[name][c] for c in runaway), default=0)
        sep = rmin - nmax
        thr = max(int(nmax * 1.5 + 0.999), nmax + 1)
        if sep <= 0:
            mark = "❌ 분리 없음 — 못 쓴다"
        elif sep <= 3:
            mark = "⚠️ 분리폭이 좁다 — 정상이 길어지면 겹친다"
        else:
            mark = "✅ 분리됨"
            if sep > best_sep:
                best_rule, best_sep = name, sep
        print(f"  {name:<16}{nmax:>9}{rmin:>9}{sep:>8}{thr:>10}   {mark}")

    print("\n  분리폭 = 폭주 최소 − 정상 최대. **이게 규칙의 품질이다.**")
    print("  '임계를 넘느냐'가 아니라 '정상과 폭주가 얼마나 떨어져 있느냐'를 봐야 한다 —")
    print("  임계는 분리가 있고 나서 그 사이에서 고르는 것이지, 먼저 정하는 게 아니다.")
    if best_rule:
        nmax = max((results[best_rule][c] for c in normal), default=0)
        thr = max(int(nmax * 1.5 + 0.999), nmax + 1)
        print(f"\n  → 가장 잘 분리하는 규칙: **{best_rule}** (분리폭 {best_sep}), 도출 임계 {thr}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
