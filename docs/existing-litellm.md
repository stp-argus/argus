# 이미 운영 중인 LiteLLM 에 ARGUS 얹기

게이트웨이는 그대로 두고 **판정 엔진(오퍼레이터) + 가드레일 파일 3개**만 더한다.
요구: LiteLLM **v1.96.0 이상** — 그 미만은 커스텀 가드레일을 config 에서 읽기만 하고 **에러 없이** 실행하지 않는다
(`/guardrails/list` 의 `guardrail_id` 가 `null` 이면 미발화).

## 1. 오퍼레이터만 설치

```bash
helm install argus oci://ghcr.io/stp-argus/charts/argus --version 0.2.0 -n argus-system --create-namespace -f examples/quickstart.yaml \
  --set litellm.enabled=false --set mockUpstream.enabled=false
```
빠른 시작 프리셋의 정책 임계(Observe)만 가져오고 게이트웨이·mock 은 설치하지 않는다. `policy.detect`·`policy.backstop`
을 비우면 활성 축이 0 이라 아무것도 탐지하지 않는다(CR 상태 `Programmed=False`). 판정 API: `http://argus.argus-system.svc:8080/v1/decide`.

판정 API 는 호출자의 SA 토큰을 확인한다(`decideAuth`, 기본 켬). 외부 LiteLLM 의 SA 를 허용 목록에 넣는다.
목록이 비면(이 설치처럼 `litellm.enabled=false` 이고 아래를 안 주면) 오퍼레이터는 인증을 끄고 시작한다:
```bash
  --set 'decideAuth.extraAllowedServiceAccounts[0]=<litellm-ns>/<litellm-sa>'
```
네트워크로 한 겹 더 막으려면 NetworkPolicy 도 켠다:
```bash
  --set networkPolicy.enabled=true \
  --set 'networkPolicy.decideFrom[0].namespaceSelector.matchLabels.kubernetes\.io/metadata\.name=<litellm-ns>'
```

## 2. 가드레일 파일을 LiteLLM 파드에 마운트

```bash
kubectl -n <litellm-ns> create configmap argus-guardrail \
  --from-file=hack/guardrail/argus_guardrail.py \
  --from-file=hack/guardrail/argus_signals.py \
  --from-file=hack/guardrail/argus_decide.py
```
LiteLLM Deployment 에 (디렉터리째 — 세 파일이 서로 import 한다):
```yaml
volumeMounts:
  - {name: argus-guardrail, mountPath: /app/guardrails}
  - {name: argus-decide-token, mountPath: /var/run/secrets/argus, readOnly: true}
volumes:
  - {name: argus-guardrail, configMap: {name: argus-guardrail}}
  # 판정 API 전용 토큰. audience 가 API 서버가 아니라 이 토큰으로는 쿠버네티스 API 를 못 부른다.
  - name: argus-decide-token
    projected: {sources: [{serviceAccountToken: {audience: argus-decide, expirationSeconds: 3600, path: token}}]}
env:
  - {name: ARGUS_DECIDE_URL,    value: http://argus.argus-system.svc:8080/v1/decide}
  - {name: ARGUS_DECIDE_TOKEN_FILE, value: /var/run/secrets/argus/token}
  - {name: ARGUS_BLOCK_STATUS,  value: "403"}                 # 429 면 SDK 가 자동 재시도한다
  - {name: ARGUS_RECORDS_PATH,  value: /var/argus/records/records.jsonl}
```

## 3. LiteLLM config 에 등록

```yaml
guardrails:
  - guardrail_name: argus
    litellm_params:
      guardrail: guardrails.argus_guardrail.ArgusObserve
      mode: pre_call
      default_on: true
```
그리고 LiteLLM 을 `--num_workers 1` 로 띄운다 (판정 폴백·기록 대기열이 프로세스 메모리에 있다).

## 4. 확인

- LiteLLM 로그에 요청마다 `[argus]` 줄 또는 `ARGUS_RECORDS_PATH` 에 레코드가 쌓인다
- 레코드의 `principal_source` 가 `key:user_id` 면 가상 키 신원이 잡힌 것 (`header` 면 마스터 키로 호출 중)
- `kubectl -n argus-system get runawaypolicy` 의 `controlLevel` 을 Enforce 로 바꾸면 폭주 호출이 403

## 알아둘 것

- 정책 파일(`/etc/argus/policy/policy.json`)은 오퍼레이터 네임스페이스의 ConfigMap `argus-policy` 다.
  **기본 설치에선 다른 네임스페이스의 LiteLLM 이 마운트할 수 없다** — 원격 판정·실패 모드·identity 는 판정 응답으로 전달되고,
  오퍼레이터가 죽었을 때의 로컬 폴백 **임계**만 env 기본값(Observe)으로 돈다. 같은 네임스페이스라면 마운트하고 `ARGUS_POLICY_FILE` 을 준다.
- 판정이 실패하면 기본은 **통과시킨다(fail-open)** — 게이트웨이가 ARGUS 때문에 멈추지 않는다. 정책 `backstop.failureMode: Closed` 면
  Guard·Enforce 에서 오퍼레이터가 60초 넘게 안 닿을 때 막는다. 정책 파일이 없는 다른 네임스페이스에서도 동작한다 —
  가드레일이 오퍼레이터 판정 응답의 `failure_mode` 를 기억한다. CR 의 `identity`(대화 헤더 등)도 같은 방식으로 전달된다 — 첫 호출만 기본값. 기억은 프로세스 메모리라 오퍼레이터가 죽은 동안 LiteLLM 이 재기동되면 fail-open 이다.
