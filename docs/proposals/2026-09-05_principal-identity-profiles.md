# 주체(principal) 식별을 정책으로 — 레퍼런스 프로파일 제안

> 2026-09-05 · **제안이다(구현 아님)** · 관련 Linear STP-16
> 근거 코드: `api/v1alpha1/runawaypolicy_types.go` IdentitySpec · `hack/guardrail/argus_signals.py` `_principal_ladder()`

## 1. 문제 — 우리 원칙이 반쪽만 적용돼 있다

`IdentitySpec` 주석에 원칙이 이미 박혀 있다:

> 대화 축을 어디서 얻을지는 **클러스터에 어떤 클라이언트가 들어오는가**에 달렸고,
> 그건 운영자가 아는 사실이지 우리가 코드에 박을 사실이 아니다.

이 원칙을 **대화 축**에는 적용했다 — `conversationHeaders` 로 선언한다.
**주체 축에는 적용하지 않았다.** 사다리 순서가 파이썬에 하드코딩돼 있다:

```
key:user_id > key:team_id > key:alias > header > api_key > unavailable
```

그런데 **주체야말로 조직마다 다르다.** 거버넌스를 하는 조직은 이미 자기 신원 체계를 갖고 있고,
그게 우리 사다리와 같을 이유가 없다:

| 조직 유형 | "이 에이전트는 누구인가"의 정본 |
| --- | --- |
| 게이트웨이 가상키 운영 | LiteLLM `user_id` / `team_id` |
| 쿠버네티스 워크로드 아이덴티티 | ServiceAccount · SPIFFE ID |
| 클라우드 IAM | IAM Role ARN · Workload Identity |
| 사내 SSO | 게이트웨이가 주입하는 `X-Acme-Employee-Id` |
| 아무것도 없음 | 자기신고 헤더뿐 (= 사실상 신원 없음) |

우리가 이 중 하나를 고르는 건 월권이다.
**고르는 자리를 열어주고, 고를 수 있게 레퍼런스를 주는 것**이 맞다.

## 2. 먼저 — 두 축은 다르다 (자주 혼동된다)

| | 대화 축 | 주체 축 |
| --- | --- | --- |
| 묻는 것 | "같은 대화인가" | "같은 에이전트/사람인가" |
| 쓰이는 판정축 | `duplicate_calls` | `principal_calls` · `rotation_gap` · `budget_usd` |
| 현재 | ✅ `IdentitySpec.conversationHeaders` 로 선언 가능 | ❌ 하드코딩 |
| 못 정하면 | 해당 축을 **평가 안 함** (`onUnknownConversation: Skip`) | `unavailable` — 주체 스코프 축 전부 평가 안 함 |

주체 축이 무너지면 **판정 5축 중 3축이 죽는다.**
핑퐁 재귀가 제품의 최대 차별점인데 그게 정확히 주체 축 위에 서 있다.

## 3. 제안 스키마 — `identity.principal`

`conversationHeaders` 와 같은 결(순서 = 우선순위)로 맞춘다.

```yaml
spec:
  identity:
    # (기존) 대화 축
    conversationHeaders: ["x-acme-session"]
    trustGatewaySession: true
    onUnknownConversation: Skip

    # (신규) 주체 축 — 순서가 신뢰 순서다
    principal:
      ladder:
        - from: keyUserId                       # 게이트웨이 인증 산출물
        - from: keyTeamId
        - from: header
          name: X-Acme-Workload-Id              # 조직 고유 신원을 원하는 우선순위에 끼운다
        - from: keyAlias
        - from: header
          name: X-Principal-Id                  # 자기신고 (기본 헤더)
        - from: apiKey                          # 최후 폴백
      ignoreValues: ["default_user_id"]         # key 티어로 승격 금지 자리표시자
      onUnavailable: Skip                       # Skip | UseApiKey
```

`from` 열거값: `keyUserId` · `keyTeamId` · `keyAlias` · `header`(+`name`) · `apiKey`.
**비우면 지금의 내장 사다리와 동일하게 동작한다** (하위호환).

### 왜 `header` 를 임의 우선순위에 끼울 수 있어야 하나

게이트웨이 앞단(Envoy·Istio·API GW)이 **검증한 뒤 주입한 헤더**는 자기신고가 아니다.
그런 조직에서는 `X-Acme-Workload-Id` 가 `keyUserId` 보다 위에 와야 맞다.
헤더를 무조건 key 아래로 내리는 현재 구현은 **그 조직에서 틀린 답을 낸다.**

## 4. 거버넌스의 핵심 — 보증 등급(assurance)을 명시한다

조직이 알아야 하는 건 "무엇을 주체로 쓰는가"가 아니라
**"그 주체를 얼마나 믿을 수 있는가"** 다. 레코드의 `principal_source` 가 이미 그걸 담고 있다.

| 등급 | source | 위조 가능? | 의미 |
| --- | --- | --- | --- |
| **A 인증됨** | `key:user_id` · `key:team_id` · `key:alias` | ❌ | 게이트웨이가 키를 검증한 뒤 채운 값 |
| **A 인증됨** | `header` — 앞단이 검증·주입 | ❌ | 단, 그 앞단을 우회할 수 없어야 성립 |
| **C 자기신고** | `header` — 클라이언트가 붙임 | ✅ | 협조적 에이전트용. 악의적 호출자에겐 무의미 |
| **D 구별 불가** | `api_key` | — | master key 하나면 전원이 한 주체 |
| **X 없음** | `unavailable` | — | 주체 스코프 축 3개를 평가하지 않는다 |

