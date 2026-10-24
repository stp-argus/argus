# 차단 응답 계약 — 실측 결과와 계약이 다르다

**측정일** 2026-08-12 · LiteLLM 1.96.0 · `docs/research/litellm-lab/`

> **한 줄**: 샘플앱 계약(`decision_id` **최상위** 키)은 LiteLLM 탭에서 **구현할 수 없다.**
> 프록시가 가드레일 예외를 자기 오류 봉투로 감싸기 때문이다. 계약을 고쳐야 한다.

---

## 1. 현재 계약 (sample-app spec 003 D-7)

러너는 아래 두 조건이 **모두** 참이면 차단으로 본다:

```ts
BLOCK_STATUSES = [429]                      // 상태코드
BLOCK_MARKER   = "decision_id"              // 응답 **최상위**에 이 키가 있을 것
hasMarker = Object.prototype.hasOwnProperty.call(parsed, blockMarker())
```

`hasOwnProperty` 라서 **최상위만** 본다. 경로 탐색이 없다.

## 2. 실제로 나오는 모양

가드레일이 `HTTPException(status_code=429, detail={...})` 를 던지면
(LiteLLM 자체 가드레일인 `bedrock_guardrails` 도 같은 패턴을 쓴다),
프록시가 OpenAI 오류 봉투로 감싸서 내보낸다:

```json
{
  "error": {
    "message": "Argus blocked a runaway pattern: duplicate_calls=20>=20",
    "type": "None",
    "param": "None",
    "code": "429",
    "provider_specific_fields": {
      "decision_id": "d-359df70a5b282e34",
      "error": { "type": "argus_runaway_blocked", "code": "runaway_blocked", "message": "..." },
      "control_level": "Enforce",
      "policy_hit": [
        {"axis": "duplicate_calls", "level": "hard", "observed": 20, "threshold": 20,
         "detail": "최근 20콜 중 동일 (tool,args) 20회"},
        {"axis": "run_steps", "level": "soft", "observed": 20, "threshold": 8,
         "detail": "run shape2-01 20콜"}
      ]
    }
  }
}
```

**최상위 키는 `error` 하나뿐이다.** 우리가 넣은 dict 는 `error.provider_specific_fields` 로 들어간다.

## 3. 관측된 결과 — 막히지만 클라이언트가 모른다

Enforce 로 재시도 폭풍을 돌린 결과:

```
[step 026] status=quota-limited(429)    ← 여기서부터 차단
...
[step 050] status=quota-limited(429)
■ HARD STOP at step 50 — Σ$0.0013 (no block encountered)
```

- ✅ **차단은 동작한다** — 26콜째부터 업스트림 호출이 안 나가고 비용이 멈춘다
- ❌ **러너는 차단으로 인식하지 못한다** — `quota-limited` 로 분류하고 **50콜까지 계속 두드린다**

이건 단순한 표기 문제가 아니다. 러너가 차단을 인식하면 **멈추고 exit 2** 로 끝나는데,
지금은 벽에 계속 부딪힌다. 실제 폭주라면 클라이언트가 영원히 재시도한다.

## 4. 검토한 대안

| 방안 | 결과 |
| --- | --- |
| `HTTPException(detail=...)` | ❌ 위 봉투. `decision_id` 가 최상위에 안 온다 |
| `ModifyResponseException` | ❌ **200 으로 반환**한다("It should be caught by the proxy and returned with a 200 status code"). 차단을 성공으로 위장하는 셈이라 더 나쁘다 |
| `BLOCK_MARKER=error` 로 변경 | ❌ 모든 429 오류가 차단으로 보인다. 진짜 레이트리밋과 Argus 차단을 뒤섞는다 — 이 프로젝트가 비판하는 바로 그 종류의 부정직함 |

## 5. 제안 — 계약을 고친다

**마커를 최상위 키가 아니라 JSON 경로로 정의한다.**

```
BLOCK_STATUSES = [429]
BLOCK_MARKER_PATH = "error.provider_specific_fields.decision_id"   (기본값)
```

근거:
- 봉투 모양은 **탭 구현이 정한다.** LiteLLM 탭, Envoy ext_proc 탭, 사이드카 탭이
  각각 다른 봉투를 쓸 것이다 (로드맵 Phase 3). 계약이 최상위 키를 못 박으면 탭마다 깨진다
- `error.provider_specific_fields` 는 OpenAI 오류 봉투의 **확장 슬롯**이라 의미상으로도 맞는 자리다
- 러너 쪽 변경은 경로 탐색 한 곳이다

**이건 `argus-sample-app` 변경이라 @BarrelZebra2149 님 판단입니다.** 여기서는 실측과 근거만 제시합니다.

## 6. 부수 발견 — 러너 비용 추정이 0이다

```
── measurement: cost estimated=$0.0000 measured=$0.0034 error=-100.0%
── measurement: 25 call(s) had no cost data (unregistered model)
```

`src/shared/pricing.ts` 의 정적 가격표에 `gpt-5.6-luna` 가 없어서 추정이 항상 0이고,
오차가 언제나 -100% 로 표시된다. provider 실측(`measured`)은 정상이므로 **측정 자체는 유효**하지만,
추정/실측 비교(spec 003 D-5 의 의도)는 이 모델로는 성립하지 않는다. 가격표에 한 줄 추가하면 된다.

## 7. 실측 추가 (2026-09-08, kind · LiteLLM v1.96.0 · `ARGUS_BLOCK_STATUS=403`)

§5 의 "헤더로 우회" 가설을 쟀다. `controlLevel: Enforce` 에서 한 principal 로 0.1초 간격 호출:

```
call 1:  HTTP 200   x-argus-decision-id: None        x-argus-control-level: None
call 26: HTTP 403   x-argus-decision-id: d-fbc2d7df9588c345   x-argus-control-level: Enforce
   body: {"error":{"message":"Argus blocked a runaway pattern: principal_calls=26>26","code":"403",
          "provider_specific_fields":{"decision_id":"d-fbc2d7df9588c345","control_level":"Enforce",
          "policy_hit":[{"axis":"principal_calls","level":"hard","observed":26,"threshold":26,...}],...}}}
```

- ✅ **응답 헤더는 LiteLLM 오류 봉투를 뚫고 그대로 나온다.** `x-argus-decision-id` · `x-argus-control-level` 둘 다.
  FastAPI 미들웨어 없이 `HTTPException(headers=...)` 로 충분하다
- ✅ 상태코드 403 도 그대로 나온다 (`code: "403"`)
- 본문의 `decision_id` 는 여전히 `error.provider_specific_fields` 아래다 (§2 와 같음)
- 허용 응답에는 argus 헤더가 없다 — 차단일 때만 실린다

**샘플앱 쪽 제안을 §5 의 JSON 경로에서 헤더로 바꾼다**: `x-argus-decision-id` 헤더 존재 = 차단.
경로 탐색보다 한 줄 짧고, 봉투 모양(탭 구현)에 아예 의존하지 않는다. Linear STP-22 에 전달.
