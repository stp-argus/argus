# ARGUS 데이터 계약 — 레코드 스키마 · 신원 헤더 · 차단 계약 (v0.1 draft)

> **목적**: 채팅앱(발신) · 대시보드(소비) · 엔진(판정) 세 스트림이 **같은 데이터 모양**에
> 합의하기 위한 단일 근거. 이 문서가 세 스트림의 인터페이스다. 필드를 바꾸려면 여기부터 바꾼다.
>
> **경계**: 이 문서는 song2(엔진/가드레일)의 산출물이다. 대시보드 UI(spec 007/004)와 샘플앱
> 채팅 UI(argus-sample-app)는 각 소유 스트림이 만든다. 여기서 정하는 것은 *데이터*뿐.
>
> **상태**: draft v0.1 · 기준일 2026-08-26 · 소스: `hack/guardrail/argus_guardrail.py`
> (`_record_request` L353–423, `async_log_success_event` L455–504), `argus_signals.py`.
> 필드가 소스와 어긋나면 **소스가 정본**이다 — 이 문서를 고친다.

---

## 1. 한 호출의 생애 (누가 무엇을 채우나)

```
에이전트/채팅앱                 LiteLLM + ArgusObserve 가드레일                엔진(operator)
     │                                    │                                       │
     │ POST /v1/chat/completions          │                                       │
     │ (+ 신원 헤더 §3)         ┌──────────▼ pre_call ──────────┐                  │
     │─────────────────────────▶│ _record_request:              │  Signals(§4)     │
     │                          │  신원·요청모양 필드 채움 ──────┼─────────────────▶│ decide()
     │                          │                               │◀─ decision ──────│ (5축 §5)
     │                          │  would_block/policy_hit/       │                  │
     │                          │  control_level/decision_id 채움│                  │
     │            ┌─ Enforce & hard ─┐   └──────────┬────────────┘                  │
     │◀ 차단(§6) ─┤ HTTPException     │              │ 통과                          │
     │  403+헤더  └──────────────────┘   ┌──────────▼ success_event ─┐              │
     │◀─ 정상 응답 ──────────────────────│ tokens/cost/latency 채움  │              │
     │                                   │ _budget_settle (예산 정산)│              │
     │                                   └──────────┬────────────────┘              │
     │                                              ▼                               │
     │                                   records.jsonl 1줄 (§2)                     │
     │                                   (대시보드가 폴링해서 소비)                  │
```

- **pre_call**에서 신원·요청 모양 필드가 채워지고 판정이 난다. 차단이면 여기서 응답을 세운다.
- **success_event**(응답 후)에서 토큰·비용·지연이 채워진다. 비용은 호출을 해봐야 알기 때문.
- 대시보드는 완성된 `records.jsonl` 줄을 읽는다 (실시간이면 tail).

---

## 2. records.jsonl 스키마 (JSONL, 한 줄 = 한 호출)

필드는 `argus_guardrail.py`의 record dict 그대로. **소스가 정본.** 그룹별로:

### 2.1 시각·상태
| 필드 | 타입 | 의미 |
|---|---|---|
| `ts` | string | ISO8601 UTC ms (`2026-08-26T04:05:06.123Z`) |
| `status` | string | `observed` \| `blocked` \| `error`(업스트림 실패, `error` 필드에 사유) |
| `call_type` | string | LiteLLM call_type (예: `completion`) |

### 2.2 신원 (identity) — 대시보드 "누가" 축의 원천
| 필드 | 타입 | 의미 |
|---|---|---|
| `principal` | string\|null | **주체 해시** `sha256(...)[:16]`. 실키 원문 없음 |
| `principal_source` | string | 신뢰 티어 — `key:user_id` \| `key:team_id` \| `key:alias` \| `header` \| `api_key` \| `unavailable` (§3.1 사다리) |
| `team` | string\|null | `sha256("team:"+team_id)[:16]` — 팀 스코프 집계용 |
| `org` | string\|null | `sha256("org:"+org_id)[:16]` |
| `run_id` | string\|null | 실행 단위 (헤더 `X-Run-Id` 또는 metadata) |
| `conversation_hash` | string\|null | 대화 스코프 해시 |
| `conversation_source` | string | `gateway` \| `client` \| `protocol` \| `derived` \| `unavailable` |
| `namespace` | string\|null | 호출자 파드 네임스페이스 (헤더 `x-argus-namespace` / env `ARGUS_NAMESPACE`) — **spec 007 식별 축** |
| `pod` | string\|null | 호출자 파드명 (헤더 `x-argus-pod` / env `ARGUS_POD`) |
| `client_identity` | object | 협조 없이 얻은 신원 재료(세션·protocol_user·claude-code agent id 등). 없으면 `{}` |
| `litellm_call_id` | string\|null | 게이트웨이 로그·OTel 스팬과 잇는 **상관 키** (join은 이걸로, `request_id` 아님) |

