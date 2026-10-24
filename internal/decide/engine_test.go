// SPDX-License-Identifier: Apache-2.0

package decide

// 파이썬 판본(hack/guardrail/test_argus_decide.py)과 **같은 케이스**를 돈다.
// 두 구현이 같은 입력에 다른 답을 내면 원격↔폴백 전환 순간이 오탐이 된다.
// (EnvContract 는 제외 — env 파싱은 파이썬 폴백만의 사정이다. Go 엔진은 컨트롤러가
//  렌더한 정책을 직접 받는다.)
//
// 가장 중요한 테스트는 차단이 되는지가 **아니라** 정상이 안 막히는지다.
// 오탐 하나가 남의 진짜 작업을 끊는다.

import (
	"fmt"
	"testing"
)

// 실측 도출값 (derive_thresholds.py, 129콜 3코호트)
func measured(level string) Policy {
	return Policy{
		ControlLevel: level, WindowS: 60, CooldownS: 30,
		RepeatWindow: 20, RepeatSoft: 2, RepeatHard: 20,
		PrincipalCallsSoft: 11, PrincipalCallsHard: 26,
		RunStepsSoft: 8, RunStepsHard: 27,
		RotationGapSoft: 3.0, RotationGapHard: 5.2,
		MaxPrincipals: 10_000, Source: "test",
	}
}

func s(principal, run, conv, tool string, args map[string]string) Signals {
	if args == nil {
		args = map[string]string{"path": "aaa"}
	}
	return Signals{Principal: principal, RunID: run, ConversationHash: conv,
		ConversationSource: "client", ToolName: tool, ToolArgsValueHash: args}
}

// ── 오탐 없음 ───────────────────────────────────────────────────────────────

func TestMeasuredBaselineShapeIsNeverBlocked(t *testing.T) {
	// 대조군 실측 형태: principal 4명 · 각 2런 · 런당 5스텝 · 인자 매번 다름
	e := New(measured(Enforce))
	now := 1000.0
	for p := 0; p < 4; p++ {
		for run := 0; run < 2; run++ {
			for step := 0; step < 5; step++ {
				d := e.Decide(s(
					fmt.Sprintf("base%d", p),
					fmt.Sprintf("base%d-r%d", p, run),
					fmt.Sprintf("base%d-c%d", p, run),
					"read_file",
					map[string]string{"path": fmt.Sprintf("file-%d-%d-%d", p, run, step)},
				), now)
				if d.Decision != "allow" {
					t.Fatalf("정상이 차단됨: %s", d.Reason)
				}
				if d.WouldBlock {
					t.Fatalf("정상에 would_block: %s", d.Reason)
				}
				now += 2.5
			}
		}
	}
}

func TestSingleSoftHitNeverBlocks(t *testing.T) {
	e := New(measured(Enforce))
	now := 1000.0
	var d Decision
	for i := 0; i < 2; i++ {
		d = e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
		now += 5
	}
	if d.Decision != "allow" {
		t.Fatalf("soft 하나로 차단됐다: %s", d.Reason)
	}
	found := false
	for _, h := range d.PolicyHit {
		if h.Level == "soft" {
			found = true
		}
	}
	if !found {
		t.Fatal("soft 히트는 기록돼야 한다")
	}
}

func TestTwoSoftHitsDoNotBlockByDefault(t *testing.T) {
	e := New(measured(Enforce))
	now := 1000.0
	var d Decision
	for i := 0; i < 12; i++ { // principal_calls soft(11) + duplicate soft(2)
		d = e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
		now += 1
	}
	softs := 0
	for _, h := range d.PolicyHit {
		if h.Level == "soft" {
			softs++
		}
	}
	if softs < 2 {
		t.Fatalf("soft 두 축이 걸린 상황이어야 한다: %d", softs)
	}
	if d.Decision != "allow" {
		t.Fatal("기본값에서는 soft 2개로 막지 않는다")
	}
}

func TestSoftEscalationOptInBlocks(t *testing.T) {
	p := measured(Enforce)
	p.SoftEscalation = true
	e := New(p)
	now := 1000.0
	var d Decision
	for i := 0; i < 12; i++ {
		d = e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
		now += 1
	}
	if d.Decision != "block" {
		t.Fatal("옵트인 시 soft 2개로 막아야 한다")
	}
}

