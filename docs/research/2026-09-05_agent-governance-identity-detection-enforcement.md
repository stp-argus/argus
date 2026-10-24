# 에이전트 거버넌스 — 식별·탐지·차단 레퍼런스 조사

> 2026-09-05 · 조사 문서 (구현 아님)
> 조사 질문: **"누가 · 어떻게 쓰는가 · 무엇을 막는가"** 를 다루는 표준·논문이 지금 어디까지 왔고,
> 그중 ARGUS 가 지금 당장 가져다 쓸 수 있는 것이 무엇인가.

## 0. 이 조사의 전제 — ARGUS 의 핵심은 거버넌스다

이 제품은 "비용 절감 도구"가 아니다. **조직이 자기 에이전트들을 통치할 수 있게 하는 것**이 핵심이고,
거버넌스는 세 가지 질문으로 갈린다:

| 질문 | ARGUS 의 답 | 관련 축 |
| --- | --- | --- |
| **누가** 쓰는가 | 주체 사다리 (위조 불가 인증 산출물 우선) | `principal_source` |
| **어떻게** 쓰는가 | 행위 프로파일 — 반복·지속·회전 | `duplicate_calls` · `run_steps` · `rotation_gap` |
| **무엇을** 막는가 | 정책 선언 → 인-패스 차단 | `controlLevel` · backstop |

비용(`budget_usd`)은 **다섯 축 중 하나**일 뿐이다. 이 구분이 아래 5장에서 결정적이 된다.

---

## 1. 지형이 최근 9개월 사이에 바뀌었다

우리 최초 조사(2026-07-26) 이후에 나온 것들이다. **프로젝트 문서가 아직 이걸 반영하지 않았다.**

| 시점 | 무엇 | 우리에게 |
| --- | --- | --- |
| 2025-12-09 | **OWASP Top 10 for Agentic Applications** (LLM Top 10 과 **별개 문서**) | 우리는 아직 구 `LLM10 Unbounded Consumption` 만 인용 중 |
| 2026-02 | **NIST AI Agent Standards Initiative** — 3축(산업 표준·프로토콜·에이전트 보안/신원 연구) | 표준화 트랙 존재 |
| 2026-05 | **Five Eyes 6개 기관 공동 지침** — 권한 위험을 1급 우려로, 최소권한을 핵심으로 명시 | 신원/권한이 규제 언어로 진입 |
| 2026-08 | **CSA Agent Identity Maturity Model** (5단계) | 우리 "보증 등급"과 직접 매핑 |

### 우리가 인용해야 할 항목이 바뀌었다

- **ASI03 · Agent Identity & Privilege Abuse** — *"AI 에이전트는 자기 신원을 받지 못한다. 당신 것을 물려받는다.
  권한은 스코프되지 않고 누적되며, 토큰은 그것을 필요로 했던 작업보다 오래 산다."*
  → **우리 주체 사다리가 정확히 이 항목의 통제다.**
- **ASI08 · Cascading Agent Failures** — 권고 통제가 **circuit breaker · fan-out cap · tenant isolation** 이다.
  → 우리 `cooldown` 이 이미 circuit breaker 다(코드 주석도 "서킷 브레이커식 히스테리시스"라 부른다).
  **fan-out cap 은 우리에게 없다.**

---

## 2. 누가 — 식별

### 2.1 CSA 신원 성숙도 5단계와 우리 위치

| 단계 | 정의 | ARGUS 가 하는 일 |
| --- | --- | --- |
| 1 Untracked | 인벤토리 없음. 표준 자격증명·상속 권한. 사고 시 **포렌식에 안 보임** | — |
| 2 Discovered | 목록엔 있으나 스코프 미평가 | 레코드가 principal 을 남기는 것 자체 |
| 3 Contextualized | 소유자·목적·실측 권한이 붙는다 | `principal_source` + `team`/`org` 수집 |
| 4 Right-sized | 최소권한 강제, 상시 자격증명 → 시간제한 스코프. **실시간 탐지·회수 가능** | ← **우리가 겨냥하는 곳** |
| 5 Governed | 등록·소유·회전·만료가 생성 시점에 자동화 | 범위 밖 (IdP 소관) |