> 🔒 **금지**: 실 API 키·`user_email`·프롬프트 텍스트를 레코드에 남기지 않는다. 해시·길이만.

### 2.3 요청 모양 (request shape)
| 필드 | 타입 | 의미 |
|---|---|---|
| `model` | string | 호출자가 지정한 별칭 (예: `argus-mock-gpt`) |
| `provider` | string\|null | pre_call엔 null; success가 `custom_llm_provider`로 채움 |
| `wire` / `wire_source` | string | `chat` \| `anthropic` \| `responses` \| `bedrock`(Bedrock Converse) \| `unknown` / `call_type` \| `shape` \| `passthrough`(LiteLLM 제공자 pass-through 본문을 풀어 읽음) |
| `msg_count` | int | 턴 수 |
| `msg_bytes` | int | 대화 크기(바이트) — **G4 크기 사전 게이트 재료(pre_call에 이미 앎)** |
| `prefix_hash` | string | 정규화 prefix 해시 — **G1 챗 반복 축 재료** |
| `tool_name` | string\|null | 이번 호출의 도구명 |
| `tool_args_value_hashes` | object\|null | 인자별 `{h,s,d}`(값해시·모양·변화부호). duplicate 축 지문 |
| `tool_arg_facets` | object\|null | spec 006e — 해시가 파괴한 정보(모양·변화부호) 별도 보존. **007 대시보드가 이걸로 만든다** |
| `stream` | bool | 스트리밍 여부 |
| `declared_tools` | array\|null | 요청이 선언한 도구 이름 목록(최대 30) |

### 2.4 판정 (decision) — 대시보드 "탐지/차단" 표시의 원천
| 필드 | 타입 | 의미 |
|---|---|---|
| `would_block` | bool\|null | 임계 도달로 **막혔을 것**인가 (Observe에선 표시만) |
| `policy_hit` | array | `[{axis, level}]` — 어떤 축이 soft/hard에 걸렸나. 쿨다운 중이면 `cooldown` 하나(detail 에 트립 사유). **Guard** 는 쿨다운 중 넘은 사용량·비용 hard 를 그 뒤에 덧붙인다 — Guard 가 막은 이유 |
| `categories` | array | **거버넌스 범주** `cost`·`behavior`·`volume`·`identity` (§9). 쿨다운 hit 제외, 위조 시도면 `identity`. 각 `policy_hit` 원소에도 `category` 가 붙는다 (2026-09-24). **쿨다운 hit 의 `category` 는 트립한 축의 범주**(detail 첫 축 기준, 모르면 `other`) — axis 는 그대로 `cooldown` (2026-10-14) |
| `decision_id` | string\|null | 판정 식별자 — **차단 판별 마커(§6)** |
| `control_level` | string | `Observe` \| `Guard` \| `Enforce` |
| `decide_reason` | string\|null | 사람용 사유 문자열 (⚠️ trimFloat 센트미만 표시버그 있음 — §7) |
| `policy_source` | string | 예: `crd:argus-system/observe-default` |
| `observations` | object | 5축 관측치 (§5) |
| `decide_source` | string | `operator` \| `local` \| `fail_closed` (누가 판정했나. `fail_closed` = 오퍼레이터가 `ARGUS_FAIL_CLOSED_GRACE_S`(기본 60초, 첫 실패 호출부터) 넘게 안 닿았고 `backstop.failureMode: Closed` + Guard·Enforce 라 막았다. `decision_id` 는 `fc-`. 로컬 판정까지 실패해 유예 없이 막은 호출은 `local` + `decide_error` 에 `fc-` id) |
| `decide_error` | string\|null | 판정 실패 시 사유 |

### 2.5 응답측 (success_event가 채움)
| 필드 | 타입 | 의미 |
|---|---|---|
| `tokens_in`/`tokens_out`/`tokens_cached`/`tokens_reasoning` | int\|null | 토큰 |
| `cost_usd` | float\|null | 이 호출 비용 |
| `cost_source` | string\|null | `provider` \| `litellm_costmap` \| `unknown` |
| `usage_source` | string\|null | `success_event` \| `blocked_before_call` \| `failure_event` \| `missing`(어느 이벤트도 안 옴) \| `uncorrelated` \| `response_only`. 재시도 끝에 성공하면 `success_event` + `retried_after_error` |
| `latency_ms` | int\|null | 지연 |

