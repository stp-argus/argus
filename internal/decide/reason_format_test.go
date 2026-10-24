// SPDX-License-Identifier: Apache-2.0

package decide

import (
	"fmt"
	"strings"
	"testing"
)

// reasonFormatCases — 파이썬 판본 hack/guardrail/test_argus_decide.py 의 REASON_FORMAT_CASES 와
// **같은 표**다. 한쪽만 바꾸면 두 구현이 다른 문장을 만든다 (parity 테스트는 reason 을 안 본다).
var reasonFormatCases = []struct {
	in   float64
	want string
}{
	{20, "20"},
	{0, "0"},
	{96.98, "96.98"},
	{130.2, "130.2"},
	{0.0023826, "0.0023826"}, // 센트 미만 예산 — 예전 %.2f 는 "0" 으로 뭉갰다
	{25.806577444076538, "25.8066"},
	{400, "400"},
	{1e-7, "1e-07"},
}

func TestTrimFloatMatchesPythonG6(t *testing.T) {
	for _, c := range reasonFormatCases {
		if got := trimFloat(c.in); got != c.want {
			t.Errorf("trimFloat(%v) = %q, want %q", c.in, got, c.want)
		}
	}
}

// 판정은 observed >= threshold 다. 문장이 ">" 면 관객 눈에 "20>20" — 틀린 식으로 읽힌다.
func TestReasonUsesGreaterOrEqual(t *testing.T) {
	p := DefaultPolicy()
	p.ControlLevel, p.RunStepsHard = Enforce, 3
	e := New(p)
	var d Decision
	for i := 0; i < 3; i++ {
		d = e.Decide(Signals{Principal: "p", RunID: "r"}, float64(1000+i))
	}
	if want := "run_steps=3>=3"; d.Reason != want {
		t.Fatalf("reason = %q, want %q", d.Reason, want)
	}
}

// 정수가 아닌 값이 **엔진 reason 을 실제로 통과**하는지. 위 표는 trimFloat 만 본다.
// 0.123456789 는 날것(%v)·3자리·6자리가 전부 다르게 찍히는 값이다 — 실측 로그의
// "cooldown/hard=29.284574270248413>0" 같은 꼴을 잡는다.
// 파이썬 test_argus_decide.py ReasonText.test_fractional_reason_matches_go 와 같은 기대 문장.
func TestReasonFractionalBudget(t *testing.T) {
	p := DefaultPolicy()
	p.ControlLevel, p.BudgetUSDHard = Enforce, 0.1
	d := New(p).Decide(Signals{Principal: "p", RunID: "r", CostUSD: 0.123456789}, 1000)
	if want := "budget_usd=0.123457>=0.1"; d.Reason != want {
		t.Fatalf("reason = %q, want %q", d.Reason, want)
	}
}

// 오퍼레이터 로그 줄도 같은 식이어야 한다 — 가드레일 로그는 >= 인데 여기만 > 로 남았던 적이 있다.
func TestServerLogUsesGreaterOrEqual(t *testing.T) {
	var got string
	s := NewServer(New(DefaultPolicy()), func(f string, a ...any) { got = fmt.Sprintf(f, a...) })
	s.logDecision(Signals{Principal: "p", RunID: "r"}, Decision{
		Decision: "block", WouldBlock: true, ControlLevel: Enforce,
		PolicyHit: []Hit{{Axis: "run_steps", Level: "hard", Observed: 18, Threshold: 18}},
	})
	if !strings.Contains(got, "run_steps/hard=18>=18") {
		t.Fatalf("log = %q", got)
	}
}
