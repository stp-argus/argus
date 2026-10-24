# SPDX-License-Identifier: Apache-2.0
"""ARGUS 판정 엔진 — 폭주 여부를 결정하고 무엇이 차단됐을 것인지 남긴다.

**이 모듈은 LiteLLM 을 import 하지 않는다.** 순수 함수 + 인메모리 상태만 쓴다. 이유:

  1. 단위 테스트가 프록시 없이 돈다 — 임계값 회귀를 초 단위로 잡는다
  2. `/v1/decide` (spec 006b) 로 그대로 옮길 수 있다. 입출력이 그 계약과 같은 모양이라
     오퍼레이터가 준비되면 호출부 한 줄만 바뀐다
  3. 판정 로직이 게이트웨이 구현에 묶이지 않는다 (로드맵 Phase 2 가 Redis decide.lua 를 예고)

── 설계 근거 (현존 레퍼런스) ────────────────────────────────────────────────

**다중 신호 확인** — 에이전트 무한루프 연구는 단일 신호로 판정하지 말라고 한다
("cross-referencing multiple signals before flagging a genuine infinite loop",
 "Always use multiple guards ... no single mechanism is sufficient").
그래서 축마다 soft/hard 두 임계를 둔다: hard 1개 = 차단, soft 2개 = 차단.
soft 하나만 넘은 것은 기록만 한다.

**행동 해싱 + 슬라이딩 윈도우** — 같은 연구가 제시하는 표준 탐지법이
"hash the last k (action/observation) pairs; if the current hash matches one from
the last w steps, loop detected" 다. 우리 `tool_args_value_hashes` 가 정확히 그 재료다.

**고정 윈도우가 아니라 슬라이딩** — 고정 윈도우는 경계에서 2배 버스트를 허용한다.
시간 기반 슬라이딩 윈도우로 오래된 항목을 버린다.

**fail-open** — AI 게이트웨이 fail 모드는 "정당한 요청을 거절하는 비용 vs 부당한 요청을
통과시키는 비용"으로 노선별 판단하라는 것이 권고다. 폭주 제어에서 전자는 남의 진짜 작업을
끊는 것이고 후자는 몇 센트다. **그래서 fail-open** (spec FR-007 과 같은 결론).
단, 판정 실패는 반드시 `decide_error` 로 감사 기록에 남긴다 — 성숙한 게이트웨이는
두 결과를 같은 감사 추적에 status 필드로 구분해 쓴다는 것이 권고다.

**단계적 적용** — 쿠버네티스 정책 생태계(Gatekeeper/Kyverno)의 합의는 audit → enforce 다.
"새 정책을 운영에 갑자기 강제하는 것이 플랫폼 팀이 앱 팀의 신뢰를 잃는 방식"이라고까지 말한다.
`controlLevel` 사다리가 그 구조다: Observe(기록만) → Guard(사용량·비용 축만 차단) → Enforce(모든 축 차단).

**히스테리시스** — 서킷 브레이커처럼, 한 번 걸리면 쿨다운 동안 유지한다.
윈도우가 미끄러지며 걸렸다 풀렸다 하는 플래핑을 막는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque, Literal

# ── 제어 수준 (PSA 스타일 자세 계층) ────────────────────────────────────────
OBSERVE = "Observe"   # 판정은 돌지만 통과. would_block 만 기록
GUARD = "Guard"       # 사용량·비용(volume·cost) 축 hard 만 막는다. 행동 축은 기록만
ENFORCE = "Enforce"   # 차단
LEVELS = (OBSERVE, GUARD, ENFORCE)


# 범주 — 축을 거버넌스 관점으로 묶는다 (탐지 로직은 그대로, 이름표만).
#   cost 이상 비용 · behavior 이상 행동 · volume 이상 사용량 · (identity 는 탭이 붙인다)
# ⚠️ Go 판본 internal/decide/category.go 의 categoryByAxis 와 **같은 표**다. 한쪽만 바꾸면
#    메트릭(Go)과 레코드·알림(파이썬)이 같은 탐지를 다른 범주로 센다. 양쪽 테스트가 같은 기대값을 쓴다.
CATEGORY_BY_AXIS = {
    "budget_usd": "cost",
    "duplicate_calls": "behavior", "run_steps": "behavior", "conv_growth": "behavior",
    "msg_bytes": "behavior", "rotation_gap": "behavior",
    "principal_calls": "volume", "team_calls": "volume",
}


def category(axis: str) -> str:
    """모르는 축은 other — 새 축을 추가하고 표를 잊으면 대시보드에 other 로 드러난다."""
    return CATEGORY_BY_AXIS.get(axis, "other")


def hit_category(hit: dict) -> str:
    """레코드의 policy_hit 한 줄에 붙일 범주. 쿨다운은 축이 아니라 **트립한 축**의 범주를 따른다.

    쿨다운 히트의 detail 은 트립 사유(`principal_calls=19>=19 · ...`)다. 그 첫 축으로 범주를 정한다 —
    안 그러면 Enforce 차단 대부분(쿨다운 중 차단)이 화면에 `other` 로 보인다 (STP-17, 2026-10-03).
    axis 는 그대로 `cooldown` 이라 알림·categories 의 쿨다운 제외 규칙(축 기준)은 바뀌지 않는다.
    """
    axis = hit.get("axis") or ""
    if axis == "cooldown":
        tripped = str(hit.get("detail") or "").split("=", 1)[0].strip()
        if tripped in CATEGORY_BY_AXIS:
            return CATEGORY_BY_AXIS[tripped]
    return category(axis)


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def load_policy_file(path: str | None = None) -> "Policy | None":
    """컨트롤러가 렌더한 정책 파일을 읽는다 (RunawayPolicy CR → ConfigMap → 파일).

    **파일이 없으면 None 을 돌려주고 호출자가 env 로 폴백한다** — spec 006 FR-006 이
    "임계값은 CR 에서 읽되, CR 이 없으면 env 기본값으로 동작한다" 고 규정한 그 폴백이다.
    CRD 비준을 기다리지 않고도 돌아야 하고, 컨트롤러가 죽어도 탭은 계속 판정해야 한다.

    파싱 실패도 None 이다. 깨진 정책으로 판정하느니 env 기본값이 낫고, 조용히 넘기지 않도록
    한 번은 시끄럽게 찍는다.
    """
    p = path or os.getenv("ARGUS_POLICY_FILE", "/etc/argus/policy/policy.json")
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"[argus-decide] 정책 파일을 읽을 수 없다 — env 기본값으로 돈다: {p!r}: {exc}",
              flush=True)
        return None

    axes = doc.get("axes") or {}
    ident = doc.get("identity") or {}

    def ax(name: str, key: str) -> float:
        return float((axes.get(name) or {}).get(key) or 0)

    return Policy(
        control_level=doc.get("control_level") or OBSERVE,
        window_s=float(doc.get("window_s") or 60.0),
        cooldown_s=float(doc.get("cooldown_s") or 30.0),
        principal_calls_soft=int(ax("principal_calls", "soft")),
        principal_calls_hard=int(ax("principal_calls", "hard")),
        repeat_soft=int(ax("duplicate_calls", "soft")),
        repeat_hard=int(ax("duplicate_calls", "hard")),
        run_steps_soft=int(ax("run_steps", "soft")),
        run_steps_hard=int(ax("run_steps", "hard")),
        rotation_gap_soft=ax("rotation_gap", "soft"),
        rotation_gap_hard=ax("rotation_gap", "hard"),
        msg_bytes_soft=int(ax("msg_bytes", "soft")),
        msg_bytes_hard=int(ax("msg_bytes", "hard")),
        conv_growth_soft=ax("conv_growth", "soft"),
        conv_growth_hard=ax("conv_growth", "hard"),
        team_calls_soft=int(ax("team_calls", "soft")),
        team_calls_hard=int(ax("team_calls", "hard")),
        budget_usd_soft=float(doc.get("budget_usd_soft") or 0.0),
        budget_usd_hard=float(doc.get("budget_usd_hard") or 0.0),
        # 신원 — 컨트롤러가 CR 의 identity 블록을 렌더해 준다. 없으면 탭 기본값.
        conversation_headers=tuple(ident.get("conversation_headers") or ()),
        trust_gateway_session=bool(ident.get("trust_gateway_session", True)),
        on_unknown_conversation=str(ident.get("on_unknown_conversation") or "Skip"),
        source=f"crd:{','.join(doc.get('source_policies') or []) or '?'}",
        failure_mode=str(doc.get("backstop_failure_mode") or "Open"),
    )


@dataclass(frozen=True)
class Policy:
    """임계값. 기본값은 **실측에서 도출**한다 — 임의값을 두지 않는다.

    각 값의 근거는 docs/thresholds.md 에 측정 표로 남긴다.
    CR(RunawayPolicy)이 있으면 그 값이 우선하고, 없으면 여기 env 기본값으로 동작한다
    (spec 006 FR-006 — 판정은 CRD 비준을 기다리지 않는다).
    """

    control_level: str = os.getenv("ARGUS_CONTROL_LEVEL", OBSERVE)
    window_s: float = _env_f("ARGUS_WINDOW_S", 60.0)

    # A1 · principal 슬라이딩 윈도우 누적 호출 (핑퐁 재귀 backstop)
    principal_calls_soft: int = _env_i("ARGUS_PRINCIPAL_CALLS_SOFT", 0)
    principal_calls_hard: int = _env_i("ARGUS_PRINCIPAL_CALLS_HARD", 0)

    # A2 · 동일 (tool, args) 반복 — 대화 안에서, 최근 w 호출 중
    #
    # env 이름은 **축 이름과 반드시 같아야 한다** (`duplicate_calls` → ARGUS_DUPLICATE_CALLS_*).
    # 임계값 도출기가 축 이름에서 env 이름을 만들기 때문이다. 처음에 이 필드만
    # ARGUS_REPEAT_* 로 두었더니 도출기가 뱉은 ARGUS_DUPLICATE_CALLS_* 를 아무도 읽지
    # 않아 **축이 조용히 꺼진 채로 배포**됐다. 랩 관통 테스트에서야 드러났다.
    # test_env_names_match_axis_names 가 이 짝을 고정한다.
    repeat_window: int = _env_i("ARGUS_DUPLICATE_CALLS_WINDOW", 20)
    repeat_soft: int = _env_i("ARGUS_DUPLICATE_CALLS_SOFT", 0)
    repeat_hard: int = _env_i("ARGUS_DUPLICATE_CALLS_HARD", 0)

    # A3 · 한 run 의 스텝 수 (LiteLLM 도 커버하지만 근거로 같이 본다)
    run_steps_soft: int = _env_i("ARGUS_RUN_STEPS_SOFT", 0)
    run_steps_hard: int = _env_i("ARGUS_RUN_STEPS_HARD", 0)

    # A4 · principal 누적 비용 (윈도우 무관, run 수명 누적)
    budget_usd_soft: float = _env_f("ARGUS_BUDGET_USD_SOFT", 0.0)
    budget_usd_hard: float = _env_f("ARGUS_BUDGET_USD_HARD", 0.0)

    # A5 · run 회전 격차 = principal 누적 ÷ run 당 최대. 핑퐁의 서명
    rotation_gap_soft: float = _env_f("ARGUS_ROTATION_GAP_SOFT", 0.0)
    rotation_gap_hard: float = _env_f("ARGUS_ROTATION_GAP_HARD", 0.0)

    # A6 · 한 호출의 메시지 크기(바이트) — 단일 초대형 호출 (G4, STP-19)
    msg_bytes_soft: int = _env_i("ARGUS_MSG_BYTES_SOFT", 0)
    msg_bytes_hard: int = _env_i("ARGUS_MSG_BYTES_HARD", 0)

    # A7 · 대화 성장률 = 이번 호출 크기 ÷ 그 대화의 첫 호출 크기 — 호출 수는 적은데 매 턴 부푸는
    # 폭주 (G3, STP-19). 컨텍스트 누적은 비용을 O(N²) 로 키우는데 호출 카운터는 조용하다.
    # ⚠️ 임계는 실트래픽에서 도출 전까지 0(끔). 정상 사용에도 긴 대화는 있다 — soft 부터.
    conv_growth_soft: float = _env_f("ARGUS_CONV_GROWTH_SOFT", 0.0)
    conv_growth_hard: float = _env_f("ARGUS_CONV_GROWTH_HARD", 0.0)

    # A8 · 팀 단위 창 누적 (G2 backstop, STP-16). CRD 에선 backstop.teamCalls(hard) 로만 온다.
    team_calls_soft: int = _env_i("ARGUS_TEAM_CALLS_SOFT", 0)
    team_calls_hard: int = _env_i("ARGUS_TEAM_CALLS_HARD", 0)

    # soft 2개로도 차단할 것인가. **기본은 끈다.**
    #
    # 다중 신호 확인이 오탐을 줄인다는 것이 레퍼런스 합의지만, 그건 각 신호의 임계가
    # 믿을 만할 때 얘기다. 우리 soft 는 **관측한 정상 29콜의 최대값**에서 나왔다.
    # 정상 표본이 작고 한 가지 패턴(합성 대조군)뿐이라, 두 축이 서로 독립적으로 틀리는
    # 게 아니라 **같이 과소평가돼 있을** 수 있다 — 더 바쁜 정상 에이전트는
    # principal_calls 도 넘고 같은 도구를 두 번 부르기도 한다. 그러면 soft 두 개가
    # 동시에 걸리고 진짜 작업이 끊긴다.
    #
    # hard 는 폭주 관측의 중앙값이라 정상과 훨씬 멀다. 그래서 **기본 차단 조건은 hard 1개**다.
    # 대조군이 충분히 커지면(실사용 트래픽 확보 후) 이 스위치를 켜서 민감도를 올린다.
    soft_escalation: bool = os.getenv("ARGUS_SOFT_ESCALATION", "0") == "1"

    # 히스테리시스 — 한 번 걸린 principal 은 이 시간 동안 유지
    cooldown_s: float = _env_f("ARGUS_COOLDOWN_S", 30.0)

    # 메모리 상한 — principal 이 무한히 늘어나면 게이트웨이가 죽는다
    max_principals: int = _env_i("ARGUS_MAX_PRINCIPALS", 10_000)

    # ── 신원 (spec 006f · CRD identity 블록) ────────────────────────────
    # 대화 축을 무엇에서 얻을지는 **클러스터에 어떤 클라이언트가 들어오는가**에 달렸고,
    # 그건 운영자가 아는 사실이지 코드에 박을 사실이 아니다. 그래서 CR 로 뺐다.
    #
    # conversation_headers 가 비면 탭의 내장 기본 사다리를 쓴다 (Claude Code → Codex → 일반).
    conversation_headers: tuple = ()
    # 게이트웨이(LiteLLM)가 이미 정규화한 세션을 믿을 것인가. 기본 신뢰.
    trust_gateway_session: bool = os.getenv("ARGUS_TRUST_GATEWAY_SESSION", "1") != "0"
    # 대화를 끝내 못 정했을 때: Skip(세지 않는다) | Derive(첫 메시지로 파생)
    on_unknown_conversation: str = os.getenv("ARGUS_ON_UNKNOWN_CONVERSATION", "Skip")

    # 이 정책이 어디서 왔는가 — "env" 또는 "crd:<네임스페이스/이름,...>".
    # 레코드에 남겨서 "선언한 정책이 실제로 도는가"를 화면에서 확인할 수 있게 한다.
    # 이게 없으면 CR 을 고쳤는데 반영이 안 된 상태를 눈으로 구별할 수 없다.
    source: str = "env"
    # backstop.failureMode (STP-40). 오퍼레이터가 유예보다 오래 안 닿거나 로컬 판정까지 실패할 때 Guard·Enforce 면 막을지.
    # 엔진은 이 값을 쓰지 않는다 — 가드레일의 실패 경로만 읽는다.
    failure_mode: str = "Open"


@dataclass
class Hit:
    axis: str
    level: Literal["soft", "hard"]
    observed: float
    threshold: float
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "axis": self.axis,
            "level": self.level,
            "observed": self.observed,
            "threshold": self.threshold,
            "detail": self.detail,
        }


@dataclass
class Decision:
    decision: Literal["allow", "block"]
    would_block: bool
    policy_hit: list[dict]
    reason: str
    decision_id: str | None = None
    control_level: str = OBSERVE
    policy_source: str = "env"
    # 축별 관측치. 임계를 안 넘어도 **항상** 채운다 — 임계값 도출이 런타임과
    # 정확히 같은 계산을 쓰게 하려는 것이다. 분석기가 따로 구현하면 그 둘이 갈라진다.
    observations: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "would_block": self.would_block,
            "policy_hit": self.policy_hit,
            "reason": self.reason,
            "decision_id": self.decision_id,
            "control_level": self.control_level,
        }


@dataclass
class _PrincipalState:
    """한 principal 의 슬라이딩 윈도우 상태. 전부 상한이 있다."""

    calls: Deque[float] = field(default_factory=deque)          # 호출 시각
    run_calls: dict[str, Deque[float]] = field(default_factory=dict)
    cost_usd: float = 0.0
    # 집행 상태(blocked_until·trip_reason)는 여기 없다 — DecisionEngine._enforce 에 있다.
    # 카운터는 창이 지나면 버려도 되지만 쿨다운은 창과 무관한 집행 상태라 LRU 축출로
    # 같이 지워지면 안 된다 (STP-24: 차단당한 주체가 잠깐 조용한 사이 축출되면 쿨다운이 풀렸다).

    def prune(self, now: float, window_s: float) -> None:
        cutoff = now - window_s
        while self.calls and self.calls[0] < cutoff:
            self.calls.popleft()
        empty = []
        for run, dq in self.run_calls.items():
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq:
                empty.append(run)
        for run in empty:
            del self.run_calls[run]


@dataclass
class _ConversationState:
    """대화 안의 최근 (tool, args) 지문. 행동 해싱 윈도우."""

    fingerprints: Deque[str] = field(default_factory=deque)
    steps: int = 0
    first_bytes: int = 0   # 대화 첫 호출의 msg_bytes — 성장률의 분모
    # spec 006e "파라미터 제외" 정규화 (STP-20). (tool, key) 마다 관찰 수·고유 값·고유 모양·단조 횟수만
    # 들고 있다 — 값은 매번 변하는데 모양이 고정이거나 단조 증가하면 파라미터(attempt, ts …)다.
    norm_fingerprints: Deque[str] = field(default_factory=deque)
    profile: dict = field(default_factory=dict)   # (tool, key) -> [n, set(h), set(s), mono]


_PROFILE_MIN_OBS = 4   # 이만큼은 봐야 "변한다"고 말할 수 있다 (normalize_study.OnlineProfile.MIN_OBS)


def _normalized_fingerprint(cst: "_ConversationState", tool: str, facets: dict) -> str | None:
    """파라미터로 분류된 키를 빼고 남은 정체성 키로 지문을 만든다. 남는 키가 없으면 None —
    정상 에이전트가 매번 다른 파일을 읽으면 path 가 파라미터가 되는데, 그때 뭉개면 정상이 통째로
    반복이 된다 (006e 실측: "모양만" 규칙이 정상을 17 까지 끌어올렸다). 근사이므로 soft 전용."""
    keep = {}
    for k, v in facets.items():
        a = cst.profile.setdefault((tool, k), [0, set(), set(), 0])
        a[0] += 1
        a[1].add(v.get("h"))
        a[2].add(v.get("s"))
        if v.get("d") in ("+1", "+"):
            a[3] += 1
        n, hs, shs, mono = a
        is_param = n >= _PROFILE_MIN_OBS and (mono >= n - 1 or (len(hs) >= n * 0.9 and len(shs) == 1))
        if not is_param:
            keep[k] = v.get("h")
    if not keep:
        return None
    return tool + "|" + "|".join(f"{k}={keep[k]}" for k in sorted(keep))


class DecisionEngine:
    """인메모리 판정기.

    LiteLLM 워커가 여러 개면 카운터가 쪼개진다 — 그건 3단계에서 Redis 로 옮길 문제이고
    (로드맵 Phase 2 decide.lua), 지금 dev 는 replicas: 1 이라 정확하다.
    **쪼개짐이 생기면 과소 카운트 = fail-open 방향**이라 안전한 쪽으로 틀린다.
    """

    # 정책 파일을 다시 읽는 간격(초). ConfigMap 마운트는 kubelet 이 주기적으로 갱신하므로
    # 파드를 재시작하지 않아도 CR 변경이 반영된다. mtime 이 바뀌었을 때만 다시 읽는다.
    RELOAD_EVERY_S = 5.0

    def __init__(self, policy: Policy | None = None) -> None:
        # 우선순위: 명시 인자 > CR 렌더 파일 > env (FR-006 폴백)
        self._explicit = policy is not None
        self.policy = policy or load_policy_file() or Policy()
        self._principals: OrderedDict[str, _PrincipalState] = OrderedDict()
        self._conversations: OrderedDict[str, _ConversationState] = OrderedDict()
        # principal -> (blocked_until, trip_reason). 축출 대상이 아니다. 수명 = 쿨다운.
        self._enforce: dict[str, tuple[float, str, bool]] = {}   # (blocked_until, reason, backstop)
        # team(해시) -> 창 안 호출 시각. principal 이 갈라져도 팀은 하나다 (G2 backstop).
        self._teams: OrderedDict[str, Deque[float]] = OrderedDict()
        self.evicted = 0
        self._next_reload = 0.0
        self._policy_mtime = 0.0

    def refresh_policy(self) -> None:
        """판정 없이 정책 파일만 따라간다 — 원격 판정이 정상이라 decide() 가 안 불릴 때 가드레일이 부른다."""
        self._maybe_reload(time.time())

    def _maybe_reload(self, now: float) -> None:
        """CR 이 바뀌면 파드 재시작 없이 따라간다."""
        if self._explicit or now < self._next_reload:
            return
        self._next_reload = now + self.RELOAD_EVERY_S
        path = os.getenv("ARGUS_POLICY_FILE", "/etc/argus/policy/policy.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == self._policy_mtime:
            return
        loaded = load_policy_file(path)
        if loaded is None:
            return
        self._policy_mtime = mtime
        if loaded != self.policy:
            print(f"[argus-decide] 정책 갱신됨 ({loaded.source}) "
                  f"level={loaded.control_level}", flush=True)
            self.policy = loaded

    # ── 상태 ────────────────────────────────────────────────────────────
    def _principal(self, key: str) -> _PrincipalState:
        st = self._principals.get(key)
        if st is None:
            st = _PrincipalState()
            self._principals[key] = st
            while len(self._principals) > self.policy.max_principals:
                self._principals.popitem(last=False)
                self.evicted += 1
        else:
            self._principals.move_to_end(key)
        return st

    def _team(self, key: str) -> Deque[float]:
        dq = self._teams.get(key)
        if dq is None:
            dq = deque()
            self._teams[key] = dq
            while len(self._teams) > self.policy.max_principals:
                self._teams.popitem(last=False)
                self.evicted += 1
        else:
            self._teams.move_to_end(key)
        return dq

    def _conversation(self, key: str) -> _ConversationState:
        st = self._conversations.get(key)
        if st is None:
            st = _ConversationState()
            self._conversations[key] = st
            while len(self._conversations) > self.policy.max_principals:
                self._conversations.popitem(last=False)
                self.evicted += 1
        else:
            self._conversations.move_to_end(key)
        return st

    # ── 판정 ────────────────────────────────────────────────────────────
    def decide(self, signals: dict, now: float | None = None) -> Decision:
        """`signals` 는 가드레일이 pre_call 에서 이미 뽑아 둔 것과 같은 모양이다.

        필요한 키: principal, run_id, conversation_hash, tool_name,
                   tool_args_value_hashes, cost_usd(principal 누적 지출 절대값, 선택), msg_bytes(선택),
                   tool_arg_facets(선택 — 정규화 soft), team(선택 — 팀 backstop)
        """
        now = time.time() if now is None else now
        self._maybe_reload(now)
        p = self.policy
        principal = signals.get("principal") or "(unknown)"
        run_id = signals.get("run_id") or "(no-run)"
        # 대화 키를 못 만든 요청은 **대화 스코프 축을 평가하지 않는다** (spec 006f).
        #
        # 예전에는 `f"{principal}|{run_id}"` 로 대충 버킷을 만들었다. 그러면 대화를
        # 식별 못 하는 와이어(Responses + previous_response_id 처럼 꼬리만 오는 요청)에서
        # **무관한 작업이 서로의 반복으로 세어진다** = 오탐. 모르는 것은 세지 않는 편이 낫고,
        # 그 사실이 `conversation_source: "unavailable"` 로 기록에 남아야
        # 나중에 "왜 안 걸렸나"를 답할 수 있다.
        conv = signals.get("conversation_hash")
        conv_known = bool(conv) and signals.get("conversation_source") != "unavailable"

        pst = self._principal(principal)
        pst.prune(now, p.window_s)

        # 쿨다운 중인가 — 판정은 유지하되(히스테리시스), **누적은 계속한다**. 예전엔 여기서 조기
        # 반환해 창에 넣지도 않았는데, 그러면 임계가 낮은 축 하나가 먼저 걸리는 순간 나머지 축은
        # 누적조차 못 했다 (2026-09-07 실측: budget 이 먼저 걸리자 principal_calls 가 2 에서 멈췄다).
        blocked_until, trip_reason, _ = self._enforce.get(principal, (0.0, "", False))
        cooling = blocked_until > now
        if not cooling and principal in self._enforce:
            del self._enforce[principal]

        # 이번 호출을 윈도우에 넣는다. 차단되면 끝에서 되돌린다 — undo 에 되돌릴 일을 쌓는다 (engine.go 와 같다).
        undo: list = []
        pst.calls.append(now)
        undo.append(pst.calls.pop)
        had_run = run_id in pst.run_calls
        pst.run_calls.setdefault(run_id, deque()).append(now)
        undo.append(lambda: pst.run_calls[run_id].pop() if had_run else pst.run_calls.pop(run_id))
        # cost_usd 는 **절대 누적값**(탭이 아는 이 principal 의 총지출). += 가 아니라 보관 —
        # 같은 값을 두 번 받아도 두 배가 안 되고, 축출·재기동으로 0 이 됐어도 출처가 영속이면 되살아난다.
        # LiteLLM 이 영속한 값(cost_authoritative)이면 최신값: 줄어든 값은 budget_duration 리셋이다.
        # 탭 메모리 정산은 파드마다 따로이고 재기동에 0 이 되므로 max (engine.go 와 같다).
        cost = float(signals.get("cost_usd") or 0.0)
        pst.cost_usd = cost if signals.get("cost_authoritative") else max(pst.cost_usd, cost)

        cst = self._conversation(conv) if conv_known else None
        if cst is not None:
            saved = (deque(cst.fingerprints), deque(cst.norm_fingerprints), cst.steps, cst.first_bytes)

            def _restore_conv(c=cst, s=saved):
                c.fingerprints, c.norm_fingerprints, c.steps, c.first_bytes = s
            undo.append(_restore_conv)
            cst.steps += 1

        hits: list[Hit] = []
        obs: dict[str, float] = {}

        # A1 · principal 누적 (핑퐁 backstop)
        n_calls = len(pst.calls)
        _add(hits, obs, "principal_calls", n_calls, p.principal_calls_soft, p.principal_calls_hard,
             f"{n_calls}콜/{p.window_s:.0f}초")

        # A2 · 동일 (tool, args) 반복 — 최근 w 호출 안에서.
        # hard 는 완전일치 지문, soft 는 정규화(파라미터 제외) 지문도 본다 (STP-20) — 정규화는 근사라 단독 차단 금지.
        fp = _fingerprint(signals) if cst is not None else None
        if fp:
            cst.fingerprints.append(fp)
            while len(cst.fingerprints) > p.repeat_window:
                cst.fingerprints.popleft()
            repeats = sum(1 for x in cst.fingerprints if x == fp)
            obs["duplicate_calls"] = repeats
            nrep = 0
            facets = signals.get("tool_arg_facets")
            if isinstance(facets, dict) and facets:
                nfp = _normalized_fingerprint(cst, str(signals.get("tool_name")), facets)
                if nfp:
                    cst.norm_fingerprints.append(nfp)
                    while len(cst.norm_fingerprints) > p.repeat_window:
                        cst.norm_fingerprints.popleft()
                    nrep = sum(1 for x in cst.norm_fingerprints if x == nfp)
                    obs["duplicate_calls_norm"] = nrep
            detail = f"최근 {len(cst.fingerprints)}콜 중 동일 (tool,args) {repeats}회"
            if p.repeat_hard and repeats >= p.repeat_hard:
                hits.append(Hit("duplicate_calls", "hard", repeats, p.repeat_hard, detail))
            elif p.repeat_soft and max(repeats, nrep) >= p.repeat_soft:
                if nrep > repeats:
                    detail = f"파라미터 제외 지문 {nrep}회 (완전일치 {repeats}회)"
                hits.append(Hit("duplicate_calls", "soft", max(repeats, nrep), p.repeat_soft, detail))

        # A3 · run 스텝 수
        # run ID 가 없으면 평가하지 않는다 — "(no-run)" 버킷은 주체의 창 안 호출 수와 같아서 사용량 문제를
        # "run 스텝 폭증"(행동 축)으로 잘못 이름 붙인다 (engine.go 와 같다).
        if signals.get("run_id"):
            run_n = len(pst.run_calls.get(run_id, ()))
            _add(hits, obs, "run_steps", run_n, p.run_steps_soft, p.run_steps_hard,
                 f"run {run_id} {run_n}콜")
        # 관측치도 남기지 않는다 — 대화를 모를 때의 duplicate_calls 와 같다(도출 분포에 0 이 섞이지 않게)

        # A4 · 누적 비용
        _add(hits, obs, "budget_usd", round(pst.cost_usd, 8), p.budget_usd_soft, p.budget_usd_hard,
             f"${pst.cost_usd:.6f}")

        # A5 · 회전 격차 — run 을 돌려 per-run 상한을 피하는 서명
        per_run_max = max((len(dq) for dq in pst.run_calls.values()), default=1) or 1
        gap = n_calls / per_run_max
        # run 이 하나뿐이면 격차는 정의상 1.0 이라 신호가 아니다
        if len(pst.run_calls) > 1:
            _add(hits, obs, "rotation_gap", round(gap, 2), p.rotation_gap_soft, p.rotation_gap_hard,
                 f"누적 {n_calls} ÷ run당최대 {per_run_max} = ×{gap:.1f}")

        # A8 · 팀 단위 창 누적 (G2 backstop) — 주체를 여러 개 쥔 한 조직은 여기서 다시 묶인다
        team = signals.get("team")
        if team:
            tq = self._team(str(team))
            cutoff = now - p.window_s
            while tq and tq[0] < cutoff:
                tq.popleft()
            tq.append(now)
            undo.append(tq.pop)
            _add(hits, obs, "team_calls", len(tq), p.team_calls_soft, p.team_calls_hard,
                 f"팀 {len(tq)}콜/{p.window_s:.0f}초")

        # A6 · 단일 호출 크기 (G4) — 재료는 pre_call 에 이미 있다
        msg_bytes = int(signals.get("msg_bytes") or 0)
        _add(hits, obs, "msg_bytes", msg_bytes, p.msg_bytes_soft, p.msg_bytes_hard, f"{msg_bytes}B")

        # A7 · 대화 성장률 (G3) — 첫 호출 대비. 첫 호출·크기 모름·대화 모름이면 평가하지 않는다
        if cst is not None and msg_bytes > 0:
            if cst.first_bytes <= 0:
                cst.first_bytes = msg_bytes
            elif cst.steps > 1:
                growth = msg_bytes / cst.first_bytes
                _add(hits, obs, "conv_growth", round(growth, 2), p.conv_growth_soft, p.conv_growth_hard,
                     f"{msg_bytes}B ÷ 첫 {cst.first_bytes}B = ×{growth:.1f}")

        if cooling:
            # 쿨다운 중에도 축은 계속 잰다. 행동 축으로 트립한 뒤 사용량·비용 상한까지 넘으면 Guard 도 막는다.
            if _backstop_hit(hits) and not self._enforce[principal][2]:
                self._enforce[principal] = (*self._enforce[principal][:2], True)
            # Guard 는 쿨다운 중에 넘은 사용량·비용 hard 도 싣는다 — 안 실으면 막은 이유가 레코드에 안 남는다.
            # (Observe·Enforce 는 예전처럼 cooldown 하나 — 기존 소비자가 그 모양에 기댄다)
            d = self._render(
                [Hit("cooldown", "hard", blocked_until - now, 0, trip_reason)]
                + [h for h in hits if p.control_level == GUARD and _backstop_hit([h])],
                principal, now, cooling=True, observations=obs,
            )
        else:
            d = self._render(hits, principal, now, observations=obs)
        if d.decision == "block":
            # 막힌 호출은 창에 남기지 않는다 — 나가지 않은 호출이다. 남기면 재시도가 창(60초)을 계속 채워
            # 재시도를 멈출 때까지 풀리지 않는다. 안 남기면 창 안의 통과 호출이 빠지는 대로 풀린다.
            # 정규화 지문 프로파일은 되돌리지 않는다(soft 전용).
            for fn in reversed(undo):
                fn()
        return d

    # ── 결정 규칙 ───────────────────────────────────────────────────────
    def _render(self, hits: list[Hit], principal: str, now: float,
                cooling: bool = False,
                observations: dict[str, float] | None = None) -> Decision:
        p = self.policy
        hard = [h for h in hits if h.level == "hard"]
        soft = [h for h in hits if h.level == "soft"]

        # 차단 조건. 기본은 **hard 1개**. soft 는 기록만 한다.
        # soft 2개 escalation 은 대조군이 충분히 커진 뒤에 켠다 (Policy.soft_escalation 주석).
        # 어느 쪽이든 soft 하나만으로는 절대 차단하지 않는다.
        tripped = bool(hard) or (p.soft_escalation and len(soft) >= 2)
        reason = ""
        if tripped:
            drivers = hard or soft
            # 판정이 `>=` 이므로 문장도 `>=`. 숫자는 Go trimFloat 와 같은 .6g (engine.go).
            reason = " · ".join(f"{h.axis}={h.observed:.6g}>={h.threshold:.6g}" for h in drivers)
            if not cooling:
                self._enforce[principal] = (now + p.cooldown_s, reason, _backstop_hit(hard))
                # 상한: 만료된 항목은 그 주체가 다시 올 때 지워지는데, 안 오는 주체가 쌓일 수 있다.
                # 넘칠 때만 훑는다. ponytail: 활성 쿨다운이 max_principals 를 넘으면 그대로 둔다 —
                # 그 규모면 상한이 아니라 정책이 문제다.
                if len(self._enforce) > p.max_principals:
                    for k in [k for k, (bu, _, _) in self._enforce.items() if bu <= now]:
                        del self._enforce[k]

        did = None
        if tripped:
            # 결정 식별자 — 감사 추적에서 이 판정을 되짚는 키
            seed = f"{principal}|{now:.3f}|{reason}"
            did = "d-" + hashlib.sha256(seed.encode()).hexdigest()[:16]

        # Guard — 사용량·비용(backstop) 축 hard 만 막는다. 쿨다운 중엔 트립 원인을 따른다 (engine.go 와 같다).
        guard_blocks = self._enforce.get(principal, (0.0, "", False))[2] if cooling else _backstop_hit(hard)
        blocking = tripped and (p.control_level == ENFORCE or (p.control_level == GUARD and guard_blocks))
        return Decision(
            decision="block" if blocking else "allow",
            would_block=tripped,
            policy_hit=[h.as_dict() for h in hits],
            reason=reason,
            decision_id=did,
            control_level=p.control_level,
            policy_source=p.source,
            observations=observations or {},
        )


def _backstop_hit(hits: list[Hit]) -> bool:
    """volume·cost 범주의 hard 가 있는가 (Guard 의 차단 대상). engine.go backstopHit 과 같다."""
    return any(h.level == "hard" and category(h.axis) in ("volume", "cost") for h in hits)


def _add(hits: list[Hit], obs: dict[str, float], axis: str,
         observed: float, soft: float, hard: float, detail: str) -> None:
    """임계 0 = 그 축 비활성. **측정으로 분리력을 증명 못 한 축은 켜지 않는다.**

    관측치는 임계와 무관하게 항상 기록한다 — 축을 끈 상태로도 분포가 쌓여야
    나중에 근거를 만들 수 있다.
    """
    obs[axis] = observed
    if hard and observed >= hard:
        hits.append(Hit(axis, "hard", observed, hard, detail))
    elif soft and observed >= soft:
        hits.append(Hit(axis, "soft", observed, soft, detail))


def _fingerprint(signals: dict) -> str | None:
    """행동 해싱 — (도구, 인자값 해시들)이 한 지문.

    주의: 대화 전체를 해싱하면 안 된다. 폭주 대화는 스텝마다 자라서 매번 다른 해시가
    나오고(샘플앱 러너가 정확히 그렇게 만든다), 그러면 반복을 영원히 못 잡는다.
    도구 호출만 보면 그 성장에 오염되지 않는다.
    """
    tool = signals.get("tool_name")
    if not tool:
        return None
    args = signals.get("tool_args_value_hashes") or {}
    parts = "|".join(f"{k}={args[k]}" for k in sorted(args))
    return hashlib.sha256(f"{tool}|{parts}".encode()).hexdigest()[:16]
