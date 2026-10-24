# ARGUS 데모 — 샘플앱이 폭주하고, ARGUS 가 잡는 것을 보여준다

**터미널 세 개**로 진행한다. 판정이 오퍼레이터로 옮겨온 뒤(8/17)로는
오퍼레이터 로그가 관제탑 화면이다 — 데모의 정점(4막)이 거기서 나온다.

```
터미널 1 (왼쪽·크게)   레코드 스트림 + 축 게이지          live_view.py
터미널 2 (오른쪽 위)   관제탑 — 오퍼레이터 탐지·차단 로그   kubectl logs -f
터미널 3 (오른쪽 아래)  에이전트가 도는 화면               run_demo.sh
```

## 준비 — kind 기준 (권장: dev 와 같은 경로, 남의 트래픽 없음)

```bash
colima start --cpu 4 --memory 6
bash ../argus-helm/hack/kind-up.sh   # 차트 기준 (CRD·오퍼레이터·탭·정책 CR·Secret). 2026-09-08 부터

# 배선이 실제로 도는지 — 존재가 아니라 동작의 흔적을 본다 (6단계)
KCTX=kind-argus-local bash hack/verify-cluster.sh

# 터미널 1 — 레코드를 로컬로 흘리면서 라이브 뷰
kubectl -n argus-system port-forward svc/litellm 4400:4000 &
POD=$(kubectl -n argus-system get pod -l app.kubernetes.io/component=litellm -o name | head -1)
kubectl -n argus-system exec ${POD#pod/} -- sh -c 'tail -f /var/argus/records/records.jsonl' \
  > /tmp/demo-records.jsonl &
python3 hack/demo/live_view.py /tmp/demo-records.jsonl

# 터미널 2 — 관제탑
kubectl -n argus-system logs -l app.kubernetes.io/component=operator -f | grep --line-buffered '\[argus\]'

# 터미널 3 — 데모 드라이버
ARGUS_BASE=http://127.0.0.1:4400 ARGUS_KEY=sk-argus-kind \
  RECORDS=/tmp/demo-records.jsonl DEMO_KCTX=kind-argus-local \
  bash hack/demo/run_demo.sh
```

시작 전 확인 둘 — 이게 null/default 면 시작하지 않는다:

```bash
curl -s localhost:4400/guardrails/list -H "Authorization: Bearer sk-argus-kind" | jq '.guardrails[0].guardrail_id'
kubectl -n argus-system exec deploy/litellm -- python3 -c \
  'import urllib.request;print(urllib.request.urlopen("http://argus.argus-system.svc:8080/v1/policy").read().decode())'
#  → "source":"crd:argus-system/observe-default" 여야 한다. "default" 면 선언이 안 닿은 것
```

---

## 4막 구성

### 1막 · 정상 에이전트 — **아무 일도 안 일어난다**

여러 principal 이 각자, run 당 3~5스텝에 작업을 끝낸다. 도구 인자가 매번 다르다.

> **말할 것**: "폭주를 잘 막는 것보다 정상을 안 막는 것이 훨씬 어렵습니다.
> 오탐 하나가 남의 진짜 작업을 끊습니다. 그래서 이 화면이 조용한 것이 결과입니다.
> 실측 오탐 0/29 — 임계값을 정상 트래픽 분포에서 도출했기 때문입니다."

화면: 게이지 낮음 · 관제탑(터미널 2) 침묵 · 차단 0.

### 2막 · 재시도 폭풍 — **동일 호출 반복 축이 잡는다**

같은 도구를 같은 인자로 반복한다. **대화는 매 스텝 자란다.**

> **말할 것**: "대화가 자라니까 전체 프롬프트 해시는 매번 다릅니다.
> 프롬프트를 통째로 해싱하는 탐지기는 여기서 전부 실패합니다.
> 우리는 (도구, 인자) 지문을 봅니다 — 대화 성장에 오염되지 않습니다."

