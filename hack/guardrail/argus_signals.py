# SPDX-License-Identifier: Apache-2.0
"""신호 추출 — **어느 프록시에도 묶이지 않는 부분.**

요청(헤더 + 본문)에서 판정에 필요한 신호를 뽑아 레코드 한 줄로 만든다.
LiteLLM 도, FastAPI 도, 어떤 웹 프레임워크도 import 하지 않는다.

## 왜 파일을 나눴나

지금은 LiteLLM 을 탭으로 쓰지만 자체 프록시나 다른 프록시로 갈 가능성을 열어둔다.
그때 **다시 써야 하는 것과 그대로 쓰는 것**이 파일 경계로 드러나 있어야 한다.

    argus_signals.py   ← 이 파일. 프록시 무관. 그대로 간다
    argus_decide.py    ← 판정 엔진. 프록시 무관 (litellm 참조 0건)
    argus_guardrail.py ← **LiteLLM 어댑터.** 프록시가 바뀌면 여기만 다시 쓴다

## 새 프록시 어댑터가 채워야 하는 것

    headers   dict[str, str]   소문자 키
    body      dict             요청 본문 그대로
    wire hint str | None       call_type 같은 힌트. 없으면 본문 모양으로 판별한다
    call_id   str              요청↔응답 상관 키
    → 그리고 Decision 을 받아 집행한다 (차단이면 그 프로토콜의 방식으로 거절)

그게 전부다. 이 파일의 함수들은 그 넷만 있으면 돈다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import Any

RECORDS_PATH_ENV = "ARGUS_RECORDS_PATH"
PREFIX_DEPTH = int(os.getenv("ARGUS_PREFIX_DEPTH", "2"))
PENDING_TTL_S = float(os.getenv("ARGUS_PENDING_TTL_S", "60"))

# ── 탐지 로그 ────────────────────────────────────────────────────────────────
# 판정 결과는 레코드(JSONL)에 다 남지만, 그건 파일을 꺼내 읽어야 보인다.
# "지금 뭐가 걸렸나"는 로그로 보여야 한다 — 데모에서도, 새벽 3시에도.
#
# soft 는 기본으로 안 찍는다. dev 실측에서 90콜 중 soft 히트가 36건이었다.
LOG_SOFT = os.getenv("ARGUS_LOG_SOFT", "0") == "1"
COOLDOWN_LOG_EVERY = int(os.getenv("ARGUS_COOLDOWN_LOG_EVERY", "10"))
_cooldown_seen: dict[str, int] = {}

# call_id -> (monotonic_deadline, partial_record)
_pending: dict[str, tuple[float, dict[str, Any]]] = {}
# NFR-002: 기록은 best-effort. 유실은 세되 절대 던지지 않는다.
_dropped = {"write_error": 0, "hook_error": 0, "no_success_event": 0, "decide_error": 0, "upstream_error": 0}
# 훅(이벤트 루프)과 스위퍼(데몬 스레드)가 같은 파일에 쓴다 — 줄이 섞이지 않게.
_write_lock = threading.Lock()


def _resolve_records_path() -> str:
    """파일 경로도 마운트 디렉터리도 받는다.

    차트가 PVC **마운트 지점**(디렉터리)을 주입한 적이 있는데, 디렉터리를 열면
    IsADirectoryError 가 나고 best-effort 라이터가 그걸 삼켜 **모든 레코드가 조용히
    유실됐다.** 수집 파이프라인에서 가장 나쁜 실패라 여기서 정규화한다.
    """
    p = os.getenv(RECORDS_PATH_ENV, "./records.jsonl")
    return os.path.join(p, "records.jsonl") if os.path.isdir(p) else p


RECORDS_PATH = _resolve_records_path()


def _sha(value: str, n: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


# ── 클라이언트 신원 재료 (spec 006d) ────────────────────────────────────────
#
# 우리는 지금 X-Principal-Id 하나만 본다. 그건 **호출자가 자기 입으로 말하는 값**이고,
# Claude Code·Codex 같은 실제 도구는 우리 헤더 규약을 모른다. 그런데 그 도구들도
# 자기를 식별할 재료를 이미 흘리고 있다 — 협조 없이 얻을 수 있는 것들이다.
#
# 값까지 기록하는 것은 **아래 목록에 있는 것만**이다. 나머지는 **키 이름만** 남긴다.
# 인증 헤더에 값이 딸려 나가는 사고를 구조적으로 막으려는 것이다.
_ID_HEADERS = frozenset({
    "user-agent",                    # 도구 자기소개. 거의 모든 클라이언트가 보낸다
    "traceparent", "tracestate",     # W3C Trace Context — 표준 상관 키
    "originator",                    # Codex CLI — 기본값 "codex_cli_rs" (소스 확인)
    "x-app",                         # 앱 구분
    "anthropic-version", "anthropic-beta",
    "openai-organization", "openai-project", "openai-beta",
    # 세션·대화 식별자. 도구가 **협조 없이 이미 주고 있는** 대화 축이다 —
    # 우리가 (principal, model, 첫 메시지) 로 파생하던 것보다 정확하다.
    #
    # 두 지배적 클라이언트 모두 **공식 계약으로** 준다:
    #   Claude Code  x-claude-code-session-id
    #     └ 문서: "Use it to aggregate all requests from one session
    #              **without parsing request bodies**"
    #   Codex CLI    session-id · thread-id · x-client-request-id(=thread id)
    #     └ codex-rs/codex-api/src/requests/headers.rs 의 build_session_headers()
    "x-session-id", "session-id", "thread-id", "x-client-request-id",
    "x-conversation-id", "x-claude-code-session-id",
    # 서브에이전트 — 부모가 자식을 띄우는 구조. 아직 판정 축은 아니지만 **지금 안 남기면
    # 나중에 과거 데이터로는 못 만든다**. 병렬 팬아웃과 폭주를 가르는 재료가 여기 있다.
    #   Claude Code: 문서상 "Use it with the session ID to attribute cost to parallel agents".
    #                ⚠️ 같은 문서가 "identifies an agent, not a person" 이라고 못박는다 —
    #                   principal 대용으로 쓰면 안 된다.
    #   Codex:       x-openai-subagent = review|compact|memory_consolidation|collab_spawn
    "x-claude-code-agent-id", "x-claude-code-parent-agent-id", "x-openai-subagent",
    "http-referer", "x-title",       # OpenRouter 관례
    # Stainless 생성 SDK(공식 OpenAI·Anthropic SDK) 계열.
    # x-stainless-retry-count 는 **클라이언트가 몇 번째 재시도인지 직접 알려주는 값**이라
    # 재시도 폭풍을 추론이 아니라 관측으로 잡을 수 있다.
    "x-stainless-lang", "x-stainless-package-version", "x-stainless-runtime",
    "x-stainless-runtime-version", "x-stainless-os", "x-stainless-arch",
    "x-stainless-retry-count", "x-stainless-timeout", "x-stainless-helper-method",
})

# 대화 축으로 쓸 세션 헤더 — **우선순위 순서다.** 앞에 있을수록 정확하다.
# 두 지배적 클라이언트가 공식 계약으로 주는 것을 맨 앞에 둔다.
_SESSION_HEADERS = (
    "x-claude-code-session-id",   # Claude Code (문서화된 계약)
    "session-id",                 # Codex CLI (build_session_headers)
    "thread-id",                  # Codex CLI — 세션보다 좁은 단위
    "x-session-id", "x-conversation-id",
)

# 값을 절대 기록하지 않는 것. 키 이름조차 남기지 않는다.
_SECRET_HEADERS = frozenset({
    "authorization", "x-api-key", "api-key", "cookie", "set-cookie",
    "proxy-authorization", "x-auth-token",
})

# 라우팅·전송 잡음. 신원 재료가 아니므로 미지 목록에서도 뺀다.
_BORING_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "connection", "content-length",
    "content-type", "host", "user", "expect", "te", "transfer-encoding",
})


# LiteLLM 이 master key 인증에 채워 넣는 자리표시자 user_id. 이 값이 key 티어로 승격되면
# master key 로 온 전원이 "한 명"이 되면서도 principal_source 는 key 를 자칭하게 된다 —
# 예전 api_key 폴백과 같은 병인데 라벨만 그럴듯해지는 것이 더 나쁘다. 부재로 취급한다.
_KEY_TIER_PLACEHOLDER_USER_IDS = frozenset({"default_user_id"})


def _principal_ladder(headers: dict, principal_header: str, *,
                      user_id: str | None = None, team_id: str | None = None,
                      key_alias: str | None = None,
                      api_key: str | None = None) -> tuple[str | None, str]:
    """주체 사다리 (2026-08-19 재설계) — (해시 시드, principal_source) 를 돌려준다.

    순서가 신뢰 순서다:

        key          인증 산출물(가상 키의 user_id > team_id > key_alias). 게이트웨이가
                     키를 검증한 뒤 채우는 값이라 호출자가 위조할 수 없다
        header       X-Principal-Id. 자기신고 — 규약을 아는 협조적 에이전트용
        api_key      마지막 폴백. 가상 키 DB 가 없으면 master key 하나라 호출자를
                     구별하지 못한다 (7/26 조사 §c) — 그래도 출처를 정직하게 남긴다
        unavailable  주체 스코프 축은 평가하지 않는다

    시드에 티어를 섞는 이유: 다른 티어에서 우연히 같은 문자열이 나와도
    (예: user_id 와 헤더가 같은 값) 해시가 갈라져야 "어떻게 알아냈는가"가 보존된다.
    """
    if user_id and user_id not in _KEY_TIER_PLACEHOLDER_USER_IDS:
        return f"key:user_id:{user_id}", "key:user_id"
    if team_id:
        return f"key:team_id:{team_id}", "key:team_id"
    if key_alias:
        return f"key:alias:{key_alias}", "key:alias"
    header_value = headers.get(principal_header)
    if header_value:
        return f"header:{header_value}", "header"
    if api_key:
        return f"api_key:{api_key}", "api_key"
    return None, "unavailable"


def _team_seed(team_id: str | None, principal_source: str, api_key: str | None) -> str | None:
    """팀 backstop(team_calls) 의 해시 시드.

    header 티어는 자기신고라 호출자가 헤더 값을 바꿀 때마다 새 주체·새 창을 받는다. 그런 키(user_id·team_id·alias
    없음)는 키 자체를 팀으로 묶는다 — 헤더를 돌려도 같은 키의 호출은 team_calls 에서 다시 모인다.
    """
    if team_id:
        return f"team:{team_id}"
    if principal_source == "header" and api_key:
        return f"key:{api_key}"
    return None


def _claimed_principal_conflict(headers: dict, principal_header: str,
                                principal_source: str, resolved_seed: str | None = None) -> dict:
    """자기신고 헤더가 인증 산출물에 밀렸을 때 **그 사실만** 남긴다 (Linear STP-32).

    "위조했는데 그대로 A" 가 제품의 가장 강한 장면인데, 헤더를 버리면 레코드엔 결과(principal=A)만
    남아 사후 감사가 안 된다. 원문은 싣지 않는다(US4/FR-003) — header 티어와 같은 시드로 해시해
    "그 값이 header 티어였다면 어떤 principal 이 됐을까"를 대조할 수 있게만 한다.
    헤더가 곧 principal 인 경우(header 티어)는 어긋난 게 아니라 잡음이므로 비운다.
    헤더 값이 키 티어 값과 같으면(협조적 에이전트가 자기 user_id 를 붙인 경우) 역시 비운다 (STP-41).
    """
    claimed = headers.get(principal_header)
    if not claimed or not principal_source.startswith("key:"):
        return {}
    if resolved_seed and resolved_seed.split(":", 2)[-1] == claimed:
        return {}
    return {"claimed_principal": _sha(f"header:{claimed}", 16),
            "claimed_conflicts_with_resolved": True}


def _client_identity(headers: dict, data: dict) -> dict:
    """협조 없이 얻을 수 있는 신원 재료를 모은다.

    세 층에서 긁는다:
      1. 알려진 식별 헤더 → 값까지
      2. 알려지지 않은 헤더 → **키 이름만** (다음에 무엇을 목록에 넣을지 알려준다)
      3. 프로토콜에 이미 있는 신원 필드 — OpenAI `user`, Anthropic `metadata.user_id`.
         커스텀 헤더가 전혀 필요 없는 자리인데 우리는 여태 안 보고 있었다.
    """
    # 접두어 규칙 — Claude Code 게이트웨이 문서가 명시적으로 요구한다:
    # *"Treat the headers and body fields as open lists, not closed ones. Claude Code gains
    #   capabilities over releases … occasionally new `anthropic-*` or `x-claude-code-*` headers."*
    # 고정 목록만 보면 다음 릴리스가 주는 것을 놓친다.
    known = {k: v for k, v in headers.items()
             if k in _ID_HEADERS or k.startswith(("x-claude-code-", "x-stainless-"))}
    unknown = sorted(
        k for k in headers
        if k not in known and k not in _SECRET_HEADERS and k not in _BORING_HEADERS
        and not k.startswith(("x-argus-", "x-principal", "x-run-"))
    )

    out: dict[str, Any] = {}
    if known:
        out["headers"] = known
    if unknown:
        out["unknown_header_keys"] = unknown[:40]

    # W3C traceparent: "00-<trace-id 32자>-<parent-id 16자>-<flags>"
    tp = headers.get("traceparent")
    if tp:
        parts = str(tp).split("-")
        if len(parts) >= 3:
            out["trace_id"], out["parent_span_id"] = parts[1], parts[2]

    # 프로토콜 신원 — 원문이 아니라 해시로. 사용자 식별자일 수 있다.
    meta = data.get("metadata") or {}
    for label, value in (("user", data.get("user")),
                         ("metadata.user_id", meta.get("user_id"))):
        if value:
            out.setdefault("protocol_user", {})[label] = _sha(str(value), 16)

    ua = headers.get("user-agent")
    if ua:
        # "claude-cli/2.1.220 (external, sdk-cli)" → client=claude-cli, version=2.1.220
        head = str(ua).split()[0]
        name, _, ver = head.partition("/")
        out["client"] = name[:40]
        if ver:
            out["client_version"] = ver[:24]

    # 클라이언트가 준 세션 식별자. 있으면 파생 대화 해시보다 이쪽이 정확하다.
    for k in _SESSION_HEADERS:
        if k in headers:
            out["client_session"] = _sha(str(headers[k]), 16)
            out["client_session_header"] = k
            break

    # 서브에이전트. 부모가 자식을 띄우면 한 사람의 작업이 여러 run 으로 흩어진다 —
    # 그게 정상 팬아웃인지 폭주인지는 **부모를 알아야** 구분된다. 지금은 기록만 한다.
    agent = {}
    if "x-claude-code-agent-id" in headers:
        agent["agent"] = _sha(str(headers["x-claude-code-agent-id"]), 16)
    if "x-claude-code-parent-agent-id" in headers:
        agent["parent"] = _sha(str(headers["x-claude-code-parent-agent-id"]), 16)
    if "x-openai-subagent" in headers:
        # Codex 는 ID 가 아니라 **종류**를 준다 (review|compact|memory_consolidation|collab_spawn).
        # 값이 열거형이라 해시하지 않는다 — 개인정보가 아니고, 종류별 분포가 그 자체로 신호다.
        agent["kind"] = str(headers["x-openai-subagent"])[:40]
    if agent:
        out["subagent"] = agent
    return out


# ── 인자 값의 모양 (spec 006e) ──────────────────────────────────────────────
#
# **해시 전에 모양을 뽑는다.** 값을 바로 해시하면 동등성 말고 아무것도 안 남는데,
# 실제 폭주는 "매번 조금씩 다른 요청"으로 나타난다(1차 검증 피드백). 완전 동일만
# 잡는 방식으로는 못 거른다.
#
# 로그 템플릿 추출(Drain)이 20년째 같은 문제를 푸는데, 그 문서가 못박는 한 줄이
# "템플릿 추출 정확도는 **마스킹 설정 품질**에 크게 좌우된다" 이고 마스킹은 파싱 **전에**
# 해야 한다. 우리는 그 단계를 통째로 건너뛰고 있었다.
#
# 모양은 내용이 아니라 형식이다 — 키 이름을 평문으로 두는 것과 같은 수준의 노출이다.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")
_DIGITS_RE = re.compile(r"\d+")
_ALPHA_RE = re.compile(r"[A-Za-z]+")


def _shape(v: Any) -> str:
    """값의 형식만 남긴다. 내용은 남기지 않는다.

        "config.yaml"            → "a.a"
        "file-42.txt"            → "a-#.a"
        3                        → "<int>"
        "2026-08-14T10:00:00Z"   → "<ts>"
        "550e8400-e29b-..."      → "<uuid>"
    """
    if isinstance(v, bool):
        return "<bool>"
    if isinstance(v, int):
        return "<int>"
    if isinstance(v, float):
        return "<float>"
    if v is None:
        return "<null>"
    if isinstance(v, (list, tuple)):
        return f"<list:{len(v)}>"
    if isinstance(v, dict):
        return "<obj:" + ",".join(sorted(map(str, v))[:6]) + ">"
    s = str(v)
    if _UUID_RE.match(s):
        return "<uuid>"
    if _TS_RE.match(s):
        return "<ts>"
    if _HEX_RE.match(s):
        return "<hex>"
    if len(s) > 120:
        return "<text>"
    # 숫자 런은 #, 알파 런은 a 로 접는다. 구두점 구조는 남긴다.
    return _ALPHA_RE.sub("a", _DIGITS_RE.sub("#", s))[:48]


# (conversation, tool, key) -> 마지막 수치값. 단조 증가(= 재시도 카운터)를 보려면
# 순서 구조가 필요한데 해시가 그걸 파괴한다. 값이 아니라 **부호만** 남기려고 최소 상태를 둔다.
_last_num: dict[tuple, float] = {}
_LAST_NUM_MAX = int(os.getenv("ARGUS_LAST_NUM_MAX", "5000"))


def _delta_sign(conv: str, tool: str, key: str, v: Any) -> str | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    k = (conv, tool, key)
    prev = _last_num.get(k)
    if len(_last_num) >= _LAST_NUM_MAX and k not in _last_num:
        _last_num.pop(next(iter(_last_num)), None)   # 상한. 무한히 늘면 게이트웨이가 죽는다
    _last_num[k] = float(v)
    if prev is None:
        return "first"
    d = float(v) - prev
    return "+1" if d == 1 else ("+" if d > 0 else ("-" if d < 0 else "0"))


def _tool_args_value_hashes(tool_name: str | None, raw_args) -> tuple[str | None, dict[str, str] | None]:
    """FR-002 — the single most important field in this spec.

    Split a tool call's argument JSON per key and hash ONLY the values. Key names
    stay in the clear (they are not sensitive); values become sha256[:12]. Hashing
    the whole argument blob would make the dedup work impossible: in two weeks we
    need a per-key variance table to see which argument actually changes between
    repeated calls (path stable + attempt incrementing = a retry loop).

    **와이어 파싱은 여기 없다.** 예전에는 이 함수가 `messages[].tool_calls` 를 직접
    뒤졌는데, 그건 OpenAI Chat Completions 모양이라 Anthropic Messages 와 Responses 에서
    아무것도 못 찾았다 — 그리고 못 찾으면 축이 조용히 꺼진다. 이제 `_normalize` 가
    와이어별로 (이름, 인자)를 뽑아 주고 여기는 **해싱 규칙만** 책임진다.
    """
    if not tool_name:
        return None, None
    if raw_args is None:
        return tool_name, None
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except (json.JSONDecodeError, TypeError):
        # Unparseable arguments are still a fact worth recording.
        return tool_name, {"_unparsed": _sha(str(raw_args), 12)}
    if not isinstance(args, dict):
        return tool_name, {"_scalar": _sha(json.dumps(args, sort_keys=True, default=str), 12)}
    return tool_name, {k: _sha(json.dumps(v, sort_keys=True, default=str), 12)
                       for k, v in args.items()}


def _tool_arg_facets(tool_name: str | None, raw_args, conv: str) -> dict | None:
    """spec 006e — 인자 키마다 **해시·모양·변화부호** 를 같이 남긴다.

    `_tool_args_value_hashes` 는 그대로 둔다 (007 대시보드가 그 필드로 만들고 있다).
    여기서 얻는 것을 나중에 못 만든다는 게 요점이다 — **지금 안 남기면 과거 데이터로는
    정규화 규칙을 영영 못 만든다.** 판정은 아직 안 바꾼다. 기록만 늘린다.

        "path":    {"h": "5e83…", "s": "a.a",   "d": null}     ← 값·모양 고정 = 정체성
        "attempt": {"h": "6b86…", "s": "<int>", "d": "+1"}     ← 값만 변동 + 1씩 증가 = 카운터
    """
    if not tool_name or raw_args is None:
        return None
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(args, dict):
        return None
    return {
        k: {
            "h": _sha(json.dumps(v, sort_keys=True, default=str), 12),
            "s": _shape(v),
            "d": _delta_sign(conv, str(tool_name), k, v),
        }
        for k, v in args.items()
    }


# ── 와이어 정규화 (spec 006f) ────────────────────────────────────────────────
#
# 우리는 세 가지 와이어를 받는다. 예전 파서는 그중 하나(OpenAI Chat Completions)만
# 알았고, 나머지 둘에서 도구 호출을 **하나도 못 뽑았다.** 못 뽑으면 판정 엔진의
# 지문이 None 이 되고 `duplicate_calls` 축이 평가 자체를 안 한다 — 에러도 로그도 없다.
#
# 실측(2026-08-16, `hack/analyze/wire_probe.py` + 실물 CLI):
#
#   Claude Code 2.1.220  POST /v1/messages?beta=true  stream=true  도구 27개 선언
#                        도구 호출이 messages[].content[].tool_use 에 있다 → 못 뽑았다
#   Codex / Agents SDK   POST /v1/responses           도구 호출이 input[].function_call
#   LangChain 계열       POST /v1/chat/completions    messages[].tool_calls  → 유일하게 됐다
#
# 판정 엔진(argus_decide.py)은 **와이어를 모른다.** 그 경계가 이 설계의 강점이라
# 정규화는 전부 여기서 끝낸다.

# LiteLLM CallTypes → 와이어. dev 파드의 litellm/types/utils.py 에서 확인한 이름들이다.
_WIRE_BY_CALL_TYPE = {
    "completion": "chat", "acompletion": "chat",
    "text_completion": "chat", "atext_completion": "chat",
    "anthropic_messages": "anthropic", "aanthropic_messages": "anthropic",
    "responses": "responses", "aresponses": "responses",
    "embedding": "embedding", "aembedding": "embedding",
}


# LiteLLM 의 제공자 pass-through(`/bedrock/model/{id}/converse` 등). 가드레일은 이 call_type 으로 불리고
# 제공자 원문 본문은 data["data"] 에 들어 있다 (litellm 1.96.0 pass_through_endpoints/llm_passthrough_endpoints.py
# bedrock_llm_proxy_route → data["data"] = request_body). 이걸 풀지 않으면 wire=unknown 이라 도구·크기를 못 본다.
_PASSTHROUGH_CALL_TYPES = frozenset({"allm_passthrough_route", "pass_through_endpoint"})


def _unwrap_passthrough(call_type: str, data: dict) -> tuple[dict, str | None]:
    """pass-through 면 (제공자 원문 본문, 와이어) — 아니면 (data, None)."""
    if call_type not in _PASSTHROUGH_CALL_TYPES or not isinstance(data.get("data"), dict):
        return data, None
    inner = data["data"]
    endpoint = str(data.get("endpoint") or "")
    if "converse" in endpoint:
        return inner, "bedrock"
    # InvokeModel — 모델마다 본문이 다르다. Anthropic 모델은 Messages 형식(anthropic_version)이다.
    if "anthropic_version" in inner or isinstance(inner.get("messages"), list):
        return inner, _sniff_wire(inner) if "anthropic_version" not in inner else "anthropic"
    return inner, "unknown"


def _sniff_wire(data: dict) -> str:
    """call_type 이 모르는 값일 때 본문 모양으로 판별한다.

    call_type 을 못 믿는 게 아니라, LiteLLM 이 타입을 늘릴 때 우리가 조용히
    `unknown` 으로 떨어지는 것을 막으려는 것이다. 어느 쪽이 판별했는지는 기록에 남긴다.
    """
    if isinstance(data.get("input"), (list, str)) and "messages" not in data:
        return "responses"
    msgs = data.get("messages")
    if isinstance(msgs, list):
        # Anthropic 은 system 이 최상위 필드이고 content 가 블록 배열이다.
        if isinstance(data.get("system"), (str, list)):
            return "anthropic"
        for m in msgs:
            if isinstance(m, dict) and isinstance(m.get("content"), list):
                for blk in m["content"]:
                    if isinstance(blk, dict) and blk.get("type") in (
                            "tool_use", "tool_result", "input_text", "text"):
                        return "anthropic"
        return "chat"
    return "unknown"


def _text_of(content) -> str:
    """어느 와이어든 사람이 쓴 텍스트만 뽑는다 (파생 대화 키용)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for blk in content:
            # Bedrock Converse 의 텍스트 블록은 type 없이 {"text": ...} 다
            if isinstance(blk, dict) and (blk.get("type") in ("text", "input_text")
                                          or ("type" not in blk and isinstance(blk.get("text"), str))):
                out.append(str(blk.get("text") or ""))
            elif isinstance(blk, str):
                out.append(blk)
        return "".join(out)
    return ""


