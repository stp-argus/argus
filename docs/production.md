# 운영 배포

빠른 시작(`examples/quickstart.yaml`)과 달라지는 것만 적는다.

## 1. 비밀값은 미리 만든다

`litellm.generateSecrets` 는 빠른 시작용이다. GitOps(ArgoCD·Flux)는 `helm template` 으로 렌더해
`lookup` 이 비고, 동기화할 때마다 값이 바뀐다. 운영에서는 끄고 Secret 을 먼저 만든다. **값은 git 에 넣지 않는다.**

```bash
NS=argus-system
kubectl create ns $NS
kubectl -n $NS create secret generic litellm-master-key --from-literal=LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"
PGPW=$(openssl rand -hex 16)
kubectl -n $NS create secret generic litellm-db \
  --from-literal=POSTGRES_USER=litellm --from-literal=POSTGRES_PASSWORD="$PGPW" --from-literal=POSTGRES_DB=litellm \
  --from-literal=DATABASE_URL="postgresql://litellm:${PGPW}@litellm-db.${NS}.svc:5432/litellm"
kubectl -n $NS create secret generic provider-keys --from-literal=OPENROUTER_API_KEY=…   # 쓰는 공급자 키
```

## 2. values

출발점은 [`examples/production.yaml`](../examples/production.yaml) — OpenRouter·Bedrock 모델 등록, Bedrock 용
ServiceAccount(IRSA), `/v1/decide` NetworkPolicy, 영속 볼륨·레코드 보존이 들어 있다. 쓰지 않는 공급자는 지운다.

```bash
helm install argus oci://ghcr.io/stp-argus/charts/argus --version 0.2.0 -n argus-system -f examples/production.yaml
```

잘못된 키·값(예: `policy.detect.duplicateCall`, `controlLevel: Lockdown`)은 `values.schema.json` 이 설치 전에 거절한다.

- **Observe 로 시작한다.** 판정·기록은 다 돌고 차단만 안 한다. 정상 트래픽의 `would_block` 이 0 인 것을 본 뒤
  `controlLevel: Enforce` 로 바꾼다. GitOps 라면 이 한 줄을 커밋한다 (selfHeal 이 `kubectl patch` 를 되돌린다).
- **Bedrock**: LiteLLM 파드가 IRSA/Pod Identity 로 자격증명을 받게 한다 — `litellm.serviceAccount.create: true` +
  `annotations: {eks.amazonaws.com/role-arn: …}`(IRSA). 정적 키를 써야 하면 `aws_*` 파라미터를 Secret 간접 참조로.
- **에이전트는 `OPENAI_BASE_URL`(또는 `ANTHROPIC_BASE_URL`)만 게이트웨이로** 바꾸고, 각자 **가상 키**를 쓴다.
  키 발급·헤더·확인 방법은 [agents.md](agents.md) — `user_id` 는 에이전트, `team_id` 는 팀.
- **우회 차단은 네트워크 정책의 일이다.** 게이트웨이를 거치지 않는 직결 호출은 못 본다 — 모델 공급자 egress 를 막는다.

## 3. 알림 · 메트릭

차트 README §9 (`helm show readme oci://ghcr.io/stp-argus/charts/argus --version 0.2.0`) — Slack/webhook 알림(트립 순간 한 번), Prometheus
메트릭·알림 규칙(팀 지출 급증 포함), Grafana 대시보드. 전부 기본 off.

## 4. 알려진 한계 (0.x)

