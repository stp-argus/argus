# 한화솔루션 LLM Gateway 사례 + LiteLLM 헤더 계약 정리 (2026-08-18)

**출처** [AWS 기술블로그 — 한화솔루션 Claude Cowork 전사 도입](https://aws.amazon.com/ko/blogs/tech/hanwha-claude-cowork-on-bedrock/) (2026-08-10) ·
[LiteLLM request_headers](https://docs.litellm.ai/docs/proxy/request_headers) · [response_headers](https://docs.litellm.ai/docs/proxy/response_headers)

> **한 줄**: 우리와 **같은 스택**(LiteLLM 게이트웨이)을 운영하는 실제 엔터프라이즈가,
> 우리가 잡으려는 **바로 그 폭주를 실제로 겪었고**, 그들의 대응은 탐지·차단이 아니라
> **사후 분석 + 교육**이었다. "보는 것 ≠ 막는 것"의 실사례가 공개 문서로 나왔다.

---

## 1. ⭐ 하이퍼세션 $79 — 우리 논지의 실사례

블로그 §7.2, 그대로 인용:

> *"1,000턴을 넘는 초장기 세션이 다수 발견되었고, 극단적으로는 **한 세션이 12,388턴까지
> 이어지며 Haiku만으로 약 $79를 소비**한 사례가 있었습니다. 세션을 종료하지 않고 이어 쓰며
> 심층 리서치가 조사를 과도하게 반복한 탓이었습니다."*

주목할 것은 **어떻게 발견했고 어떻게 대응했는가**다:

| | 한화의 방식 | 의미 |
| --- | --- | --- |
| 발견 | **한 달치 SpendLogs 사후 분석** 중 우연히 | 도는 동안에는 아무것도 몰랐다 |
| 대응 | 심층 리서치 **기본 비활성화** + "세션을 분리하라" **교육** | 기능을 끄고 사람을 가르쳤다 — 다음 폭주를 막는 장치가 아니다 |

12,388턴이 도는 동안 SpendLogs 는 전부 기록하고 있었다. **보고 있었지만 아무도 막지 않았다.**
이게 관측 도구의 구조적 한계이고, ARGUS 가 경로 위에 있는 이유다.

우리 축으로 환산하면: `principal_calls`(윈도우 누적)·`run_steps`·`budget_usd` 어느 것이든
수백 턴 언저리에서 걸렸다. Haiku 라 단가가 낮아 $79 로 끝났지, Opus 였으면 자릿수가 다르다.

**발표(8/22)에서의 용법**: 경쟁 비교가 아니라 **검증된 수요**로 쓴다. "가상의 시나리오가
아니라, 국내 최초 전사 도입 사례가 공개 블로그에 이 문제를 스스로 기록했다.
그들의 스택이 우리 스택이다 — RunawayPolicy CR 하나면 교육이 아니라 장치가 된다."

## 2. 같은 스택이라는 사실 — 배포 대상의 실증

한화 게이트웨이 구성이 우리 전제와 정확히 겹친다:

| 한화 | 우리 |
| --- | --- |
| LiteLLM 다중 인스턴스 (LB 뒤) | LiteLLM 탭 (우리 가드레일이 얹히는 곳) |
| `config.yaml` = single source of truth | 같은 파일에 가드레일 등록 |
| **서버 측 정책 강제** — "클라이언트가 헤더를 지우면 우회되므로 게이트웨이에서 강제" | `default_on: true` — 같은 신뢰 경계 논리 |
| 감사·관측을 요청 경로에서 분리 (비동기 콜백) | 기록 best-effort · 판정만 인라인 |
| 본문은 감사 저장소 한 곳에만, 관측엔 메타데이터만 | FR-003 원문 미저장과 동일 사상 |

**주의할 차이 하나**: 한화는 LiteLLM **다중 인스턴스**다. 우리 판정을 탭 안에 뒀다면
카운터가 인스턴스 수만큼 쪼개졌다 — **판정 중앙화(8/17)가 이런 배포에서 필수 전제**임을
실사례가 보여준다. (오퍼레이터 엔진도 아직 단일 파드 제약이 있다 — Phase 2 Redis 의 근거)

## 3. 가상 키 = 우리 신원 사다리의 2등급이 실환경에선 이미 있다

한화 구조: Entra ID JWT → Token Service 가 **단기 가상 키** 발급(`{tid}:{oid}` 캐시)
→ `tid` 를 LiteLLM `team_id` 로 매핑 → SpendLogs 에 조직 축.

이게 의미하는 것 — 학습노트 09 의 신원 사다리에서 "(미구현)" 으로 표시한
**2등급(게이트웨이 발급 키, 위조 불가)** 이 이런 배포에서는 **이미 존재한다.**
LiteLLM 이 가상 키 인증을 하면 훅의 `user_api_key_dict` 에 `user_id`·`team_id`·
`key_alias` 가 실려 온다.

지금 우리 가드레일은 `user_api_key_dict.api_key` 를 **최후 폴백**으로만 쓴다
(마스터키 단독 환경에선 호출자 구분이 안 되므로 — 7/26 조사 §c). 그 판단은
마스터키 환경에선 맞지만, **가상 키 환경에선 거꾸로 최우선이어야 한다**:

```
principal 사다리 (제안):
  key     user_api_key_dict.user_id / team_id   ← 가상 키 환경. 위조 불가
  header  X-Principal-Id                         ← 자기신고 (지금의 1순위)
  api_key 해시                                   ← 최후 폴백
```

`x-litellm-end-user-id` / `x-litellm-customer-id` 요청 헤더도 문서화된 최종사용자
식별자다 — `_ID_HEADERS` 후보.

## 4. 응답 헤더 — 차단 계약 문제의 탈출구일 수 있다

LiteLLM 은 응답에 `x-litellm-call-id`(상관 키)·`x-litellm-response-cost`(실비용)·
`x-litellm-attempted-retries` 등을 싣는다. 두 가지 함의:

**① `x-argus-decision-id` 응답 헤더 (검증 필요).** 차단 계약의 고질 문제 —
LiteLLM 이 우리 429 detail 을 자기 오류 봉투로 감싸 `decision_id` 가 본문 최상위에
못 온다(block-contract.md). 그런데 **FastAPI `HTTPException` 은 `headers=` 를 받는다.**
봉투는 본문을 감싸지 몸이 아니라 헤더는 건드리지 않을 가능성이 높다:

```python
HTTPException(status_code=429, detail=body,
              headers={"x-argus-decision-id": decision.decision_id})
```

전파되면 샘플앱 러너가 `BLOCK_MARKER` 를 헤더에서 읽는 한 줄로 오분류가 끝난다.
**랩에서 실측 후 채택** — 추측으로 계약을 다시 쓰지 않는다.

**② `x-litellm-call-id` 는 클라이언트도 받는다.** 사용자가 "이 요청 왜 막혔죠"라고
물을 때 그 값 하나로 우리 레코드(`litellm_call_id` 상관)까지 역추적이 이어진다 —
지원 절차에 쓸 수 있다.

**③ `x-litellm-response-cost`** — 응답 헤더로 콜당 실비용이 온다. 러너의 비용 집계가
본문 usage 파싱 대신 이 헤더를 읽으면 와이어 무관해진다 (샘플앱 개선 후보, 남의 스트림).

## 5. 요청 헤더 재확인 — 우리가 이미 맞게 정렬돼 있는 부분

- `x-litellm-trace-id` / `x-litellm-session-id` → `litellm_session_id` 로 정규화,
  `x-<vendor>-session-id` 폴백 — 8/16 에 소스로 확인한 그대로 문서에도 있다.
  `trustGatewaySession` 설계 유효
- `x-litellm-tags`·`x-litellm-spend-logs-metadata` — 스펜드 분류용. 우리 레코드와
  별개 축이지만, 한화처럼 team_id 운영을 하는 곳에선 우리 `principal`/`team` 매핑의
  대응물이다

## 6. 액션 (우선순위 순)

1. **발표 자료에 하이퍼세션 사례 반영** — "보는 것 ≠ 막는 것"의 공개된 실증.
   $108/시간(Occludra) 옆에 12,388턴/$79(한화)
2. **`x-argus-decision-id` 응답 헤더 실측** — 봉투가 헤더를 보존하는지 랩에서 확인,
   되면 차단 계약 재설계 없이 구멍이 닫힌다
3. **principal 사다리에 `key` 등급** — `user_api_key_dict.user_id/team_id` 우선 사용.
   가상 키 배포(한화 패턴)에서 위조 불가 신원이 공짜로 생긴다
4. `_ID_HEADERS` 에 `x-litellm-end-user-id`·`x-litellm-customer-id` 추가
