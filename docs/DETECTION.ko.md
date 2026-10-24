<!-- SPDX-License-Identifier: Apache-2.0 -->

# 어떤 원리로 탐지하는가 (한국어)

> 실측 기준일 **2026-09-07**. 숫자는 전부 이 날 로컬 kind(169콜)와 dev 클러스터에서
> **탐지 경로를 통과해** 나온 값이다. 별도 프로브로 잰 값이 아니다.

## 한 줄

에이전트를 찾아다니지 않는다. **모든 LLM 호출이 반드시 지나는 길목에 서서, 지나가는
호출마다 이름표를 붙이고, 같은 이름표끼리 눈금을 센다.**

## 1. 길목 — `pre_call`, 인증 직후

에이전트 코드는 고치지 않는다. `OPENAI_BASE_URL` 만 프록시로 돌린다.

```
에이전트 → ┌ LiteLLM 프로세스 ─────────────────┐ → 업스트림(Bedrock·OpenRouter…)
           │ ① 키 인증 → user_api_key_dict     │
           │ ② pre_call  ★ 아거스가 여기 ★     │
           │      └→ /v1/decide (Go 엔진)      │
           │ ③ 라우팅·provider 변환·서명       │
           └───────────────────────────────────┘
```

②여야 하는 이유 셋:

- **돈이 나가기 전이다.** 여기서 막으면 과금이 0이다.
- **클라이언트 원형을 여기서만 본다.** 세션 헤더·도구 호출·선언 도구가 ③에서
  provider 형식으로 변환되면 사라진다.
- **신원이 여기서만 온전하다.** `user_api_key_dict`(user_id·team_id·key_alias)는
  ①의 산출물이라 ②에 살아 있다.

## 2. 이름표 — 신원은 두 축이다

호출 하나만 보면 폭주인지 알 수 없다. 여러 호출을 **같은 바구니**에 모아야
"30번째인데?"가 보인다. 바구니 이름표가 둘이다.

### 주체(principal) — "누가"

신뢰 순서대로 내려가는 사다리다 (`hack/guardrail/argus_signals.py::_principal_ladder`).

| 티어 | 재료 | 성질 |
| --- | --- | --- |
| `key:user_id` / `key:team_id` / `key:alias` | 가상 키 인증 산출물 | **위조 불가** — 게이트웨이가 키를 검증한 뒤 채운다 |
| `header` | `X-Principal-Id` | 자기신고. 규약을 아는 협조적 에이전트용 |
| `api_key` | API 키 원본 | 가상 키 DB 가 없으면 마스터 키 하나 = **호출자 구별 불가** |
| `unavailable` | — | 코드는 `(unknown)` **공용 버킷**에 누적한다 — 신원 없는 호출자 전원이 한 주체로 세어져 backstop(`principal_calls`·`budget_usd`)에 같이 걸린다. 평가를 생략하면 `failureMode: Open` 에서 신원 없는 트래픽이 backstop 을 통째로 빠져나가는 새 구멍이 되므로 일부러 이렇게 둔다(2026-09-08 정정). 오탐 가능성은 신원 없는 클라이언트를 허용한 운영자 몫 — 가상 키를 발급하라 |

시드에 티어 문자열을 섞는다(`key:user_id:agent-a`). 다른 티어에서 우연히 같은 문자열이
와도 해시가 갈라져야 **"어떻게 알아냈는가"** 가 보존된다.

> **조직 배포에서 중요한 성질** — 시드가 `user_id` 기반이라 **키를 재발급·회전해도
> principal 해시가 안 바뀐다.** 2026-09-07 실측: 13일 전 키로 만든 해시와 새 키로 만든
> 해시가 동일했고, kind 와 dev 두 클러스터에서도 같은 값이 나왔다.

### 대화(conversation) — "어느 작업"

같은 에이전트라도 별개 작업 셋을 돌리면 서로의 반복으로 세면 안 된다.
`litellm_session_id`(게이트웨이 정규화) → 클라이언트 세션 헤더 → 프로토콜 필드 → 파생 순.
이 사다리는 **정책이다** — CRD `identity.conversationHeaders`(순서=우선순위) ·
`trustGatewaySession` · `onUnknownConversation: Skip|Derive`. 사내 에이전트가 자체 헤더를
쓰면 CR 한 줄로 붙인다. 가드레일 파이썬을 고칠 일이 아니다.