> 차단된 호출: `status:"blocked"`, `usage_source:"blocked_before_call"`, `cost_usd` 없음
> (= 장부상 $0. "막으면 돈이 안 나간다"의 데이터 증거).

---

## 3. 신원 헤더 계약 (채팅앱 → 가드레일)

### 3.1 신원 사다리 (가드레일이 정하는 우선순위 — 채팅앱은 재료만 제공)
```
key   (인증 산출물, 위조 불가)  user_id > team_id > key_alias   ← LiteLLM 가상 키(DB 필요)
  >  header (자기신고)          X-Principal-Id
  >  api_key (폴백)             가상 키 없으면 master key라 무의미
  >  unavailable               주체 스코프 축 미평가
```
- **key가 header를 이긴다** — 채팅앱이 `X-Principal-Id`를 위조해도 가상 키 신원이 우선.
- `default_user_id`(LiteLLM이 master key에 채우는 자리표시자)는 **부재 취급** — 승격 금지.

### 3.2 채팅앱이 보낼 헤더 (이름은 env로 주입 — Argus 특정 문자열 강제 아님)
| 헤더 | 기본 이름 | env override | 용도 |
|---|---|---|---|
| principal | `X-Principal-Id` | `ARGUS_PRINCIPAL_HEADER` | 자기신고 주체(가상 키 없을 때만 유효) |
| run | `X-Run-Id` | `ARGUS_RUN_ID_HEADER` | 실행 단위 |
| namespace | `X-Argus-Namespace` | — (Downward API env `ARGUS_NAMESPACE`도 가능) | spec 007 식별 축 |
| pod | `X-Argus-Pod` | — (env `ARGUS_POD`) | spec 007 식별 축 |

> 가상 키를 쓰면(권장) principal은 `key:user_id`로 자동 확정 — 헤더 없이도 신원이 선다.
> 헤더는 가상 키가 없는 로컬/데모 경로의 폴백.

---

## 4. 엔진에 전달되는 Signals (판정 입력)

가드레일이 pre_call에 엔진으로 보내는 것 (record 전체가 아니라 판정용 6개):
`principal, run_id, conversation_hash, conversation_source, tool_name,
tool_args_value_hashes, cost_usd(=직전 정산 누적 델타 §5 budget)`.
→ 엔진은 principal별 상태에 누적하고 5축 점수를 매겨 `decision`을 돌려준다.

---

## 5. 탐지 5축 (observations) 과 임계

| 축 | 창/기준 | 잡는 폭주 | 비고 |
|---|---|---|---|
| `principal_calls` | 60초 **창** | 레이트 급증 | 누적 아님 — 느린 폭주엔 budget이 최후 방어선 |
| `duplicate_calls` | 최근 20콜 `(tool,인자)` 지문 | 중복 반복 | 도구 없으면 평가 안 됨(G1), 인자변조에 약함 |
| `run_steps` | run 의 60초 창 | 스텝 초과·재시도 폭풍 | run ID(`X-Run-Id`) 없으면 평가도 관측치도 없음 (2026-09-26) |
| `rotation_gap` | `nCalls/perRunMax` | 핑퐁·키 회전 | principal 안정 시에만 유의미 |
| `budget_usd` | principal 정산 누적 | 예산 초과·느린 폭주 | 2026-08-25 부활(G0). accumulate-then-gate-next |

- 각 축은 **soft**(관찰) / **hard**(차단 트리거) 두 임계. 임계는 CRD가 렌더한 `policy.json`이 정본.
- 예산은 `detect`가 아니라 `backstop.budgetUSD`(단일 하드캡)에 둔다 — "예산은 재는 게 아니라 정하는 것".

---

## 6. 차단 계약 (가드레일 → 채팅앱)

- **상태코드**: 가드레일 기본 `BLOCK_STATUS=429`(env `ARGUS_BLOCK_STATUS`). **kind는 403으로 오버라이드.**
  - ⚠️ **왜 403으로 가야 하나**: openai·anthropic SDK가 `408/409/429/5xx`를 **기본 2회 자동 재시도** →
    429로 막으면 차단이 호출을 늘린다. 403은 재시도 대상 아님. 402는 RFC상 의미 미정의.
  - **현재 상태**: kind=403 실측 완료, dev=429(샘플앱 러너 `BLOCK_STATUSES` 기본이 `[429]`라
    계약 비준과 함께 이동). → **샘플앱은 `BLOCK_STATUSES`에 403을 추가**해야 한다.
