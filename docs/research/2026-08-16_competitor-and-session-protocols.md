# 경쟁 현황 + 클라이언트 세션 전달 규약 (2026-08-16)

**조사자** @song2h · **선행** [2026-06-13 landscape-refresh](./2026-06-13_landscape-refresh.md) ·
[2026-07-02 framework-coverage](./2026-07-02_framework-coverage.md)

> **한 줄**: 우리가 쓰는 탐지 기법(지문 + 슬라이딩 윈도우 + 429 + 쿨다운)을 **똑같이 광고하는
> 상용 게이트웨이가 생겼다.** 다만 아무도 **숫자를 공개하지 않는다.** 그리고 시장 지배적
> 클라이언트 두 개는 세션·서브에이전트 식별자를 **공식 계약으로** 주고 있었다 —
> 우리는 그중 하나만 쓰고 있었다.

---

## 1. ⭐ 세션 전달 규약 — 문서에 이미 있었다

이게 이번 조사에서 가장 실용적인 소득이다. **커스텀 헤더 규약을 만들 필요가 없다.**

### Claude Code — [게이트웨이 프로토콜 레퍼런스](https://code.claude.com/docs/en/llm-gateway-protocol) (공식)

| 헤더 | 문서상 정의 |
| --- | --- |
| `x-claude-code-session-id` | *"A unique identifier for the current Claude Code session. Use it to aggregate all requests from one session **without parsing request bodies**"* |
| `x-claude-code-agent-id` | 서브에이전트가 낸 요청에만 붙는다. *"Use it with the session ID to **attribute cost to parallel agents**"* |
| `x-claude-code-parent-agent-id` | 중첩 에이전트의 부모 |

**우리 용도가 문서에 그대로 적혀 있다.** 8/14 에 실측해놓고 파생 해시를 계속 쓰고 있었다.

문서가 못박는 제약 셋 (전부 우리 설계에 영향):

1. **`x-claude-code-agent-id` 를 사용자 식별자로 쓰지 말 것** —
   *"the ID identifies an agent, not a person or a device"*. principal 대용 금지.
   단, 에이전트 팀의 teammate 는 재접속에도 **이름 기반 안정 ID** 를 재사용한다
2. **열린 목록으로 다뤄라** — *"Treat the headers and body fields as open lists, not closed
   ones … occasionally new `anthropic-*` or `x-claude-code-*` headers."*
   고정 allowlist 는 다음 릴리스가 주는 것을 놓친다
3. **본문을 고치지 말 것** — *"A gateway that rewrites or redacts request bodies for content
   inspection breaks the pairing the same way stripping does, so **inspect without modifying**"*

그리고 **우리 차단 계약 문제의 근본 원인이 여기 적혀 있다**:

> *"forward error response bodies unmodified. A gateway that **wraps upstream errors in its own
> envelope** breaks the recovery path even when it preserves the status code."*

LiteLLM 이 우리 차단 예외를 자기 OpenAI 오류 봉투로 감싸는 것이 정확히 이 안티패턴이다
(`block-contract.md` 참조). 우리가 만든 문제가 아니라 **문서화된 게이트웨이 실패 모드**다.

부수 확인: 8/16 프로브 로그의 정체불명 `[Bun] /api/hello` 는 **Claude Code 의 연결 워밍 프로브**였다
(*"receives a `HEAD /api/hello` connection-warming probe"*). 노이즈가 아니었다.

### Codex CLI — 소스에서 확인 (공개 문서 없음)

`codex-rs/codex-api/src/requests/headers.rs` 의 `build_session_headers()`:

| 헤더 | 값 |
| --- | --- |
| `session-id` | 세션 식별자 |
| `thread-id` | 스레드 (세션보다 좁은 단위) |
| `x-client-request-id` | thread id 를 한 번 더 |
| `x-openai-subagent` | `review` \| `compact` \| `memory_consolidation` \| `collab_spawn` |
| `originator` | 기본값 `codex_cli_rs` (`CODEX_INTERNAL_ORIGINATOR_OVERRIDE` 로 변경 가능) |

