# SPDX-License-Identifier: Apache-2.0
"""LiteLLM 어댑터 — **프록시가 바뀌면 다시 쓰는 유일한 파일.**

신호 추출과 판정은 여기 없다. 세 파일의 역할이 이렇게 나뉜다:

    argus_signals.py    신호 추출. 프록시 무관. 그대로 간다
    argus_decide.py     판정 엔진. 프록시 무관 (litellm 참조 0건)
    argus_guardrail.py  ← 이 파일. LiteLLM 에 묶인 부분만

지금은 LiteLLM 을 탭으로 쓰지만 자체 프록시나 다른 프록시로 갈 가능성을 열어둔다.
그래서 **무엇이 묶여 있는지**가 파일 경계로 드러나 있어야 한다.

## 이 파일이 LiteLLM 에 의존하는 지점 (전부)

    CustomGuardrail / CustomLogger    훅 기반 클래스
    async_pre_call_hook(...)          요청측 훅 시그니처
    async_log_success_event(...)      응답측 훅 시그니처
    data["proxy_server_request"]      헤더가 여기 들어온다
    data["litellm_call_id"]           요청↔응답 상관 키
    data["litellm_session_id"]        게이트웨이가 정규화한 세션
    kwargs["standard_logging_object"] 토큰·비용
    CallTypes 이름                     와이어 힌트 (없으면 본문 모양으로 판별한다)
    fastapi.HTTPException             차단 집행 방식

## 다른 프록시로 옮긴다면

`argus_signals` 의 함수들에 넷만 주면 된다 — 헤더(소문자 키) · 본문 · 와이어 힌트 ·
상관 키. 그리고 `Decision` 을 받아 그 프로토콜의 방식으로 거절하면 된다.

---

훅이 둘인 이유: 요청측과 응답측 데이터가 따로 도착한다.
    async_pre_call_hook      → 신원·메시지 모양·도구 인자
    async_log_success_event  → 토큰·비용·지연
`litellm_call_id` 로 상관시키고, 응답이 영영 안 오면 TTL sweep 이 요청측만이라도 쓴다
(spec US2: "저장을 늦추는 어떤 결정도 하지 않는다").

기록은 controlLevel 과 무관하게 항상 한다. 차단은 Guard(사용량·비용 축)·Enforce(모든 축)에서만 한다.
"""
from __future__ import annotations

import os
import time
import uuid
from types import SimpleNamespace
from typing import Any

from litellm.caching.caching import DualCache
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import UserAPIKeyAuth

# 신호 추출부 — 프록시 무관. 랩은 디렉터리 마운트(패키지), 차트는 평면 복사라 둘 다 받는다.
try:  # pragma: no cover - import shim
    from .argus_signals import (
        _claimed_principal_conflict, _client_identity, _conversation, _dropped, _log_decision, _normalize, _notify,
        _pending, _principal_ladder, _sha, _start_sweeper, _team_seed, _sweep, _tool_arg_facets, _tool_args_value_hashes, _write,
        model_of, PENDING_TTL_S,
    )
    from .argus_decide import ENFORCE, GUARD, Decision, DecisionEngine, hit_category
except ImportError:  # pragma: no cover
    from argus_signals import (
        _claimed_principal_conflict, _client_identity, _conversation, _dropped, _log_decision, _normalize, _notify,
        _pending, _principal_ladder, _sha, _start_sweeper, _team_seed, _sweep, _tool_arg_facets, _tool_args_value_hashes, _write,
        model_of, PENDING_TTL_S,
    )
    from argus_decide import ENFORCE, GUARD, Decision, DecisionEngine, hit_category

# ── 여기부터는 LiteLLM 계약 ──────────────────────────────────────────────────
PRINCIPAL_HEADER = os.getenv("ARGUS_PRINCIPAL_HEADER", "X-Principal-Id").lower()
RUN_ID_HEADER = os.getenv("ARGUS_RUN_ID_HEADER", "X-Run-Id").lower()
# 훅에 실제로 무엇이 도착하는지 찍는다. 클러스터에서는 끈다.
DEBUG_DUMP = os.getenv("ARGUS_DEBUG_DUMP", "0") == "1"
# 차단 상태코드. 403 인 이유 (2026-10-03 기본값 429 → 403, 차트 기본값과 통일):
#  - LiteLLM 은 400·403·422 만 "가드레일 의도 차단"으로 기록하고 429 는 가드레일 실패로 기록한다
#    (litellm/integrations/custom_guardrail.py `_GUARDRAIL_BLOCK_STATUS_CODES`, v1.96.0·v1.103.2)
#  - openai·anthropic SDK 는 429 를 기본 2회 자동 재시도한다 — 403 은 재시도하지 않는다
BLOCK_STATUS = int(os.getenv("ARGUS_BLOCK_STATUS", "403"))


