#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Observe 레코드 판독기 — 수집된 JSONL에서 폭주 신호가 실제로 보이는지 확인한다.

이 도구는 **판정하지 않는다.** 임계값을 제시하지도 않는다.
1차 검증에서 검증단 5곳이 만장일치로 지적한 것이 "임계값이 전부 임의값"이었고,
그 빚을 갚는 방법은 새 임의값을 제안하는 게 아니라 **관측된 분포를 보여주는 것**이다.
따라서 출력은 전부 "실제로 이렇게 나왔다"이고, 어떤 줄도 "그러므로 N으로 하자"라고 말하지 않는다.

읽기 전용이다. 운영 중인 수집 파일에는 절대 쓰지 않는다.

    python3 observe_report.py records.jsonl
    kubectl -n argus-system exec <litellm-pod> -- cat /var/argus/records/records.jsonl \
      | python3 observe_report.py -
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime

RATE_WINDOW_S = 60.0


def _load(path: str) -> list[dict]:
    src = sys.stdin if path == "-" else open(path, encoding="utf-8")
    rows, bad = [], 0
    try:
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    finally:
        if src is not sys.stdin:
            src.close()
    if bad:
        # 운영 파일에 손으로 뭔가 쓴 적이 있다는 뜻이다 (실제로 겪은 사고).
        print(f"⚠️  파싱 실패 {bad}줄 — 수집 파일에 JSONL 아닌 내용이 섞여 있다\n", file=sys.stderr)
    return rows


def _ts(row: dict) -> float | None:
    raw = row.get("ts")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:23], "%Y-%m-%dT%H:%M:%S.%f").timestamp()
    except ValueError:
        return None


def _bar(n: int, width: int = 28, scale: int = 1) -> str:
    return "█" * min(width, max(0, round(n / max(scale, 1) * width)))


def _h(title: str) -> None:
    print(f"\n{title}\n{'─' * len(title)}")


# ── 0. 개요 ────────────────────────────────────────────────────────────────
def overview(rows: list[dict]) -> None:
    _h("수집 개요")
    stamps = [t for t in (_ts(r) for r in rows) if t]
    span = (max(stamps) - min(stamps)) if len(stamps) > 1 else 0.0
    cost = sum(r.get("cost_usd") or 0.0 for r in rows)
    principals = {r.get("principal") for r in rows if r.get("principal")}
    runs = {r.get("run_id") for r in rows if r.get("run_id")}

    print(f"  레코드            {len(rows)}줄")
    if stamps:
        print(f"  기간              {datetime.fromtimestamp(min(stamps)):%m-%d %H:%M} ~ "
              f"{datetime.fromtimestamp(max(stamps)):%m-%d %H:%M}  ({span/60:.1f}분)")
    print(f"  principal / run   {len(principals)}명 / {len(runs)}런")
    print(f"  누적 비용         ${cost:.6f}")

    src = Counter(r.get("principal_source") for r in rows)
    usrc = Counter(r.get("usage_source") for r in rows)
    csrc = Counter(r.get("cost_source") for r in rows)
    print(f"  principal_source  {dict(src)}")
    print(f"  usage_source      {dict(usrc)}    ← missing/uncorrelated 가 많으면 응답측 유실")
    print(f"  cost_source       {dict(csrc)}    ← provider = 실측, litellm_costmap = 정적표")

    # 수집 건전성 — 이게 나쁘면 아래 분석 전부가 못 믿을 것이 된다.
    holes = sum(1 for r in rows if r.get("tokens_in") is None)
    if holes:
        print(f"  ⚠️ 응답측 비어 있는 레코드 {holes}줄 — 토큰·비용 축 분석은 그만큼 과소집계")


