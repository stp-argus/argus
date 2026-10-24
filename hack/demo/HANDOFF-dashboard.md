# 핸드오프 — 탐지/차단 대시보드 (담당: @BusanGukbap, spec 007)

> song2 → DongYoon. 데모데이 제안 WS-B. **대시보드는 너의 스트림(spec 007/004)이 소유·머지한다.**
> song2는 아래 "씨앗"과 데이터 계약까지만 넘긴다 — 007 코드는 건드리지 않는다.

## 씨앗 — `hack/demo/dashboard.py` (이미 도는 스탠드인)
내가 로컬 검증용으로 만든 **버릴 스탠드인**을 007의 출발점으로 승격 제안. 지금 하는 일:
- `http.server` stdlib, 포트 **8899**, 의존성 0.
- `records.jsonl`을 `kubectl exec ... tail -f`로 폴링(파드 재기동 자동 재연결) + `policy.json` 30s 폴링.
- 렌더: **주체별 카드**(도구 목록·5축 게이지·`would_block`·신원 티어 태그 + `header`엔 spoof 태그)
  + 40행 이벤트 스트림. `/` HTML(2s 폴링), `/data` JSON.
- 실행: `python3 hack/demo/dashboard.py` → http://localhost:8899

→ 이건 **제품이 아니라 참조 동작본**이다. 007 스코프대로 네가 소유해서 키우면 된다.

## 대시보드 범위와 씨앗의 매핑
| 007 tasks | 씨앗이 이미 하는 것 | 네가 추가할 것 |
|---|---|---|
| B1 식별 축(namespace/pod) | 레코드에 `namespace`/`pod` 필드 **이미 채워짐**(계약 §2.2) | 화면에 그 축 필터/그룹 |
| C1 실시간 호출/비용 곡선 | 축 게이지·이벤트 스트림 | 누적 호출/비용 **곡선**(cumsum) — 007 tasks의 streamlit 스케치 |
| C2 탐지 타임라인 | 이벤트 스트림(40행) | "언제·누가·어떤 정책·왜" 한 줄씩 + `policy_hit` 점등 |

007 tasks.md에 "가짜 records 20줄로 오늘 착수" 경로가 있는데, 씨앗이 그 **가짜 데이터 화면의
실제 동작본**이라 곧장 실데이터로 갈 수 있다.

## 데이터 계약 (반드시 이걸로 그려라) — `argus/docs/decision-event-contract.md`
- 소비할 필드는 계약 §2 전체. "누가"=§2.2 신원, "탐지/차단"=§2.4 판정, "곡선"=§2.5 cost/tokens.
- ⚠️ **한계 2개 주의**(계약 §7):
  1. `decide_reason` 은 사람용 문자열이다 — 표시버그(`budget_usd=0>0`·`20>20`)는 2026-09-24 에 고쳤지만,
     **그리는 근거는 `observations`/`policy_hit` 값으로** 하라. 문자열은 바뀔 수 있다.
  2. `tool_arg_facets` 필드는 006e에서 "007 대시보드가 이걸로 만든다"고 예약해둔 것 — 활용 여지.
- 헤더 이름·403 차단 계약도 계약 문서에 있으니 "차단" 표시는 §6 마커(`x-argus-decision-id`) 기준.

## 데모 노스스타 (한 화면)
정상 채팅 → 폭주 → **비용/스텝 곡선 치솟음 → 주체 카드 빨강 + `observed vs threshold → 차단`**
→ `Observe→Enforce` 라이브 전환 순간. spec 004 US1 그대로. (씨앗의 카드가 이미 would_block로 빨강.)

## 로컬 재현
`hack/demo/README.md` 부록 + QUICKSTART. 탭: `port-forward svc/litellm 4400:4000`.
씨앗은 `kubectl exec`로 클러스터 records를 직접 tail하므로, kind 스택만 떠 있으면 바로 붙는다.

## 코어는 같이 (WS-공통)
식별 축을 뽑는 사람이 화면을 그리므로, **신원 사다리·식별 필드 개선 의견은 네가 제일 잘 본다.**
G2(team backstop)·G1(챗 반복 축)은 대시보드에서 "안 잡히는 폭주"로 먼저 드러날 것 — 발견하면 이슈로.
