# 중앙 판정 구조 — 탭은 전달하고, 판단은 컨트롤러가 한다

**작성** 2026-08-17 @song2h · **상태** 제안 (구현 전)
**계기** *"앞에 프록시에서 전달만 잘 해주면 argus 에서 탐지해서 정책대로 하는 것.
네임스페이스·앱마다 CRD 를 별도로 설정하고, istio 처럼 중앙에 정책 판단하는 애가 있어야 한다"*

---

## 1. 지금 구조의 한계 — 왜 이 문서가 필요한가

지금까지 만든 것은 **LiteLLM 안쪽 설정**이다. 가드레일 파이썬이 ConfigMap 으로 들어가고,
판정 엔진이 그 프로세스 안에서 돌고, 카운터도 거기 있다.

그래서 세 가지가 안 된다:

| | 왜 안 되나 |
| --- | --- |
| **앱마다 정책** | 렌더 결과가 축 하나짜리 평면이다. 대상별 구획이 없다 |
| **부착 범위** | `Kind=Namespace` 까지만. Deployment 를 겨냥하면 `NotAttached` |
| **테넌트 격리** | ⚠️ **남의 네임스페이스가 탭을 겨냥해 임계를 올릴 수 있다** (2026-08-16 확인) |

마지막 것은 버그가 아니라 **구조의 결과**다. 모든 정책이 하나로 병합되니
누가 마지막으로 쓰느냐가 이긴다. 팀이 늘면 바로 터진다.

## 2. 업계는 이 문제를 어떻게 풀었나

### ⚠️ Istio Mixer — 중앙 per-request 판정으로 갔다가 **제거했다**

Mixer 는 사이드카가 요청마다 중앙에 policy check 를 묻는 구조였다. **1.8 에서 제거됐다.**

> 정책을 **쓰지 않는 사용자도** 그 지연을 냈고, 테스트에서 **P99 가 2배**(6ms → 12ms)가 됐다.
> 어떤 회사는 그 델타 때문에 Linkerd 로 옮겼다.

**교훈**: 순진한 `/v1/decide` 를 모든 호출에 걸면 같은 실패를 반복한다.

### 그런데 Envoy 는 **중앙 rate limit 서비스를 여전히 쓴다**

`envoyproxy/ratelimit` — gRPC 서비스 + Redis. **이유는 하나다: 카운터는 공유돼야 한다.**
레플리카가 여러 개면 로컬 카운터로는 전역 한도를 못 지킨다.

그리고 Envoy 는 **둘 다** 제공한다:

| | 어디서 | 상태 | 언제 |
| --- | --- | --- | --- |
| **local rate limit** | 프록시 안 (토큰버킷) | 인스턴스별 | 외부 호출 없음. 빠르다 |
| **global rate limit** | 외부 RLS + Redis | 공유 | 레플리카 간 한도 공유가 필요할 때 |

> **결론: "중앙이냐 로컬이냐"가 아니라 "무엇을 중앙에 둘 것이냐"다.**
> 설정 배포는 push(istiod/xDS), 카운터 판정만 중앙(RLS).

### 앱 신원 — Istio 는 **검증해서 헤더로 넘겨준다**

> *"The Istio proxy **validates and verifies the workload identity** before passing the request on
> to the application, with the identity added to the **X-Forwarded-Client-Cert** header …
> the URI key holding the **SPIFFE identifier** of the workload."*

SPIFFE ID 형식이 우리에게 정확히 필요한 입도다:

```
spiffe://<trust-domain>/ns/<네임스페이스>/sa/<서비스어카운트>
                            └─ 네임스페이스 정책     └─ 앱 정책
```

**위조 불가**다. 메시가 mTLS 로 검증한 것이고, 앱이 자기신고하는 값이 아니다.

이게 정확히 *"앞에 프록시에서 전달만 잘 해주면"* 이다.
그리고 우리 원칙 **"신원은 발급하지 말고 소비한다"** 의 실현이기도 하다.

## 3. 제안하는 구조

```
   RunawayPolicy CR              RunawayPolicy CR
   (team-a 네임스페이스)          (team-b 네임스페이스)
          │                             │
          └──────────┬──────────────────┘
                     ▼ watch
        ┌────────────────────────────────┐
        │   ARGUS 컨트롤러 (중앙)         │   ← istiod 자리
        │   · CR → **대상별** 정책 계산   │
        │   · 카운터 (principal × 대상)   │   ← Envoy RLS 자리
        └────────────────────────────────┘
             ▲                      │
   /v1/decide│  (임계 근처일 때만)   │ 정책 배포 (거의 안 바뀜)
             │                      ▼
  앱 ─mTLS─▶ 메시 ─XFCC─▶  LiteLLM 탭
             (SPIFFE)        · 신호 추출 · 로컬 fast-path · 집행
```

### 3.1 두 층으로 나눈다 (Mixer 의 교훈)

