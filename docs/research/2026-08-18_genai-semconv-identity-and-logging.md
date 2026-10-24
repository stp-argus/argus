# 에이전트 식별자와 로그 스키마 — OTel GenAI 표준 정렬

날짜: 2026-08-18
질문: ① 에이전트 식별자는 어떤 값을 기준으로 해야 하는가 ② 로그(records.jsonl)를
gen_ai 시맨틱 컨벤션 등 자리 잡는 표준과 같은 모양으로 가져갈 수 있는가.

원문 소스:
- 속성 레지스트리: `open-telemetry/semantic-conventions-genai` `docs/registry/attributes/gen-ai.md` (main, 2026-08-18 열람)
- 에이전트 스팬: 같은 레포 `docs/gen-ai/gen-ai-agent-spans.md`
- Claude Code 텔레메트리: https://code.claude.com/docs/en/monitoring-usage
- LiteLLM OTel 통합: https://docs.litellm.ai/docs/observability/opentelemetry_integration
- 상태 정리(외부): https://john-hodge.com/blog/opentelemetry-genai-semantic-conventions/ (2026-07)

---

## 1. 표준의 현재 상태 — "자리 잡는 중"이지 "자리 잡음"이 아니다

- 2026-06-12, semantic-conventions v1.42.0 에서 gen_ai.* 전체가 본체 레포에서
  **deprecated 처리되고 전용 레포 `semantic-conventions-genai` 로 이관**됐다.
- 전용 레포는 2026-08 현재 **태그된 릴리스가 없고**, 스팬·이벤트·메트릭·속성 전부
  **Development** 상태다. Stable 은 하나도 없다.
- 12개월 사이 `gen_ai.system` → `gen_ai.provider.name` 개명(v1.37.0), invoke_agent
  스팬의 client/internal 분리(v1.41.0) 등 **이름이 실제로 깨져 왔다.**
- 실무 가이드(위 상태 정리 글)의 결론: *"내부 표현을 소유하라. Development 상태
  외부 스키마에 내부 표현을 종속시키지 말라. gen_ai.* 네임스페이스에 커스텀 속성을
  넣지 말라."*

→ **어휘와 규칙은 표준을 따르되, 저장 스키마를 통째로 개명해 종속시키는 것은
표준 쪽 실무 가이드가 오히려 말리는 일**이다. 정렬 방식은 §5.

## 2. ⭐ 표준이 우리 설계 두 곳을 문장으로 검증해 준다

### 2-1. `gen_ai.conversation.id` — 합성 폴백 금지

레지스트리 주석 [3], 원문 그대로:

> "When no identifier for the conversation is available, instrumentations SHOULD NOT
> populate conversation id. For example, a new UUID, a trace identifier, **or a hash
> of request content SHOULD NOT be used as a fallback value.**"

우리가 8/16 에 겪은 대화 버킷 뭉침 사고(파생 해시가 `sha(principal|model|None)` 로
전부 합쳐진 것)와, 그 수습으로 만든 사다리
`gateway > client > protocol > derived > unavailable`(unavailable 이면 대화 축 미평가)이
표준 규칙과 정확히 같은 결론이다. 단 함의가 하나 있다:

- **`derived`(첫 user 메시지 해시) 는 표준이 명시적으로 금지하는 바로 그 값이다.**
  내부 폴백으로 쓰는 것은 우리 사정이지만, 밖으로 내보낼 때 `gen_ai.conversation.id`
  자리에 넣으면 안 된다. → 매핑 규칙: `conversation_source ∈ {gateway, client, protocol}`
  일 때만 그 속성으로 수출, `derived` 는 `argus.conversation.derived_hash` 로 격리.

### 2-2. `gen_ai.agent.id` — 일시적 인스턴스 ID 금지

레지스트리 주석 [1]:

> "For hosted agents, this SHOULD be the provider-assigned **stable** identifier …
> It's NOT RECOMMENDED to record in-memory agent instance ids on this attribute
> due to their transient nature."

Claude Code 의 `x-claude-code-agent-id`(서브에이전트 인스턴스, 세션마다 새로 생김)는
표준이 이 속성에 넣지 말라는 바로 그 종류다. 우리가 이것을 `client_identity.subagent`
아래 별도 보관하고 principal 로 승격하지 않은 것도 결과적으로 표준과 일치한다.

