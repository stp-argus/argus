// SPDX-License-Identifier: Apache-2.0

package decide

import (
	"io"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"github.com/prometheus/common/expfmt"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
)

func ctrlmetricsGather() (string, error) {
	mfs, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		return "", err
	}
	var b strings.Builder
	for _, mf := range mfs {
		if _, err := expfmt.MetricFamilyToText(&b, mf); err != nil {
			return "", err
		}
	}
	return b.String(), nil
}

// categoryCases — 파이썬 test_argus_decide.py 의 CATEGORY_CASES 와 **같은 표**다.
var categoryCases = map[string]string{
	"budget_usd": "cost", "duplicate_calls": "behavior", "run_steps": "behavior",
	"conv_growth": "behavior", "msg_bytes": "behavior", "rotation_gap": "behavior",
	"principal_calls": "volume", "team_calls": "volume", "cooldown": "other", "새축": "other",
}

func TestCategoryMatchesPython(t *testing.T) {
	for axis, want := range categoryCases {
		if got := Category(axis); got != want {
			t.Errorf("Category(%q) = %q, want %q", axis, got, want)
		}
	}
}

// 트립은 새로 걸린 순간 한 번만 센다 — 쿨다운 동안의 반복 차단은 트립이 아니다.
func TestTripCountedOnceAcrossCooldown(t *testing.T) {
	p := DefaultPolicy()
	p.ControlLevel, p.RunStepsHard, p.CooldownS = Enforce, 3, 30
	e := New(p)
	before := testutil.ToFloat64(mTrips.WithLabelValues("behavior", Enforce))
	var trips int
	for i := 0; i < 10; i++ {
		d := e.Decide(Signals{Principal: "trip-once", RunID: "r", Team: "t-trip"}, float64(2000+i))
		trips += len(observe(Signals{Team: "t-trip"}, d))
	}
	if trips != 1 {
		t.Fatalf("트립 엣지 %d 번, want 1 (3번째 콜에서 걸리고 나머지는 쿨다운)", trips)
	}
	if got := testutil.ToFloat64(mTrips.WithLabelValues("behavior", Enforce)) - before; got != 1 {
		t.Fatalf("argus_trips_total 증가 %v, want 1", got)
	}
}

// 비용 증분 — 재기동·축출 뒤 첫 관측은 세지 않는다 (과거 누적 전체가 급증으로 찍히는 것 방지).
func TestCostDeltaSkipsFirstSighting(t *testing.T) {
	e := New(DefaultPolicy())
	d1 := e.Decide(Signals{Principal: "cost-p", CostUSD: 12.5}, 3000)
	d2 := e.Decide(Signals{Principal: "cost-p", CostUSD: 12.75}, 3001)
	d3 := e.Decide(Signals{Principal: "cost-p", CostUSD: 12.75}, 3002) // 늘지 않음
	d4 := e.Decide(Signals{Principal: "cost-p", CostUSD: 12.0}, 3003)  // 줄어든 값(리셋) — 증분 없음
	if d1.CostDeltaUSD != 0 || d2.CostDeltaUSD != 0.25 || d3.CostDeltaUSD != 0 || d4.CostDeltaUSD != 0 {
		t.Fatalf("deltas = %v %v %v %v, want 0 0.25 0 0", d1.CostDeltaUSD, d2.CostDeltaUSD, d3.CostDeltaUSD, d4.CostDeltaUSD)
	}
}

// 영속 지출 — 동시 요청이 뒤처진 값을 보내도 두 번 세지 않고, 절반 아래로 떨어지면 리셋으로 본다.
func TestCostDeltaAuthoritativeStaleAndReset(t *testing.T) {
	e := New(DefaultPolicy())
	var got []float64
	for i, c := range []float64{6, 5, 6, 6.5, 1, 1.5} {
		d := e.Decide(Signals{Principal: "auth-p", CostUSD: c, CostAuthoritative: true}, 5000+float64(i))
		got = append(got, d.CostDeltaUSD)
	}
	want := []float64{0, 0, 0, 0.5, 0, 0.5}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("deltas = %v, want %v", got, want)
		}
	}
}

// 쿨다운 중(Observe 라 호출이 나감)에도 비용 증분이 실린다.
func TestCostDeltaDuringCooldown(t *testing.T) {
	p := DefaultPolicy()
	p.RunStepsHard, p.CooldownS = 2, 30
	e := New(p)
	e.Decide(Signals{Principal: "cool-p", RunID: "r", CostUSD: 1}, 4000)
	e.Decide(Signals{Principal: "cool-p", RunID: "r", CostUSD: 1.5}, 4001) // 여기서 트립
	d := e.Decide(Signals{Principal: "cool-p", RunID: "r", CostUSD: 2}, 4002)
	if len(d.PolicyHit) != 1 || d.PolicyHit[0].Axis != "cooldown" {
		t.Fatalf("쿨다운 경로가 아니다: %+v", d.PolicyHit)
	}
	if d.CostDeltaUSD != 0.5 {
		t.Fatalf("쿨다운 중 증분 %v, want 0.5", d.CostDeltaUSD)
	}
}

func TestMetricsEndpointServesArgusSeries(t *testing.T) {
	s := NewServer(New(DefaultPolicy()), nil)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	body := `{"principal":"m-p","run_id":"r","team":"t-metrics"}`
	if _, err := ts.Client().Post(ts.URL+"/v1/decide", "application/json", strings.NewReader(body)); err != nil {
		t.Fatal(err)
	}
	res, err := ts.Client().Get(ts.URL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(res.Body)
	for _, want := range []string{"argus_decisions_total", `argus_team_calls_total{team="t-metrics"} 1`} {
		if !strings.Contains(string(b), want) {
			t.Errorf("/metrics 에 %q 없음", want)
		}
	}
}

// 재기동 직후에도 트립 시리즈가 0 으로 있어야 increase() 가 첫 트립을 본다.
// ⚠️ 여기서 WithLabelValues 를 부르면 그 호출이 시리즈를 만들어 버려 시험이 무의미해진다 —
// 노출 텍스트만 읽고, 다른 테스트가 건드리지 않는 조합(Guard)을 본다.
func TestKnownSeriesExistAtZeroOnStartup(t *testing.T) {
	ms, err := ctrlmetricsGather()
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		`argus_trips_total{category="other",control_level="Guard"} 0`,
		`argus_trips_total{category="cost",control_level="Guard"} 0`,
		`argus_detections_total{axis="budget_usd",category="cost",control_level="Guard",level="hard"} 0`,
		`argus_decisions_total{control_level="Guard",decision="block",would_block="true"} 0`,
	} {
		if !strings.Contains(ms, want) {
			t.Errorf("0 값 시리즈 없음: %s", want)
		}
	}
}

// soft_escalation(soft 2개로 트립)도 트립으로 센다 — hard 가 없다고 빠지면 안 된다.
func TestSoftEscalationTripIsCounted(t *testing.T) {
	p := DefaultPolicy()
	p.ControlLevel, p.SoftEscalation, p.CooldownS = Enforce, true, 30
	p.RunStepsSoft, p.PrincipalCallsSoft = 2, 2
	e := New(p)
	var got []string
	for i := 0; i < 2; i++ {
		d := e.Decide(Signals{Principal: "soft-esc", RunID: "r"}, float64(5000+i))
		if !d.WouldBlock {
			continue
		}
		got = observe(Signals{}, d)
	}
	if len(got) == 0 {
		t.Fatal("soft 2개 트립이 트립으로 안 세졌다")
	}
}