| 한계 | 영향 |
| --- | --- |
| 판정 카운터가 오퍼레이터 메모리에 있다 (replicas 1 고정 — 2 이상은 렌더가 실패한다) | 오퍼레이터 재기동·롤링 순간 창이 초기화된다. 새 파드는 정책을 렌더한 뒤에 Ready 가 된다. 판정 실패는 기본 **fail-open** — Envoy ext_authz·LiteLLM Guardrail API 의 기본(fail-closed)과 반대인 가용성 쪽 선택이다. `backstop.failureMode: Closed` 면 Guard·Enforce 에서 유예 60초 뒤 차단. 판정 서버가 안 보이면 `ArgusDecideDown` 이 뜬다 |
| LiteLLM `num_workers` 1 (파드당) | 수평 확장은 `litellm.replicas` — 레코드 저장소가 ReadWriteMany 여야 한다(`litellm.records.accessMode`, 파드별 `records-<pod>.jsonl`). 탐지는 오퍼레이터가 한 곳에서 센다 — kind 실측(단일 노드, 레코드 끔): 2 레플리카에 흩어진 25콜이 19번째부터 403 |
| `/v1/decide` 호출자 인증은 SA 토큰이다 (`decideAuth`, 기본 켬) | 같은 릴리스의 LiteLLM SA 만 판정을 요청할 수 있다(TokenReview, audience `argus-decide`). 외부 LiteLLM 은 `decideAuth.extraAllowedServiceAccounts` 와 투영 토큰 마운트가 필요하다([existing-litellm.md](existing-litellm.md)). `/metrics`·`/v1/policy` 는 열려 있다(임계가 보인다). 탭 쪽 실패(토큰 파일 없음·401/403)는 요청이 오퍼레이터에 닿지 않아 서버 메트릭에 안 잡힌다 — LiteLLM 로그의 `원격 판정 실패` 와 레코드 `decide_source: local` 비율로 본다. 네트워크로 한 겹 더 막으려면 `networkPolicy.enabled: true` — kind 실측: 켜면 다른 파드 000(타임아웃), 끄면 200 |
| 기본 임계는 출발점이다 | `principalCalls` hard 19/60초는 2026-09-07 대조군(정상 최대 11)에서 나왔고, 09-20 재도출은 26 을 냈다. 병렬 도구 호출·서브에이전트를 쓰는 에이전트는 분당 19콜을 쉽게 넘는다 — Guard·Enforce 전에 Observe 로 우리 트래픽의 정상 최대를 보고 다시 정한다([DETECTION.ko.md §5](https://github.com/stp-argus/argus/blob/dev/docs/DETECTION.ko.md)). 이 축은 volume 범주라 Guard 에서도 막는다 |
| 탐지 사각지대 | 창(60초)보다 천천히 새는 폭주는 `budget_usd` 만 잡는다. 도구 없는 동일 프롬프트 반복은 `duplicate_calls` 에 안 잡힌다. A/B 교대 루프는 지문별 10회라 `duplicate_calls` hard(20)에 안 닿는다. `budget_usd` 는 LiteLLM 누적 지출이라 키에 `budget_duration` 이 없으면 평생 한도다. LiteLLM 관리자는 키·팀 metadata 로 가드레일을 끌 수 있다. 정본은 [DETECTION.ko.md §6](https://github.com/stp-argus/argus/blob/dev/docs/DETECTION.ko.md) |
| 한도는 호출 수와 USD 뿐이다 | 토큰(TPM)·모델별 한도는 없다 — 거대한 호출 하나도 1콜로 센다. 필요하면 LiteLLM 의 `tpm_limit`·모델별 한도를 같이 쓴다. 같은 주체의 동시 호출은 예산을 조금 넘길 수 있다(`max_parallel_requests` 로 막는다) |
| 다른 네임스페이스의 LiteLLM 은 (기본 설치에선) 정책 파일을 마운트하지 못한다 | 실패 모드·identity 는 판정 응답으로 전달된다(오퍼레이터가 살아 있을 때 첫 호출만 기본값). 오퍼레이터가 죽은 동안 LiteLLM 이 재기동되면 기억이 없어 fail-open. 로컬 폴백의 **임계**는 env 기본값(Observe) |
| 레코드 백업·보존 CronJob 은 RWO 레코드 PVC 를 LiteLLM 파드와 동시에 마운트한다 | 멀티노드(EBS 등)에서는 다른 노드에 스케줄되면 Multi-Attach 로 멈춘다 — 단일 노드거나 RWX 스토리지에서 켠다 |
| 지원 와이어: OpenAI Chat · Anthropic Messages · OpenAI Responses · Bedrock pass-through(`/bedrock/model/{id}/converse`·`/invoke` 의 Anthropic 본문) | 스트리밍 pass-through(`converse-stream`·`invoke-with-response-stream`)는 요청 쪽 탐지·차단만 되고 응답 비용은 LiteLLM 이 정산한 값에 기댄다. Anthropic 이외 모델의 InvokeModel 본문은 해석하지 않는다(`wire=unknown` — 호출 수·예산 축은 그대로 돈다) |