## 3. 답 ① — 에이전트 식별자는 "한 값"이 아니라 스코프별 사다리다

표준 자체가 식별자를 네 스코프로 쪼개 놓았고, 지배 클라이언트/게이트웨이가 각
스코프에 실제로 내보내는 값이 있다. 우리 사다리를 표준 어휘로 고정하면:

| 스코프 | 표준 속성 | 지배 구현이 주는 값 | ARGUS 현재 필드 | 판단 |
| --- | --- | --- | --- | --- |
| **주체(누가 요금을 내나)** | gen_ai 밖 — 워크로드/사용자 신원 영역 | LiteLLM `metadata.user_api_key_user_id`·`team_id`·`org_id`, K8s SA/SPIFFE | `principal` + `principal_source` | **탐지 귀속의 기준축은 계속 이것.** 표준도 이 계층을 gen_ai 에 안 넣는다. 한화 액션 ③(가상 키 환경에서 key 등급 승격)과 같은 결론 |
| **대화(무엇이 반복되나)** | `gen_ai.conversation.id` | LiteLLM `litellm_session_id`, Claude Code `x-claude-code-session-id`, Codex `session-id`/`thread-id` | `conversation_hash` + `conversation_source` | gateway/client/protocol 등급만 이 속성에 해당. derived 는 수출 금지(§2-1) |
| **에이전트(어떤 프로그램인가)** | `gen_ai.agent.name`(앱이 부여한 이름) / `gen_ai.agent.id`(호스팅 제공자가 부여한 안정 ID) | Codex `x-openai-subagent`(종류 열거형 → name 감), Claude Code `agent.id`(인스턴스 → id 에 못 넣음), UA `claude-cli/2.1.220` | `client_identity.client`/`client_version`, `subagent.kind` | name 은 있고 id 는 **자기호스팅 에이전트라 표준상으로도 없음**. 인스턴스 ID 는 `argus.*` 로 격리 보관 |
| **호출 1건** | `gen_ai.response.id`, `gen_ai.tool.call.id` | LiteLLM `x-litellm-call-id`, Codex `x-client-request-id` | `litellm_call_id`(상관키), `client_identity.request_id` | 이미 수집 중. traceparent 도 이미 파싱함(`trace_id`) |

한 줄 요약: **폭주 귀속·차단의 기준값은 지금처럼 principal(워크로드/키 신원),
반복 계수의 버킷은 gen_ai.conversation.id 에 해당하는 등급의 세션 값** — 이 구조는
표준과 이미 동형이고, 바꿀 것은 이름과 수출 규칙뿐이다.

## 4. 답 ② — 로그: 표준 스트림은 이미 존재한다, 우리는 판정 감사 로그다

핵심 발견: **LiteLLM 프록시에 OTel 콜백이 내장돼 있고, 그것이 gen_ai.* 를 이미
표준대로 내보낸다** (`gen_ai.operation.name`, `request.model`, `usage.input_tokens`,
`gen_ai.provider.name`(semconv 최신 모드), 그리고 `metadata.user_api_key_*`).
즉 "표준 모양의 LLM 호출 로그"는 우리가 만들 물건이 아니라 **게이트웨이 설정 한 줄**
(`callbacks: ["otel"]` + collector 주소)이다.

그러면 records.jsonl 의 정체는 분명해진다: 표준 스트림이 안 담는 **판정 감사 로그**
(policy_hit, would_block, decision_id, control_level, policy_source, 행동 지문).
둘은 상관키로 잇는다 — `litellm_call_id` ↔ OTel 스팬, `trace_id`(traceparent, 이미
파싱 중) ↔ 분산 트레이스.

### 필드 매핑표 (records.jsonl → 표준 어휘)

