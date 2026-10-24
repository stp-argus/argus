<!-- SPDX-License-Identifier: Apache-2.0 -->

# ARGUS 아키텍처

> 탐지 원리(신원·축·임계의 근거)는 [DETECTION.ko.md](DETECTION.ko.md), 레코드·헤더·메트릭의 정확한
> 필드는 [decision-event-contract.md](decision-event-contract.md) 가 정본이다. 이 문서는 **부품과 흐름**만 다룬다.

## 풀려는 문제

LLM 에이전트는 사람이 보지 않는 동안 스스로 호출을 이어 간다. 같은 도구를 같은 인자로 계속 부르거나,
실패한 호출을 재시도 폭풍으로 되풀이하거나, run·세션을 바꿔 가며 같은 일을 반복한다. 호출 하나하나는
정상이라 요청 단위 검사로는 보이지 않고, 비용 대시보드에는 **돈이 나간 뒤에** 나타난다.

ARGUS 는 모든 호출이 지나는 게이트웨이에 서서, 호출을 **누가·어느 작업에서** 했는지로 묶어 세고,
패턴이 임계를 넘으면 **업스트림 호출 전에** 판정한다.

## 부품

| 부품 | 위치 | 하는 일 |
| --- | --- | --- |
| **RunawayPolicy** (CRD) | `api/v1alpha1/` | 제어 레벨(Observe 기록만 · Guard 사용량·비용 축 hard 만 차단(detect 의 principalCalls 포함) · Enforce 전부 차단), 창·쿨다운, 탐지 축 임계, backstop 을 선언 |
| **컨트롤러** | `internal/controller/` | 정책을 watch → 여러 CR 을 하나로 렌더 → 엔진에 즉시 반영 + `argus-policy` ConfigMap 기록 + CR `status`(activeAxes·conditions) |
| **판정 엔진** | `internal/decide/` | `POST /v1/decide`. 인메모리 카운터로 축별 관측값을 계산하고 allow·block 과 `would_block` 플래그를 돌려준다. `:8080` 에 `/metrics` 도 같이 둔다 |
| **가드레일** (탭) | `hack/guardrail/` | LiteLLM `pre_call` 훅. 요청에서 신호(주체·대화·run·도구 호출·비용)를 뽑아 엔진에 묻고, 차단 판정이면 차단 응답(차트 기본 403 · 코드 기본 429, `ARGUS_BLOCK_STATUS`)을 낸다. 호출마다 레코드 한 줄, 트립마다 알림 |
| **LiteLLM** | (외부, 차트가 배포) | 가상 키 인증 → `user_id`·`team_id` 제공, 모델 라우팅·비용 정산 |
| **차트** | `oci://ghcr.io/stp-argus/charts/argus` | 위 전부 + Postgres(가상 키) + 선택 기능(mock 모델·알림·ServiceMonitor·PrometheusRule·Grafana) |

## 한 호출의 흐름

```
에이전트 ── OPENAI_BASE_URL ──► LiteLLM
                                 ① 가상 키 인증 ─► user_id · team_id
                                 ② ARGUS 가드레일 (pre_call)
                                      신호 추출: 주체 · 대화 · run · 도구 호출 · 누적 비용
                                      │  POST /v1/decide  (타임아웃 500ms)
                                      ▼
                                 ARGUS 오퍼레이터 ─ 판정 엔진 (인메모리 창)
                                      │  allow · block (+ would_block)
                                      ▼
                                   block        ─► 403 + x-argus-decision-id   (비용 0)
                                   그 외           ─► 통과
                                 ③ 모델 호출 ─► 업스트림 (OpenAI · OpenRouter · Bedrock …)
                                 ④ 성공 훅: 실제 비용·토큰을 레코드에 채움 (실패 훅: status=error + 사유)
```

- **②에 서는 이유**: 돈이 나가기 전이고, 세션 헤더·도구 호출 같은 클라이언트 원형이 ③의 provider 변환
  전에만 온전하며, 인증된 신원(`user_api_key_dict`)이 이미 있다.