- **응답 헤더** (LiteLLM 오류 봉투를 뚫고 도착 — 검증됨):
  - `x-argus-decision-id` : 아거스 차단 판별의 **최소 해법**. 봉투 구조와 무관하게 이걸로 판별.
  - `x-argus-control-level` : Observe/Guard/Enforce
  - `x-litellm-call-id` : 상관 키 (LiteLLM이 실어줌)
- **바디**: `detail`에 자연어 사유 + `decision_id` 포함.
- **채팅앱 판별 규칙**: 응답 status가 `BLOCK_STATUSES`에 있고 **`x-argus-decision-id` 헤더(우선)
  또는 바디 `decision_id` 마커**가 있으면 → "아거스 차단". 마커 없는 429는 업스트림 rate-limit.

---

## 7. 알려진 한계 (계약 소비자가 알아야)

1. ~~**trimFloat 표시버그**~~ → **2026-09-24 해소.** `decide_reason` 이 `observed>=threshold` 로,
   숫자는 유효숫자 6자리(`0.0023826`, `96.98`, `20`)로 찍힌다 — Go·Python 이 바이트 단위로 같다
   (`internal/decide/reason_format_test.go` ↔ `hack/guardrail/test_argus_decide.py` 같은 표).
   예전엔 `%.2f` 반올림으로 센트 미만 예산이 `budget_usd=0>0`, 경계값이 `20>20` 으로 읽혔다.
   그래도 **대시보드는 사람용 문자열이 아니라 `observations`/`policy_hit` 로 그릴 것** — 문자열은 표시용이다.
2. **budget settle-lag**: 예산 판정이 약 1콜 뒤처짐(비용은 응답 후에야 아므로).
3. **records 경로 함정**: `ARGUS_RECORDS_PATH`는 **파일 경로**여야 함(디렉토리면 조용히 유실).
4. **conversation_source=unavailable**이면 대화 스코프 축은 평가 안 됨(가짜 버킷 금지).

---

## 8. 판정 이벤트 OTel (로드맵 — 계약에 예약, 구현은 데모 후)

> 2026-09-24: 대시보드·알림이 구독할 경로는 **Prometheus 메트릭(§9.2)** 으로 먼저 열었다. OTel 이벤트는 여전히 예약.

`argus.decision.*` 네임스페이스로 판정 이벤트 방출 예정:
`decision, would_block, policy_hit, decision_id, control_level, policy_source`.
`gen_ai.*`에는 표준 속성만(파생 해시를 `gen_ai.conversation.id`로 넣지 않는다 — 8/18 결정).
→ 대시보드가 JSONL 폴링 대신 메트릭/이벤트를 구독하는 경로. spec 007이 소비 대상.

## 9. 거버넌스 신호 — 범주 · 메트릭 · 알림 (2026-09-24)

탐지 로직은 그대로, **보이고·분류되고·알려지게** 한다. 벤치마크: LiteLLM 알림(`alert_types`·webhook 페이로드) ·
AWS Cost Anomaly Detection("% + 절대금액" 이중 기준) — 근거는 Argus 볼트 `06-레퍼런스-AWS-LLM게이트웨이`.

### 9.1 범주 (Go `internal/decide/category.go` ↔ Python `argus_decide.CATEGORY_BY_AXIS`, 같은 표·같은 테스트)

| 범주 | 뜻 | 축 |
| --- | --- | --- |
| `cost` | 이상 비용 | `budget_usd` |
| `behavior` | 이상 행동 | `duplicate_calls` · `run_steps` · `conv_growth` · `msg_bytes` · `rotation_gap` |
| `volume` | 이상 사용량 | `principal_calls` · `team_calls` |
| `identity` | 신원 위조 시도 | (축 아님) `client_identity.claimed_conflicts_with_resolved` |
| `other` | 표에 없는 축 | 새 축을 추가하고 표를 잊으면 여기로 — 알림 기본값에 포함돼 **조용히 빠지지 않는다** |

쿨다운 hit(`axis: cooldown`)는 축이 아니라 **트립 사유의 첫 축**으로 범주를 정한다 — `principal_calls=19>=19` 로 걸린 주체의 쿨다운 차단은 `volume`. 알림·`categories` 의 쿨다운 제외는 axis 기준이라 그대로다 (`argus_decide.hit_category`).

### 9.2 메트릭 — 오퍼레이터 판정 서버 `:8080/metrics` (Prometheus)

판정은 오퍼레이터 한 곳이라 게이트웨이 레플리카와 무관하게 **한 번씩** 센다. principal 은 라벨에 넣지 않는다(카디널리티).