_TOOL_CALL_BLOCKS = frozenset({"tool_use", "function_call"})


def _is_assistant_text_turn(item: object) -> bool:
    """모델이 도구를 부르지 않고 글로 답한 턴인가 — 그 뒤로는 앞선 도구 루프가 끝난 것이다."""
    if not isinstance(item, dict) or item.get("role") != "assistant":
        return False
    if item.get("tool_calls"):
        return False
    content = item.get("content")
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and (blk.get("type") in _TOOL_CALL_BLOCKS or "toolUse" in blk):
                return False
    return True


def _last_tool_call(wire: str, data: dict) -> tuple[str | None, object]:
    """**지금 진행 중인** 도구 루프의 마지막 도구 호출 (이름, 원시 인자). 와이어마다 있는 곳이 다르다.

    모델이 도구 없이 글로 답한 assistant 턴이 그 뒤에 있으면 루프가 끝난 것으로 보고 None 이다 — 안 그러면
    도구를 한 번 쓴 대화에서 이어지는 평범한 질문마다 같은 옛 호출이 지문으로 나와 duplicate_calls 가 오탐한다(K1).
    ⚠️ user 턴을 경계로 삼으면 안 된다: 매 스텝 user 메시지로 재촉하는 루프(샘플앱·오케스트레이터)의 도구 호출이
    전부 사라진다 — 2026-10-04 dev 실모델에서 그렇게 duplicate_calls 가 통째로 꺼졌다.
    """
    msgs = data.get("messages") or []
    if wire == "chat":
        for msg in reversed(msgs):
            if _is_assistant_text_turn(msg):
                return None, None
            for call in (msg.get("tool_calls") or []) if isinstance(msg, dict) else []:
                fn = call.get("function") or {}
                return fn.get("name"), fn.get("arguments")
        return None, None

    if wire == "anthropic":
        # assistant 턴의 content 블록 안에 tool_use 로 들어온다 (실측 확인).
        for msg in reversed(msgs):
            if _is_assistant_text_turn(msg):
                return None, None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for blk in reversed(content):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    return blk.get("name"), blk.get("input")
        return None, None

    if wire == "bedrock":
        # Bedrock Converse — assistant 턴 content 에 {"toolUse": {"toolUseId", "name", "input"}}
        for msg in reversed(msgs):
            if _is_assistant_text_turn(msg):
                return None, None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for blk in reversed(content):
                tu = blk.get("toolUse") if isinstance(blk, dict) else None
                if isinstance(tu, dict):
                    return tu.get("name"), tu.get("input")
        return None, None

    if wire == "responses":
        inp = data.get("input")
        if not isinstance(inp, list):
            return None, None
        for item in reversed(inp):
            if not isinstance(item, dict):
                continue
            if _is_assistant_text_turn(item):
                return None, None
            if item.get("type") == "function_call":
                return item.get("name"), item.get("arguments")
            content = item.get("content")
            if isinstance(content, list):
                for blk in reversed(content):
                    if isinstance(blk, dict) and blk.get("type") in ("tool_use", "function_call"):
                        return blk.get("name"), blk.get("arguments") or blk.get("input")
        return None, None

    return None, None