- **신원은 두 축**: 주체(principal)는 가상 키 `user_id` 우선의 사다리라 키를 재발급해도 같은 주체로 센다.
  대화(conversation)는 세션 헤더 → 프로토콜 필드 → 파생 순이다. 자세한 사다리는 DETECTION §2.

## 판정 축과 범주

| 축 | 스코프 | 범주 |
| --- | --- | --- |
| `duplicate_calls` — 같은 도구·같은 인자 반복 | 대화 | behavior (이상 행동) |
| `run_steps` — 한 run 안의 스텝 수 (`X-Run-Id` 없으면 평가 안 함) | run | behavior |
| `conv_growth` — 대화 크기 증가율 | 대화 | behavior |
| `principal_calls` — 창 안의 주체 호출 수 (+ backstop) | 주체 | volume (이상 사용량) |
| `team_calls` — 창 안의 팀 호출 수 (backstop) | 팀 | volume |
| `budget_usd` — 주체 누적 비용 (backstop) | 주체 | cost (이상 비용) |

`msg_bytes`·`rotation_gap` 은 구현돼 있지만 기본값은 꺼짐이다(근거는 [thresholds.md](thresholds.md)).
축마다 soft(기록)와 hard(차단 후보) 두 임계를 둔다. hard 를 실제로 막는 건 Enforce(모든 축)와 Guard(volume·cost 축만)다. 인증된 신원(키 티어)이 있는데 자기신고 헤더
(`X-Principal-Id`)가 **다른 값으로** 함께 오면 가드레일이 **identity** 범주(`identity_spoof_attempt`)를 기록 신호로 붙인다.
헤더가 키 신원과 같은 값이면 협조적 에이전트이므로 붙이지 않는다.

## 정책이 바뀔 때

```
kubectl apply / GitOps ─► RunawayPolicy ─► 컨트롤러 Reconcile
                                             ├─► Engine.SetPolicy     ← 다음 /v1/decide 부터 새 정책 (재시작 없음)
                                             ├─► ConfigMap argus-policy ← 가드레일 로컬 폴백 + 신원(identity) 설정 (kubelet 동기화, 수십 초)
                                             └─► status.activeAxes · conditions
```

가드레일은 이 파일을 5초 간격으로 확인한다 — 원격 판정이 정상인 동안에도 `identity` 블록 변경을 따라간다.

`controlLevel` 을 Observe → Enforce 로 바꾸면 바로 다음 호출부터 차단된다
(kind 실측, 2026-09-24: patch 명령이 약 0.05초에 반환됐고 바로 다음 호출이 403 이었다).

## 장애 시

- **오퍼레이터가 없거나 느리면**: 가드레일이 500ms 뒤 **로컬 폴백** — 같은 판정 로직의 파이썬 판본
  (`argus_decide.py`, Go 와 동등성 테스트로 묶임)이 ConfigMap 정책으로 판정한다. 카운터는 공유되지
  않으므로 폴백 중에는 창이 새로 시작된다. 실측: Observe 에서 오퍼레이터 파드를 지우는 동안 75/75 호출 200.
- **기본은 fail-open** — 로컬 판정까지 실패하면 통과시키고, 레코드에 `decide_error` 로 남긴다.
- **`backstop.failureMode: Closed`** (Guard·Enforce) — 로컬 폴백은 backstop 카운터를 새로 세므로 과소 집계한다.
  그래서 오퍼레이터가 `ARGUS_FAIL_CLOSED_GRACE_S`(기본 60초)보다 오래 안 닿으면 막는다
  (`decide_source=fail_closed`, `decision_id` 가 `fc-…`). 유예는 오퍼레이터 재기동 몇 초로 전체 트래픽이 끊기지 않게 하려는 것이고,
  **장애 시작이 아니라 첫 실패 호출부터** 센다. 로컬 판정까지 실패하면 유예 없이 막는다(이때는 `decide_source=local` + `decide_error`).