## 3. 눈금 — 8축과 그 스코프

| 축 | 무엇을 세나 | 스코프 |
| --- | --- | --- |
| `principal_calls` | 누적 호출 수 | **주체** |
| `budget_usd` | 누적 비용 | **주체** |
| `rotation_gap` | 창 안 주체 누적 ÷ run 당 최대 — run 을 쪼갠 정도 (run 이 둘 이상일 때만) | **주체** |
| `run_steps` | 한 실행의 스텝 수 | run |
| `msg_bytes` | 한 호출의 메시지 크기 (G4, 2026-09-08 추가) | 호출 |
| `conv_growth` | 이번 호출 크기 ÷ 대화 첫 호출 크기 (G3, 2026-09-08 추가) | 대화 |
| `team_calls` | 팀(가상 키 team_id) 단위 창 누적 — backstop 전용 hard (G2, 2026-09-08 추가) | **팀** |
| `duplicate_calls` | 동일 (도구, 인자) 반복 | **대화** |

각 축에 `soft`(경고) / `hard`(차단선)가 있고, 값은 코드가 아니라 `RunawayPolicy` CR 에서
온다 — 파드 재시작 없이 바뀐다. hard 하나가 차단 조건이다. soft 2개 승격(`SoftEscalation`)은 엔진에만 있고 **CR 로 켜는 경로는 아직 없다**(파이썬 폴백만 env `ARGUS_SOFT_ESCALATION` 을 읽는다 — 켜면 원격과 폴백이 갈라지니 쓰지 말 것).
한 번 걸리면 **쿨다운**이 걸려 같은 폭주가 매 호출 알람을 내지 않는다(히스테리시스).

`controlLevel` 은 세 단계다.

| 단계 | 막는 것 |
| --- | --- |
| `Observe` | 없음 — 전역 Dryrun. 판정은 다 돌고 `would_block` 만 기록 |
| `Guard` | **volume·cost 범주의 hard** — `principal_calls`·`team_calls`·`budget_usd`. ⚠️ backstop 상한만이 아니라 `detect.principalCalls.hard` 도 포함된다(실효 hard 는 detect 와 backstop 중 낮은 값). dev·quickstart 에선 19콜/60초에서 막힌다 |
| `Enforce` | 모든 hard |

## 4. 왜 이름표가 먼저인가 — 제품 논지

**이름표가 흔들리면 눈금이 0으로 리셋된다.**

폭주 에이전트가 호출 몇 번마다 runId 나 키를 바꾸면 per-run 카운터는 매번 새로 시작한다.
2026-08-12 실측이 이걸 숫자로 못박았다 — 핑퐁 트래픽의 **run 당 스텝 최대는 5로 정상과 같다
(`run_steps` 탐지율 0%).** 같은 트래픽을 주체로 묶으면 29콜이다.

LiteLLM 의 `max_budget`·`max_iterations` 가 이 유형에 눈이 먼 이유가 여기다. 키 단위 `max_budget` 은
키를 돌리면, 세션 단위 `max_iterations`(카운터 키가 `session_id`)는 세션을 돌리면 누적이 쪼개진다. 둘 다 숫자 상한이지 패턴 판정이 아니다. **주체 사다리의 key 티어가 그 회피를 막는다** — 키가 둘이어도 `user_id` 가
같으면 한 바구니다(2026-08-25·09-07 실측, 28콜 회전이 단일 principal).

## 5. 임계값은 어디서 오는가

규칙 하나다. **정상 트래픽이 도달한 값은 임계가 될 수 없다.**

`hack/analyze/derive_thresholds.py` 가 **판정 엔진을 그대로 재생해** 정상/폭주 코호트의
관측 분포를 뽑고, `soft = 정상 최대 초과 + 여유`, `hard = 정상 최대를 넘은 폭주의 중앙값`
으로 만든다. 정상 최대 ≥ 폭주 최대면 **분리력 없음 → 축을 끈다.**

hard 를 중앙값으로 잡는 이유: **폭주의 첫 호출은 정상과 구별되지 않는다.**
1콜에서 걸리길 바라는 건 탐지가 아니라 예언이다.