**핵심 인용** — *"성숙도의 진짜 척도는 커버리지 비율이 아니라, 자격증명이 침해된 시점과
그 자격증명이 무용해지는 시점 사이의 간격이다."*

→ 우리 언어로: **탐지에서 차단까지의 시간.** CR 패치 → 3초 반영이 그 숫자다.
발표에서 "우리 지표는 커버리지가 아니라 time-to-revocation"이라고 말할 수 있는 근거다.

### 2.2 SPIFFE/SPIRE — 워크로드 신원

SVID(단명 서명 자격증명)를 워크로드 출처·런타임 컨텍스트 기반으로 발급. 사람이 아니라 워크로드에
묶이므로 에이전트에 적합하고, **단명 신원 + 자동 회전**이 에이전트의 생성/소멸 속도에 맞는다.

→ **argus#15 의 `profile-workload-identity` 가 정확히 이 케이스다.** 제안서에 이미 적은
"앞단이 검증해 주입한 헤더는 자기신고가 아니다" 가 SPIFFE 배치의 표준 형태와 일치한다.

### 2.3 적용 후보

1. **argus#15 프로파일 4종에 CSA 성숙도 단계를 붙인다.** `selfreport-only` = 단계 1~2,
   `gateway-virtualkeys` = 단계 3~4, `workload-identity` = 단계 4. 조직이 **자기 위치를 찾고
   다음 단계를 알 수 있게** 된다 — 이게 레퍼런스 문서의 값어치다
2. **감사 추적 요건**: CSA 는 *"모든 행동을 타임스탬프·실행 신원·결과와 함께 로깅"* 을 요구한다.
   `records.jsonl` 한 줄 = 한 호출이 이미 이 형태다. **계약 문서에 이 요건 매핑을 명시**하면
   거버넌스 문서로서 값이 오른다

---

## 3. 어떻게 쓰는가 — 행위 탐지

### 3.1 ⭐ 우리 자체 분류가 외부 실측과 거의 일치한다

**"When Agents Do Not Stop: Uncovering Infinite Agentic Loops in LLM Agents"** (arXiv 2607.01641)
— Python LLM 에이전트 레포 **6,549개** 정적 분석, 68건 확증(정밀도 91.9%), 47개 프로젝트 / 8개 프레임워크.

논문의 IAL 분류와 우리 폭주 6종 대조:

| 논문 분류 | 비중 | 우리 대응 축 | 상태 |
| --- | --- | --- | --- |
| Retry feedback without bounds | **25.0%** | `duplicate_calls` (재시도 폭풍) | ✅ 있음 |
| Tool-call iteration without bounds | **23.5%** | `run_steps` | ✅ 있음 |
| Multi-agent chat without turn bounds | **20.6%** | `principal_calls` (핑퐁 재귀) | ✅ 있음 |
| Workflow loops without effective bounds | 13.2% | `run_steps` 부분 대응 | 🟡 부분 |
| **Message reentry without bounds** | 10.3% | **없음** | ❌ **미대응** |

영향 분포: API 비용 고갈 95.6% · 모델 DoS 95.6% · **컨텍스트 윈도우 고갈 27.9%**

**이게 검증단 대응에 그대로 쓰인다.** 1차 검증에서 "임계값이 전부 임의값"을 지적받았는데,
**분류 자체는 외부 실측과 맞는다**는 근거가 생겼다. 우리 상위 3축이 논문 상위 3분류(합 69.1%)와 일치한다.

### 3.2 STP-19 의 우선순위에 외부 근거가 붙었다

**컨텍스트 윈도우 고갈이 영향의 27.9%.** 우리는 STP-19(대화 크기·성장 G3/G4)를 "가장 쌈"이라는
구현 비용 근거로만 1순위에 뒀는데, 이제 **영향 근거**가 생겼다. 넷 중 가장 먼저 해야 할 이유가 둘이 됐다.