def _turns_of(wire: str, data: dict) -> list:
    if wire == "responses":
        inp = data.get("input")
        if isinstance(inp, list):
            return inp
        return [{"role": "user", "content": inp}] if isinstance(inp, str) else []
    msgs = data.get("messages")
    return msgs if isinstance(msgs, list) else []


def _first_user_text(wire: str, turns: list) -> str:
    for t in turns:
        if not isinstance(t, dict):
            continue
        if t.get("role") == "user" or t.get("type") == "message" and t.get("role") == "user":
            txt = _text_of(t.get("content"))
            if txt:
                return txt
    return ""


def _normalize(call_type: str, data: dict, headers: dict) -> dict:
    """어느 와이어든 판정에 필요한 것만 뽑아 한 모양으로 돌려준다.

    절대 예외를 내지 않는다 — 모르는 모양이면 `wire="unknown"` 으로 정직하게 비운다.
    """
    data, pt_wire = _unwrap_passthrough(call_type or "", data)
    wire = pt_wire or _WIRE_BY_CALL_TYPE.get(call_type or "")
    wire_source = "passthrough" if pt_wire else "call_type"
    if wire is None:
        wire, wire_source = _sniff_wire(data), "shape"

    turns = _turns_of(wire, data)
    tool_name, raw_args = _last_tool_call(wire, data)

    # Anthropic 의 system 은 최상위 필드다. 안 세면 6KB 짜리 시스템 프롬프트가
    # 통째로 안 보인다 (Claude Code 실측 6.4KB).
    sizing = list(turns)
    if wire in ("anthropic", "bedrock") and data.get("system") is not None:
        sizing = [{"role": "system", "content": data["system"]}] + sizing

    return {
        "wire": wire,
        "wire_source": wire_source,
        "turns": turns,
        "turn_count": len(turns),
        "turn_bytes": len(json.dumps(sizing, default=str)),
        "prefix_hash": _sha(json.dumps(sizing[:PREFIX_DEPTH], sort_keys=True, default=str), 16),
        "tool_name": tool_name,
        "raw_args": raw_args,
        "first_user": _first_user_text(wire, turns),
        # Responses 에서 store=true + previous_response_id 면 클라이언트가 **꼬리만** 보낸다.
        # 그러면 첫 user 메시지에서 파생한 대화 키가 턴마다 달라진다 — 파생값을 믿으면 안 된다.
        "truncated": bool(data.get("previous_response_id")),
    }


