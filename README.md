# ARGUS

**LLM 에이전트의 폭주를 호출 경로에서 탐지하고, 원하면 돈이 나가기 전에 끊는다.**

에이전트는 사람이 보지 않는 동안 같은 도구를 같은 인자로 계속 부르고, 실패를 재시도 폭풍으로 되풀이하고,
run 을 바꿔 가며 같은 일을 반복한다. 호출 하나하나는 정상이라 비용 대시보드에는 돈이 나간 **뒤에** 보인다.
ARGUS 는 LiteLLM 게이트웨이 안에서 호출을 **누가·어느 작업에서** 했는지로 묶어 세고, 패턴이 임계를 넘으면
업스트림 호출 **전에** 판정한다. 에이전트 코드는 고치지 않는다 — `OPENAI_BASE_URL` 만 게이트웨이로 돌린다.

```yaml
apiVersion: argus.stp.io/v1alpha1
kind: RunawayPolicy
metadata: { name: observe-default, namespace: argus-system }   # 오퍼레이터가 있는 네임스페이스
spec:
  controlLevel: Observe            # 기록만 → Guard(사용량·비용 축만 차단) → Enforce(전부 차단)
  detect:
    duplicateCalls: { soft: 5, hard: 20 }    # 같은 도구·같은 인자 반복
    runSteps:       { soft: 9, hard: 18 }    # 한 run 의 스텝 폭증
  backstop:
    principalCalls: 200                      # run·키를 바꿔도 같은 주체로 누적
    budgetUSD: "1"
```

## 빠른 시작

API 키 없이 로컬 쿠버네티스에서 5분 — **[QUICKSTART.md](QUICKSTART.md)**

```bash
git clone https://github.com/stp-argus/argus && cd argus
helm install argus oci://ghcr.io/stp-argus/charts/argus --version 0.2.0 -n argus-system --create-namespace -f examples/quickstart.yaml
```

## 무엇을 하나

| | |
| --- | --- |
| **탐지** | 같은 도구 반복 · run 스텝 폭증 · run 회전 · 단일 호출 크기·대화 성장 · 주체·팀 호출 폭증 · 예산 초과 — 8축(backstop 포함). 임계는 실측 대조군에서 도출 |
| **차단** | `controlLevel` 한 줄 — `Guard` 는 사용량·비용 상한만, `Enforce` 는 모든 축. 파드 재시작 없이 다음 호출부터 403(차트 기본), 업스트림 호출 전이라 비용 0 |
| **신원** | 가상 키 `user_id`(에이전트)·`team_id`(팀)로 "누가"를 센다 — 키를 재발급하거나 세션을 바꿔도 같은 주체. 발급 절차는 [docs/agents.md](docs/agents.md) |
| **거버넌스** | 이상 비용 · 이상 행동 · 이상 사용량 · 신원(키 신원과 다른 자기신고 헤더)으로 분류 → webhook(Slack) 알림, Prometheus 메트릭, 호출별 감사 레코드 |
| **안전** | 오퍼레이터가 죽어도 가드레일이 같은 로직의 로컬 판정으로 이어받는다. 기본 fail-open, 정책으로 fail-closed 선택 |

```
에이전트 ─► LiteLLM ① 인증 ② ARGUS 가드레일 ──► ARGUS 오퍼레이터 /v1/decide ◄── RunawayPolicy
                            차단 판정이면 403
                     ③ 모델 호출 ← 여기부터 비용
```

## 문서

| | |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 부품 · 호출 흐름 · 정책 반영 · 장애 시 동작 · 기존 방식과의 차이 |
| [docs/DETECTION.ko.md](docs/DETECTION.ko.md) | 탐지 원리 — 신원 사다리, 축과 스코프, 임계의 근거, 알려진 구멍 |
| [docs/decision-event-contract.md](docs/decision-event-contract.md) | 레코드 스키마 · 헤더 · 메트릭 · 알림 페이로드 |
| [docs/thresholds.md](docs/thresholds.md) · [docs/block-contract.md](docs/block-contract.md) | 임계 도출 근거 · 차단 응답 계약 |
| [docs/design/](docs/design/) · [docs/research/](docs/research/) | 설계 결정과 조사 기록 |

## 레포

| | |
| --- | --- |
| **argus** (여기) | 오퍼레이터 · 판정 엔진 (Go) · LiteLLM 가드레일 (Python) |
| 차트 `oci://ghcr.io/stp-argus/charts/argus` | 설치 — CRD·오퍼레이터·LiteLLM·Postgres 를 `helm install` 한 번으로. 운영 문서는 [docs/production.md](docs/production.md) |

## 개발

```bash
make test                                                         # Go: 엔진 · 컨트롤러 (envtest)
python3 -m unittest discover -s hack/guardrail -p 'test_*.py'     # 가드레일
```
클러스터 관통 시험은 argus-helm 의 `hack/kind-up.sh && hack/e2e.sh`. 기여 절차는 [CONTRIBUTING.md](CONTRIBUTING.md).

## 라이선스

[Apache License 2.0](LICENSE) · CloudBro Ship to Production 시즌 III 프로젝트.