**`budget_usd` 는 도출하지 않는다.** 상한 자체가 정책이다 — 시험 데이터에서 뽑으면
1센트의 10분의 1에서 정상 사용자가 막힌다.

### 2026-09-07 재도출 (실모델 대조군)

정상 94콜(4 주체) vs 재시도 폭주 37콜:

| 축 | 정상 max | 폭주 중앙 | soft | hard | 오탐 | 탐지 |
| --- | --- | --- | --- | --- | --- | --- |
| `duplicate_calls` | 3 | 19 | 5 | 20 | 0% | 89% |
| `run_steps` | 6 | 17 | 9 | 18 | 0% | 78% |
| `principal_calls` | 11 | 17 | 17 | 19 | 0% | 57% ⚠️간격의존 |
| `rotation_gap` | 2 | — | 표본 부족 → 끈다 | | | |
| `budget_usd` | 0.00717 | 0.00125 | 정책 축 — 도출 금지 | | | |

**정상이 폭주보다 비쌌다**($0.00717 vs $0.00125). 실모델 도구 루프가 토큰을 더 쓴다 —
비용 축만으로는 폭주와 정상이 갈리지 않는다는 실측이다.

### 2026-09-20 재도출 (코호트 3종 — 새 축 G3/G4/G2 판정)

> 라벨 정정(2026-10-03): 엔진·CRD·계약 기준 **G3 = `conv_growth`(대화 성장), G4 = `msg_bytes`(단일 호출 크기)**. 이 절은 처음에 둘을 뒤바꿔 적었다.

09-16 러너 재가동(핑퐁) 뒤 정상·재시도 코호트를 채워 처음으로 **세 코호트를 한 번에** 비교했다.
정상 78콜(agent-a\~d, `realistic_agents.py`) · 핑퐁 51콜(agent-f, k8s 러너) · 재시도 30콜(agent-e, `stuck_session.py`).
전부 실모델(`argus-real-gpt` → OpenRouter `openai/gpt-5.6-luna`). **정상 78콜 오탐 0.**

| 축 | 정상 max | soft | hard | 오탐 | 탐지(핑퐁 / 재시도) | 판정 |
| --- | --- | --- | --- | --- | --- | --- |
| `duplicate_calls` | 3 | 5 | 20 | 0% | 92% / 87% | 기존 값 재확인 |
| `run_steps` | 6 | 9 | 18 | 0% | 0% / 73% | 기존 값 재확인 |
| `principal_calls` | 12 | 18 | 26 | 0% | 67% / 43% | ⚠️ 아래 참조 |
| `conv_growth` (G3) | 64.65 | **96.975** | **130.2** | 0% | 51% / 0% | **신규 활성** |
| `msg_bytes` (G4) | 19,848 | 29,772 | 29,772 | 0% | **0% / 0%** | **분리력 없음 → 끈다** |
| `team_calls` (G2) | 20 | 30 | 30 | 0% | 41% / 3% | backstop 은 정책값 → 400 |
| `rotation_gap` | 2 | 3 | 6 | 0% | 74% / — | 간격 의존 → 계속 끔 |
| `budget_usd` | — | — | — | — | — | 정책 축 — 도출 금지 |

**`duplicate_calls` 5/20 과 `run_steps` 9/18 이 09-07 값과 정확히 일치했다.** 코호트를 새로 모아
독립적으로 다시 뽑았는데 같은 숫자가 나온 것 — 그 두 축의 임계는 우연이 아니다.

#### G4 `msg_bytes` 는 왜 못 켜나

정상 max 19,848 vs 핑퐁 max 21,606 으로 **거의 같다.** "정상이 도달한 값은 임계가 될 수 없다"를
지키면 soft 가 29,772(정상 max × 1.5)로 잡히고, 그러면 두 폭주 코호트 모두 탐지 0% 다.
축이 틀린 게 아니라 **이 코호트에 단일 호출이 커질 만큼 긴 세션이 없다.** 한화 모양을
따로 모으기 전엔 켤 수 없다 — 지금 켜면 "켰는데 아무것도 안 잡는" 축이 하나 늘 뿐이다.

#### G2 `team_calls` 는 왜 도출값을 안 쓰나