def model_of(data: dict) -> str | None:
    if data.get("model"):
        return data["model"]
    # Bedrock pass-through 직접 모델 — 모델 ID 는 경로에만 있다 (model/{id}/converse)
    ep = str(data.get("endpoint") or "")
    if ep.startswith("model/") or ep.startswith("/model/"):
        parts = ep.strip("/").split("/")
        return parts[1] if len(parts) > 2 else None
    return None


def _conversation(data: dict, headers: dict, norm: dict, raw_principal: str | None,
                  *, trust_gateway: bool = True,
                  session_headers: tuple[str, ...] | None = None) -> tuple[str, str]:
    """대화 키와 그 **출처**. 출처를 같이 돌려주는 게 요점이다.

    파생 해시가 아니라 클라이언트가 준 진짜 세션 ID 를 쓸 수 있으면 그게 정확하다 —
    첫 메시지가 같은 별개 대화를 잘못 합치는 문제가 사라진다 (006d findings §3).
    실측: Claude Code 의 `x-claude-code-session-id` 는 호출 간 유지된다.

    아무것도 못 뽑으면 **`unavailable` 로 정직하게 말한다.** 예전에는 조용히
    `sha(principal|model|None)` 하나로 합쳐서, 무관한 작업이 서로의 반복으로 세어질
    수 있었다. 모르는 것은 세지 않는 편이 낫고, 그 사실이 기록에 남아야
    "왜 안 걸렸나"를 나중에 답할 수 있다.
    """
    model = data.get("model")

    # ① 게이트웨이가 이미 정규화한 세션 — **직접 파싱하기 전에 이걸 먼저 본다.**
    #
    # LiteLLM 이 `x-<vendor>-session-id` 를 훑어 `data["litellm_session_id"]` 에 넣어준다.
    # 그 함수의 독스트링이 `x-claude-code-session-id` 를 예시로 직접 명시한다
    # (litellm/proxy/litellm_pre_call_utils.py::get_chain_id_from_headers).
    # 우리가 열거하지 못한 벤더까지 덮어 주고, LiteLLM 이 좋아지면 같이 좋아진다.
    #
    # ⚠️ 다만 그 함수는 `x-litellm-trace-id` 를 **최우선**으로 쓴다. trace 는 요청마다
    # 바뀔 수 있고, 그러면 대화가 매 호출 쪼개진다. 그래서 신뢰 여부를 정책으로 뺀다
    # (`identity.trustGatewaySession`). 기본은 신뢰 — 실측에서 Claude Code 는 유지된다.
    if trust_gateway:
        v = data.get("litellm_session_id")
        if not isinstance(v, str) or not v:
            meta = data.get("metadata") or {}
            v = meta.get("litellm_session_id") if isinstance(meta, dict) else None
        if isinstance(v, str) and v:
            return _sha(f"{raw_principal}|{v}", 16), "gateway"

    # ② 클라이언트가 준 세션 ID — 게이트웨이가 못 잡는 것을 우리가 잡는다.
    # 실측: Codex 의 `session-id`/`thread-id` 는 LiteLLM 정규식(`x-<vendor>-session-id`)에
    # 안 걸린다. 이 단계가 없으면 Codex 는 대화 축을 통째로 잃는다.
    for h in session_headers or _SESSION_HEADERS:
        v = headers.get(h)
        if v:
            return _sha(f"{raw_principal}|{v}", 16), "client"

    # ③ 프로토콜이 규정한 대화 객체 (Responses conversations)
    v = data.get("conversation")
    if isinstance(v, str) and v:
        return _sha(f"{raw_principal}|{v}", 16), "protocol"

    # ③ 파생 — 첫 user 메시지에서. 꼬리만 오는 요청에서는 못 쓴다
    if norm["first_user"] and not norm["truncated"]:
        return _sha(f"{raw_principal}|{model}|{_sha(norm['first_user'], 16)}", 16), "derived"

    return "", "unavailable"