// ── 폭주는 잡는다 ───────────────────────────────────────────────────────────

func TestRetryStormBlocked(t *testing.T) {
	e := New(measured(Enforce))
	now, blockedAt := 1000.0, 0
	for i := 1; i <= 50; i++ {
		d := e.Decide(s("retry", "run-01", "c-retry", "read_file",
			map[string]string{"path": "config.yaml"}), now)
		if d.Decision == "block" && blockedAt == 0 {
			blockedAt = i
		}
		now += 0.2
	}
	if blockedAt == 0 {
		t.Fatal("재시도 폭풍이 안 막혔다")
	}
	if blockedAt > 30 {
		t.Fatalf("너무 늦게 막힘: %d콜", blockedAt)
	}
}

func TestPingpongBlockedByPrincipalNotByRun(t *testing.T) {
	// ⭐ 이 제품의 논지. 실측에서 run_steps 의 핑퐁 탐지율이 0% 였다.
	e := New(measured(Enforce))
	now, blockedAt := 1000.0, 0
	var first []string
	for i := 1; i <= 50; i++ {
		run := fmt.Sprintf("run-%02d", (i-1)/5+1) // 5콜마다 회전 (실측과 동일)
		d := e.Decide(s("pp", run, "pp-"+run, "read_file",
			map[string]string{"path": fmt.Sprintf("f%d", i)}), now)
		if d.Decision == "block" && blockedAt == 0 {
			blockedAt = i
			for _, h := range d.PolicyHit {
				if h.Level == "hard" {
					first = append(first, h.Axis)
				}
			}
		}
		now += 0.2
	}
	if blockedAt == 0 {
		t.Fatal("핑퐁이 안 막혔다")
	}
	principalCaught := false
	for _, a := range first {
		if a == "run_steps" {
			t.Fatal("run_steps 가 핑퐁을 잡으면 안 된다 — 회전이 그 축을 무력화한다")
		}
		if a == "principal_calls" || a == "rotation_gap" {
			principalCaught = true
		}
	}
	if !principalCaught {
		t.Fatalf("principal 축이 잡아야 한다. 실제: %v", first)
	}
}

func TestRunStepsAxisIsBlindToRotation(t *testing.T) {
	p := measured(Enforce)
	p.PrincipalCallsSoft, p.PrincipalCallsHard = 0, 0
	p.RotationGapSoft, p.RotationGapHard = 0, 0
	p.RepeatSoft, p.RepeatHard = 0, 0
	e := New(p)
	now := 1000.0
	var d Decision
	for i := 1; i <= 50; i++ {
		d = e.Decide(s("pp", fmt.Sprintf("run-%02d", (i-1)/5+1), "c", "read_file",
			map[string]string{"path": fmt.Sprintf("f%d", i)}), now)
		now += 0.2
	}
	if d.Decision != "allow" {
		t.Fatal("run_steps 만으로는 핑퐁을 못 잡아야 한다(측정된 사실)")
	}
}

// ── 제어 수준 ───────────────────────────────────────────────────────────────

func TestObserveRecordsButNeverBlocks(t *testing.T) {
	e := New(measured(Observe))
	now := 1000.0
	var d Decision
	for i := 0; i < 50; i++ {
		d = e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
		now += 0.2
	}
	if d.Decision != "allow" {
		t.Fatal("Observe 는 절대 차단하지 않는다")
	}
	if !d.WouldBlock {
		t.Fatal("그러나 would_block 은 기록돼야 한다")
	}
	if d.DecisionID == "" {
		t.Fatal("판정 식별자는 Observe 에서도 발급된다")
	}
}

func TestDecisionCarriesFailureMode(t *testing.T) {
	p := measured(Enforce)
	p.FailureMode = "Closed"
	if d := New(p).Decide(s("p", "r", "c", "t", nil), 1000); d.FailureMode != "Closed" {
		t.Fatalf("판정 응답에 failure_mode 가 없다: %+v", d)
	}
}

func TestBudgetRoundsLikePython(t *testing.T) {
	p := measured(Enforce)
	p.BudgetUSDHard = 1
	d := New(p).Decide(Signals{Principal: "b", RunID: "r", CostUSD: 0.999999999}, 1000)
	if d.Decision != "block" {
		t.Fatalf("파이썬 round(0.999999999, 8)=1.0 이면 트립 — Go 도 같아야 한다: %+v", d)
	}
}