### 3.3 결정적 — 그 논문은 런타임 통제를 제안하지 않는다

> "The paper evaluates **static analysis only, with no runtime interventions.**
> Detection occurs pre-deployment... **No actual execution controls proposed.**"

즉 최신 연구도 **배포 전 정적 분석**에 머문다. 우리 논지 *"보는 것 ≠ 막는 것"* 의 학술 버전이
그대로 성립한다 — **정적 분석은 배포 전에 보고, 관측 도구는 배포 후에 본다. 아무도 그 자리에서 안 막는다.**

발표에서 쓸 한 줄: *"6,549개 레포를 훑은 최신 연구도 통제는 제안하지 않는다. 우리는 거기서 시작한다."*

### 3.4 미대응 모양 — message reentry (10.3%)

메시지가 경계 없이 재진입하는 형태. 우리 축 어디에도 안 걸린다. 우리 지문은 `(tool, args)` 기준이고
`run_steps` 는 run 스코프라 **메시지 경로가 순환하는 모양은 안 본다.** 새 갭이다.

### 3.5 G1(챗 반복) 실무 임계값

STP-21 이 "임베딩 near-dup 코사인 ≥0.85"라고만 적혀 있는데, 실무 임계 범위가 확인됐다:

- **0.88** 엄격 near-duplicate · **0.75** 의미 등가 균형점 · **0.60** 주제 관련성
- 인코더: **SBERT** (품질/속도 균형), `all-MiniLM-L6-v2` (384차원) — 경량

우리 0.85 는 "엄격 near-dup"과 "균형점" 사이다. 나쁘지 않지만 **도메인 의존이므로 실측 필요**라는
것도 문헌 공통 견해다 — 임계는 우리 데이터로 뽑아야 한다.

---

## 4. 무엇을 막는가 — 차단

### 4.1 런타임 경계가 실제로 통한다 (외부 검증)

**"LoopTrap: Termination Poisoning Attacks on LLM Agents"** (arXiv 2605.05846) —
공격자가 도구 응답·데이터 소스를 오염시켜 **에이전트의 종료 판단 자체를 망가뜨린다.**

논문이 평가한 방어: 스텝 상한 · 예산 상한 · **프록시 레벨 집행**.

> "external/runtime bounds like step caps and budget constraints **do effectively stop the attack**"

**우리 배치(프록시 레벨 인-패스)가 논문이 평가한 방어 중 하나이고, 통한다는 결론이다.**
그리고 이 공격은 **에이전트 내부의 종료 로직을 신뢰할 수 없다**는 걸 보여준다 —
`MAX_CALLS` 같은 앱 자체 브레이크가 왜 부족한지의 근거다.

**단, 트레이드오프도 같이 인용해야 한다**: 고정 상한은 *"정상적인 장기 작업을 조기 종료시키고
확장 추론이 필요한 복잡한 문제에서 능력을 떨어뜨린다."* → 우리 "오탐 0" 집착과 soft/hard 이원화가
바로 이 트레이드오프에 대한 답이다.

### 4.2 ASI08 통제 중 우리에게 없는 것 — fan-out cap

| ASI08 권고 | ARGUS |
| --- | --- |
| Circuit breaker (이상률 초과 시 하류 통신 차단) | ✅ `cooldown` = 히스테리시스 서킷 브레이커 |
| **Fan-out rate limit** (한 에이전트가 한 체인에서 몇 개를 트리거할 수 있나) | ❌ **없음** |
| Tenant isolation | 🟡 namespace 수집만, 판정 스코프는 아님 |

**fan-out cap 은 멀티에이전트 폭주의 고유 축**이다. 우리 `principal_calls` 는 호출 수를 세지
**한 주체가 몇 개의 다른 주체를 깨웠는지**를 안 센다. 논문 분류의 "Multi-agent chat" 20.6% 와 맞물린다.