- **정책 파일이 없는 가드레일**(다른 네임스페이스의 LiteLLM): 오퍼레이터가 판정 응답에 `failure_mode` 와 CR 의 `identity` 를
  실어 보내고, 가드레일이 마지막 값을 기억해 다음 호출부터 쓴다. 파일이 있으면 파일이 이긴다.
  ⚠️ 기억은 프로세스 메모리다 — 오퍼레이터가 죽은 동안 LiteLLM 이 재기동되면 기억이 없어 fail-open 으로 돈다.

## 무엇이 밖으로 나오나

| 출력 | 어디서 | 쓰임 |
| --- | --- | --- |
| 레코드 `records.jsonl` | 가드레일 (PVC. 차트 백업 CronJob 이 하루 한 번 복사, `records.retention.enabled` 를 켜면 gzip 회전·보존 — 기본 꺼짐) | 호출 한 줄 = 신원·요청 모양·판정·비용. 마감(응답 대기 60초·실패 5초)이 지난 대기 레코드는 5초 간격 스위퍼가 내린다 — 다음 요청이 없어도. `hack/analyze/governance_report.py` 로 범주·주체·팀별 집계 |
| 메트릭 `argus_*` | 오퍼레이터 `:8080/metrics` | 판정·탐지·트립·팀 호출·팀 비용 카운터 → PrometheusRule(`ArgusRunawayDetected`, `ArgusTeamCostSpike` 등) · Grafana |
| 알림 webhook | 가드레일 | 트립 순간 한 번(쿨다운 중 반복 억제). Slack 형식·LiteLLM 알림과 같은 키. 신원 노출은 `hash`·`labels`·`full` 중 선택 |
| 차단 응답 | 가드레일 | `403`(차트 기본) + `x-argus-decision-id`·`x-argus-control-level` 헤더. 본문은 LiteLLM 오류 봉투에 싸이므로 클라이언트는 **헤더로** 판별 |

## 기존 방식과 다른 점

| | 비용·관측 도구 | 게이트웨이 예산·레이트 리밋 | ARGUS |
| --- | --- | --- | --- |
| 보는 시점 | 호출 후 | 호출 전 | 호출 전 |
| 세는 단위 | 요청·비용 | 키·팀별 금액·요청 수 | 주체·대화·run·팀별 **행동 패턴** |
| 같은 도구 반복 | 사후 분석 | 클라이언트 루프는 못 본다 (LiteLLM Agentic Loop Hook 은 프록시가 직접 도는 서버사이드 루프용) | `duplicate_calls` |
| run·세션 교체 | 새 단위로 보임 | 세션 한도는 새 세션에서 초기화, 키·사용자 예산은 금액만 누적 | 주체(`user_id`) 로 계속 누적 |
| 적용 방식 | — | 한도에 닿으면 차단 | Observe(기록만) → Guard(사용량·비용만 차단) → Enforce 단계적 |

ARGUS 는 게이트웨이를 대체하지 않는다. LiteLLM 의 인증·라우팅·예산 위에 **행동 판정 한 층**을 얹는다.

## 코드 지도

```
api/v1alpha1/            RunawayPolicy 타입 (kubebuilder 마커 → config/crd)
internal/controller/     정책 렌더 · 테넌트 격리(attachment) · status
internal/decide/         엔진 · /v1/decide 서버 · 범주 · 메트릭
hack/guardrail/          LiteLLM 가드레일 (신호 추출 · 원격/로컬 판정 · 레코드 · 알림)
hack/analyze/            레코드 분석기 (임계 도출 · 거버넌스 리포트)
hack/demo/               로컬 데모 스크립트
config/                  kubebuilder 매니페스트 — CRD(config/crd/bases)가 정본이고 릴리스가 차트로 복사. 나머지 배포 정본은 차트
```