**대시보드는 이 등급을 표시해야 한다.** C/D 등급에서 나온 "차단"은
"이 신원이 폭주했다"가 아니라 **"이 신원을 자칭한 트래픽이 폭주했다"** 이다.
두 문장은 감사에서 전혀 다른 무게를 갖는다.

## 5. 레퍼런스 프로파일 — `config/samples/identity/`

완전한 CR 4개를 낸다. 각 파일 머리에 **언제 쓰나 / 무엇이 보장되나 / 무엇은 보장 안 되나**.

### 5.1 `profile-gateway-virtualkeys.yaml` — 기본, 우리 데모가 이것

LiteLLM 가상키를 발급하는 조직. `DATABASE_URL` 이 있어야 성립한다 (STP-8).

- **보장**: 위조 헤더가 주체를 쪼개지 못한다. 같은 `user_id` 로 키를 회전해도 누적이 유지된다
- **미보장**: **다른** `user_id` 로 키를 여럿 발급받으면 갈라진다 (G2 → 5.5)

### 5.2 `profile-workload-identity.yaml` — ServiceAccount / SPIFFE

앞단(Envoy·Istio)이 워크로드를 검증하고 헤더로 주입하는 조직.
`ladder` 최상단에 `from: header, name: X-Forwarded-Client-Cert` 류.

- **보장**: 애플리케이션이 아니라 **워크로드**가 주체다. 앱 코드를 못 믿어도 성립한다
- **미보장**: 앞단을 우회하는 경로(파드가 게이트웨이를 직접 호출)가 있으면 무효.
  egress 차단이 전제이고 그건 Cilium/NetworkPolicy 소관 — 우리 범위가 아니다

### 5.3 `profile-sso-user.yaml` — 사람 단위

게이트웨이가 SSO 세션에서 사번을 주입하는 조직. 주체가 "에이전트"가 아니라 "사람"이다.

- **보장**: 한 사람이 에이전트를 여러 개 돌려도 **사람 단위로** 예산·호출이 합산된다
- **미보장**: 팀 공용 서비스 계정에서 사람 축이 무너진다 → `keyTeamId` 를 다음 단으로

### 5.4 `profile-selfreport-only.yaml` — 신원 체계가 없는 조직

⚠️ 이 프로파일의 존재 이유는 **위험을 명시하기 위해서**다. 사다리가 `header` → `apiKey` 뿐이라 전부 C/D 등급.

- **보장**: 협조적 에이전트의 **사고성** 폭주는 잡는다 (실제 폭주의 대부분이 이것이다)
- **미보장**: 악의적 호출자는 헤더만 바꾸면 빠져나간다. 이 프로파일에서 `Enforce` 로 올리는 것은
  **보안 통제가 아니라 안전 장치**다. 문서에 그렇게 적는다

### 5.5 공통 — G2 는 어느 프로파일에서도 남는다

주체를 **여러 개 합법적으로 획득**하는 공격(키 다중 발급·SA 다중 보유)은 사다리로 못 막는다.
그래서 `backstop` 에 상위 축(`team`)이 필요하다 — STP-16 의 나머지 절반이다.
프로파일마다 **"이 프로파일의 상위 묶음은 무엇인가"** 를 적는다.

## 6. 하위호환

- `identity.principal` 을 비우면 현재 내장 사다리와 **동일** 동작. dev 의 기존 CR 은 무변경으로 계속 돈다
- `principal_source` 문자열 값은 바꾸지 않는다 (대시보드·레코드 계약)
- Go 엔진과 파이썬 폴백을 **동시에** 고쳐야 한다 — `python_parity_test.go` 가 동치를 계약으로 잡고 있다

## 7. 열린 질문

1. `from: header` 를 key 티어 **위**에 둘 수 있게 하면, 잘못 설정한 조직이 자기신고를 최상위로
   올려놓고 "인증됨"이라 착각할 수 있다. 등급을 CR 에서 선언하게 할까(`assurance: Authenticated`),
   아니면 문서 경고로 충분한가?
2. 프로파일을 CR 샘플로 낼지, `profile: GatewayVirtualKeys` 같은 **이름 있는 프리셋**으로 낼지.
   프리셋은 쓰기 쉽지만 우리가 조직의 신원 체계를 아는 척하게 된다
3. 데모 범위: STP-16 은 데모에 키 회전 장면을 넣을 때만 필요하다. 프로파일 **문서화만으로도**
   데모 서사("우리는 조직의 신원 체계를 대체하지 않는다")에 기여한다 — 분리 가능

## 8. 이 문서가 제안하지 않는 것

- 프로토콜 어댑터(도구 호출이 본문 어디에 있는가) — 정책이 아니다
- egress 차단 · 게이트웨이 우회 방지 — Cilium/NetworkPolicy 소관
- **신원 발급 자체** — 우리는 읽는 쪽이다. 발급은 조직의 IdP·게이트웨이가 한다
