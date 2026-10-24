// SPDX-License-Identifier: Apache-2.0

package decide

// 범주 — 축을 거버넌스 관점으로 묶는다. 탐지 로직은 바꾸지 않고 **이름표만** 붙인다.
//
//	cost     이상 비용 — 돈이 기준을 넘었다
//	behavior 이상 행동 — 같은 짓을 반복하거나 대화가 비정상적으로 자란다
//	volume   이상 사용량 — 주체·팀 단위로 너무 많이 부른다
//
// (identity — 신원 위조 시도 — 는 판정 축이 아니라 기록 신호라 탭(가드레일)이 붙인다.)
//
// ⚠️ 파이썬 판본 hack/guardrail/argus_decide.py 의 CATEGORY_BY_AXIS 와 **같은 표**다.
// 한쪽만 바꾸면 메트릭(Go)과 레코드·알림(파이썬)이 같은 탐지를 다른 범주로 센다.
// governance_test.go 와 test_argus_governance.py 가 같은 기대값으로 고정한다.
var categoryByAxis = map[string]string{
	"budget_usd":      "cost",
	"duplicate_calls": "behavior",
	"run_steps":       "behavior",
	"conv_growth":     "behavior",
	"msg_bytes":       "behavior",
	"rotation_gap":    "behavior",
	"principal_calls": "volume",
	"team_calls":      "volume",
}

// Category — 모르는 축은 "other". 새 축을 추가하고 표를 잊으면 대시보드에 other 로 보인다.
func Category(axis string) string {
	if c, ok := categoryByAxis[axis]; ok {
		return c
	}
	return "other"
}
