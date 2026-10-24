#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""임계값 도출기 — 측정된 분포에서만 임계를 만든다. 없으면 축을 끈다.

1차 검증에서 검증단 5곳이 만장일치로 **"임계값이 전부 임의값"** 을 지적했다.
이 도구는 그 빚을 갚는 방식이다. 규칙은 하나다:

    **정상 트래픽이 도달한 값은 임계가 될 수 없다.**

축마다 정상(대조군)과 폭주 코호트의 관측 분포를 비교해서:
  · 정상 최대 ≥ 폭주 최대  → **분리력 없음. 축을 끈다** (임계 0)
  · 그 외                  → soft = 정상 최대 초과 첫 값, hard = 폭주 중앙값

hard 를 폭주 **중앙값**으로 잡는 이유: 폭주의 첫 호출은 정상과 구별되지 않는다.
중앙값이면 폭주가 절반쯤 진행됐을 때 걸린다 — 1콜에서 걸리길 바라는 건
탐지가 아니라 예언이다.

**관측치는 판정 엔진을 그대로 재생해서 얻는다.** 분석기가 축 계산을 따로 구현하면
런타임과 갈라지고, 그러면 도출한 임계가 실제로는 다른 값을 재게 된다.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "guardrail"))
from argus_decide import DecisionEngine, Policy  # noqa: E402

# 정수 축과 실수 축 — soft 를 한 칸 올리는 방법이 다르다
INT_AXES = {"principal_calls", "duplicate_calls", "run_steps", "msg_bytes"}

# **정책 축** — 측정에서 도출하면 안 되는 것.
# 폭주 분류 문서가 예산에 대해 "상한 자체가 정책이다. 구별이 필요 없는 순수한 숫자"라고
# 적어놨다. 3분짜리 시험에서 뽑으면 $0.0009 같은 값이 나오고, 그걸 켜면 정상 사용자가
# 1센트의 10분의 1에서 막힌다. 운영자가 정하는 값이지 우리가 재는 값이 아니다.
POLICY_AXES = {"budget_usd"}

# soft 안전 여유. "정상 최대 + 1" 은 우리가 **관측한** 정상의 최대일 뿐이다.
# 못 본 정상 트래픽이 그보다 바쁠 수 있다. 정책 생태계의 합의도 느슨하게 시작해
# 조이라는 것이므로 여유를 두고 시작한다.
MARGIN = 1.5

# 호출 **간격**에 의존하는 축. 대조군의 간격은 내가 정한 값(2.5초)이라
# 이 축들의 임계는 '측정'이 아니라 부분적으로 '설계'다. 반드시 표기한다.
PACING_SENSITIVE = {"principal_calls", "rotation_gap"}


def _ts(row: dict) -> float:
    raw = row.get("ts") or ""
    try:
        return datetime.strptime(raw[:23], "%Y-%m-%dT%H:%M:%S.%f").timestamp()
    except ValueError:
        return 0.0


