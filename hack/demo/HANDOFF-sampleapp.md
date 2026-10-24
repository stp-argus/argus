# 핸드오프 — 샘플앱에 "채팅 표면" 추가 (담당: @BarrelZebra2149)

> song2 → BarrelZebra2149. 데모데이 제안 WS-A. **너의 스트림(`argus-sample-app`)이 소유·머지한다.**
> 아래는 착수를 줄이는 재료와 지켜야 할 계약 2개일 뿐, 구현 방식은 네 판단.

## 무엇을 / 왜
지금 러너는 스크립트 시나리오만 돈다. 데모에서 **사람이 한 줄 쳐서 "정상"을 보이고, 버튼 하나로
"폭주"를 켜서** 관객이 정상 vs 폭주를 눈으로 비교하게 하고 싶다. 그 "채팅 표면"이 이 핸드오프.

## 재사용 (이미 있는 것 — 새로 짤 필요 없음)
- `src/runner/client.ts` — fetch 기반 LLM 클라이언트(`callLlm`, `/v1/chat/completions`).
- `src/runner/scenarios/{retry-storm,pingpong-recursion}.ts` — 폭주 시나리오. 버튼이 이걸 트리거.
- `src/runner/conversation.ts`, `src/mock/server.ts` — 대화 루프 + mock 업스트림(실비용 0).
- 붙는 법: `OPENAI_BASE_URL`을 LiteLLM 탭으로 지정. **샘플앱은 Argus를 몰라야 한다(무침투 규칙).**

## 지킬 계약 2개 (근거: `argus/docs/decision-event-contract.md`)
1. **신원 헤더 발신** (계약 §3.2) — 요청에 실어라(이름은 env override 가능):
   - `X-Principal-Id`(`ARGUS_PRINCIPAL_HEADER`), `X-Run-Id`(`ARGUS_RUN_ID_HEADER`)
   - `X-Argus-Namespace`/`X-Argus-Pod` (또는 Downward API env `ARGUS_NAMESPACE`/`ARGUS_POD` — spec 007 식별 축)
   - 가상 키를 쓰면 principal은 `key:user_id`로 자동 확정(헤더보다 강함). 헤더는 폴백.
2. **차단 처리 — `BLOCK_STATUSES`에 403 추가** (계약 §6):
   - 현재 `src/runner/client.ts:113` `blockStatuses()` 기본값이 `[429]`. block-contract가 403으로
     이동 중(kind는 이미 403). `[429, 403]`로 열어라.
   - 판별 마커: **`x-argus-decision-id` 응답 헤더(우선)** 또는 바디 `decision_id`. 이게 있으면
     아거스 차단, 없는 429는 업스트림 rate-limit — 섞지 마라.

## 데모 형태 — 열린 질문 (네 스트림 의견 우선)
- (a) 샘플앱 레포 안 **얇은 웹 채팅 페이지** (입력창 + "폭주 실행" 버튼) vs
- (b) 기존 CLI 러너에 **`manual` 시나리오** 추가 (stdin으로 사람이 입력).
→ 어느 쪽이 네 코드에 맞는지 정해줘. 데모 노스스타는 "정상 채팅 → 버튼 → 폭주 → 화면에서 차단".

## 로컬 재현
`HANDOFF-dashboard.md`와 공통 — `hack/demo/README.md` 부록 + 아래 QUICKSTART 참조.
탭 접속: `kubectl -n argus-system port-forward svc/litellm 4400:4000` → `http://127.0.0.1:4400`.

## 코어는 같이 (WS-공통)
탐지/차단/신원 개선 아이디어는 스트림 무관하게 환영. 특히 차단 계약(403/헤더/마커) 개선 의견은
네가 소비자라 제일 잘 보인다 — 이슈로 열어줘.
