# ARGUS Quick Start

**5분, API 키 없이.** 로컬 쿠버네티스에 ARGUS 를 설치하고, 폭주하는 에이전트를 흉내 내서
**① 탐지(Observe) → ② 차단(Enforce, 403)** 을 직접 본다.

## 준비물

| 도구 | 확인 |
| --- | --- |
| 쿠버네티스 클러스터 (kind · minikube · Docker Desktop 중 하나) | `kubectl get nodes` 가 Ready |
| Helm 3.12+ | `helm version` |
| curl | |

클러스터가 없다면 kind 로 하나 만든다: `kind create cluster --name argus`

## 1. 설치

```bash
git clone https://github.com/stp-argus/argus && cd argus
helm install argus oci://ghcr.io/stp-argus/charts/argus --version 0.2.0 -n argus-system --create-namespace -f examples/quickstart.yaml
kubectl -n argus-system rollout status deploy/litellm --timeout=5m
```

설치되는 것: ARGUS 오퍼레이터 · LiteLLM 게이트웨이(+ ARGUS 가드레일) · 가상 키용 Postgres ·
**mock 모델**(실제 LLM 대신 — 키·비용 0) · 정책 `observe-default`(Observe).

## 2. 실행 — 폭주하는 에이전트 흉내

터미널 A — 게이트웨이를 연다:
```bash
kubectl -n argus-system port-forward svc/litellm 4000:4000
```

터미널 B — 가상 키를 발급하고(ARGUS 는 이 키의 `user_id` 로 "누가"를 센다), 같은 도구 호출을 25번 반복한다:
```bash
MASTER=$(kubectl -n argus-system get secret litellm-master-key -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
KEY=$(curl -s localhost:4000/key/generate -H "Authorization: Bearer $MASTER" \
  -H 'content-type: application/json' -d '{"user_id":"demo-agent"}' | sed -E 's/.*"key":"([^"]+)".*/\1/')

BODY='{"model":"any","messages":[{"role":"user","content":"read x"},{"role":"assistant","content":null,"tool_calls":[{"id":"c1","type":"function","function":{"name":"read_file","arguments":"{\"path\":\"x\"}"}}]},{"role":"tool","tool_call_id":"c1","content":"EIO"}]}'
runaway() { for i in $(seq 1 25); do curl -s -o /dev/null -w '%{http_code} ' localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -H "X-Run-Id: $1" -d "$BODY"; done; echo; }

runaway run-1
```
출력: `200 200 … 200` — 지금은 **Observe** 라 아무것도 막지 않는다.

## 3. 첫 결과 확인

### ① 탐지 — "보고는 있었다"
```bash
kubectl -n argus-system logs -l app.kubernetes.io/component=operator | grep '\[argus\]'
```
```
[argus] 탐지 (would_block · Observe — 통과시킴) principal=… run=run-1 | run_steps/hard=18>=18 | id=d-…
```
한 run(run-1) 안의 18번째 호출이 `run_steps` 임계(18)에 닿아 폭주로 판정했다. Observe 라 통과시켰다.

### ② 차단 — "선언 한 줄을 바꾼다"
```bash
kubectl -n argus-system patch runawaypolicy observe-default --type merge -p '{"spec":{"controlLevel":"Enforce"}}'
runaway run-2
```
출력: `403 403 … 403` — 파드 재시작 없이 **바로 다음 호출부터** 막힌다. run 을 바꿨는데도 첫 호출부터 403 인 이유는 둘이다 —
같은 에이전트(`demo-agent`)가 방금 폭주 판정을 받아 **쿨다운(30초)** 중이고, 같은 대화(세션 헤더가 없어 첫 메시지로 묶인다)에
같은 도구 호출 지문 20개가 남아 있다. 지문 창에는 시간 감쇠가 없어서 쉬었다가 같은 대화로 다시 돌려도 첫 호출부터 막힌다.
```bash
curl -s -D - -o /dev/null localhost:4000/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -H 'X-Run-Id: run-2' -d "$BODY" | grep -i '^x-argus'
```
```
x-argus-decision-id: d-…
x-argus-control-level: Enforce
```
차단된 호출은 **모델에 도달하기 전에** 끊겼다 — 비용 0.

## 여기서 무엇이 일어났나

```
에이전트 ──► LiteLLM 게이트웨이 ──► ① 인증(가상 키 → user_id)
                                 ② ARGUS 가드레일 pre_call ──► 오퍼레이터 /v1/decide (6축 판정)
                                      Enforce 면 여기서 403  ◄──  RunawayPolicy CRD
                                 ③ 모델 호출  ← 여기부터 돈이 나간다
```
ARGUS 는 ②에 있다. 관측 도구는 ③ **이후**를 본다 — *보는 것 ≠ 막는 것.*

## 정리
```bash
helm uninstall argus -n argus-system
kubectl delete ns argus-system && kubectl delete crd runawaypolicies.argus.stp.io
```

## 막혔을 때

| 증상 | 원인·해결 |
| --- | --- |
| `ImagePullBackOff` (argus) | 오퍼레이터 이미지를 못 받는다. 소스에서 빌드해 넣는다: `docker build -t argus-operator:local . && kind load docker-image argus-operator:local --name argus` 후 `helm upgrade argus oci://ghcr.io/stp-argus/charts/argus --version 0.2.0 -n argus-system -f examples/quickstart.yaml --set image.repository=argus-operator --set image.tag=local --set image.pullPolicy=Never` |
| PVC `Pending` | 기본 StorageClass 가 없다. `kubectl get sc` 로 확인하고 `--set litellm.records.storageClassName=<이름>` |
| 전부 200, 탐지 로그 없음 | 25콜을 60초 안에 보냈는지, `X-Run-Id` 헤더가 붙었는지 확인 |
| `403` 이 안 나온다 | `kubectl -n argus-system get runawaypolicy observe-default -o jsonpath='{.spec.controlLevel}'` 가 `Enforce` 인지 |

## 다음
- 실제 모델(OpenRouter·OpenAI·Bedrock 등)과 운영 설정 → [docs/production.md](docs/production.md)
- 이미 운영 중인 LiteLLM 에 ARGUS 만 얹기 → [docs/existing-litellm.md](docs/existing-litellm.md)
- 알림(Slack·webhook)·메트릭·대시보드 → 차트 README §9 (`helm show readme oci://ghcr.io/stp-argus/charts/argus --version 0.2.0`)