def _headers(data: dict) -> dict[str, str]:
    """LiteLLM 은 원본 헤더를 여기에 넣어 준다. **다른 프록시면 이 한 줄이 달라진다.**"""
    raw = (data.get("proxy_server_request") or {}).get("headers") or {}
    return {str(k).lower(): v for k, v in raw.items()}


# ── budget 축 배선 (2026-08-25, G0) ─────────────────────────────────────────
# 비용은 호출을 해봐야 안다(success_event). 그래서 "이 호출의 비용으로 이 호출을 막는" 것은
# 원천적으로 불가능하고, 예전엔 pre_call 에 cost_usd=0 을 보내 budget 축이 죽어 있었다.
# 유일하게 가능한 건 **직전까지 정산된 누적 비용으로 다음 호출을 게이트**하는 것
# (reserve-then-settle, 설계 정본 v0.1). DoW 문헌이 "요청수 말고 누적 비용을 세라, 하드
# 예산 상한이 최후 방어선"이라 지목한 축이 이것 — low-and-slow 는 rate 임계만 피하지 누적은
# 못 피한다. 엔진은 cost_usd 를 **절대 누적값**으로 받아 최신값으로 보관한다 (2026-09-08 STP-28, 2026-10-18 max→최신값):
# 델타 방식은 파드 재기동·LRU 축출에 누적이 조용히 0 이 됐다. 절대값이면 출처가 영속(LiteLLM DB)일 때
# 재기동 뒤에도 이어지고, 같은 값을 두 번 보내도 두 배가 되지 않는다. 줄어든 값은 budget_duration 리셋이라 따른다.
_principal_settled: dict[str, float] = {}   # principal -> 탭이 정산한 누적 비용 (key 티어가 아닐 때의 폴백)
_BUDGET_ACCT_MAX = int(os.getenv("ARGUS_BUDGET_ACCT_MAX", "10000"))

# principal 티어 → LiteLLM 이 Postgres 에 영속하는 spend 필드. **티어와 같은 단위**여야 한다 —
# key:user_id 주체가 키 2개를 돌리면 키별 spend 는 갈라져서 "키 회전 = 예산 리셋"이 되고
# 09-07 의 핵심 결과(회전해도 누적 유지)가 무너진다. (STP-28, 2026-09-08 필드 확인)
_SPEND_FIELD_BY_TIER = {"key:user_id": "user_spend", "key:team_id": "team_spend", "key:alias": "spend"}


def _principal_spend(user_api_key_dict: Any, principal_source: str) -> float | None:
    """LiteLLM 이 이미 정산·영속한 누적 spend. key 티어가 아니면 None (탭 정산으로 폴백)."""
    field_name = _SPEND_FIELD_BY_TIER.get(principal_source)
    if not field_name:
        return None
    try:
        v = getattr(user_api_key_dict, field_name, None)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _budget_settle(principal: str | None, cost: float | None) -> None:
    """success_event 에서 탭 정산 (key 티어가 아닌 주체용). 파드 재기동에 0 이 되는 알려진 한계.
    무한 증가 방지 상한."""
    if not principal or not cost:
        return
    if len(_principal_settled) >= _BUDGET_ACCT_MAX and principal not in _principal_settled:
        _principal_settled.pop(next(iter(_principal_settled)), None)
    _principal_settled[principal] = _principal_settled.get(principal, 0.0) + float(cost)


def _block_exception(decision) -> Exception:
    """차단 응답을 만든다.

    `HTTPException` 을 던지는 건 LiteLLM 자체 가드레일(bedrock_guardrails)과 같은 방식이다.

    ⚠️ **계약 불일치가 실측으로 확인됐다.** 샘플앱(spec 003 D-7)은 응답 **최상위**의
    `decision_id` 를 보는데, LiteLLM 은 이 detail 을 자기 OpenAI 오류 봉투로 감싸
    `error.provider_specific_fields` 아래에 넣는다. 그래서 러너는 차단을 `quota-limited`
    로 오분류하고 계속 두드린다 — 차단 자체는 동작하고 비용도 멈추지만.
    측정한 실제 본문과 대안 검토는 docs/block-contract.md 에 있다.

    (`ModifyResponseException` 은 **200 으로** 반환하므로 대안이 아니다 — 차단을
     성공으로 위장하게 된다.)
    """
    from fastapi import HTTPException  # 지연 import — 판정 엔진은 웹 프레임워크를 모른다

    body = {
        "decision_id": decision.decision_id,
        "error": {
            "type": "argus_runaway_blocked",
            "message": f"Argus blocked a runaway pattern: {decision.reason}",
            "code": "runaway_blocked",
        },
        "control_level": decision.control_level,
        "policy_hit": decision.policy_hit,
    }
    # 응답 헤더 실험 (2026-08-25, 한화 사례 노트의 파생 액션): LiteLLM 봉투가 detail(본문)은
    # 감싸지만 **헤더는 안 건드릴 가능성**이 있다. 헤더가 살아남으면 클라이언트는 봉투 구조와
    # 무관하게 `x-argus-decision-id` 로 아거스 차단을 판별할 수 있다 — 차단 계약의 최소 해법.
    return HTTPException(status_code=BLOCK_STATUS, detail=body, headers={
        "x-argus-decision-id": decision.decision_id or "",
        "x-argus-control-level": decision.control_level or "",
    })