`backstop.teamCalls` 는 탐지 임계가 아니라 **넘을 수 없는 바닥**이다. 도출기가 낸 30 은 탐지
임계라 backstop 에 넣으면 정상 팀을 막는다. `principalCalls: 200` 이 정상 principal 최대(12)의
약 16배인 것과 같은 배율로 팀 정상 최대 20 → 320, 여유를 둬 **400** 으로 정했다. 정책값이다.

#### ⚠️ `principal_calls` 는 손대지 않았다

이번 도출은 soft 18 / hard 26 을 냈는데 현재 CR 은 17/19 다. hard 를 19→26 으로 **느슨하게**
바꾸는 방향이라, 데모 핵심 축을 얇은 근거로 완화하지 않는다. 09-07 값(정상 max 11 기준)과
이번(정상 max 12)의 차이는 대조군 호출 간격이 설계값인 데서 오는 흔들림으로 본다.

## 6. 알려진 구멍

- **identity exhaustion** — 한 행위자가 `user_id` 가 다른 키를 여러 개 쥐면 principal 이 갈라진다. **2026-09-08 부분 해소**: `backstop.teamCalls` 로 팀 단위 창 누적을 세는 `team_calls` 축(hard 전용)을 추가했다 — 키를 아무리 많이 발급받아도 같은 `team_id` 면 여기서 다시 묶인다. 팀을 넘어 조직을 갈라 쓰는 경우는 여전히 못 잡는다. 팀이 없는 header 티어는 2026-10-18 부터 **키 단위**로 팀 창에 묶인다 — 헤더를 돌려도 같은 키의 호출이 모인다. 대신 한 키를 나눠 쓰는 정직한 에이전트들도 한 팀 창을 같이 쓴다(팀 임계는 팀 규모라 보통 넉넉하다). 사다리를 정책으로 여는 `identity.principal`(STP-16 나머지)은 #15 합의 후.
- ~~**인자 변조**~~ → **2026-09-08 엔진 반영** (spec 006e "파라미터 제외" 규칙, STP-20). `duplicate_calls` 의 **hard 는 완전일치 지문, soft 는 정규화 지문도** 본다 — 값은 매번 변하는데 모양이 고정이거나 단조 증가하는 키(attempt·ts·직전 오류문)를 빼고 남은 정체성 키로 지문을 만든다. 남는 키가 없으면(매번 다른 파일) 세지 않는다. 관측치 `duplicate_calls_norm`. 정규화는 근사라 **단독 차단 금지** — 그래서 soft 전용이다.
- **`duplicate_calls` 에 시간 감쇠가 없다** — 대화별 최근 20개 지문 링버퍼라 쉬어도 안 풀린다. hard(20)가 창 크기(20)와 같아서 **A/B 교대 루프**(최대 10회)는 hard 에 안 걸리고, 같은 대화로 다시 돌면 첫 호출부터 막힌다(QUICKSTART run-3). 병렬 tool_calls 는 하나만 지문으로 쓴다(chat 첫 번째, Anthropic 마지막). 데모 이후 과제.
- ~~**도구를 한 번 쓴 대화의 평범한 질문이 반복으로 셈해진다**~~ → **2026-10-03 수정 (K1).** 마지막 도구 호출을 이력 전체에서 찾던 탓에 이어지는 일반 턴마다 같은 지문이 나와 20턴째 차단됐다. 사람이 새로 말한 턴 뒤엔 도구 호출이 없는 것으로 본다(`_is_plain_user_turn`).
- **`budget_usd` 는 기간 한도가 아니다** — LiteLLM `user_spend`/`team_spend` 의 누적 절대값이라 키에 `budget_duration` 이 없으면 사실상 평생 한도다. 한 번 넘은 주체는 쿨다운 뒤에도 매번 다시 걸린다. `budget_duration` 이 있으면 LiteLLM 이 지출을 리셋할 때 엔진도 따라 내려간다(2026-10-18, `cost_authoritative`). 탭 메모리 정산(key 티어가 아닌 주체)은 리셋을 모르므로 max 로 둔다.
- **관리자는 키·팀 단위로 ARGUS 를 끌 수 있다** — LiteLLM 관리자 metadata `disable_global_guardrails`·`opted_out_global_guardrails` 가 `default_on` 가드레일을 건너뛴다(v1.96.0 `custom_guardrail.py`). 꺼지면 훅이 안 돌아 레코드도 없다. 호출자는 못 끈다.
- **도구 없는 챗 반복** — `duplicate_calls` 의 축 키가 도구 인자 해시라, 도구를 안 쓰는
  동일 프롬프트 재시도는 이 축에 안 잡힌다. 백스톱(`principal_calls`·`budget_usd`) 소관이다.