### 4.3 fail-open vs fail-closed — 우리가 반대편에 서 있다

**"Before the Tool Call: Deterministic Pre-Action Authorization"** (arXiv 2603.20953) —
에이전트를 **principal 로 취급**하고, 도구 호출 직전에 결정적(binary) 인가. **fail-closed** 다.

우리는 `failMode: open` 이다. 이건 의도된 선택이고 이유가 있다 — *"폴백이 차단이면
컨트롤러 장애가 곧 서비스 장애가 된다"* (CRD 주석). 하지만 **문헌의 기본값이 반대편**이라는 걸
알고 있어야 하고, 검증단이 물으면 답이 준비돼 있어야 한다:

> 우리는 **가용성 우선**을 택했다. 대신 backstop(덮어쓸 수 없는 천장)이 fail-open 의 위험을 상쇄한다.
> 조직이 원하면 `failureMode: Closed` 로 뒤집을 수 있게 **정책 필드로 열려 있다.**

또 하나 — 그 논문은 인가를 **에이전트 결정 루프 안에** 심는다. 우리는 게이트웨이다.
논문 스스로 "상호보완적"이라 하고, **우리 쪽 장점은 무침투(에이전트 코드 무수정)** 다.

---

## 5. ⚠️ LiteLLM 재조사 — 지형이 움직였다

우리 판단 근거인 2026-07-26 조사("LiteLLM 이 이미 절반을 한다")가 **낡았다.** 현재 LiteLLM:

| 기능 | 상태 | 우리에게 |
| --- | --- | --- |
| 스코프 7단계 (global/team/team-member/internal-user/**virtual key**/customer/**agent**) | 있음 | **`agent` 스코프 세션 tpm/rpm 이 새로 생겼다** |
| `max_budget` + `budget_duration`, 동일 키에 **다중 시간척도 동시 예산** | 있음 | |
| **Budget Reservation — 기본 활성.** 요청 본문·모델 단가로 최대 비용을 **선추정**하고 실비용과 정산 | 있음 | **우리 G0 reserve-then-settle 과 같은 메커니즘** |
| `fail_closed_budget_enforcement: true` — 모든 예산 요청이 DB 대조 후 입장 | 있음 | 4.3 논의와 직결 |
| 예산 소진 시 **0원 모델로 폴백** | 있음 | 차단 대안 |
| 예산은 **DB 필수**. DB 없으면 fail-open, 기동 시 로깅만 | — | STP-8 의 근거가 하나 더 |

### 5.1 우리 논지는 여전히 서지만, 문장을 바꿔야 한다

낡은 문장: ~~"LiteLLM 은 절대 상한만 있고 우리는 패턴을 본다"~~ — 절반만 맞다.

**정확한 문장**: LiteLLM 의 통제는 전부 **스코프 내부의 절대 임계**다. 키/팀/유저 각각의 상한을 센다.
**스코프를 가로지르는 상관을 안 본다** —

- 같은 `(tool, args)` 가 반복되는가 (지문 기준 중복)
- run 을 쪼개도 주체로 합산되는가 (`rotation_gap`)
- 주체가 신원을 회전하며 회피하는가

즉 **LiteLLM 은 "얼마나 썼나"를 통치하고, ARGUS 는 "어떻게 쓰고 있나"를 통치한다.**
거버넌스 프레이밍에서 이 구분이 오히려 더 선명해진다 — 비용 축 하나를 LiteLLM 에 양보해도
**행위 4축이 우리 것으로 남는다.**

### 5.2 ⭐ 바로 적용 가능 — 예산 누적을 LiteLLM 에서 읽는다

우리는 `_principal_settled` / `_principal_sent` 를 **파드 메모리**에 들고 있다. 이건 두 가지 문제다:

1. 파드 재기동 시 누적이 **조용히 0으로** 리셋된다
2. STP-24 — LRU 축출이 `costUSD` 를 지운다

**LiteLLM 의 spend 는 Postgres 에 영속된다.** 우리가 그 값을 읽으면 두 문제가 동시에 사라지고
자체 정산 코드도 지운다. **STP-8(가상키 DB)이 들어가면 그 순간 가능해진다.**

→ STP-14/STP-24 에 반영할 것. 코드를 더 쓰는 게 아니라 **지우는** 방향이다.

---

## 6. 적용 후보 — 우선순위

| # | 무엇 | 근거 | 크기 | 데모 |
| --- | --- | --- | --- | --- |
| 1 | **예산 누적을 LiteLLM spend 로 대체** | 5.2 — 코드가 줄고 버그 2개가 사라진다 | 작음 | 가능 |
| 2 | **문서의 표준 인용을 ASI03/ASI08 로 갱신** | 1장 — 지금 구 LLM10 만 인용 | 작음 | 발표 직결 |
| 3 | **argus#15 프로파일에 CSA 성숙도 단계 매핑** | 2.3 — 레퍼런스 문서의 값어치 | 작음 | 발표 직결 |
| 4 | **IAL 분류 대조표를 발표 자료에** | 3.1 — "우리 분류가 6,549 레포 실측과 일치" | 작음 | **강함** |
| 5 | STP-19(컨텍스트 성장)에 27.9% 근거 추가 | 3.2 | 이미 있음 | — |
| 6 | **fan-out cap 축** | 4.2 — ASI08 권고 중 유일한 미대응 | 중간 | 범위 밖 |
| 7 | **message reentry 축** | 3.4 — 논문 분류 10.3% 미대응 | 중간 | 범위 밖 |
| 8 | STP-21 임계를 0.75~0.88 범위에서 실측 | 3.5 | 큼 | 범위 밖 |

**1~4 는 데모 전에 다 된다.** 전부 문서·설정이고 판정 경로를 안 건드린다.

---

## 7. 하지 말 것

- **문헌 수치를 우리 수치처럼 쓰지 마라.** 27.9%·25.0% 는 논문의 정적 분석 결과지 우리 측정이 아니다.
  인용 시 출처와 방법(6,549 레포 정적 분석)을 같이 적는다
- **fail-open 을 변명하지 마라.** 의도된 선택이고 이유가 있다. 문헌 기본값과 다르다는 걸 먼저 말하고
  이유를 대는 편이 방어적으로 들리지 않는다
- **LiteLLM 과 겹치는 기능을 새로 만들지 마라.** 5.1 의 구분선을 넘지 않는다 —
  "얼마나 썼나"는 LiteLLM, "어떻게 쓰고 있나"가 우리다

---

## 출처

- OWASP Top 10 for Agentic Applications 2026 (2025-12-09) — https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/
- CSA · Governing AI Agent Identities: An Identity Maturity Model (2026-08-20) — https://cloudsecurityalliance.org/blog/2026/08/20/governing-ai-agent-identities-an-identity-maturity-model-for-ai-agents
- When Agents Do Not Stop: Uncovering Infinite Agentic Loops in LLM Agents — https://arxiv.org/html/2607.01641v1
- LoopTrap: Termination Poisoning Attacks on LLM Agents — https://arxiv.org/pdf/2605.05846
- Before the Tool Call: Deterministic Pre-Action Authorization for Autonomous AI Agents — https://arxiv.org/pdf/2603.20953
- aiAuthZ: Off-Host, Identity-Bound Authorization for AI Agents — https://arxiv.org/pdf/2607.05518
- Identity Management for Agentic AI — https://arxiv.org/pdf/2510.25819
- Security Considerations for Multi-agent Systems — https://arxiv.org/pdf/2603.09002
- LiteLLM 문서 · Budgets & Rate Limits — https://docs.litellm.ai/docs/proxy/users
- LiteLLM 문서 · Custom Guardrail — https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail
- SPIFFE — https://www.paloaltonetworks.com/cyberpedia/what-is-spiffe
