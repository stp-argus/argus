# 에이전트 식별 — 조직에서 "누가"를 세게 하기

ARGUS 는 호출을 **주체(principal) · 팀 · 대화 · run** 으로 묶어 센다. 주체와 팀은 LiteLLM 가상 키에서 오고,
대화와 run 은 요청 헤더에서 온다. 이 문서의 절차는 2026-09-26 kind(LiteLLM v1.96.0)에서 그대로 실행해 확인했다.

## 1. 원칙 — 에이전트마다 가상 키, 키에는 user_id 와 team_id

| 키 필드 | ARGUS 에서 | 권장 값 |
| --- | --- | --- |
| `user_id` | **주체** — 사다리 최상위(`principal_source=key:user_id`). 키를 교체해도 같은 주체로 누적된다 | 에이전트 이름 (`agent-refund-bot`) |
| `team_id` | **팀** — `backstop.teamCalls` 의 단위. user_id 가 없으면 주체도 된다 | LiteLLM 팀 (`/team/new` 가 준 ID) |
| `key_alias` | user_id·team_id 가 모두 없을 때의 주체 | 사람이 읽을 이름 (`refund-bot-2026-09`) |

주체 사다리: `user_id > team_id > key_alias > X-Principal-Id 헤더 > 키 자체`. 헤더는 키에 신원이 하나도 없을
때(마스터 키 호출 등)만 쓴다. 키 신원과 **다른 값의** 헤더가 오면 위조 시도로 `identity` 범주가 붙는다.

## 2. 발급

```bash
# 팀 — 한 번
curl -s $LITELLM/team/new -H "Authorization: Bearer $MASTER" -H 'content-type: application/json' \
  -d '{"team_alias":"payments"}'                                  # → {"team_id": "d517…", …}

# 에이전트 키 — 에이전트마다. 교체할 때도 같은 user_id 로 새로 발급한다
curl -s $LITELLM/key/generate -H "Authorization: Bearer $MASTER" -H 'content-type: application/json' \
  -d '{"user_id":"agent-refund-bot","team_id":"d517…","key_alias":"refund-bot-2026-09"}'
```

⚠️ **LiteLLM 은 없는 `team_id` 로도 키를 발급한다**(실측: `team_id: "does-not-exist"` → 200). 팀 ID 오타는
에러 없이 별도 팀으로 세어져 팀 상한을 빠져나간다. 팀 ID 는 `/team/new` 응답에서 복사한다.

## 3. 에이전트 쪽 — 코드 변경 없이 환경 변수와 헤더

```bash
OPENAI_BASE_URL=https://<게이트웨이>/v1      # Anthropic SDK 면 ANTHROPIC_BASE_URL
OPENAI_API_KEY=<위에서 발급한 가상 키>
```

| 헤더 | 없으면 |
| --- | --- |
| 세션: `x-session-id` · `x-conversation-id` · `x-claude-code-session-id`(Claude Code) · `session-id`/`thread-id`(Codex) | 첫 사용자 메시지에서 대화 키를 파생한다 — 같은 프롬프트로 시작한 다른 작업이 한 대화로 합쳐질 수 있다 |
| run: `X-Run-Id` (차트 `litellm.guardrail.runIdHeader`) | `run_steps` 축이 평가되지 않는다 |

대화 헤더 목록은 정책 `identity.conversationHeaders` 로 바꿀 수 있다.

## 4. 확인 — 레코드 한 줄

```bash
kubectl -n argus-system exec deploy/litellm -- sh -c 'tail -1 /var/argus/records/records*.jsonl'   # litellm.replicas>1 이면 파드별 records-<pod>.jsonl
```

| 필드 | 기대값 | 다르면 |
| --- | --- | --- |
| `principal_source` | `key:user_id` | `header`·`api_key` 면 마스터 키나 신원 없는 키로 부르는 중 |
| `team` | 해시 값 | `null` 이면 키에 team_id 가 없다 — 팀 상한이 이 호출을 세지 않는다 |
| `conversation_source` | `client` 또는 `gateway` | `derived` 면 세션 헤더가 없다 |
| `run_id` | 보낸 값 | `null` 이면 run 헤더가 없다 |

실측(키 교체 전후 두 호출): `principal` 해시·`team` 해시가 같고 `principal_source=key:user_id`, `run_id` 만 `r-1`→`r-2`.

## 5. 알림에 누가 찍히나

`litellm.guardrail.alerts.identity`: `hash`(해시만) · `labels`(+team_id·key_alias, 기본) · `full`(+user_id).
user_id 에 이메일을 쓰는 조직이면 `full` 은 Slack 에 이메일이 남는다.