**와이어는 Responses API** — Kong 의 Codex 연동 가이드가 확인해준다:
*"we use the Responses route because the Codex CLI calls it by default"*.
006f 에서 합성 페이로드로만 쟀던 가정이 맞았다.

Codex 의 서브에이전트는 **ID 가 아니라 종류**를 준다. 열거형이라 해시할 이유가 없고,
`compact`(대화 압축)·`review` 같은 종류별 분포 자체가 신호다.

### 대조

| | Claude Code | Codex CLI |
| --- | --- | --- |
| 와이어 | Anthropic Messages (SSE 필수) | Responses |
| 세션 | `x-claude-code-session-id` | `session-id` / `thread-id` |
| 서브에이전트 | **ID + 부모 ID** (트리 복원 가능) | **종류만** (트리 복원 불가) |
| 문서화 | ✅ 공식 게이트웨이 계약 | ❌ 소스에만 |

---

## 2. 경쟁 현황 — 6월 대비 델타

### 가장 중요한 변화: 우리 기법을 그대로 파는 상용 제품이 생겼다

**Occludra** (구 AI Security Gateway) — [ai-agent-loop-protection](https://occludra.ai/blog/ai-agent-loop-protection)

> *"a fingerprint is computed as a SHA-256 hash of multiple request attributes. A sliding window
> counter tracks how many times each fingerprint has been seen within the detection window.
> When the count exceeds a configurable threshold, the request is blocked with HTTP 429 and a
> cooldown period."*

**우리 엔진의 문장 그대로다.** 지문 + 슬라이딩 윈도우 + 임계 + 429 + 쿨다운.

그런데 페이지를 끝까지 읽으면 **구체가 하나도 없다**:

| 우리가 공개한 것 | Occludra |
| --- | --- |
| 지문 구성 = (도구, 인자값 해시) · 왜 대화 전체를 해싱하면 안 되는지 | 항목 미공개 |
| 임계 도출 규칙 = "정상이 도달한 값은 임계가 될 수 없다" + 129콜 근거표 | 기본값·권장범위 없음 |
| 오탐 0/29 실측 | *"designed to … avoiding false positives"* (정량 없음) |
| 와이어 3종 실측 (Claude Code 실물 통과) | 언급 없음 |
| 대화 축 사다리 + `unavailable` 처리 | 세션 식별 방식 미설명 |

**포지셔닝**: 기법은 더 이상 차별점이 아니다. **차별점은 근거의 공개**다.
"우리도 지문 씁니다" 대신 **"이 숫자는 이렇게 나왔고, 여기서 틀렸습니다"** 가 우리 자리다.

그쪽이 공개한 유일한 숫자: 단일 GPT-4급 에이전트 루프가 **시간당 $108**.
그 외 업계 사례로 인용되는 것 — 14시간 $48,000 (2026-04-22), 11일간 $47,000 (2025-11).
**우리 발표 자료의 문제 규모 근거로 쓸 수 있다.**

### LiteLLM — 가장 가까운 선행 기술, 그러나 방향이 다르다

[Agentic Loop Hook](https://docs.litellm.ai/docs/proxy/agentic_loop_hook) 확인:

- *"Identical tool-call fingerprints abort the loop automatically"* · **기본 max reruns = 3**
- 지원: `/v1/messages` + `/v1/chat/completions`, **async 전용, 비스트리밍 전용**

**중요한 구분**: 이건 **LiteLLM 이 스스로 도구 루프를 대신 돌려줄 때**의 안전장치다
(서버사이드 도구 실행 → 모델 재호출). **클라이언트가 자기 루프를 도는 경우는 대상이 아니다.**

그런데 실제 폭주는 후자다 — Claude Code 도 Codex 도 자기가 루프를 돈다.
그리고 **비스트리밍 전용**이라 Claude Code(SSE 필수) 경로에는 애초에 안 걸린다.

`max_iterations` · `max_budget_per_session` 은 여전히 **숫자 상한**이지 패턴 판정이 아니다.

### 나머지

| | 상태 (2026-08) | 우리와의 관계 |
| --- | --- | --- |
| **agentgateway** (LF/Solo.io) | K8s CRD(GEP-713), 예산 하드 리밋, **JWT 폐기 킬스위치** | 신원이 **부여식**(virtual key/JWT). 우리는 **관측식**(협조 없이 얻는다). 패턴 탐지 없음 |
| **InferCost** | Observe·Report·Alert 만 라이브. **Enforce 여전히 미출시** | 6월과 동일. 구조적 충돌 lane 이지만 아직 관측 도구 |
| **OpenCost 1.121.0** (2026-08-05) | **신규** — K8s 추론 비용 추적 첫 도입 | 비용 관측 lane 이 붐빈다. 우리는 관측이 아니라 **경로 위 차단** |
| **Portkey** | Palo Alto Networks 인수 예정 (FY Q4 2026) | 상용 게이트웨이 통합 진행 중 |
| **Helicone** | Mintlify 인수 (2026-03) | 위와 같음 |

**시장 신호**: 관측 도구는 인수되어 플랫폼에 흡수되고 있고, 비용 관측 lane 은 OpenCost 진입으로
붐빈다. **"보는 것"은 상품화가 끝나가고 "막는 것"이 남았다** — 우리 논지와 맞는 방향이다.

---

## 2.5 ⚠️ 정정 — LiteLLM 이 이미 하고 있었다

조사 도중 팀에서 *"LiteLLM 이 값을 전달 못 하나? 잘 되어 있는 걸로 아는데"* 라는 지적을 받고
확인했다. **맞았다.** `litellm/proxy/litellm_pre_call_utils.py::get_chain_id_from_headers`:

> *Priority order: 1. `x-litellm-trace-id` 2. `x-litellm-session-id`
> 3. **Any `x-<vendor>-session-id` header** whose value looks like a session id
> (alphanumeric / UUID, at least 8 chars). **E.g. `x-claude-code-session-id`.***

독스트링이 우리 헤더를 **예시로 직접 명시**한다. 결과는 `data["litellm_session_id"]` 에 담긴다.
`x-litellm-agent-id` → `metadata["agent_id"]`, Anthropic `metadata.user_id` 폴백도 있다.

dev 파드에서 실제로 돌려본 매칭 결과:

| 클라이언트 | 헤더 | LiteLLM 이 잡나 |
| --- | --- | --- |
| Claude Code | `x-claude-code-session-id` | ✅ |
| Codex CLI | `session-id` · `thread-id` | ❌ `None` |
| Codex CLI | `x-client-request-id` | ❌ `None` |

정규식이 **`x-<vendor>-session-id`** 를 요구해서 Codex 의 접두어 없는 이름은 안 걸린다.

**교훈**: Claude Code 용으로 만든 헤더 파싱은 **중복이었다.** 플랫폼이 이미 정규화해 주는
값을 우리가 다시 파싱하고 있었다. 순서를 바꿔 **게이트웨이 값을 먼저 보고**(우리가 열거
못 한 벤더까지 덮고, LiteLLM 이 좋아지면 같이 좋아진다), 우리 사다리는 게이트웨이가
놓치는 것(=Codex)을 줍는 2순위로 내렸다. 새 출처 등급 `gateway` 로 기록에 남는다.

**다만 무조건 신뢰하면 안 된다** — 그 함수는 `x-litellm-trace-id` 를 최우선으로 쓴다.
trace 를 요청마다 새로 발급하는 클라이언트가 있으면 **대화가 매 호출 쪼개진다.**
그래서 신뢰 여부를 `identity.trustGatewaySession` 으로 뺐다 (기본 신뢰).

> **재사용 규칙**: 통합 대상이 이미 하는 일인지 **먼저 확인한다.** 우리가 만든 파싱은
> 유지보수 부채이고, 플랫폼이 하는 일은 공짜로 좋아진다.

## 3. 이 조사가 바꾼 것 (바로 반영)

1. `_SESSION_HEADERS` 에 **Codex 의 `session-id`·`thread-id` 추가.**
   전에는 `session_id`(밑줄)를 찾고 있어서 **Codex 세션을 못 잡았다**
2. `x-claude-code-*` **접두어 규칙** — 문서가 명시적으로 요구한 "열린 목록"
3. **서브에이전트 포착** — `x-claude-code-agent-id`/`parent-agent-id`, `x-openai-subagent`.
   아직 판정 축은 아니지만 **지금 안 남기면 과거 데이터로는 못 만든다**
4. 테스트 6건 추가 (총 45건) — 특히 *"agent id 를 principal 로 쓰지 않는다"* 를 고정
5. **CRD 에 `identity` 블록을 열었다.** 세션 헤더 목록·우선순위·모를 때의 처리를 파이썬
   상수로 두고 있었는데, 그건 정책이다 — 어떤 클라이언트가 들어오는가는 **운영자가 아는
   사실**이지 우리가 코드에 박을 사실이 아니다. 사내 에이전트가 자체 헤더를 쓴다고
   ConfigMap 안의 파이썬을 고치게 할 수는 없다:

   ```yaml
   spec.identity:
     conversationHeaders: [x-acme-session]   # 순서가 우선순위
     trustGatewaySession: true               # 게이트웨이 정규화 값을 믿는가
     onUnknownConversation: Skip             # 못 정했을 때
   ```

   **와이어 파싱은 넣지 않았다** — 도구 호출이 본문 어디 있는지는 프로토콜 어댑터지
   정책이 아니고, 선언으로 표현해도 쓸모가 없다.

   병합 규칙은 backstop 과 **반대**다. backstop 은 안전 천장이라 낮추기만 되지만,
   신원은 환경 사실의 선언이고 그 사실은 대상이 좁을수록 정확하다 → 구체적인 쪽이 이긴다.

## 4. 다음에 볼 것

- **서브에이전트 트리 축** — 재료는 이제 쌓인다. 부모를 알면 **정상 팬아웃과 폭주**를 가를 수
  있다 (지금 `rotation_gap` 은 둘을 구별 못 한다)
- **Codex 실물 측정** — 이 맥의 `codex` 는 바이너리 없는 심이다. 헤더는 소스로 확인했지만
  실물 통과는 아직
- **`x-stainless-retry-count`** — 재시도를 추론이 아니라 관측으로. 006d 에서 발견해놓고 미사용
- **차단 계약** — 게이트웨이 문서가 "오류 봉투로 감싸지 말라"고 명시한다.
  LiteLLM 이 그걸 하고 있으니 우리 `decision_id` 전달 방식을 다시 설계해야 한다

## 출처

- [Claude Code Gateway protocol reference](https://code.claude.com/docs/en/llm-gateway-protocol)
- [openai/codex `requests/headers.rs`](https://github.com/openai/codex) · `endpoint/responses.rs` · `login/src/auth/default_client.rs`
- [Kong — Route Codex CLI through AI Gateway](https://developer.konghq.com/how-to/use-codex-with-ai-gateway/)
- [LiteLLM Agentic Loop Hook](https://docs.litellm.ai/docs/proxy/agentic_loop_hook)
- [Occludra — AI agent loop protection](https://occludra.ai/blog/ai-agent-loop-protection)
- [agentgateway — budget limits](https://agentgateway.dev/docs/kubernetes/1.2.x/llm/cost-controls/budget-limits/) · [kill switch](https://agentgateway.dev/blog/2026-02-21-kill-switch/)
- [InferCost](https://github.com/defilantech/infercost) · [OpenCost 1.121.0 (CNCF)](https://www.cncf.io/blog/2026/08/05/opencost-1-121-0-first-of-a-kind-kubernetes-inference-cost-tracking/)