FAILURE_GRACE_S = float(os.getenv("ARGUS_FAILURE_GRACE_S", "5"))
FAIL_CLOSED_GRACE_S = float(os.getenv("ARGUS_FAIL_CLOSED_GRACE_S", "60"))


def _fail_closed_decision(policy):
    """판정 불능 + failureMode=Closed + (Enforce|Guard) 일 때의 차단 판정. 아니면 None (fail-open).

    Guard 도 포함한다 — Guard 는 backstop 을 막는 단계이고, 이 노브는 backstop 의 실패 모드다.
    """
    if policy is None or getattr(policy, "failure_mode", "Open") != "Closed" \
            or getattr(policy, "control_level", None) not in (ENFORCE, GUARD):
        return None
    return Decision(decision="block", would_block=True, reason="decide unavailable (failureMode=Closed)",
                    policy_hit=[{"axis": "decide_unavailable", "level": "hard", "observed": 0, "threshold": 0,
                                 "detail": "failureMode=Closed", "category": "other"}],
                    decision_id=f"fc-{uuid.uuid4().hex[:12]}", control_level=policy.control_level,
                    policy_source=getattr(policy, "source", "env"))


class _RemoteDecision:
    """오퍼레이터 /v1/decide 응답을 로컬 Decision 과 같은 모양으로.

    아래 훅 코드가 판정의 출처를 몰라도 되게 한다 — 원격이든 폴백이든 같은 속성을 읽는다.
    """

    def __init__(self, d: dict) -> None:
        self.decision = d.get("decision") or "allow"
        self.would_block = bool(d.get("would_block"))
        self.policy_hit = d.get("policy_hit") or []
        self.reason = d.get("reason") or ""
        self.decision_id = d.get("decision_id") or None
        self.control_level = d.get("control_level") or "Observe"
        self.policy_source = d.get("policy_source") or "?"
        self.observations = d.get("observations") or {}