# ── OTel GenAI 표준 뷰 ──────────────────────────────────────────────────────
#
# 표준에 존재하는 개념은 표준 이름으로도 남긴다 (semantic-conventions-genai,
# docs/research/2026-08-18_genai-semconv-identity-and-logging.md 의 매핑표가 정본).
# 내부 필드는 당분간 병행한다 — 007 대시보드 등 기존 소비자가 그걸 읽고 있고,
# 표준 자체가 아직 Development(릴리스 0개)라 이름이 또 바뀔 수 있다.
#
# 표준이 문장으로 강제하는 불변식 두 개를 여기서 코드로 강제한다:
#   · conversation.id — "식별자가 없으면 채우지 말라. request content 의 해시를
#     폴백으로 쓰지 말라." → derived/unavailable 등급은 절대 이 키를 만들지 않는다.
#   · conversation.compacted — "확실할 때만 true. false 는 설정하지 말라."
# ARGUS 고유 개념(판정·지문·principal)은 넣지 않는다 — gen_ai.* 네임스페이스에
# 커스텀 속성을 두지 말라는 것도 표준 규칙이다.

# 대화 키를 gen_ai.conversation.id 로 내보내도 되는 출처 등급.
_CONV_EXPORTABLE = ("gateway", "client", "protocol")