func TestNoRunIDDoesNotEvaluateRunSteps(t *testing.T) {
	p := measured(Enforce)
	p.RunStepsHard, p.PrincipalCallsHard, p.RotationGapHard = 5, 0, 0
	e := New(p)
	var d Decision
	for i := 0; i < 8; i++ {
		d = e.Decide(s("p", "", "c1", "read_file", map[string]string{"path": fmt.Sprint(i)}), 1000+float64(i))
	}
	if _, seen := d.Observations["run_steps"]; d.WouldBlock || seen {
		t.Fatalf("run ID 없는 호출이 run_steps 로 트립했다: %+v", d)
	}
}

// Guard — 행동 축은 기록만, 사용량·비용(backstop) 축 hard 는 막는다.
func TestGuardRecordsBehaviorTrips(t *testing.T) {
	e := New(measured(Guard))
	now := 1000.0
	var d Decision
	for i := 0; i < 22; i++ { // duplicate_calls hard 20 만 넘고 principal_calls hard 26 은 안 넘는다
		d = e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
		now += 0.2
	}
	if d.Decision != "allow" || !d.WouldBlock {
		t.Fatalf("Guard 는 행동 축 트립을 기록만 한다: %+v", d)
	}
}

func TestGuardBlocksVolumeBackstop(t *testing.T) {
	p := measured(Guard)
	p.RunStepsHard, p.RotationGapHard = 0, 0 // 행동 축을 꺼서 사용량 축만 본다
	e := New(p)
	now := 1000.0
	var d Decision
	for i := 0; i < 26; i++ { // 인자·run 을 바꿔 행동 축을 피하고 principal_calls 만 26
		d = e.Decide(s("p", fmt.Sprintf("r%d", i), "c1", "read_file", map[string]string{"path": fmt.Sprint(i)}), now)
		now += 0.2
	}
	if d.Decision != "block" {
		t.Fatalf("Guard 는 사용량 상한을 막는다: %+v", d)
	}
	if d = e.Decide(s("p", "rx", "c1", "read_file", map[string]string{"path": "x"}), now+1); d.Decision != "block" {
		t.Fatalf("쿨다운 동안도 막는다: %+v", d)
	}
}

func TestGuardBlocksVolumeDuringBehaviorCooldown(t *testing.T) {
	e := New(measured(Guard))
	now := 1000.0
	var ds []string
	for i := 0; i < 27; i++ {
		ds = append(ds, e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "same"}), now).Decision)
		now += 0.2
	}
	if ds[19] != "allow" || ds[26] != "block" {
		t.Fatalf("20번째(행동)는 allow, 27번째(사용량)는 block 이어야 한다: %v", ds)
	}
	last := e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
	found := false
	for _, h := range last.PolicyHit {
		found = found || (h.Axis == "principal_calls" && h.Level == "hard")
	}
	if !found {
		t.Fatalf("막은 이유(principal_calls hard)가 policy_hit 에 없다: %+v", last.PolicyHit)
	}
}

// ── 역학 ────────────────────────────────────────────────────────────────────

func TestSlidingWindowForgets(t *testing.T) {
	e := New(measured(Enforce))
	now := 1000.0
	for i := 0; i < 25; i++ {
		e.Decide(s("w", "r1", "c1", "read_file",
			map[string]string{"path": fmt.Sprintf("%f", now)}), now)
		now += 1
	}
	d := e.Decide(s("w", "r1", "c1", "read_file", map[string]string{"path": "x"}), now+3600)
	if d.Observations["principal_calls"] != 1 {
		t.Fatalf("윈도우가 안 미끄러졌다: %v", d.Observations["principal_calls"])
	}
}