- ~~**대화 크기·성장**~~ → **2026-09-08 축 추가** (`msg_bytes`·`conv_growth`, CRD `detect.messageBytes`·`detect.conversationGrowth`).
  2026-09-20 재도출로 `conv_growth` 는 **활성**(96.98 / 130.2), `msg_bytes` 는 분리력이 없어 **끔**(위 §5 2026-09-20 절). 실클라이언트 대조군에서 다시 도출할 것 — 첫 메시지가 아주 짧은 클라이언트는 성장률이 빨리 커진다.
- ~~**위조 시도가 레코드에 안 남는다**~~ → **2026-09-08 해소.** 자기신고 헤더가 key 티어에 밀리면
  `client_identity.claimed_principal`(header 티어 시드의 해시) + `claimed_conflicts_with_resolved: true`
  가 남는다. 원문은 안 싣고, 어긋날 때만 싣는다 (STP-32). 헤더 값이 키 티어 값(user_id·team_id·alias)과
  같으면 위조가 아니므로 남기지 않는다 — 2026-09-24 전에는 값을 비교하지 않아 협조적 에이전트가 매 호출 찍혔다 (STP-41).
- ~~**쿨다운이 다른 축을 가린다**~~ → **2026-09-08 해소.** 쿨다운 중에도 창·대화 지문·비용을 계속 누적하고 관측치를 실시간으로 낸다. 판정만 cooldown 으로 유지(히스테리시스). 집행 상태(blocked_until)는 LRU 축출과 분리된 맵에 둔다(STP-24 도 함께 해소). 쿨다운이 끝났을 때 창에 아직 임계 이상이 남아 있으면 즉시 다시 걸린다 — 의도된 동작이다.

## 7. 데이터 수명 — 판단에 쓰는 정보는 얼마나 사나 (2차 검증 B-F2, 2026-09-08)

| 데이터 | 어디 | 수명 | 근거 |
| --- | --- | --- | --- |
| 창 카운터 (`principal_calls`·`run_steps`·`rotation_gap`·`team_calls`) | 엔진 메모리 | `window` (기본 60s) 지나면 잊는다 | 슬라이딩 창 |
| 대화 지문 (`duplicate_calls`·정규화 프로파일·`conv_growth` 분모) | 엔진 메모리 | 최근 `repeat_window`(20) 콜 · 대화 LRU 상한 `max_principals`(10k) | 대화가 끝나면 쓸모없다 |
| 쿨다운 (`blocked_until`) | 엔진 메모리, LRU 밖 | `cooldown` (기본 30s) | 집행 상태 — 카운터와 수명이 달라 분리 |
| 예산 누적 (`budget_usd`) | key 티어: LiteLLM Postgres · 그 외: 탭 메모리 | 영속 / 파드 재기동까지 | 러닝 토탈 — 리셋은 최후 방어선 소실 |
| success 대기 레코드 | 탭 메모리 | `ARGUS_PENDING_TTL_S` (60s) — 안 오면 `usage_source: missing` 으로 기록 | 조용히 잃지 않기 위해 |
| `records.jsonl` | PVC | 매일 회전 → `records-YYYY-MM-DD.jsonl.gz`, `keepDays`(dev 30일) 뒤 삭제. 백업 폴더도 같은 기간 | argus-helm `litellm.records.retention` |
| 판정 이벤트(OTel `gen_ai.*`) | collector | 백엔드가 정한다 (지금은 debug exporter) | |

엔진 상태는 전부 상한이 있고 **오퍼레이터 재기동이면 사라진다** — key 티어 예산만 DB 에서 되살아난다.
카운터가 사라지는 방향은 과소 카운트 = fail-open 이라 안전한 쪽으로 틀린다.

## 함께 보기

- 폭주 6종 분류 — `docs/research/2026-07-26_runaway-taxonomy-draft.md`
- 위치 근거(AWS 레퍼런스 대조) — `docs/research/2026-08-18_hanwha-case-and-litellm-headers.md`
- 세션 헤더 근거표 — `docs/research/2026-08-16_competitor-and-session-protocols.md`