# LiteLLM call_type → gen_ai.operation.name (표준 열거값: chat, text_completion,
# embeddings, …). 와이어가 달라도 연산은 같다 — anthropic/responses 도 chat 이다.
_OPERATION_BY_CALL_TYPE = {
    "completion": "chat", "acompletion": "chat",
    "anthropic_messages": "chat", "aanthropic_messages": "chat",
    "responses": "chat", "aresponses": "chat",
    "text_completion": "text_completion", "atext_completion": "text_completion",
    "embedding": "embeddings", "aembedding": "embeddings",
}


def _genai_view(record: dict) -> dict:
    """레코드의 표준 번역 블록. 키는 `gen_ai.` 접두어를 뗀 표준 속성명이다."""
    out = {}
    op = _OPERATION_BY_CALL_TYPE.get(str(record.get("call_type")))
    if op:
        out["operation.name"] = op
    if record.get("model"):
        out["request.model"] = record["model"]
    if record.get("provider"):
        out["provider.name"] = record["provider"]
    if record.get("conversation_source") in _CONV_EXPORTABLE and record.get("conversation_hash"):
        out["conversation.id"] = record["conversation_hash"]
    if record.get("truncated"):
        out["conversation.compacted"] = True
    if record.get("tool_name"):
        out["tool.name"] = record["tool_name"]
    if record.get("stream"):
        out["request.stream"] = True
    for ours, std in (("tokens_in", "usage.input_tokens"),
                      ("tokens_out", "usage.output_tokens"),
                      ("tokens_cached", "usage.cache_read.input_tokens"),
                      ("tokens_reasoning", "usage.reasoning.output_tokens")):
        v = record.get(ours)
        if v is not None:
            out[std] = v
    return out