func TestCooldownHoldsWithoutReevaluating(t *testing.T) {
	e := New(measured(Enforce))
	now := 1000.0
	var d Decision
	for i := 0; i < 30; i++ {
		d = e.Decide(s("cd", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
		now += 0.2
	}
	if d.Decision != "block" {
		t.Fatal("선행 조건: 차단 상태여야 한다")
	}
	d2 := e.Decide(s("cd", "r1", "c1", "read_file", map[string]string{"path": "brand-new"}), now+1)
	if d2.Decision != "block" {
		t.Fatal("쿨다운 중에는 유지돼야 한다")
	}
	d3 := e.Decide(s("cd", "r1", "c1", "read_file", map[string]string{"path": "brand-new"}), now+3600)
	if d3.Decision != "allow" {
		t.Fatal("쿨다운이 끝나면 풀려야 한다")
	}
}

func TestGrowingConversationDoesNotDefeatFingerprint(t *testing.T) {
	e := New(measured(Enforce))
	now := 1000.0
	var d Decision
	for i := 1; i <= 30; i++ {
		d = e.Decide(s("g", "r1", "c1", "read_file",
			map[string]string{"path": "config.yaml"}), now)
		now += 0.2
	}
	if d.Decision != "block" {
		t.Fatal("대화가 자라도 도구 반복은 잡혀야 한다")
	}
}

func TestNoToolCallYieldsNoDuplicateAxis(t *testing.T) {
	e := New(measured(Enforce))
	d := e.Decide(Signals{Principal: "p", RunID: "r", ConversationHash: "c",
		ConversationSource: "client"}, 1000.0)
	if _, ok := d.Observations["duplicate_calls"]; ok {
		t.Fatal("도구 호출이 없으면 duplicate_calls 관측치도 없다")
	}
}

func TestUnknownConversationSkipsDuplicateAxis(t *testing.T) {
	// spec 006f — 대화를 모르면 세지 않는다. 가짜 버킷은 오탐을 만든다.
	e := New(measured(Enforce))
	var d Decision
	for i := 0; i < 6; i++ {
		d = e.Decide(Signals{Principal: "p", RunID: "r1",
			ConversationSource: "unavailable", ToolName: "read_file",
			ToolArgsValueHash: map[string]string{"path": "abc"}}, 1000.0+float64(i))
	}
	if _, ok := d.Observations["duplicate_calls"]; ok {
		t.Fatal("대화를 모르는데 반복을 셌다")
	}
}

func TestPrincipalStateIsBounded(t *testing.T) {
	p := measured(Enforce)
	p.MaxPrincipals = 50
	e := New(p)
	for i := 0; i < 500; i++ {
		e.Decide(s(fmt.Sprintf("p%d", i), "r", "c", "read_file", nil), 1000.0+float64(i))
	}
	if len(e.principals) > 50 {
		t.Fatalf("principal 상태가 상한을 넘었다: %d", len(e.principals))
	}
	if e.Evicted == 0 {
		t.Fatal("축출이 기록돼야 한다")
	}
}

func TestDecisionIDOnlyWhenTripped(t *testing.T) {
	e := New(measured(Enforce))
	d := e.Decide(s("p", "r1", "c1", "read_file", map[string]string{"path": "one"}), 1000.0)
	if d.DecisionID != "" {
		t.Fatal("안 걸렸으면 판정 식별자가 없다")
	}
}

func TestSingleRunHasNoRotationSignal(t *testing.T) {
	e := New(measured(Enforce))
	now := 1000.0
	var d Decision
	for i := 0; i < 20; i++ {
		d = e.Decide(s("one", "only", "c1", "read_file",
			map[string]string{"path": fmt.Sprintf("%f", now)}), now)
		now += 0.5
	}
	if _, ok := d.Observations["rotation_gap"]; ok {
		t.Fatal("run 이 하나뿐이면 rotation_gap 은 신호가 아니다")
	}
}

// ── 파이썬과의 동등성 ───────────────────────────────────────────────────────

func TestFingerprintMatchesPython(t *testing.T) {
	// 파이썬: sha256("read_file|attempt=6b86…|path=5e83…")[:16]
	// 같은 입력 → 같은 지문이어야 원격↔폴백 전환 순간 반복 카운트가 안 끊긴다.
	// 기대값은 파이썬 _fingerprint 로 직접 계산했다:
	//   >>> _fingerprint({"tool_name":"read_file",
	//   ...   "tool_args_value_hashes":{"path":"abc123","attempt":"def456"}})
	got := fingerprint(Signals{ToolName: "read_file",
		ToolArgsValueHash: map[string]string{"path": "abc123", "attempt": "def456"}})
	want := pythonFingerprint(t, "read_file",
		map[string]string{"path": "abc123", "attempt": "def456"})
	if got != want {
		t.Fatalf("지문 불일치: go=%s python=%s", got, want)
	}
}

func TestBudgetObservesAbsoluteSpendNotSum(t *testing.T) {
	// cost_usd 는 principal 누적 지출의 절대값 — 같은 값 두 번은 두 배가 아니고 (STP-28),
	// 영속 출처가 아니면(탭 정산) 줄어든 값은 무시한다. 영속 출처는 TestBudgetResetUnblocksAfterCooldown.
	p := measured(Enforce)
	p.BudgetUSDHard = 1
	e := New(p)
	sc := func(cost float64) Signals { x := s("p", "r1", "c1", "read_file", nil); x.CostUSD = cost; return x }
	e.Decide(sc(0.5), 1000)
	if d := e.Decide(sc(0.5), 1001); d.Observations["budget_usd"] != 0.5 {
		t.Fatalf("같은 값 두 번 = %v, 기대 0.5", d.Observations["budget_usd"])
	}
	if d := e.Decide(sc(0.7), 1002); d.Observations["budget_usd"] != 0.7 {
		t.Fatalf("증가 = %v, 기대 0.7", d.Observations["budget_usd"])
	}
	if d := e.Decide(sc(0.1), 1003); d.Observations["budget_usd"] != 0.7 {
		t.Fatalf("탭 정산의 감소는 무시 = %v, 기대 0.7", d.Observations["budget_usd"])
	}
}

func TestBudgetResetUnblocksAfterCooldown(t *testing.T) {
	// 예산을 넘겨 막힌 주체도 LiteLLM 이 지출을 리셋하면 쿨다운 뒤 풀린다. 예전 max 의미론은 재기동 전까지 막았다.
	// 탭 정산(영속 아님)의 감소는 리셋이 아니다 — LiteLLM 파드 재기동으로 0 이 된 것일 수 있다.
	p := measured(Enforce)
	p.BudgetUSDHard = 1
	for _, c := range []struct {
		authoritative bool
		want          string
	}{{true, "allow"}, {false, "block"}} {
		e := New(p)
		sc := func(cost float64) Signals {
			x := s("reset", "r1", "c1", "read_file", nil)
			x.CostUSD, x.CostAuthoritative = cost, c.authoritative
			return x
		}
		if d := e.Decide(sc(1.2), 1000); d.Decision != "block" {
			t.Fatalf("선행 조건: 예산 초과로 막혀야 한다, got %s", d.Decision)
		}
		if d := e.Decide(sc(0.05), 1000+p.CooldownS+1); d.Decision != c.want {
			t.Fatalf("authoritative=%v: 감소 뒤 %s, want %s (%s)", c.authoritative, d.Decision, c.want, d.Reason)
		}
	}
}

func TestRetryingDuringCooldownDoesNotExtendBlock(t *testing.T) {
	// 막힌 호출은 창에 남지 않는다. 남으면 1초마다 재시도하는 클라이언트는 창(60초)이 쿨다운(30초)보다 길어 영원히 막힌다.
	p := measured(Enforce)
	p.PrincipalCallsSoft, p.PrincipalCallsHard = 0, 5
	e := New(p)
	sawBlock, freedAt := false, 0.0
	for now := 1000.0; now <= 1090; now++ {
		d := e.Decide(s("retry", "r1", "c1", "read_file", map[string]string{"path": fmt.Sprint(now)}), now)
		sawBlock = sawBlock || d.Decision == "block"
		if sawBlock && d.Decision == "allow" {
			freedAt = now
			break
		}
	}
	if !sawBlock {
		t.Fatal("선행 조건: 5번째 호출에서 막혀야 한다")
	}
	// 통과한 4콜(1000~1003)이 창에서 빠지는 1064 즈음 풀린다.
	if freedAt == 0 || freedAt > 1065 {
		t.Fatalf("재시도가 차단을 연장했다: freedAt=%v", freedAt)
	}
}

func TestCooldownSurvivesPrincipalEviction(t *testing.T) {
	// STP-24 재현 테스트 그대로: 차단당한 주체가 조용한 사이 다른 주체들이 LRU 를 넘겨도 쿨다운은 남는다.
	p := measured(Enforce)
	p.MaxPrincipals = 3
	e := New(p)
	now := 1000.0
	var d Decision
	for i := 0; i < 30; i++ {
		d = e.Decide(s("victim", "r1", "c1", "read_file", map[string]string{"path": "same"}), now)
		now += 0.2
	}
	if d.Decision != "block" {
		t.Fatalf("선행 조건: 차단 상태여야 한다, got %s", d.Decision)
	}
	for i := 0; i < 20; i++ {
		e.Decide(s(fmt.Sprintf("noise%d", i), "r", "c", "read_file", map[string]string{"path": fmt.Sprint(i)}), now)
		now += 0.1
	}
	if e.Evicted == 0 {
		t.Fatal("선행 조건: 축출이 일어났어야 한다")
	}
	d2 := e.Decide(s("victim", "r2", "c2", "read_file", map[string]string{"path": "brand-new"}), now)
	if d2.Decision != "block" {
		t.Fatalf("쿨다운 중인데 축출로 풀렸다 — decision=%s evicted=%d", d2.Decision, e.Evicted)
	}
}

func TestCooldownKeepsAccumulatingObservations(t *testing.T) {
	// 쿨다운 중에도 나간 호출은 계속 쌓인다 — 임계가 낮은 축 하나가 나머지 축의 눈을 가리면 안 된다.
	// Observe 라 호출이 나간다. Enforce 처럼 막힌 호출은 쌓지 않는다 (TestRetryingDuringCooldownDoesNotExtendBlock).
	p := measured(Observe)
	p.BudgetUSDHard = 0.001
	e := New(p)
	first := s("b", "r1", "c1", "read_file", nil)
	first.CostUSD = 0.5
	if d := e.Decide(first, 1000); !d.WouldBlock {
		t.Fatal("선행 조건: 예산이 먼저 걸려야 한다")
	}
	var d Decision
	for i := 0; i < 5; i++ {
		d = e.Decide(s("b", "r1", "c1", "read_file", map[string]string{"path": fmt.Sprint(i)}), 1001+float64(i))
	}
	if len(d.PolicyHit) != 1 || d.PolicyHit[0].Axis != "cooldown" {
		t.Fatalf("쿨다운 중 hits = %+v", d.PolicyHit)
	}
	if d.Observations["principal_calls"] != 6 {
		t.Fatalf("쿨다운 중 누적이 멈췄다: principal_calls=%v", d.Observations["principal_calls"])
	}
}

func TestGrowthIsRelativeToFirstCallAndSoftOnlyNeverBlocks(t *testing.T) {
	p := measured(Enforce)
	p.ConvGrowthSoft, p.MsgBytesSoft = 3.0, 5000
	e := New(p)
	var d Decision
	for i, b := range []float64{1000, 1500, 2200, 3300, 5000, 7500} {
		x := s("p", "r1", "c1", "read_file", map[string]string{"path": fmt.Sprint(i)})
		x.MsgBytes = b
		d = e.Decide(x, 1000+float64(i))
	}
	if d.Observations["conv_growth"] != 7.5 || d.Observations["msg_bytes"] != 7500 {
		t.Fatalf("obs = %v", d.Observations)
	}
	got := map[string]string{}
	for _, h := range d.PolicyHit {
		got[h.Axis] = h.Level
	}
	if got["conv_growth"] != "soft" || got["msg_bytes"] != "soft" || len(got) != 2 {
		t.Fatalf("hits = %v", got)
	}
	if d.Decision != "allow" {
		t.Fatal("soft 만으로는 절대 막지 않는다")
	}
}

func TestGrowthNotEvaluatedWithoutConversationOrSize(t *testing.T) {
	p := measured(Enforce)
	p.ConvGrowthSoft = 2.0
	e := New(p)
	x := s("p", "r1", "c1", "read_file", nil)
	x.MsgBytes = 100
	if d := e.Decide(x, 1000); d.Observations["conv_growth"] != 0 {
		if _, ok := d.Observations["conv_growth"]; ok {
			t.Fatal("첫 호출엔 분모가 없다")
		}
	}
	y := s("p", "r1", "", "read_file", map[string]string{"path": "b"})
	y.ConversationSource, y.MsgBytes = "unavailable", 9999
	d := e.Decide(y, 1001)
	if _, ok := d.Observations["conv_growth"]; ok {
		t.Fatal("대화를 모르면 평가하지 않는다")
	}
	if d.Observations["msg_bytes"] != 9999 {
		t.Fatal("단일 크기는 대화 없이도 관측한다")
	}
}

func facets(kv map[string][3]string) map[string]Facet {
	out := map[string]Facet{}
	for k, v := range kv {
		out[k] = Facet{H: v[0], S: v[1], D: v[2]}
	}
	return out
}

func TestRetryWithAttemptCounterIsSoftCaughtButNeverHard(t *testing.T) {
	p := measured(Enforce)
	p.RepeatSoft, p.RepeatHard = 5, 20
	e := New(p)
	var d Decision
	for i := 1; i <= 12; i++ {
		x := s("p", "r1", "c1", "read_file", map[string]string{"path": "cfg", "attempt": fmt.Sprint(i)})
		x.ToolArgFacets = facets(map[string][3]string{"path": {"cfg", "a.a", ""}, "attempt": {fmt.Sprint(i), "<int>", "+1"}})
		d = e.Decide(x, 1000+float64(i))
	}
	if d.Observations["duplicate_calls"] != 1 {
		t.Fatalf("완전일치는 매번 다르다: %v", d.Observations["duplicate_calls"])
	}
	if d.Observations["duplicate_calls_norm"] < 5 {
		t.Fatalf("정규화 반복 = %v", d.Observations["duplicate_calls_norm"])
	}
	var dup []string
	for _, h := range d.PolicyHit {
		if h.Axis == "duplicate_calls" {
			dup = append(dup, h.Level)
		}
	}
	if len(dup) != 1 || dup[0] != "soft" {
		t.Fatalf("duplicate_calls hits = %v (all: %+v)", dup, d.PolicyHit)
	}
	if d.Decision != "allow" {
		t.Fatal("정규화만으로는 절대 hard 가 아니다")
	}
}

func TestReadingDifferentFilesIsNotCountedAsRepeat(t *testing.T) {
	p := measured(Enforce)
	p.RepeatSoft, p.RepeatHard = 3, 20
	e := New(p)
	var d Decision
	for i := 1; i <= 12; i++ {
		f := fmt.Sprintf("f%d", i)
		x := s("p", "r1", "c1", "read_file", map[string]string{"path": f})
		x.ToolArgFacets = facets(map[string][3]string{"path": {f, "a.a", ""}})
		d = e.Decide(x, 1000+float64(i))
	}
	if _, ok := d.Observations["duplicate_calls_norm"]; ok {
		t.Fatal("남는 정체성 키가 없으면 세지 않는다")
	}
	for _, h := range d.PolicyHit {
		if h.Axis == "duplicate_calls" {
			t.Fatalf("duplicate_calls 가 걸렸다: %+v", h)
		}
	}
}

func TestManyPrincipalsOneTeamTripTeamBackstop(t *testing.T) {
	p := measured(Enforce)
	p.TeamCallsHard = 10
	e := New(p)
	var d Decision
	for i := 0; i < 12; i++ {
		x := s(fmt.Sprintf("p%d", i), fmt.Sprintf("r%d", i), fmt.Sprintf("c%d", i), "read_file", map[string]string{"path": fmt.Sprint(i)})
		x.Team = "team-x"
		d = e.Decide(x, 1000+float64(i))
	}
	// 10번째부터 막히고, 막힌 호출은 팀 창에 남지 않아 관측은 10 에 머문다.
	if d.Observations["team_calls"] != 10 {
		t.Fatalf("team_calls = %v", d.Observations["team_calls"])
	}
	var hard []string
	for _, h := range d.PolicyHit {
		if h.Level == "hard" {
			hard = append(hard, h.Axis)
		}
	}
	if len(hard) != 1 || hard[0] != "team_calls" || d.Decision != "block" {
		t.Fatalf("hard = %v decision = %s", hard, d.Decision)
	}
}

func TestNoTeamNoAxis(t *testing.T) {
	p := measured(Enforce)
	p.TeamCallsHard = 1
	e := New(p)
	d := e.Decide(s("p", "r1", "c1", "read_file", nil), 1000)
	if _, ok := d.Observations["team_calls"]; ok || d.Decision != "allow" {
		t.Fatalf("obs = %v decision = %s", d.Observations, d.Decision)
	}
}