def replay(rows: list[dict]) -> dict[str, list[float]]:
    """한 코호트를 판정 엔진에 그대로 흘려 축별 관측 분포를 얻는다.

    임계는 전부 0(비활성)이다 — 판정하지 않고 관측만 한다.
    시각은 레코드의 `ts` 를 쓴다. 슬라이딩 윈도우가 시간 기반이라
    실제 실행 간격이 그대로 반영돼야 한다.
    """
    engine = DecisionEngine(Policy(
        control_level="Observe", window_s=60.0,
        principal_calls_soft=0, principal_calls_hard=0,
        repeat_soft=0, repeat_hard=0, run_steps_soft=0, run_steps_hard=0,
        budget_usd_soft=0.0, budget_usd_hard=0.0,
        rotation_gap_soft=0.0, rotation_gap_hard=0.0,
    ))
    out: dict[str, list[float]] = defaultdict(list)
    for row in sorted(rows, key=_ts):
        d = engine.decide(row, now=_ts(row))
        for axis, val in d.observations.items():
            out[axis].append(val)
    return out


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": min(vals),
        "p50": statistics.median(vals),
        "max": max(vals),
    }


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "records.jsonl"
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

    # 코호트는 **principal 로** 가른다. run_id 로는 안 된다 —
    # retry-storm 시나리오가 runId 를 "run-01" 로 고정하는데 pingpong 의 첫 회전과
    # 문자열이 같아서 둘이 섞인다. 실제로 처음에 그렇게 섞였다.
    # principal 은 레코드에 sha256[:16] 로만 남으므로 평문에서 해시를 만들어 맞춘다.
    def h(name: str) -> str:
        return hashlib.sha256(name.encode()).hexdigest()[:16]

    label = {h("pingpong-principal-01"): "핑퐁", h("retry-principal-01"): "재시도"}
    for i in range(1, 9):
        label[h(f"baseline-p{i:02d}")] = "정상"
    # key 티어 코호트 (2026-09-07) — 가상 키의 user_id 가 곧 주체다.
    # 시드 문자열은 가드레일 _principal_ladder 와 **같아야** 한다. 다르면 조용히
    # 라벨이 안 붙고 코호트가 빈 채로 "표본 부족"만 나온다.
    for u in ("agent-a", "agent-b", "agent-c", "agent-d"):
        label[h(f"key:user_id:{u}")] = "정상"
    label[h("key:user_id:agent-e")] = "재시도"
    # agent-f = k8s 러너 Job (pingpong-recursion) 전용 주체. 로컬 드라이버와 섞이면
    # 정상/폭주 코호트가 오염되므로 분리한다. 라벨이 없으면 레코드가 **에러 없이
    # 코호트에서 빠진다** — 09-07 에 그 실패를 한 번 겪었다 (STP-36).
    label[h("key:user_id:agent-f")] = "핑퐁"

    cohorts: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        name = label.get(str(r.get("principal")))
        if name:
            cohorts[name].append(r)

    print("=" * 78)
    print("임계값 도출  ·  정상이 도달한 값은 임계가 될 수 없다")
    print("=" * 78)
    for name, rs in cohorts.items():
        principals = {r.get("principal") for r in rs}
        runs = {r.get("run_id") for r in rs}
        cost = sum(r.get("cost_usd") or 0 for r in rs)
        print(f"  {name:<5} {len(rs):>3}콜 · principal {len(principals)} · run {len(runs)} · ${cost:.6f}")

    obs = {name: replay(rs) for name, rs in cohorts.items()}
    axes = sorted({a for o in obs.values() for a in o})
    runaway_names = [n for n in obs if n != "정상"]

    print("\n축별 분포 (min / 중앙값 / max)")
    print("─" * 78)
    hdr = f"  {'축':<18}" + "".join(f"{n:>18}" for n in ["정상"] + runaway_names)
    print(hdr)
    for axis in axes:
        line = f"  {axis:<18}"
        for n in ["정상"] + runaway_names:
            s = _stats(obs[n].get(axis, []))
            line += f"{'—':>18}" if not s.get("n") else \
                f"{f'{s['min']:g}/{s['p50']:g}/{s['max']:g}':>18}"
        print(line)

    print("\n판정 — 분리력이 있는 축만 켠다")
    print("─" * 78)
    policy_env: dict[str, float] = {}
    notes: list[str] = []
    for axis in axes:
        normal = obs["정상"].get(axis, [])
        runaway = [v for n in runaway_names for v in obs[n].get(axis, [])]

        if axis in POLICY_AXES:
            print(f"  {axis:<18} ⊘  정책 축 — 측정에서 도출하지 않는다 (운영자가 정한다)")
            notes.append(f"{axis}: 상한 자체가 정책. 시험 데이터로 뽑으면 정상 사용자를 막는다")
            continue
        if not normal or not runaway:
            print(f"  {axis:<18} ⊘  표본 부족 — 끈다")
            continue
        nmax, rmax = max(normal), max(runaway)
        if nmax >= rmax:
            print(f"  {axis:<18} ✘  분리 없음 (정상 max {nmax:g} ≥ 폭주 max {rmax:g}) — **끈다**")
            continue

        # soft = 관측된 정상 최대에 여유를 곱한 값. hard = 정상을 넘은 폭주 관측의 중앙값.
        raw_soft = max(nmax * MARGIN, nmax + 1)
        soft = int(raw_soft + 0.999) if axis in INT_AXES else round(raw_soft, 8)
        over = [v for v in runaway if v > nmax]
        hard = max(soft, statistics.median(over))
        if axis in INT_AXES:
            hard = int(hard)

        fp = sum(1 for v in normal if v >= soft) / len(normal) * 100
        # **코호트별** 탐지율. 합산하면 사각지대가 가려진다 —
        # per-run 상한이 핑퐁에 눈이 먼다는 것이 이 제품의 논지인데,
        # 합산 숫자는 재시도 폭풍의 성적으로 그 실패를 덮는다.
        per = []
        for n in runaway_names:
            vals = obs[n].get(axis, [])
            if not vals:
                per.append(f"{n} —")
                continue
            d = sum(1 for v in vals if v >= soft) / len(vals) * 100
            per.append(f"{n} {d:.0f}%")
        tag = " ⚠️간격의존" if axis in PACING_SENSITIVE else ""
        print(f"  {axis:<18} ✔  soft={soft:g} hard={hard:g}  오탐 {fp:.0f}%  "
              f"탐지[{' · '.join(per)}]{tag}")

        blind = [n for n in runaway_names
                 if obs[n].get(axis) and
                 sum(1 for v in obs[n][axis] if v >= soft) == 0]
        if blind:
            notes.append(f"{axis}: {', '.join(blind)} 에 **완전히 눈이 먼다** — 단독으로 쓰면 안 된다")
        if axis in PACING_SENSITIVE:
            notes.append(f"{axis}: 대조군 호출 간격이 설계값이라 이 임계는 부분적으로 설계다")
        policy_env[f"ARGUS_{axis.upper()}_SOFT"] = soft
        policy_env[f"ARGUS_{axis.upper()}_HARD"] = hard

    if notes:
        print("\n주의 — 이 표만 보고 켜면 안 되는 것")
        print("─" * 78)
        for n in notes:
            print(f"  · {n}")

    print("\n도출된 정책 (env)")
    print("─" * 78)
    if not policy_env:
        print("  (없음 — 어떤 축도 분리력을 보이지 않았다)")
    for k, v in policy_env.items():
        print(f"  {k}={v:g}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
