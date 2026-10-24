// SPDX-License-Identifier: Apache-2.0

package decide

// Prometheus 메트릭 — 판정 서버(:8080)의 /metrics.
//
// 판정은 오퍼레이터 한 곳에서만 내리므로 여기서 세면 게이트웨이 레플리카 수와 무관하게
// **한 번씩만** 센다. 레지스트리는 controller-runtime 의 것을 써서 컨트롤러 기본 메트릭도
// 같은 엔드포인트에 나온다 (오퍼레이터의 별도 metrics 서버는 기본 off 라 쓰지 않는다).
//
// 라벨에 principal 은 넣지 않는다 — 카디널리티가 사용자 수만큼 터진다. 누가(주체)는
// 레코드·알림이 답하고, 메트릭은 팀·범주 단위 추세를 답한다.

import (
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
)

var (
	mDecisions = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "argus_decisions_total",
		Help: "판정 수. decision=allow|block, would_block=true|false.",
	}, []string{"control_level", "decision", "would_block"})

	mDetections = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "argus_detections_total",
		Help: "임계를 넘은 축 관측 수 (쿨다운 유지 제외). 호출마다 센다.",
	}, []string{"category", "axis", "level", "control_level"})

	mTrips = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "argus_trips_total",
		Help: "폭주 판정이 새로 걸린 순간(트립 엣지) 수. 쿨다운 동안의 반복은 세지 않는다.",
	}, []string{"category", "control_level"})

	mTeamCalls = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "argus_team_calls_total",
		Help: "팀별 LLM 호출 수. team 은 해시(레코드와 같은 값).",
	}, []string{"team"})

	mTeamCost = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "argus_team_cost_usd_total",
		Help: "팀별 누적 지출 증가분(USD). 주체 누적 지출의 증분을 합산 — 재기동 뒤 첫 관측은 세지 않는다.",
	}, []string{"team"})

	// 판정 처리 시간 — 탭 타임아웃(기본 500ms)과 견줄 값. 네트워크 왕복은 빠진다(탭 쪽에서만 보인다).
	mDecideDuration = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name:    "argus_decide_duration_seconds",
		Help:    "/v1/decide 요청 하나의 서버 처리 시간(인증 포함).",
		Buckets: []float64{0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5},
	})

	mAuthFailures = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "argus_decide_auth_failures_total",
		Help: "/v1/decide 인증 실패. reason=missing|invalid|forbidden|review_error.",
	}, []string{"reason"})
)

func init() {
	ctrlmetrics.Registry.MustRegister(mDecisions, mDetections, mTrips, mTeamCalls, mTeamCost,
		mDecideDuration, mAuthFailures)
	for _, r := range []string{"missing", "invalid", "forbidden", "review_error"} {
		mAuthFailures.WithLabelValues(r)
	}
	// 알려진 조합을 0 으로 미리 만든다. 카운터가 첫 Inc() 때 값 1 로 **태어나면** increase() 가
	// 그 첫 증가를 못 본다 — 오퍼레이터가 재기동할 때마다(릴리스마다) 범주별 첫 폭주 알림이 빠진다.
	// (Prometheus 권장: "initialize your metrics". 발행 전 검증에서 잡힌 것 — 2026-09-24)
	for _, cl := range []string{Observe, Guard, Enforce} {
		for _, dec := range []string{"allow", "block"} {
			for _, wb := range []string{"true", "false"} {
				mDecisions.WithLabelValues(cl, dec, wb)
			}
		}
		for _, c := range []string{"cost", "behavior", "volume", "other"} {
			mTrips.WithLabelValues(c, cl)
		}
		for axis, c := range categoryByAxis {
			for _, lv := range []string{"soft", "hard"} {
				mDetections.WithLabelValues(c, axis, lv, cl)
			}
		}
	}
}

func metricsHandler() http.Handler {
	return promhttp.HandlerFor(ctrlmetrics.Registry, promhttp.HandlerOpts{})
}

// observe — 판정 하나를 메트릭에 반영한다. 트립 엣지 여부를 돌려준다(테스트용).
func observe(sig Signals, d Decision) (tripCategories []string) {
	mDecisions.WithLabelValues(d.ControlLevel, d.Decision, boolLabel(d.WouldBlock)).Inc()

	team := sig.Team
	if team == "" {
		team = "(none)"
	}
	mTeamCalls.WithLabelValues(team).Inc()
	// 증분이 0 이어도 시리즈를 만든다 — 새 팀의 첫 호출은 주체 첫 관측이라 증분 0 이고,
	// 그때 0 으로 태어나야 다음 증분부터 increase() 가 본다.
	mTeamCost.WithLabelValues(team).Add(d.CostDeltaUSD)

	cooling := false
	var hard, soft []string
	for _, h := range d.PolicyHit {
		if h.Axis == "cooldown" {
			cooling = true // 이미 걸린 폭주의 연장 — 탐지·트립으로 다시 세지 않는다
			continue
		}
		c := Category(h.Axis)
		mDetections.WithLabelValues(c, h.Axis, h.Level, d.ControlLevel).Inc()
		if h.Level == "hard" {
			hard = appendOnce(hard, c)
		} else {
			soft = appendOnce(soft, c)
		}
	}
	// 트립 엣지 = 엔진의 폭주 판정 그대로(WouldBlock) + 쿨다운 아님. 범주는 hard 축의 것,
	// hard 가 없으면(soft_escalation: soft 2개로 트립) soft 축의 것 — 트립이 메트릭에서 빠지지 않게.
	if d.WouldBlock && !cooling {
		tripCategories = hard
		if len(tripCategories) == 0 {
			tripCategories = soft
		}
		for _, c := range tripCategories {
			mTrips.WithLabelValues(c, d.ControlLevel).Inc()
		}
	}
	return tripCategories
}

func appendOnce(xs []string, x string) []string {
	for _, v := range xs {
		if v == x {
			return xs
		}
	}
	return append(xs, x)
}

func boolLabel(b bool) string {
	if b {
		return "true"
	}
	return "false"
}