| records.jsonl | 표준 | 비고 |
| --- | --- | --- |
| `model` | `gen_ai.request.model` | 응답측 실모델은 `gen_ai.response.model` |
| `provider` | `gen_ai.provider.name` | 성공 이벤트에서 채움 — 표준도 client 계측의 best knowledge 허용 |
| `call_type`/`wire` | `gen_ai.operation.name` + (wire 는 우리 고유 → `argus.wire`) | chat/embeddings 는 열거값 존재 |
| `tool_name` | `gen_ai.tool.name` | |
| `tool_args_value_hashes`/`tool_arg_facets` | 없음 → `argus.tool.args_fingerprint` 등 | 표준의 `gen_ai.tool.call.arguments` 는 **원문**이라 FR-003(해시만) 위반 — 우리 것 유지 |
| `tokens_in/out/cached/reasoning` | `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read.input_tokens` / `reasoning.output_tokens` | 1:1 전부 존재 |
| `cost_usd` | **표준에 없음** | LiteLLM 의 `gen_ai.cost.*` 는 비표준 확장. `argus.cost_usd` 로 |
| `conversation_hash` (gateway/client/protocol) | `gen_ai.conversation.id` | 해시 그대로 넣어도 됨 — "식별자"이기만 하면 됨 |
| `conversation_hash` (derived) | 수출 금지(§2-1) | `argus.conversation.derived_hash` |
| `client_identity.request_id` | (Codex previous_response 문맥은 `gen_ai.request.previous_response.id` 도 존재) | Responses truncated 표시와 함께 |
| `truncated` | `gen_ai.conversation.compacted` | 표준: 확실할 때만 true, false 는 설정하지 말 것 — 우리 구현과 동일 규칙 |
| `policy_hit`/`would_block`/`decision_id`/`control_level` | 없음 → `argus.decision.*` | gen_ai.* 침범 금지 규칙에 따라 우리 네임스페이스 |
| `principal`/`principal_source` | 없음(gen_ai 밖) → `argus.principal.*` | 워크로드 신원은 K8s/SPIFFE 리소스 속성 영역 |

### 참고 — Claude Code 쪽 표준 스트림

Claude Code 자체도 `CLAUDE_CODE_ENABLE_TELEMETRY=1` 로 OTel 을 내보낸다
(`session.id`, `user.account_uuid`, `organization.id`, 스팬에 `gen_ai.*`).
엔터프라이즈가 이걸 켜면 **클라이언트측 session.id 와 우리가 헤더에서 잡는
`x-claude-code-session-id` 가 같은 값**이라, 회사 관측 스택과 ARGUS 레코드가
자연 조인된다. 영업/데모 포인트로 기억해 둘 것.

## 5. 결정 (2026-08-18 확정 — 표준 채택으로 감)

> 갱신: 아래 1안(이름 유지+문서 매핑)으로 제안했으나 **"표준으로 간다"로 결정**됐다.
> 구현: 각 레코드에 `gen_ai` 표준 뷰 블록을 싣는다(`argus_signals.py::_genai_view`,
> `_write` 단일 관문에서 주입). 내부 필드는 007 대시보드 등 기존 소비자가 이전할
> 때까지 병행하고, 표준이 1.x 릴리스를 내면 내부 이름을 은퇴시킨다.
> §2 의 두 불변식(derived 수출 금지·compacted true-only)은 `test_argus_genai_view.py`
> 로 고정했다.

1. **records.jsonl 내부 필드명은 병행 유지한다** (007 대시보드·분석기·테스트
   소비자 무파손). 표준 이름은 레코드 안 `gen_ai` 블록이 정본이다.
2. **신규 필드부터는 표준 어휘를 쓴다** — 이름 지을 일이 생기면 레지스트리 먼저 확인.
3. **수출 규칙 세 가지를 불변식으로 고정**: ① derived 대화 해시는
   `gen_ai.conversation.id` 로 내보내지 않는다 ② 일시적 에이전트 인스턴스 ID 는
   `gen_ai.agent.id` 에 넣지 않는다 ③ ARGUS 고유 개념은 전부 `argus.*` 네임스페이스
   (gen_ai.* 에 커스텀 속성 금지).
4. **표준 gen_ai 스트림이 필요해지는 시점**(고객 관측 스택 연동)에는 재구현하지 말고
   LiteLLM `otel` 콜백을 켠다. ARGUS 판정 이벤트의 OTel 수출(`argus.decision.*` 속성을
   단 로그 이벤트)은 그때 오퍼레이터 쪽에 붙인다 — 로드맵 항목, 지금 아님.
5. 표준이 태그된 릴리스(1.x)를 내면 이 문서를 재검토한다. 그 전까지 gen_ai.* 이름을
   저장 스키마에 직접 쓰는 것은 보류.