# ── 1. 재시도 폭풍 — 완전 동일 호출의 반복 ──────────────────────────────────
def retry_storm(rows: list[dict]) -> None:
    _h("① 재시도 폭풍  ·  탐지축 = 동일 호출 반복")
    print("  같은 대화에서 도구·인자가 **완전히 같은** 호출이 몇 번 반복됐는가.")
    print("  LiteLLM은 이걸 못 잡는다 — 각각은 평범한 호출로 보인다.\n")

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if not r.get("tool_name"):
            continue
        key = (r.get("conversation_hash"), r.get("tool_name"),
               json.dumps(r.get("tool_args_value_hashes") or {}, sort_keys=True))
        groups[key].append(r)

    repeats = sorted((g for g in groups.values() if len(g) > 1), key=len, reverse=True)
    if not repeats:
        print("  반복 없음 — 동일 호출이 2회 이상 나온 조합이 하나도 없다.")
        return

    top = len(repeats[0])
    print(f"  {'대화':<10} {'도구':<14} {'반복':>4}  {'중앙 간격':>9}  분포")
    for g in repeats[:10]:
        stamps = sorted(t for t in (_ts(r) for r in g) if t)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        gap = f"{sorted(gaps)[len(gaps)//2]:.2f}s" if gaps else "—"
        print(f"  {str(g[0].get('conversation_hash'))[:8]:<10} "
              f"{str(g[0].get('tool_name'))[:14]:<14} {len(g):>4}  {gap:>9}  {_bar(len(g), scale=top)}")


# ── 2. 핑퐁 재귀 — per-run은 무해, principal 누적이 폭발 ─────────────────────
def pingpong(rows: list[dict]) -> None:
    _h("② 핑퐁 재귀  ·  탐지축 = principal backstop  ⭐ 가장 강한 차별점")
    print("  개별 run은 다 작아 보이는데 principal로 묶으면 커지는가.")
    print("  per-run 상한만 보는 도구는 여기서 전부 눈이 먼다.\n")

    by_p: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("principal"):
            by_p[r["principal"]].append(r)

    if not by_p:
        print("  principal 없음.")
        return

    print(f"  {'principal':<12} {'런':>3} {'총호출':>5} {'런당최대':>8} {'누적비용':>11}   판정 근거")
    for p, rs in sorted(by_p.items(), key=lambda kv: -len(kv[1])):
        runs = Counter(r.get("run_id") for r in rs)
        per_run_max = max(runs.values())
        cost = sum(r.get("cost_usd") or 0.0 for r in rs)
        # per-run 시야와 principal 시야의 격차 = 이 축이 존재하는 이유
        gap = len(rs) / per_run_max if per_run_max else 1
        note = f"per-run {per_run_max} → principal {len(rs)} (×{gap:.1f})" if gap > 1 else "런 1개뿐 — 격차 관측 불가"
        print(f"  {p[:12]:<12} {len(runs):>3} {len(rs):>5} {per_run_max:>8} ${cost:>10.6f}   {note}")


# ── 3. 중복 입력 반복 — 같은 프롬프트 앞부분 재전송 ──────────────────────────
def dedup(rows: list[dict]) -> None:
    _h("③ 중복 입력 반복  ·  탐지축 = 입력 해시")
    counts = Counter(r.get("prefix_hash") for r in rows if r.get("prefix_hash"))
    dup = {h: c for h, c in counts.items() if c > 1}
    total = sum(counts.values())
    if not total:
        print("  prefix_hash 없음.")
        return
    dup_calls = sum(dup.values())
    print(f"  고유 prefix {len(counts)}종 / 총 {total}호출 · 중복에 속한 호출 "
          f"{dup_calls}건 ({dup_calls/total*100:.0f}%)")
    for h, c in sorted(dup.items(), key=lambda kv: -kv[1])[:5]:
        print(f"    {h[:12]}  ×{c}  {_bar(c, scale=max(dup.values()))}")


# ── 4. 스텝 수 / 예산 / 레이트 — LiteLLM이 이미 보는 축 ─────────────────────
def litellm_axes(rows: list[dict]) -> None:
    _h("④ 스텝수 · 예산 · 레이트  ·  LiteLLM이 이미 커버하는 축 (대조용)")

    by_run: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_run[r.get("run_id") or "(run_id 없음)"].append(r)

    print(f"  {'run_id':<20} {'호출':>4} {'msg_count 최대':>13} {'토큰(in/out)':>16} {'비용':>11}")
    for run, rs in sorted(by_run.items(), key=lambda kv: -len(kv[1]))[:10]:
        msgs = max((r.get("msg_count") or 0) for r in rs)
        tin = sum(r.get("tokens_in") or 0 for r in rs)
        tout = sum(r.get("tokens_out") or 0 for r in rs)
        cost = sum(r.get("cost_usd") or 0.0 for r in rs)
        print(f"  {run[:20]:<20} {len(rs):>4} {msgs:>13} {f'{tin}/{tout}':>16} ${cost:>10.6f}")

    stamps = sorted(t for t in (_ts(r) for r in rows) if t)
    if len(stamps) > 1:
        peak, j = 0, 0
        for i, t in enumerate(stamps):
            while stamps[i] - stamps[j] > RATE_WINDOW_S:
                j += 1
            peak = max(peak, i - j + 1)
        print(f"\n  {RATE_WINDOW_S:.0f}초 창 최대 호출률   {peak}회/분")

    reasoning = sum(r.get("tokens_reasoning") or 0 for r in rows)
    out = sum(r.get("tokens_out") or 0 for r in rows)
    if reasoning:
        print(f"  리즈닝 토큰          {reasoning} / 출력 {out}  "
              f"({reasoning/out*100:.0f}%) ⚠️ 출력에 포함됨 — 비용에서 별도 축으로 빼면 이중계산")