class ArgusObserve(CustomGuardrail):
    """레코더 + 판정기.

    `controlLevel` 이 무엇이든 **기록은 항상** 한다. 차단은 Guard(사용량·비용 축)·Enforce(모든 축)에서만 한다.
    쿠버네티스 정책 생태계(Gatekeeper/Kyverno)의 합의가 audit → enforce 단계 적용이고,
    "새 정책을 운영에 갑자기 강제하는 것이 플랫폼 팀이 앱 팀의 신뢰를 잃는 방식"이라는
    경고가 그 이유다. 여기서 Observe 가 그 audit 단계다.
    """

    # 워커 프로세스 하나에 엔진 하나. 슬라이딩 윈도우 상태가 호출 간에 살아 있어야 한다.
    # ARGUS_DECIDE_URL 이 있으면 이 엔진은 **폴백**이다 — 평소엔 오퍼레이터가 판정한다.
    _engine = DecisionEngine()
    _remote_down_since: float | None = None   # 원격 판정이 연속으로 실패하기 시작한 시각 (monotonic)
    _learned_fail: tuple[str, str | None] = ("Open", None)   # 마지막 원격 응답의 (failure_mode, control_level)
    _learned_identity: dict | None = None                    # 마지막 원격 응답의 identity (CR)

    def _identity_policy(self):
        """대화 키 규칙. CR 에서 온 정책 파일이 있으면 그것, 없으면 오퍼레이터가 알려 준 CR 값, 그것도 없으면 기본값."""
        local = getattr(self._engine, "policy", None)
        learned = ArgusObserve._learned_identity
        if learned is None or (local is not None and str(getattr(local, "source", "")).startswith("crd")):
            return local
        return SimpleNamespace(conversation_headers=tuple(learned.get("conversation_headers") or ()),
                               trust_gateway_session=bool(learned.get("trust_gateway_session", True)),
                               on_unknown_conversation=str(learned.get("on_unknown_conversation") or "Skip"))

    def _fail_policy(self):
        """fail-closed 판단에 쓸 정책. CR 에서 온 정책 파일이 있으면 그것, 없으면 마지막으로 본 오퍼레이터 값."""
        local = getattr(self._engine, "policy", None)
        if local is not None and str(getattr(local, "source", "")).startswith("crd"):
            return local
        fm, level = ArgusObserve._learned_fail
        return SimpleNamespace(failure_mode=fm, control_level=level, source="operator(last seen)")

    async def _decide(self, record: dict):
        """판정. 원격(오퍼레이터 /v1/decide)을 먼저, 실패·타임아웃이면 로컬 폴백.

        판정이 오퍼레이터로 옮겨간 이유 (spec 006b, 설계 문서 §3):
          · 카운터는 공유돼야 한다 — 탭 안에 있으면 replicas: 1 에 묶인다
          · CR 변경이 kubelet(~85초) 없이 **즉시** 판정에 반영된다
          · "선언이 판정을 구동한다"의 판정이 실제로 오퍼레이터에 있게 된다

        폴백이 안전한 이유: Go 엔진은 파이썬 판본의 포팅이고 **동등성 테스트**
        (internal/decide/python_parity_test.go — 실제 파이썬을 실행해 스텝별 비교)가
        같은 판정을 보장한다. 다만 카운터는 공유되지 않으므로 폴백 중에는 창이
        따로 돈다 — 과소 카운트 = fail-open 방향이라 안전한 쪽으로 틀린다.

        어느 쪽이 판정했는지는 record["decide_source"] 로 남는다. 이게 없으면
        "오퍼레이터가 죽어서 폴백으로 돌았다"를 레코드만 보고 알 수 없다.
        """
        url = os.getenv("ARGUS_DECIDE_URL", "")
        if url:
            try:
                # 블로킹 HTTP 를 스레드로 내린다 — async 훅 안에서 urllib 을 그대로 부르면
                # **이벤트 루프가 타임아웃만큼 선다.** 동시 호출이 많은 게이트웨이에서
                # 그건 우리가 지연을 만드는 꼴이다 (Mixer 를 비웃을 자격이 없어진다).
                import asyncio
                d = await asyncio.to_thread(self._decide_remote, url, record)
                record["decide_source"] = "operator"
                ArgusObserve._remote_down_since = None
                # 오퍼레이터가 알려 준 실패 모드를 기억한다 — 정책 파일이 없는 곳(다른 네임스페이스)에서도
                # 오퍼레이터가 사라졌을 때 fail-closed 할 수 있게.
                ArgusObserve._learned_fail = (d.get("failure_mode") or "Open", d.get("control_level"))
                if isinstance(d.get("identity"), dict):
                    ArgusObserve._learned_identity = d["identity"]
                return _RemoteDecision(d)
            except Exception as exc:  # noqa: BLE001 — 원격 실패는 로컬 폴백 (fail-open)
                _dropped["decide_remote_error"] = _dropped.get("decide_remote_error", 0) + 1
                n = _dropped["decide_remote_error"]
                if n == 1 or n % 100 == 0:
                    print(f"[argus] 원격 판정 실패 — 로컬 폴백 (n={n}): "
                          f"{type(exc).__name__}: {exc}", flush=True)
                # backstop.failureMode: Closed (STP-40) — 로컬 폴백은 카운터가 새로 시작해 backstop 을
                # 과소 집계한다. 공유 카운터(오퍼레이터)가 유예 시간보다 오래 안 닿으면 막는다.
                # 유예는 설계 재검토(2026-07-02 §5-2)의 "fail-closed 절벽" 완화 — 재기동 몇 초로 전 트래픽이 끊기지 않게.
                now = time.monotonic()
                if ArgusObserve._remote_down_since is None:
                    ArgusObserve._remote_down_since = now
                closed = _fail_closed_decision(self._fail_policy())
                if closed is not None and now - ArgusObserve._remote_down_since >= FAIL_CLOSED_GRACE_S:
                    record["decide_source"] = "fail_closed"
                    return closed
        record["decide_source"] = "local"
        # 로컬 폴백도 같은 누적값을 먹인다 (엔진이 cost_usd 를 최신값으로 보관한다).
        return self._engine.decide({**record, "cost_usd": record.get("decide_cost_usd") or 0.0,
                                    "cost_authoritative": record.get("budget_source") == "litellm_spend"})

    @staticmethod
    def _decide_remote(url: str, record: dict) -> dict:
        import json as _json
        import urllib.error
        import urllib.request  # 지연 import — 폴백 경로는 이 모듈이 필요 없다
        timeout_s = float(os.getenv("ARGUS_DECIDE_TIMEOUT_MS", "500")) / 1000.0
        body = _json.dumps({
            "principal": record.get("principal"),
            "run_id": record.get("run_id"),
            "conversation_hash": record.get("conversation_hash"),
            "conversation_source": record.get("conversation_source"),
            "tool_name": record.get("tool_name"),
            "tool_args_value_hashes": record.get("tool_args_value_hashes"),
            # G0: pre_call 엔 이 호출 비용을 모른다. 대신 직전까지 정산된 **누적 절대값**을
            # 보낸다 — 엔진이 최신값으로 보관해 budget_usd 축을 살린다 (accumulate-then-gate-next).
            "cost_usd": record.get("decide_cost_usd") or 0.0,
            # LiteLLM 이 영속한 spend 일 때만 엔진이 줄어든 값을 리셋으로 믿는다 (탭 정산은 파드마다 따로).
            "cost_authoritative": record.get("budget_source") == "litellm_spend",
            "msg_bytes": record.get("msg_bytes") or 0,   # G3/G4 대화 크기 축 (STP-19)
            "tool_arg_facets": record.get("tool_arg_facets"),   # 006e 정규화 soft (STP-20)
            "team": record.get("team"),                         # 팀 backstop (STP-16 G2)
        }).encode()
        headers = {"Content-Type": "application/json"}
        # 오퍼레이터가 호출자를 TokenReview 로 확인한다. kubelet 이 토큰을 회전하므로 매번 읽는다(작은 파일).
        token_file = os.getenv("ARGUS_DECIDE_TOKEN_FILE", "")
        if token_file:
            try:
                with open(token_file, encoding="utf-8") as f:
                    headers["Authorization"] = "Bearer " + f.read().strip()
            except OSError as exc:
                raise RuntimeError(f"판정 토큰 파일을 못 읽는다 ({token_file}) — decideAuth 투영 토큰 마운트를 확인") from exc
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — 클러스터 내부 URL
                return _json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(f"판정 API 가 인증을 거부했다 ({exc.code}) — 오퍼레이터의 "
                                   "decideAuth 허용 SA·audience 를 확인") from exc
            raise

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ):
        record = None
        try:
            record = self._record_request(user_api_key_dict, data, call_type)
        except Exception:  # noqa: BLE001 — 기록 실패가 호출을 깨지 않는다
            _dropped["hook_error"] += 1

        if record is None:
            return data

        # ── 판정 ────────────────────────────────────────────────────────
        # fail-open: 판정을 못 내리면 통과시킨다. AI 게이트웨이 fail 모드 권고는
        # "정당한 요청을 거절하는 비용 vs 부당한 요청을 통과시키는 비용"으로 정하라는
        # 것인데, 폭주 제어에서 전자는 남의 진짜 작업이 끊기는 것이고 후자는 몇 센트다.
        # 다만 **판정 실패는 반드시 감사 기록에 남긴다** — 조용한 fail-open 은
        # "정책이 통과시켰다"와 "정책이 없었다"를 구별할 수 없게 만든다.
        # 예외: 운영자가 CR 에 backstop.failureMode: Closed 를 선언했고 Guard·Enforce 면 막는다 (STP-40).
        try:
            decision = await self._decide(record)
        except Exception as exc:  # noqa: BLE001
            _dropped["decide_error"] += 1
            record["decide_error"] = f"{type(exc).__name__}: {exc}"[:200]
            record["would_block"] = None
            closed = _fail_closed_decision(self._fail_policy())
            if closed is not None:
                record["would_block"] = True
                record["decision_id"] = closed.decision_id
                record["control_level"] = closed.control_level
                record["decide_reason"] = closed.reason
                _log_decision(record, closed)
                self._flush_blocked(data, record)
                raise _block_exception(closed)
            # fail-open 은 조용하면 안 된다. 레코드에만 남기면 "정책이 통과시켰다"와
            # "판정이 고장나서 통과했다"가 화면에서 똑같아 보인다. 첫 건과 100건마다 시끄럽게.
            n = _dropped["decide_error"]
            if n == 1 or n % 100 == 0:
                print(
                    f"[argus] 판정 실패 — fail-open 으로 통과시킴 (n={n})"
                    f" principal={record.get('principal')} run={record.get('run_id')}"
                    f": {type(exc).__name__}: {exc}",
                    flush=True,
                )
            return data

        record["policy_hit"] = decision.policy_hit
        record["would_block"] = decision.would_block
        record["decision_id"] = decision.decision_id
        record["control_level"] = decision.control_level
        record["decide_reason"] = decision.reason or None
        # 이 판정이 **CR 에서 온 정책인지 env 기본값인지**. 이게 없으면 CR 을 고쳤는데
        # 반영이 안 된 상태를 레코드만 보고는 구별할 수 없다.
        record["policy_source"] = decision.policy_source
        # 축별 관측치를 임계와 무관하게 남긴다. 히트만 남기면 "얼마나 가까웠나"를 볼 수 없고,
        # 화면(007)도 게이지를 못 그린다. 숫자 5개라 레코드 크기에 실질적 영향이 없다.
        record["observations"] = decision.observations
        # 거버넌스 범주 — 원격(Go)·로컬 폴백 판정 모두 여기서 같은 표로 붙인다.
        for h in decision.policy_hit or []:
            if isinstance(h, dict):
                h["category"] = hit_category(h)
        cats = {h["category"] for h in decision.policy_hit or []
                if isinstance(h, dict) and h.get("axis") != "cooldown"}
        if (record.get("client_identity") or {}).get("claimed_conflicts_with_resolved"):
            cats.add("identity")
        record["categories"] = sorted(cats)
        _log_decision(record, decision)
        _notify(record, decision, {
            "user_id": getattr(user_api_key_dict, "user_id", None),
            "team_id": getattr(user_api_key_dict, "team_id", None),
            "key_alias": getattr(user_api_key_dict, "key_alias", None),
        })

        if decision.decision == "block":
            self._flush_blocked(data, record)
            raise _block_exception(decision)

        return data

    def _flush_blocked(self, data: dict, record: dict) -> None:
        """차단된 호출은 success 이벤트가 오지 않는다 — TTL 을 기다리지 않고 지금 쓴다."""
        call_id = str(data.get("litellm_call_id") or "")
        _pending.pop(call_id, None)
        record["status"] = "blocked"
        record["usage_source"] = "blocked_before_call"
        record.pop("decide_cost_usd", None)
        _write(record)

    def _record_request(self, user_api_key_dict: UserAPIKeyAuth, data: dict, call_type: str) -> dict:
        now = time.time()
        _start_sweeper()
        _sweep(now)

        headers = _headers(data)
        metadata = data.get("metadata") or {}

        # FR-008 + 2026-08-19 재설계 — 주체는 사다리로 얻고 출처를 정직하게 남긴다.
        # 인증 산출물(key 티어)이 자기신고 헤더보다 위다: UserAPIKeyAuth 는 게이트웨이가
        # 가상 키를 검증한 **뒤에** 채우는 값이라 호출자가 위조할 수 없다. 순서 결정은
        # 프록시 무관이라 argus_signals 에 있고, 여기는 LiteLLM 객체에서 재료만 뽑는다.
        team_id = getattr(user_api_key_dict, "team_id", None)
        api_key = getattr(user_api_key_dict, "api_key", None)
        raw_principal, principal_source = _principal_ladder(
            headers, PRINCIPAL_HEADER,
            user_id=getattr(user_api_key_dict, "user_id", None),
            team_id=team_id,
            key_alias=getattr(user_api_key_dict, "key_alias", None),
            api_key=api_key,
        )
        team_seed = _team_seed(team_id, principal_source, api_key)
        org_id = getattr(user_api_key_dict, "org_id", None)
        # G0: 이 principal 의 누적 지출(절대값)을 판정에 실어 보낸다 (budget 축 구동).
        # key 티어면 LiteLLM 이 영속한 spend, 아니면 탭 메모리 정산 — 출처를 레코드에 남긴다.
        _principal_hash = _sha(raw_principal, 16) if raw_principal else None
        _spend = _principal_spend(user_api_key_dict, principal_source)
        if _spend is None:
            _spend = _principal_settled.get(_principal_hash, 0.0) if _principal_hash else 0.0
            _budget_source = "tap_ledger"
        else:
            _budget_source = "litellm_spend"

        run_id = headers.get(RUN_ID_HEADER) or metadata.get("run_id")

        # spec 006f — 와이어를 먼저 한 모양으로 편다. 이 아래로는 Chat/Anthropic/Responses 를
        # 구별하지 않는다.
        norm = _normalize(call_type, data, headers)
        # 신원 설정은 **CR 에서 온다** (spec 006f identity 블록). 판정 임계와 같은 경로로
        # 내려오므로 여기도 파드 재시작 없이 바뀐다. 엔진이 없거나 못 읽으면 내장 기본값.
        # 파일 재읽기는 원래 로컬 decide() 안에서만 돌았다 — 원격 판정이 정상이면 기동 시점 값에
        # 굳어 있었다. 여기서 직접 따라간다 (5초 간격 stat 한 번).
        if hasattr(self._engine, "refresh_policy"):
            self._engine.refresh_policy()
        ident = self._identity_policy()
        conv_hash, conv_source = _conversation(
            data, headers, norm, raw_principal,
            trust_gateway=getattr(ident, "trust_gateway_session", True),
            session_headers=(getattr(ident, "conversation_headers", None) or None),
        )
        # 대화를 못 정했을 때 파생으로 되돌릴지도 정책이 정한다.
        if conv_source == "unavailable" and \
                getattr(ident, "on_unknown_conversation", "Skip") == "Derive" and norm["first_user"]:
            conv_hash = _sha(f"{raw_principal}|{model_of(data)}|{_sha(norm['first_user'], 16)}", 16)
            conv_source = "derived"

        tool_name, tool_hashes = _tool_args_value_hashes(norm["tool_name"], norm["raw_args"])
        # delta 부호는 대화 단위로 재야 다른 대화의 카운터와 안 섞인다.
        tool_facets = _tool_arg_facets(norm["tool_name"], norm["raw_args"],
                                       conv_hash or f"{raw_principal}|{data.get('model')}")
        model = data.get("model")

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1000):03d}Z",
            "status": "observed",
            "call_type": call_type,
            # ── identity ──
            "principal": _sha(raw_principal, 16) if raw_principal else None,
            "principal_source": principal_source,
            # 팀·조직 축 (2차 검증 Q1 "Agent 단위 비용 거버넌스" 재료). principal 이
            # user_id 로 갈라져도 같은 팀이면 여기서 다시 묶인다 — identity exhaustion
            # (1차 자백 ②: 키를 여러 개 쥔 에이전트) 의 부분 해소가 이 필드에서 나온다.
            # 해시만 남긴다 — 실값은 PII 일 수 있다 (user_email 과 같은 규칙).
            "team": _sha(team_seed, 16) if team_seed else None,
            "org": _sha(f"org:{org_id}", 16) if org_id else None,
            "run_id": run_id,
            # 대화 축. 출처를 같이 남긴다 — client > protocol > derived > unavailable.
            # `unavailable` 이면 판정 엔진이 대화 스코프 축을 **평가하지 않는다**
            # (가짜 버킷에 넣어 세는 것보다 안 세는 게 낫다).
            "conversation_hash": conv_hash or None,
            "conversation_source": conv_source,
            "namespace": headers.get("x-argus-namespace") or os.getenv("ARGUS_NAMESPACE"),
            "pod": headers.get("x-argus-pod") or os.getenv("ARGUS_POD"),
            # ── request shape ──
            # Callers only ever see the alias (e.g. "argus-mock-gpt"), so the real
            # provider is not derivable here — the success event fills it in from
            # custom_llm_provider. F0 confirmed 2026-08-09.
            "provider": None,
            "model": model,
            # 어느 와이어로 왔는가. 이게 없으면 "우리가 이 클라이언트를 실제로 보고 있나"를
            # 데이터로 답할 수 없다. unknown 이 쌓이면 그게 다음 작업 목록이다.
            "wire": norm["wire"],
            "wire_source": norm["wire_source"],
            "msg_count": norm["turn_count"],
            # Anthropic 의 system 은 최상위 필드라 turns 에 없다 — 크기에는 포함시킨다.
            "msg_bytes": norm["turn_bytes"],
            # FR-003 — hashes and lengths only. No prompt text is ever stored.
            "prefix_hash": norm["prefix_hash"],
            "tool_name": tool_name,
            "tool_args_value_hashes": tool_hashes,
            # spec 006e — 해시가 파괴한 정보(모양·변화부호)를 같이 남긴다.
            # 기존 필드는 그대로 둔다: 007 대시보드가 그걸로 만들고 있다.
            "tool_arg_facets": tool_facets,
            "stream": bool(data.get("stream")),
            # spec 006d — 협조 없이 얻은 신원 재료. 자기신고 헤더(X-Principal-Id)와 달리
            # 도구가 우리 규약을 몰라도 남는다. 없으면 빈 dict.
            "client_identity": {**_client_identity(headers, data),
                                **_claimed_principal_conflict(headers, PRINCIPAL_HEADER, principal_source,
                                                               raw_principal)},
            # 요청이 선언한 도구 목록의 형태. 무엇을 할 수 있는 에이전트인지 드러난다.
            # 이름만 남긴다 — 스키마 전문은 크고 민감할 수 있다.
            "declared_tools": sorted(
                t.get("function", {}).get("name") or t.get("name")
                for t in (data.get("tools") or [])
                if isinstance(t, dict)
            )[:30] or None,
            # ── response side, filled by async_log_success_event ──
            "tokens_in": None,
            "tokens_out": None,
            "tokens_cached": None,
            "tokens_reasoning": None,
            "cost_usd": None,
            "cost_source": None,
            "latency_ms": None,
            "usage_source": None,
            # ── 006b fills this; kept in the schema so the shape never changes ──
            "policy_hit": [],
        }
        # 판정에만 쓰는 누적값 (레코드 출력엔 안 남긴다 — 최종 cost_usd 는 success 가 채운다).
        record["decide_cost_usd"] = _spend
        record["budget_source"] = _budget_source

        call_id = data.get("litellm_call_id") or metadata.get("litellm_call_id")
        # 상관 키를 레코드에도 남긴다 (2026-08-19 남은 것) — 게이트웨이 로그·OTel 스팬과
        # 레코드를 한 호출 단위로 이어 붙일 수 있는 유일한 값이다.
        record["litellm_call_id"] = str(call_id) if call_id else None
        if DEBUG_DUMP:
            print(
                "[argus-observe] PRE_CALL"
                f" call_id={call_id!r} data_keys={sorted(data.keys())}"
                f" header_keys={sorted(headers.keys())}"
                f" principal_source={principal_source} run_id={run_id!r}",
                flush=True,
            )
        if call_id:
            _pending[str(call_id)] = (now + PENDING_TTL_S, record)
        else:
            # No correlation key -> write now rather than lose the row.
            record["usage_source"] = "uncorrelated"
            record.pop("decide_cost_usd", None)
            _write(record)
        return record

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            self._record_response(kwargs, response_obj, start_time, end_time)
        except Exception:  # noqa: BLE001
            _dropped["hook_error"] += 1

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """업스트림 실패 — 레코드를 error 로 표시하고 곧 내리게 한다.

        바로 쓰지 않는 이유: 라우터 재시도에서는 실패 이벤트 뒤에 같은 호출의 성공이 올 수 있다.
        마감만 FAILURE_GRACE_S 뒤로 당기고, 그 안에 성공이 오면 성공 레코드가 이긴다. 스위퍼가 내린다.
        차단(403)은 _flush_blocked 가 이미 썼으므로 여기서 찾지 못하고 지나간다.
        """
        try:
            call_id = str(kwargs.get("litellm_call_id") or "")
            entry = _pending.get(call_id)
            if entry is None:
                return
            deadline, record = entry
            record["status"] = "error"
            exc = kwargs.get("exception")
            payload = kwargs.get("standard_logging_object") or {}
            record["error"] = (f"{type(exc).__name__}: {exc}" if exc is not None
                               else str(payload.get("error_str") or "unknown"))[:200]
            try:
                record["latency_ms"] = int((end_time - start_time).total_seconds() * 1000)
            except (TypeError, AttributeError):
                record["latency_ms"] = None
            _pending[call_id] = (min(deadline, time.time() + FAILURE_GRACE_S), record)
        except Exception:  # noqa: BLE001
            _dropped["hook_error"] += 1

    def _record_response(self, kwargs, response_obj, start_time, end_time) -> None:
        call_id = str(kwargs.get("litellm_call_id") or "")
        payload = kwargs.get("standard_logging_object") or {}

        if DEBUG_DUMP:
            print(
                "[argus-observe] SUCCESS"
                f" call_id={call_id!r} kwargs_keys={sorted(kwargs.keys())}"
                f" slp_keys={sorted(payload.keys()) if payload else None}",
                flush=True,
            )

        entry = _pending.pop(call_id, None)
        record = entry[1] if entry else None
        if record is None:
            # Request side missing (hook order surprise) — still record what we have.
            record = {"status": "observed", "usage_source": "response_only", "policy_hit": []}
        if record.get("status") == "error":   # 재시도 끝에 성공 — 앞선 시도의 실패 표시를 걷어낸다
            record["status"] = "observed"
            record["retried_after_error"] = record.pop("error", None)

        usage = _usage_dict(response_obj) or {}
        breakdown = (payload.get("response_cost_breakdown") or payload.get("cost_breakdown") or {})
        details = usage.get("prompt_tokens_details") or {}
        out_details = usage.get("completion_tokens_details") or {}

        record["tokens_in"] = usage.get("prompt_tokens") or usage.get("input_tokens")
        record["tokens_out"] = usage.get("completion_tokens") or usage.get("output_tokens")
        # F0 item ③ — are cached / reasoning tokens broken out separately?
        record["tokens_cached"] = _get(details, "cached_tokens")
        record["tokens_reasoning"] = _get(out_details, "reasoning_tokens")
        record["cost_breakdown"] = breakdown or None

        cost = payload.get("response_cost")
        if cost is None:
            cost = kwargs.get("response_cost")
        # pricing.ts D-5: the provider's own measured cost wins; a computed table
        # is the fallback. OpenRouter returns it in usage.cost when the request
        # carries `usage: {include: true}` (verified 2026-08-09 on gpt-5.6-luna).
        # Both are kept so the two can be reconciled later — for luna they happened
        # to be identical, which is exactly why storing only one would be misleading.
        provider_cost = usage.get("cost")
        record["cost_usd_litellm"] = cost
        record["cost_details"] = usage.get("cost_details")
        if provider_cost is not None:
            record["cost_usd"] = provider_cost
            record["cost_source"] = "provider"
        else:
            record["cost_usd"] = cost
            record["cost_source"] = "litellm_costmap" if cost is not None else "unknown"
        record["provider"] = kwargs.get("custom_llm_provider") or payload.get("custom_llm_provider")
        record["usage_source"] = "success_event"

        try:
            record["latency_ms"] = int((end_time - start_time).total_seconds() * 1000)
        except (TypeError, AttributeError):
            record["latency_ms"] = None

        # G0: 탭 정산 — key 티어가 아닌 주체의 폴백 누적. key 티어는 LiteLLM DB 가 정산한다.
        if record.get("budget_source") != "litellm_spend":
            _budget_settle(record.get("principal"), record.get("cost_usd"))
        record.pop("decide_cost_usd", None)  # 내부 필드 — 레코드 출력엔 안 남긴다
        _write(record)


def _get(obj: Any, key: str):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _usage_dict(response_obj: Any) -> dict | None:
    usage = _get(response_obj, "usage")
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    for attr in ("model_dump", "dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return {k: getattr(usage, k) for k in dir(usage) if not k.startswith("_") and not callable(getattr(usage, k))}