def _write(record: dict) -> None:
    try:
        record["gen_ai"] = _genai_view(record)
    except Exception:  # noqa: BLE001 — NFR-002: 번역 블록 때문에 레코드를 잃지 않는다
        pass
    try:
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        with _write_lock, open(RECORDS_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        # NFR-002: never fail the LLM call over a record. But a silent drop is
        # how a collection pipeline ends up with an empty file and nobody
        # noticing, so make the first failure loud and then rate-limit.
        _dropped["write_error"] += 1
        n = _dropped["write_error"]
        if n == 1 or n % 100 == 0:
            print(
                f"[argus-observe] RECORD WRITE FAILED (n={n}) path={RECORDS_PATH!r}: {exc}",
                flush=True,
            )


def _sweep(now: float) -> None:
    """Flush request-side records whose success event never arrived.

    If F0 ② turns out to be unavailable, this is what keeps collection working:
    every call still produces a line, just with usage fields left null and
    usage_source = "missing".
    """
    # 스냅샷을 떠서 돈다 — 스위퍼 스레드가 도는 동안 이벤트 루프가 _pending 을 바꿀 수 있다.
    for call_id, (deadline, _) in list(_pending.items()):
        if deadline > now:
            continue
        entry = _pending.pop(call_id, None)
        if entry is None:
            continue
        record = entry[1]
        if record.get("status") == "error":
            record["usage_source"] = "failure_event"
            _dropped["upstream_error"] += 1
        else:
            record["usage_source"] = "missing"
            _dropped["no_success_event"] += 1
        _write(record)


SWEEP_EVERY_S = float(os.getenv("ARGUS_SWEEP_EVERY_S", "5"))
_sweeper_started = False


def _start_sweeper() -> None:
    """유휴 게이트웨이에서도 밀린 레코드를 내린다. 예전엔 다음 요청이 올 때만 쓸어서, 호출이 끊기면
    마지막 실패 레코드가 메모리에 머물렀다(2026-09-24 kind). 첫 요청 때 한 번 띄운다 — import 만으로는 안 띄운다."""
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True

    def loop():
        while True:
            time.sleep(SWEEP_EVERY_S)
            try:
                _sweep(time.time())
            except Exception:  # noqa: BLE001 — 스위퍼가 죽으면 다시 '다음 요청 때만'으로 돌아간다
                _dropped["hook_error"] += 1

    threading.Thread(target=loop, name="argus-sweeper", daemon=True).start()


# ── 알림 (거버넌스) ──────────────────────────────────────────────────────────
#
# 탐지 사실이 로그 한 줄·파일에만 있으면 아무도 모른다 — "보는 것 ≠ 막는 것" 이전에
# "아무도 안 보고 있다"가 먼저다. 조직이 이미 쓰는 채널(Slack·webhook)로 **트립 순간에 한 번** 보낸다.
#
# 모양은 LiteLLM 알림(general_settings.alerting, webhook 의 event/event_group/event_message)을
# 따른다 — 같은 채널에 둘이 섞여 들어와도 운영자가 같은 방식으로 읽는다.
# 판정 경로를 막지 않는다: 전송은 데몬 스레드, 실패는 세기만 한다.
ALERT_URL = os.getenv("ARGUS_ALERT_WEBHOOK_URL", "")
# other 를 기본에 넣는다 — 새 축을 추가하고 범주 표를 잊어도 그 축의 트립이 **조용히 빠지지 않게**.
ALERT_CATEGORIES = {c for c in os.getenv("ARGUS_ALERT_CATEGORIES", "cost,behavior,volume,identity,other").split(",") if c}
# soft 도 알릴 범주 — 기본은 비용만. 예산 soft = LiteLLM 의 85%·95% 경보에 해당하는 "사전 경보".
ALERT_SOFT_CATEGORIES = {c for c in os.getenv("ARGUS_ALERT_SOFT_CATEGORIES", "cost").split(",") if c}
ALERT_DEDUP_S = float(os.getenv("ARGUS_ALERT_DEDUP_S", "300"))
# 끈적한 범주 — 누적 지출(budget_usd)은 줄지 않아 한 번 넘으면 쿨다운(30초)마다 다시 트립한다.
# 5분 창이면 그 주체가 호출하는 한 5분마다 같은 알림이 온다. 비용은 하루에 한 번이면 충분하다.
ALERT_DEDUP_STICKY_S = float(os.getenv("ARGUS_ALERT_DEDUP_STICKY_S", "86400"))
ALERT_STICKY_CATEGORIES = {c for c in os.getenv("ARGUS_ALERT_STICKY_CATEGORIES", "cost").split(",") if c}
ALERT_TIMEOUT_S = float(os.getenv("ARGUS_ALERT_TIMEOUT_S", "3"))
# 알림에 싣는 신원: hash(해시만) · labels(+team_id·key_alias, 기본) · full(+user_id — 이메일일 수 있다)
# 실 API 키는 어떤 모드에서도 싣지 않는다. 레코드(records.jsonl)는 이 설정과 무관하게 해시만이다.
ALERT_IDENTITY = os.getenv("ARGUS_ALERT_IDENTITY", "labels")
_alert_last: dict[tuple, float] = {}


def _alert_events(record: dict, decision) -> list[dict]:
    """이 판정이 알릴 일인가 — 순수 함수 (중복 억제·전송과 분리해 테스트한다).

    트립 = 엔진의 폭주 판정(would_block) + 쿨다운 아님 — Go metrics.observe 와 같은 정의.
    범주는 hard 축의 것, hard 가 없으면(soft_escalation: soft 2개로 트립) soft 축의 것.
    쿨다운 중에는 hit 가 cooldown 하나뿐이라 같은 폭주를 30초마다 다시 알리지 않는다.
    트립이 아닌 soft 는 ALERT_SOFT_CATEGORIES 만 경고로.
    """
    all_hits = decision.policy_hit or []
    cooling = any(h.get("axis") == "cooldown" for h in all_hits)
    hits = [h for h in all_hits if h.get("axis") != "cooldown"]
    by_cat: dict[str, list[dict]] = {}
    for h in hits:
        by_cat.setdefault(h.get("category") or "other", []).append(h)
    hard_cats = [c for c, hs in by_cat.items() if any(h.get("level") == "hard" for h in hs)]
    trip_cats = []
    if decision.would_block and not cooling:
        trip_cats = hard_cats or list(by_cat)
    out = []
    blocked = decision.decision == "block"
    for cat, hs in by_cat.items():
        if cat in trip_cats:
            out.append({"event": "runaway_blocked" if blocked else "runaway_detected", "category": cat,
                        "severity": "critical", "hits": [h for h in hs if h.get("level") == "hard"] or hs})
        elif cat not in hard_cats and cat in ALERT_SOFT_CATEGORIES:
            out.append({"event": f"{cat}_warning", "category": cat, "severity": "warning", "hits": hs})
    ci = record.get("client_identity") or {}
    if ci.get("claimed_conflicts_with_resolved"):
        out.append({"event": "identity_spoof_attempt", "category": "identity", "severity": "warning", "hits": []})
    return [e for e in out if e["category"] in ALERT_CATEGORIES]


def _alert_payload(ev: dict, record: dict, decision, ident: dict) -> dict:
    axes = ", ".join(f"{h['axis']}={h['observed']:.6g}>={h['threshold']:.6g}" for h in ev["hits"])
    # 메시지에 찍는 "누가" 도 신원 모드를 따른다 — Slack 은 본문이 {"text"} 하나라 여기서 새면 다 샌다.
    if ALERT_IDENTITY == "full":
        who = ident.get("user_id") or ident.get("key_alias") or ident.get("team_id") or record.get("principal")
    elif ALERT_IDENTITY == "labels":
        who = ident.get("key_alias") or ident.get("team_id") or record.get("principal")
    else:
        who = record.get("principal")
    action = {"runaway_blocked": "차단함", "runaway_detected": f"탐지 ({decision.control_level} — 통과시킴)"}.get(
        ev["event"], "경고")
    label = {"cost": "이상 비용", "behavior": "이상 행동", "volume": "이상 사용량",
             "identity": "신원 위조 시도"}.get(ev["category"], ev["category"])
    msg = f"[ARGUS] {label} · {action} · {who}" + (f" · {axes}" if axes else "")
    payload = {
        "event": ev["event"], "event_group": "argus", "event_message": msg,
        "category": ev["category"], "severity": ev["severity"],
        "control_level": decision.control_level, "decision": decision.decision,
        "decision_id": decision.decision_id, "policy_hit": ev["hits"],
        "principal": record.get("principal"), "principal_source": record.get("principal_source"),
        "team": record.get("team"), "run_id": record.get("run_id"), "model": record.get("model"),
        "ts": record.get("ts"), "policy_source": decision.policy_source,
    }
    if ALERT_IDENTITY in ("labels", "full"):
        payload.update(team_id=ident.get("team_id"), key_alias=ident.get("key_alias"))
    if ALERT_IDENTITY == "full":
        payload["user_id"] = ident.get("user_id")
    return payload


def _send_alert(url: str, payload: dict) -> None:
    import threading
    import urllib.request

    # Slack incoming webhook 은 {"text": ...} 만 받는다. 그 외는 LiteLLM 식 JSON 그대로.
    body = {"text": payload["event_message"]} if "hooks.slack.com" in url else payload

    def _go():
        try:
            req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False, default=str).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=ALERT_TIMEOUT_S):  # noqa: S310 — 운영자가 지정한 URL
                pass
            _dropped["alert_sent"] = _dropped.get("alert_sent", 0) + 1
        except Exception as exc:  # noqa: BLE001 — 알림 실패가 판정·호출을 깨지 않는다
            n = _dropped["alert_error"] = _dropped.get("alert_error", 0) + 1
            if n == 1 or n % 100 == 0:
                print(f"[argus] 알림 전송 실패 (n={n}): {type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=_go, daemon=True).start()


def _notify(record: dict, decision, ident: dict | None = None, now: float | None = None) -> list[dict]:
    """알릴 일이면 보낸다. 보낸(보내려 한) 페이로드를 돌려준다 — 테스트·로그용. 절대 던지지 않는다."""
    try:
        if not ALERT_URL:
            return []
        now = time.time() if now is None else now
        sent = []
        for ev in _alert_events(record, decision):
            key = (record.get("principal"), ev["category"], ev["event"])
            window = ALERT_DEDUP_STICKY_S if ev["category"] in ALERT_STICKY_CATEGORIES else ALERT_DEDUP_S
            if now - _alert_last.get(key, -1e18) < window:
                continue  # 같은 주체·범주·사건은 창 안에서 한 번만 — 레플리카마다 따로 센다
            _alert_last[key] = now
            payload = _alert_payload(ev, record, decision, ident or {})
            _send_alert(ALERT_URL, payload)
            sent.append(payload)
        if len(_alert_last) > 10_000:  # 상한 — 오래된 것부터 버린다
            for k, _ in sorted(_alert_last.items(), key=lambda kv: kv[1])[:5_000]:
                _alert_last.pop(k, None)
        return sent
    except Exception:  # noqa: BLE001
        _dropped["alert_error"] = _dropped.get("alert_error", 0) + 1
        return []


def _log_decision(record: dict, decision) -> None:
    """탐지·차단을 `kubectl logs` 에서 보이게 한 줄로 남긴다.

    남기는 것은 넷이다 — **누가 · 무슨 축이 · 얼마나 넘겨서 · 막았는가 안 막았는가.**
    막지 않은 경우(Observe)도 반드시 찍는다. 그게 이 사이클의 산출물이고,
    "판정은 도는데 아무 일도 안 일어난다"와 "판정이 안 돈다"를 구별해 준다.

    실패해도 절대 던지지 않는다 — 로그 한 줄 때문에 남의 LLM 호출을 깨뜨리지 않는다.
    """
    try:
        hits = decision.policy_hit or []
        if not hits:
            return
        hard = [h for h in hits if h.get("level") == "hard"]
        blocked = decision.decision == "block"

        # 쿨다운 유지 = 이미 찍은 폭주의 연장선. 첫 줄과 N 콜마다만.
        only_cooldown = bool(hard) and all(h.get("axis") == "cooldown" for h in hard)
        if only_cooldown:
            key = str(record.get("principal") or "-")
            n = _cooldown_seen.get(key, 0) + 1
            _cooldown_seen[key] = n
            if n != 1 and n % COOLDOWN_LOG_EVERY != 0:
                return
        elif hard:
            _cooldown_seen.pop(str(record.get("principal") or "-"), None)

        if not hard and not (LOG_SOFT and hits):
            return

        shown = hard or hits
        axes = " ".join(
            f"{h['axis']}/{h['level']}={h['observed']:.6g}>={h['threshold']:.6g}"
            + (f"({h['detail']})" if h.get("detail") else "")
            for h in shown
        )
        if blocked:
            head = "차단"
        elif decision.would_block:
            head = f"탐지 (would_block · {decision.control_level} — 통과시킴)"
        else:
            head = "관측"
        extra = f" cooldown_n={_cooldown_seen.get(str(record.get('principal') or '-'))}" if only_cooldown else ""
        print(
            f"[argus] {head}"
            f" principal={record.get('principal')} run={record.get('run_id')}"
            f" client={(record.get('client_identity') or {}).get('client')}"
            f" | {axes}"
            f" | id={decision.decision_id} policy={decision.policy_source}{extra}",
            flush=True,
        )
    except Exception:  # noqa: BLE001 — 로그가 호출을 깨뜨리는 일은 없어야 한다
        pass