# ── 5. FR-002 키별 변동률 — 이번 사이클의 핵심 산출물 ────────────────────────
def key_variance(rows: list[dict]) -> None:
    _h("⑤ 도구 인자 키별 변동률  ·  FR-002 — 이 스펙에서 가장 중요한 요구")
    print("  '무엇이 고정이고 무엇이 변하는가'. dedup 정규화 규칙(어떤 키를 무시할지)의 유일한 재료다.")
    print("  인자를 통째로 해시했다면 여기서 아무것도 못 뽑는다.\n")

    seen: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for r in rows:
        tool, hashes = r.get("tool_name"), r.get("tool_args_value_hashes")
        if not tool or not hashes:
            continue
        for k, v in hashes.items():
            seen[tool][k][v] += 1

    if not seen:
        print("  도구 호출 레코드 없음.")
        return

    for tool, keys in seen.items():
        calls = max(sum(c.values()) for c in keys.values())
        print(f"  [{tool}]  {calls}회 호출")
        print(f"    {'인자 키':<16} {'고유값':>6} {'변동률':>7}  해석")
        for k, c in sorted(keys.items(), key=lambda kv: -len(kv[1])):
            n = sum(c.values())
            uniq = len(c)
            rate = (uniq - 1) / (n - 1) * 100 if n > 1 else 0.0
            if n == 1:
                note = "표본 1건 — 판단 불가"
            elif uniq == 1:
                note = "항상 고정 → 정규화 키 후보 (dedup 판단에 써야 함)"
            elif uniq == n:
                note = "매번 변함 → dedup에서 무시 후보 (재시도 카운터일 가능성)"
            else:
                note = "부분 변동 → 실제 분포를 더 봐야 함"
            print(f"    {k:<16} {uniq:>6} {rate:>6.0f}%  {note}")
        print()


# ── 6. 판정 상태 ───────────────────────────────────────────────────────────
def verdict(rows: list[dict]) -> None:
    _h("판정 (policy_hit)")
    hits = [r for r in rows if r.get("policy_hit")]
    print(f"  policy_hit 채워진 레코드   {len(hits)} / {len(rows)}")
    if not hits:
        print("  → 전부 빈 배열이다. **정상이다.** 판정 로직은 spec 006b(/v1/decide, @leeyonghe)이고")
        print("    006a는 스키마 자리만 고정해 둔다 — 006b가 붙을 때 대시보드가 안 깨지도록.")
        print("    지금 화면에 보이는 것은 '판정 결과'가 아니라 **판정의 원재료**다.")


def main() -> int:
    # 코호트 분리 — 한 파일에 여러 실험이 섞여 쌓인다. 진단 호출과 대조군과 폭주를
    # 같이 집계하면 어느 분포도 못 믿는다. 파일을 자르지 않고 읽을 때 거른다
    # (운영 중인 수집 파일은 절대 수정하지 않는다).
    argv = [a for a in sys.argv[1:]]
    prefix = None
    for flag in ("--run-prefix",):
        if flag in argv:
            i = argv.index(flag)
            prefix = argv[i + 1]
            del argv[i:i + 2]
    path = argv[0] if argv else "records.jsonl"

    rows = _load(path)
    total = len(rows)
    if prefix:
        rows = [r for r in rows if str(r.get("run_id") or "").startswith(prefix)]
    if not rows:
        print("레코드 없음.", file=sys.stderr)
        return 1

    print("=" * 72)
    print(f"ARGUS Observe 판독  ·  {path}")
    if prefix:
        print(f"코호트  run_id ^{prefix}   ·   {len(rows)}/{total}줄 선택")
    print("=" * 72)
    overview(rows)
    retry_storm(rows)
    pingpong(rows)
    dedup(rows)
    litellm_axes(rows)
    key_variance(rows)
    verdict(rows)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