화면: `동일 호출 반복` 게이지가 차오르고, **관제탑에 첫 줄이 뜬다**:
```
[argus] 탐지 (would_block · Observe — 통과시킴) principal=… | duplicate_calls/hard=20>20 | id=d-…
```
> "Observe 라서 지금은 기록만 합니다 — 무엇이 차단됐을 것인지가 남습니다."

### 3막 · 핑퐁 재귀 — **여기가 논지다** ⭐

runId 를 5콜마다 회전시킨다.

> **말할 것**: "두 축을 나란히 보십시오.
> **run 스텝수**는 5 를 안 넘습니다 — run 이 바뀔 때마다 리셋되니까요. 정상과 구별 불가.
> **principal 누적**은 계속 올라가서 걸립니다.
> per-run 상한만 보는 도구는 이 폭주를 **영원히** 못 잡습니다. 실측 탐지율 0% 입니다."

```
   ⛔ 동일 호출 반복       ██████████████████████   20.0 / 20
   ⛔ principal 누적       ██████████████████████   29.0 / 26
   ⛔ run 회전 격차        ██████████████████████    5.8 / 5.2
      run 스텝수           ████░░░░░░░░░░░░░░░░░░    5.0 / 27   ← 눈이 머는 축
```

### 4막 · 선언이 판정을 구동한다 — **폭주 도중에 Enforce 를 켠다** ⭐⭐

`DEMO_KCTX` 가 설정돼 있으면 스크립트가 이 막을 자동으로 진행한다.

> **말할 것**: "차단을 켜는 건 재배포가 아니라 **선언 한 줄**입니다."
>
> ```
> kubectl patch runawaypolicy observe-default -p '{"spec":{"controlLevel":"Enforce"}}'
> ```
>
> "3초 뒤 같은 폭주가 429 를 받습니다. 재시작도, 재배포도 없습니다.
> 8/17 전에는 이게 85초였습니다 — 판정을 오퍼레이터로 중앙화하면서 3초가 됐습니다."

관제탑 화면이 바뀐다: `탐지 (통과시킴)` → **`차단`**
```
[argus] 차단 principal=… | duplicate_calls/hard=20>20 | id=d-60a15a8c… policy=crd:…
```

스크립트가 끝나며 Observe 로 **자동 원복**한다 (SSA 소유권 — 손으로 올린 건 손으로 내린다).

### 마무리 · 판독

스크립트가 신선도 검사(이번 실행 레코드인지) 후 표를 낸다:

```
  코호트    콜   차단   would_block      비용
  정상      29     0             0   $0.0025
  폭주      …     …             …
  ✅ 오탐 0
```

> **말할 것**: "임계값은 전부 실측입니다. **정상 트래픽이 도달한 값은 임계가 될 수 없다** —
> 이 규칙 하나로 도출했고, 근거를 못 만든 축은 껐습니다."

---

## 데모 중 사고 대비

| 증상 | 원인 · 대응 |
| --- | --- |
| 정상 에이전트에 `업스트림 오류 HTTP 429` | provider 레이트리밋이지 Argus 차단이 아니다. 구분은 `decision_id` 유무. 실모델(OpenRouter) 대신 `argus-mock-gpt` 면 아예 안 난다 |
| 러너가 차단을 `quota-limited` 로 표기 | **알려진 계약 구멍** (`block-contract.md`) — LiteLLM 이 `decision_id` 를 자기 오류 봉투 안에 넣는다. 차단은 실제로 됐다. 관제탑과 판독 결과가 진실이다 |
| 클라이언트가 `node` 로 보임 | 샘플앱이 user-agent 를 안 보낸다 (undici 기본값) |
| 게이지가 안 움직임 | `/guardrails/list` 의 `guardrail_id` 가 null → LiteLLM 1.96.0 이상 필요 |
| 4막에서 429 가 안 옴 | `/v1/policy` 로 엔진 controlLevel 확인. `default` 면 CR 이 안 닿은 것 — CR status 의 `Accepted/Programmed` 를 본다 |
| 레코드가 0건 | 판독 전에 스크립트가 잡아준다(신선도 검사). 탭이 다른 파일에 쓰거나 가드레일 미발화 |

## 정리

