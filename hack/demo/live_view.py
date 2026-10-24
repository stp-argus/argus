#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""ARGUS 라이브 뷰 — 탐지가 일어나는 것을 실시간으로 보여준다.

데모용이다. `records.jsonl` 을 따라 읽으며 호출 스트림과 축별 게이지를 그린다.
spec 007 의 임베디드 대시보드와는 다른 물건이다 — 저건 웹 UI 고 이건 발표용 터미널 뷰다.

    python3 hack/demo/live_view.py <records.jsonl>

의존성 없음. ANSI 이스케이프만 쓴다.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque

# 실측 도출 임계 — 2026-09-07 실모델 대조군 재도출 (docs/DETECTION.ko.md §5).
# 정본은 RunawayPolicy CR(argus-helm dev-values / kind-values policy:). 여기는 화면용 사본이라
# CR 을 바꾸면 같이 바꾼다. rotation_gap 은 표본 부족으로 껐다(축 자체는 관측만 표시).
THRESHOLDS = {
    "duplicate_calls": (5, 20),
    "principal_calls": (17, 19),
    "run_steps": (9, 18),
}
AXIS_LABEL = {
    "duplicate_calls": "동일 호출 반복",
    "principal_calls": "principal 누적",
    "rotation_gap": "run 회전 격차",
    "run_steps": "run 스텝수",
    "budget_usd": "누적 비용",
}
# 이 축이 왜 존재하는지 — 데모에서 설명할 한 줄
AXIS_NOTE = {
    "principal_calls": "run 이 회전해도 남는다",
    "run_steps": "run 이 회전하면 리셋된다 ← 눈이 머는 축",
    "duplicate_calls": "대화가 자라도 도구 지문은 그대로",
    "rotation_gap": "run 을 쪼갤수록 커진다",
}

R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GRN = "\033[32m"; YEL = "\033[33m"; CYA = "\033[36m"; MAG = "\033[35m"


def bar(v: float, hard: float, width: int = 22) -> str:
    if not hard:
        return DIM + "·" * width + R
    filled = min(width, int(v / hard * width))
    if v >= hard:
        color = RED
    elif v >= hard * 0.6:
        color = YEL
    else:
        color = GRN
    return color + "█" * filled + DIM + "░" * (width - filled) + R


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "records.jsonl"
    stream: deque = deque(maxlen=12)
    # 축 게이지는 **principal 별로** 들고, 화면에는 가장 최근에 움직인 principal 것을 그린다.
    # 전역으로 하나만 두면 정상 에이전트와 폭주가 섞여 게이지가 의미를 잃는다.
    obs_by: dict[str, dict[str, float]] = {}
    focus = ""
    stats = {"calls": 0, "blocked": 0, "would": 0, "cost": 0.0}
    clients: dict[str, int] = {}
    pos = 0
    last_reason = ""

    print("\033[?25l", end="")  # 커서 숨김
    try:
        while True:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size < pos:      # 파일이 리셋됐다
                pos = 0; stream.clear(); obs_by.clear(); clients.clear(); focus = ""
                stats = {"calls": 0, "blocked": 0, "would": 0, "cost": 0.0}
            if size > pos:
                with open(path, encoding="utf-8") as f:
                    f.seek(pos)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        stats["calls"] += 1
                        stats["cost"] += r.get("cost_usd") or 0.0
                        if r.get("status") == "blocked":
                            stats["blocked"] += 1
                        if r.get("would_block"):
                            stats["would"] += 1
                            last_reason = r.get("decide_reason") or last_reason
                        p = str(r.get("principal") or "?")
                        if r.get("observations"):
                            obs_by.setdefault(p, {}).update(r["observations"])
                            focus = p
                        ci = r.get("client_identity") or {}
                        c = ci.get("client") or "(미상)"
                        clients[c] = clients.get(c, 0) + 1
                        stream.append(r)
                    pos = f.tell()
            obs = obs_by.get(focus, {})

            # ── 렌더 ────────────────────────────────────────────────
            out = ["\033[H\033[J"]
            out.append(f"{B}  ARGUS 라이브 뷰{R}   {DIM}{path}{R}\n")
            out.append(f"  {'─' * 74}\n")

            lvl = (stream[-1].get("control_level") if stream else None) or "—"
            lc = RED if lvl == "Enforce" else YEL if lvl == "Guard" else CYA
            out.append(f"  제어수준 {lc}{B}{lvl}{R}    호출 {B}{stats['calls']}{R}"
                       f"    차단 {RED}{B}{stats['blocked']}{R}"
                       f"    탐지(would_block) {YEL}{stats['would']}{R}"
                       f"    비용 {B}${stats['cost']:.6f}{R}\n")
            if clients:
                cs = "  ".join(f"{k}×{v}" for k, v in sorted(clients.items(), key=lambda x: -x[1])[:4])
                out.append(f"  {DIM}클라이언트{R} {cs}\n")
            out.append("\n")

            # 축 게이지 — 가장 최근에 움직인 principal 기준
            out.append(f"  {B}탐지축{R}  {DIM}(principal {focus[:12] or '—'} · 현재값 / hard 임계){R}\n")
            for axis, (soft, hard) in THRESHOLDS.items():
                v = obs.get(axis)
                if v is None:
                    continue
                mark = f"{RED}⛔{R}" if v >= hard else f"{YEL}⚠{R} " if v >= soft else "  "
                note = AXIS_NOTE.get(axis, "")
                out.append(f"   {mark} {AXIS_LABEL[axis]:<14} {bar(v, hard)} "
                           f"{v:>6.1f} / {hard:<5} {DIM}{note}{R}\n")
            out.append("\n")

            # 호출 스트림 — **시간순으로 정렬한다.**
            # 레코드는 도착 순서대로 append 되는데, 응답측 병합(success_event)과 TTL 스윕
            # 때문에 파일 순서가 호출 순서와 다르다. 정렬 안 하면 화면이 뒤죽박죽으로 보인다.
            out.append(f"  {B}호출 스트림{R}\n")
            for r in sorted(stream, key=lambda x: str(x.get("ts") or "")):
                ci = r.get("client_identity") or {}
                blocked = r.get("status") == "blocked"
                icon = f"{RED}✖{R}" if blocked else (f"{YEL}▲{R}" if r.get("would_block") else f"{GRN}·{R}")
                tool = r.get("tool_name") or "-"
                args = r.get("tool_args_value_hashes") or {}
                argstr = ",".join(f"{k}={v[:6]}" for k, v in list(args.items())[:2]) or "-"
                run = str(r.get("run_id") or "-")[:14]
                # 러너가 user-agent 를 안 보내면 undici 기본값이라 'node' 로만 보인다.
                # 그럴 때는 run_id 로 무엇인지 짐작할 수 있게 모델을 같이 띄운다.
                cl = (ci.get("client") or "?")[:12]
                line = (f"   {icon} {DIM}{str(r.get('ts',''))[11:19]}{R} "
                        f"{MAG}{cl:<13}{R} run={run:<15} {CYA}{tool:<10}{R} {DIM}{argstr:<26}{R}")
                out.append((RED + line + R if blocked else line) + "\n")

            if last_reason:
                out.append(f"\n  {RED}{B}▶ 판정{R} {last_reason}\n")
            out.append(f"\n  {DIM}Ctrl-C 로 종료{R}\n")
            sys.stdout.write("".join(out)); sys.stdout.flush()
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        print("\033[?25h", end="")   # 커서 복구
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