| | 무엇 | 어디 | 왜 |
| --- | --- | --- | --- |
| **설정** | 임계값·controlLevel·identity | **push** (지금 방식 유지) | 거의 안 바뀐다. 요청마다 물을 이유가 없다 |
| **판정** | 카운터 기반 축 | **중앙 질의** | 대상별 카운터는 공유돼야 한다 |
| **fast-path** | 임계 근처도 아닌 호출 | **로컬에서 끝** | Mixer 가 죽은 이유를 반복하지 않는다 |

fast-path 가 핵심이다. 탭이 로컬 근사 카운터를 들고 있다가
**"임계의 N% 미만이면 묻지 않는다."** 정상 트래픽 대부분이 여기서 끝난다.

### 3.2 정책 범위 — 자기 것만, 위임은 명시적으로

지금은 아무나 탭을 겨냥할 수 있다. 바꿀 규칙:

| targetRefs | 적용 대상 | 누가 쓸 수 있나 |
| --- | --- | --- |
| 비었음 | **선언한 그 네임스페이스** | 그 네임스페이스 팀 |
| `Kind=Namespace, name=자기 자신` | 같음 | 같음 |
| `Kind=Deployment/ServiceAccount` | 그 워크로드 | 같은 네임스페이스만 |
| **다른 네임스페이스** | ❌ 거부 | 플랫폼 정책(별도 표시)만 |

Gateway API 가 이 문제를 **`ReferenceGrant`** 로 푼다 — 크로스 네임스페이스 참조는
**받는 쪽이 명시적으로 허용**해야 한다. 같은 모델을 따른다.

그리고 **플랫폼 정책은 backstop 으로만 내려간다** — 낮추기만 되는 그 규칙이
멀티테넌시에서 비로소 제 값을 한다.

### 3.3 신원 사다리 (등급을 기록에 남긴다)

메시가 없는 환경에서도 돌아야 한다. 그래서 사다리 + 출처 기록:

| 등급 | 출처 | 위조 | 어디서 |
| --- | --- | --- | --- |
| `mesh` | XFCC 의 SPIFFE URI | **불가** | Istio/Linkerd 있는 곳 |
| `token` | SA 토큰 (TokenReview 검증) | 불가 | 메시 없어도 가능 |
| `key` | 게이트웨이 발급 virtual key | 불가 | LiteLLM |
| `header` | 자기신고 `X-Principal-Id` | 가능 | 지금 |

**등급이 낮으면 정책이 그걸 알아야 한다.** `requireIdentity: mesh` 같은 선언으로
"검증된 신원 없으면 이 정책 적용 안 함"을 표현할 수 있게 한다.

## 4. 이건 006b 를 되살린다

`/v1/decide`(spec 006b, @leeyonghe)를 두고 006c 스펙에 이렇게 썼었다:

> *"006b 는 판정이 **어디서 도는가**를 정하고, 006c 는 판정이 **무엇을 어떤 근거로**
> 결정하는가를 정한다. 경쟁하지 않는다."*

그때는 카운터가 로컬이라 `/v1/decide` 가 당장 필요 없었다.
**대상별 정책 + 공유 카운터로 가면 그게 중심이 된다.** 판정 엔진을 순수 모듈로
둔 것이 여기서 값을 한다 — `decide(signals) → Decision` 을 그대로 옮기면 된다.

## 5. 단계

| | 무엇 | 크기 | 8/22 전 |
| --- | --- | --- | --- |
| **A** | **테넌트 격리** — 남의 네임스페이스 겨냥 거부 | 작다 | ✅ 해야 한다 |
| **B** | XFCC/SPIFFE 신원 소비 + 등급 기록 | 중간 | 가능 |
| **C** | 렌더를 대상별 구획으로 + 탭이 대상 선택 | 중간 | 어렵다 |
| **D** | `/v1/decide` 중앙 판정 + 로컬 fast-path | 크다 | ❌ |
| **E** | 공유 카운터 (Redis) | 크다 | ❌ |

**A 를 먼저** 한다 — 지금 있는 구멍이고, 작고, 멀티테넌시의 전제다.
B 는 신원 재료를 **지금 남겨두지 않으면** 나중에 과거 데이터로 못 만든다는
006d/006e 의 교훈이 그대로 적용된다.

C·D·E 는 8/22 이후. 다만 **D 를 할 때 Mixer 를 반복하지 않도록**
fast-path 를 처음부터 설계에 넣는다.

## 6. 출처

- [Istio FAQ — Mixer 제거](https://istio.io/latest/faq/mixer) · [istio#11736 (정책 검사 기본 off 제안, P99 2배)](https://github.com/istio/istio/issues/11736)
- [Envoy — Global rate limiting](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/other_features/global_rate_limiting) · [envoyproxy/ratelimit](https://github.com/envoyproxy/ratelimit)
- [Envoy Gateway — Rate Limiting (local vs global)](https://gateway.envoyproxy.io/docs/concepts/rate-limiting/)
- [Istio — Gateway Network Topology (XFCC)](https://istio.io/latest/docs/ops/configuration/traffic-management/network-topologies/)
- [Gateway API — ReferenceGrant](https://gateway-api.sigs.k8s.io/api-types/referencegrant/)