```bash
kubectl -n argus-system patch runawaypolicy observe-default --type merge \
  -p '{"spec":{"controlLevel":"Observe"}}'    # 4막이 자동 원복하지만 한 번 더 확인
pkill -f "port-forward svc/litellm"; colima stop
```

## 이 데모가 보여주지 않는 것 (물어보면 정직하게)

- **인자가 변형되는 폭주** — `attempt`·타임스탬프가 섞이면 duplicate 축이 눈이 먼다.
  대안 규칙은 도출됐고(006e, 분리폭 17) 엔진 적용은 아직이다
- **앱(워크로드) 단위 정책** — 네임스페이스까지만. 팀 CR 은 `AwaitingWorkloadIdentity` 로 대기
- **분산 카운터** — 오퍼레이터 단일 파드 메모리다. Phase 2 Redis
- **샘플앱이 차단을 인지하는 것** — 위 계약 구멍. 차단 자체는 되고 비용도 멈춘다

---

## 부록 · 신원 사다리 검증 — "여러 도구를 불러도 같은 에이전트인가" (2026-08-25)

key 티어(가상 키의 인증 산출물)가 principal 을 유지하는지 4개 국면으로 증명한다.
전제: LiteLLM 에 DATABASE_URL(가상 키 DB) — kind 는 `argus-helm` 의 `kind-values.yaml` 로
Postgres·mock 업스트림까지 함께 올라간다:

```bash
cd ../argus-helm   # argus 의 형제 디렉터리
bash hack/sync-guardrail.sh --apply      # 정본 → 차트 사본
helm --kube-context kind-argus-local -n argus-system upgrade argus charts/argus \
  -f charts/argus/kind-values.yaml

kubectl -n argus-system port-forward svc/litellm 4400:4000 &
export LITELLM_MASTER_KEY=$(kubectl -n argus-system get secret litellm-master-key \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
eval "$(bash hack/demo/provision_keys.sh)"   # KEY_A/B/C1/C2/D/E
python3 hack/demo/multi_tool_identity.py
```

분석 — principal 별로 도구·출처·would_block 을 묶어 본다:

```bash
kubectl -n argus-system exec deploy/litellm -- cat /var/argus/records/records.jsonl \
  | python3 -c '
import json,sys,collections
by=collections.defaultdict(list)
for l in sys.stdin: r=json.loads(l); by[r.get("principal")].append(r)
for p,rs in by.items():
    print(p, len(rs), sorted({r.get("tool_name") for r in rs}),
          sorted({r.get("principal_source") for r in rs}),
          "would_block=%d" % sum(1 for r in rs if r.get("would_block")))'
```

기대 결과 (2026-08-25 실측):

* 국면 1+3 — 에이전트 A 7콜(도구 3종 + **X-Principal-Id 위조 1콜**)이 전부
  한 principal, `principal_source=key:user_id`. 위조 헤더는 신원을 쪼개지 못했다.
* 국면 2 — 에이전트 B 는 다른 principal 로 갈라진다. `team` 은 A 와 같다 (팀 축).
* 국면 4 — 키 2개 회전 28콜이 **한 principal 로 누적**돼 `rotation_gap` hard 에 걸리고
  `would_block` 23건 (Observe 라 429 는 0). 키를 돌려도 user_id 가 같으면 못 숨는다 —
  핑퐁 재귀의 신원 회피가 key 티어에서 무력화되는 지점이다.

### 로컬 탐지 대시보드 (브라우저)

live_view.py 의 브라우저판 — **007 대시보드가 아니라** 로컬 검증용 관측 도구다 (stdlib만, 파일 하나).

```bash
python3 hack/demo/dashboard.py     # http://localhost:8899 (KCTX=kind-argus-local 기본)
```

주체별 카드(도구 목록 · 축 게이지 soft/hard · would_block · principal_source 티어 태그) +
이벤트 스트림. 임계는 컨트롤러가 렌더한 policy.json 을 그대로 읽는다 — CR 을 고치면
게이지 기준선도 30초 안에 따라온다. 레코드는 `kubectl exec tail -f` 라 파드가 재시작해도 재접속한다.