| 메트릭 | 라벨 | 뜻 |
| --- | --- | --- |
| `argus_decisions_total` | `control_level` · `decision` · `would_block` | 판정 수 |
| `argus_detections_total` | `category` · `axis` · `level` · `control_level` | 임계를 넘은 관측 (호출마다, 쿨다운 제외) |
| `argus_trips_total` | `category` · `control_level` | **트립 엣지** = 엔진 폭주 판정(`would_block`) + 쿨다운 아님. 범주는 hard 축의 것, hard 가 없으면(soft_escalation) soft 축의 것 |
| `argus_team_calls_total` | `team` | 팀별 호출 (team = 레코드와 같은 해시) |
| `argus_team_cost_usd_total` | `team` | 팀별 지출 증분. 주체 누적 지출의 증가분 합 — **직전 값이 0 이면(재기동·LRU 축출 뒤, 새 주체의 첫 증분 포함) 세지 않는다**(과거 누적이 급증으로 찍히지 않게) |

> 알려진 (category, control_level)·(category, axis, level, control_level)·(control_level, decision, would_block) 조합은 **기동 시 0 으로 미리 만든다** — 카운터가 1 로 태어나면 `increase()` 가 재기동 뒤 첫 트립을 못 본다. 팀 시리즈는 팀을 처음 볼 때 0 으로 생긴다(첫 호출 1건은 `rate` 에서 빠질 수 있다).

알림 규칙(차트 `metrics.prometheusRule`): `ArgusRunawayDetected`(Observe 트립) · `ArgusRunawayBlocked`(Enforce 트립) ·
`ArgusTeamCostSpike`(최근 1h 지출 > 7일 시간당 평균 × ratio **그리고** > minUsdPerHour). 검증: argus-helm `hack/test-promrules.sh`.

### 9.3 알림 webhook — 가드레일, 트립 순간 한 번

| env | 기본 | 뜻 |
| --- | --- | --- |
| `ARGUS_ALERT_WEBHOOK_URL` | (없음=끔) | Secret 에서. `hooks.slack.com` 이면 `{"text"}`, 그 외 아래 JSON |
| `ARGUS_ALERT_CATEGORIES` | `cost,behavior,volume,identity,other` | 알릴 범주 |
| `ARGUS_ALERT_SOFT_CATEGORIES` | `cost` | soft 에서도 알릴 범주 — 예산 사전 경보 |
| `ARGUS_ALERT_DEDUP_S` | `300` | 같은 (주체, 범주, 사건) 은 창 안에서 한 번 — **레플리카마다 따로**, 재기동 시 초기화 |
| `ARGUS_ALERT_STICKY_CATEGORIES` · `ARGUS_ALERT_DEDUP_STICKY_S` | `cost` · `86400` | 한 번 넘으면 계속 넘어 있는 범주는 긴 창 — 누적 지출은 줄지 않아 쿨다운(30초)마다 재트립한다 |
| `ARGUS_ALERT_IDENTITY` | `labels` | `hash` · `labels`(+`team_id`·`key_alias`) · `full`(+`user_id` — 이메일일 수 있다). 페이로드 필드와 **`event_message` 문자열 모두** 이 모드를 따른다(Slack 본문은 `{"text"}` 하나). **실 API 키는 어떤 모드에서도 없음** |

사건: `runaway_detected`(Observe 트립) · `runaway_blocked`(차단된 트립 — Enforce, 또는 Guard 의 사용량·비용 축) · `<category>_warning`(soft) · `identity_spoof_attempt`.
쿨다운 중에는 보내지 않는다. 전송은 데몬 스레드·타임아웃 3초·실패는 세기만 한다 — 판정·호출 경로를 막지 않는다.

```json
{"event": "runaway_blocked", "event_group": "argus", "event_message": "[ARGUS] 이상 사용량 · 차단함 · agent-a-key · principal_calls=19>=19",
 "category": "volume", "severity": "critical", "control_level": "Enforce", "decision": "block", "decision_id": "d-…",
 "policy_hit": [{"axis": "principal_calls", "level": "hard", "observed": 19, "threshold": 19, "category": "volume"}],
 "principal": "<hash>", "principal_source": "key:user_id", "team": "<hash>", "run_id": "run-04", "model": "…", "ts": "…",
 "policy_source": "crd:argus-system/observe-default", "team_id": "…", "key_alias": "…"}
```
`event`·`event_group`·`event_message` 는 LiteLLM 알림 webhook 과 같은 키 — 같은 채널에 섞여도 같은 방식으로 읽힌다.
레코드(`records.jsonl`)의 신원은 이 설정과 무관하게 **해시만**이다 (§2 🔒).
