// SPDX-License-Identifier: Apache-2.0

package decide

// 파이썬 판본을 **실제로 실행해서** 동등성을 잰다. 기대값을 손으로 옮겨 적으면
// 옮겨 적는 순간의 스냅샷일 뿐이다 — 어느 쪽이 바뀌면 조용히 갈라진다.
// python3 가 없는 환경(CI 이미지 등)에서는 스킵한다.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func pythonFingerprint(t *testing.T, tool string, args map[string]string) string {
	t.Helper()
	argJSON, _ := json.Marshal(args)
	out := runPython(t, fmt.Sprintf(`
import sys, json
sys.path.insert(0, %q)
from argus_decide import _fingerprint
print(_fingerprint({"tool_name": %q, "tool_args_value_hashes": json.loads(%q)}))
`, guardrailDir(t), tool, string(argJSON)))
	return strings.TrimSpace(out)
}

// TestDecisionParityWithPython — 같은 호출열을 두 구현에 넣고 **판정 열이 같은지** 본다.
// reason 문자열까지는 요구하지 않는다(표현이니까). decision/would_block/걸린 축은 같아야 한다.
// Guard 도 돈다 — 행동 축 트립 뒤 쿨다운 중에 사용량 축이 넘는 국면(26번째)이 이 열에 들어 있다.
func TestDecisionParityWithPython(t *testing.T) {
	for _, lv := range []string{Enforce, Guard} {
		t.Run(lv, func(t *testing.T) { parityCase(t, lv) })
	}
}

func parityCase(t *testing.T, level string) {
	type step struct {
		Principal string            `json:"principal"`
		Run       string            `json:"run"`
		Conv      string            `json:"conv"`
		Args      map[string]string `json:"args"`
		Now       float64           `json:"now"`
	}
	// 세 국면을 다 지나는 열: 정상 → 반복 누적 → 차단 → 쿨다운
	var steps []step
	now := 1000.0
	for i := 1; i <= 25; i++ {
		steps = append(steps, step{"par", "r1", "c1",
			map[string]string{"path": "config.yaml"}, now})
		now += 0.5
	}
	steps = append(steps, step{"par", "r1", "c1", map[string]string{"path": "fresh"}, now + 1})
	steps = append(steps, step{"par", "r1", "c1", map[string]string{"path": "fresh"}, now + 3600})

	blob, _ := json.Marshal(steps)
	pyOut := runPython(t, fmt.Sprintf(`
import sys, json
sys.path.insert(0, %q)
from argus_decide import DecisionEngine, Policy
e = DecisionEngine(Policy(control_level=%q, window_s=60, cooldown_s=30,
    repeat_window=20, repeat_soft=2, repeat_hard=20,
    principal_calls_soft=11, principal_calls_hard=26,
    run_steps_soft=8, run_steps_hard=27,
    rotation_gap_soft=3.0, rotation_gap_hard=5.2,
    budget_usd_soft=0.0, budget_usd_hard=0.0, soft_escalation=False))
out = []
for s in json.loads(%q):
    d = e.decide({"principal": s["principal"], "run_id": s["run"],
                  "conversation_hash": s["conv"], "conversation_source": "client",
                  "tool_name": "read_file", "tool_args_value_hashes": s["args"]},
                 now=s["now"])
    out.append({"decision": d.decision, "would_block": d.would_block,
                "hard_axes": sorted(h["axis"] for h in d.policy_hit if h["level"] == "hard")})
print(json.dumps(out))
`, guardrailDir(t), level, string(blob)))

	var pyResults []struct {
		Decision   string   `json:"decision"`
		WouldBlock bool     `json:"would_block"`
		HardAxes   []string `json:"hard_axes"`
	}
	if err := json.Unmarshal([]byte(pyOut), &pyResults); err != nil {
		t.Fatalf("파이썬 출력 파싱 실패: %v\n%s", err, pyOut)
	}

	e := New(measured(level))
	for i, st := range steps {
		d := e.Decide(s(st.Principal, st.Run, st.Conv, "read_file", st.Args), st.Now)
		var hard []string
		for _, h := range d.PolicyHit {
			if h.Level == "hard" {
				hard = append(hard, h.Axis)
			}
		}
		py := pyResults[i]
		if d.Decision != py.Decision || d.WouldBlock != py.WouldBlock {
			t.Fatalf("스텝 %d 갈라짐: go=%s/%v python=%s/%v",
				i+1, d.Decision, d.WouldBlock, py.Decision, py.WouldBlock)
		}
		if !sameSet(hard, py.HardAxes) {
			t.Fatalf("스텝 %d hard 축 갈라짐: go=%v python=%v", i+1, hard, py.HardAxes)
		}
	}
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	m := map[string]bool{}
	for _, x := range a {
		m[x] = true
	}
	for _, x := range b {
		if !m[x] {
			return false
		}
	}
	return true
}

func guardrailDir(t *testing.T) string {
	t.Helper()
	_, self, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(self), "..", "..", "hack", "guardrail")
}

func runPython(t *testing.T, script string) string {
	t.Helper()
	py, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 없음 — 동등성 테스트 스킵 (로컬에서만 돈다)")
	}
	var out, errb bytes.Buffer
	cmd := exec.Command(py, "-c", script)
	cmd.Stdout, cmd.Stderr = &out, &errb
	if err := cmd.Run(); err != nil {
		t.Fatalf("파이썬 실행 실패: %v\n%s", err, errb.String())
	}
	return out.String()
}

// TestRandomParityWithPython — 손으로 짠 열이 못 닿는 조합(run 없음·대화 없음·비용 누적·여러 주체·
// 들쭉날쭉한 간격)을 고정 시드 무작위 열로 넣는다. 판정·트립·hard 축·관측치가 같아야 한다.
// 2026-09-26 이 테스트가 없을 때 budget_usd 반올림·run 없는 run_steps 가 따로 발견됐다.
func TestRandomParityWithPython(t *testing.T) {
	// 무작위 열이 모든 축을 실제로 발화시켜야 통과에 의미가 있다 (쿨다운만 돌면 아무것도 안 잰다).
	// 수준·시드 전체의 합집합으로 본다 — Enforce 는 막힌 호출을 창에 남기지 않아 먼저 걸린 축이 나머지를 가린다.
	seen := map[string]int{}
	for _, lv := range []string{Enforce, Guard, Observe} {
		// 인자 종류가 많은 열은 duplicate_calls 가 먼저 막지 않아 사용량 축까지 간다.
		for _, c := range []struct {
			seed     int64
			argKinds int
		}{{7, 3}, {42, 3}, {2026, 3}, {99, 60}} {
			t.Run(fmt.Sprintf("%s/%d", lv, c.seed), func(t *testing.T) { randomParity(t, lv, c.seed, c.argKinds, seen) })
		}
	}
	t.Logf("hard 발생: %v", seen)
	for _, ax := range []string{"duplicate_calls", "run_steps", "principal_calls", "rotation_gap",
		"budget_usd", "msg_bytes", "conv_growth", "team_calls"} {
		if seen[ax] == 0 && !t.Failed() {
			t.Errorf("축 %s 가 한 번도 hard 에 닿지 않았다 — 열을 조정한다", ax)
		}
	}
}

func randomParity(t *testing.T, level string, seed int64, argKinds int, seen map[string]int) {
	type step struct {
		Principal string            `json:"principal"`
		Run       string            `json:"run"`
		Conv      string            `json:"conv"`
		Args      map[string]string `json:"args"`
		Cost      float64           `json:"cost"`
		Auth      bool              `json:"auth"`
		Bytes     float64           `json:"bytes"`
		Team      string            `json:"team"`
		Now       float64           `json:"now"`
	}
	rnd := rand.New(rand.NewSource(seed))
	cost := map[string]float64{}
	var steps []step
	now := 1000.0
	for i := 0; i < 600; i++ {
		p := []string{"a", "b", "c"}[rnd.Intn(3)]
		run := []string{"", "r1", "r2", "r3"}[rnd.Intn(4)]
		conv := []string{"", "c1", "c2"}[rnd.Intn(3)]
		cost[p] += float64(rnd.Intn(4)) * 0.0013
		if rnd.Intn(60) == 0 {
			cost[p] = 0 // budget_duration 리셋 — 영속 출처(a·b)면 엔진이 따른다
		}
		team := map[string]string{"a": "t1", "b": "t1", "c": ""}[p] // c 는 팀 없음
		steps = append(steps, step{p, run, conv,
			map[string]string{"path": fmt.Sprint(rnd.Intn(argKinds))}, cost[p], p != "c",
			float64(200 + rnd.Intn(1200)*int(1+now/200)), team, now})
		now += float64(rnd.Intn(30)) / 10
	}
	blob, _ := json.Marshal(steps)
	pyOut := runPython(t, fmt.Sprintf(`
import sys, json
sys.path.insert(0, %q)
from argus_decide import DecisionEngine, Policy
e = DecisionEngine(Policy(control_level=%q, window_s=60, cooldown_s=2,
    repeat_window=20, repeat_soft=2, repeat_hard=6,
    principal_calls_soft=8, principal_calls_hard=14,
    run_steps_soft=4, run_steps_hard=7,
    rotation_gap_soft=1.5, rotation_gap_hard=2.2,
    budget_usd_soft=0.1, budget_usd_hard=0.3,
    msg_bytes_soft=4000, msg_bytes_hard=9000, conv_growth_soft=2.5, conv_growth_hard=4.0,
    team_calls_soft=12, team_calls_hard=22, soft_escalation=False))
out = []
for s in json.loads(%q):
    d = e.decide({"principal": s["principal"], "run_id": s["run"] or None,
                  "conversation_hash": s["conv"] or None,
                  "conversation_source": "client" if s["conv"] else "unavailable",
                  "tool_name": "read_file", "tool_args_value_hashes": s["args"], "cost_usd": s["cost"], "cost_authoritative": s["auth"],
                  "msg_bytes": s["bytes"], "team": s["team"] or None},
                 now=s["now"])
    out.append({"decision": d.decision, "would_block": d.would_block,
                "hard_axes": sorted(h["axis"] for h in d.policy_hit if h["level"] == "hard"),
                "obs": d.observations})
print(json.dumps(out))
`, guardrailDir(t), level, string(blob)))

	var py []struct {
		Decision   string             `json:"decision"`
		WouldBlock bool               `json:"would_block"`
		HardAxes   []string           `json:"hard_axes"`
		Obs        map[string]float64 `json:"obs"`
	}
	if err := json.Unmarshal([]byte(pyOut), &py); err != nil {
		t.Fatalf("파이썬 출력 파싱 실패: %v\n%s", err, pyOut)
	}
	p := measured(level)
	p.CooldownS = 2
	p.RepeatHard, p.RunStepsSoft, p.RunStepsHard = 6, 4, 7
	p.PrincipalCallsSoft, p.PrincipalCallsHard = 8, 14
	p.RotationGapSoft, p.RotationGapHard = 1.5, 2.2
	p.BudgetUSDSoft, p.BudgetUSDHard = 0.1, 0.3
	p.MsgBytesSoft, p.MsgBytesHard, p.ConvGrowthSoft, p.ConvGrowthHard = 4000, 9000, 2.5, 4.0
	p.TeamCallsSoft, p.TeamCallsHard = 12, 22
	e := New(p)
	for i, st := range steps {
		src := "client"
		if st.Conv == "" {
			src = "unavailable"
		}
		d := e.Decide(Signals{Principal: st.Principal, RunID: st.Run, ConversationHash: st.Conv,
			ConversationSource: src, ToolName: "read_file", ToolArgsValueHash: st.Args, CostUSD: st.Cost, CostAuthoritative: st.Auth,
			MsgBytes: st.Bytes, Team: st.Team}, st.Now)
		var hard []string
		for _, h := range d.PolicyHit {
			if h.Level == "hard" {
				hard = append(hard, h.Axis)
				seen[h.Axis]++
			}
		}
		g := py[i]
		if d.Decision != g.Decision || d.WouldBlock != g.WouldBlock || !sameSet(hard, g.HardAxes) {
			t.Fatalf("스텝 %d 갈라짐 %+v\n go=%s/%v %v\n py=%s/%v %v", i+1, st,
				d.Decision, d.WouldBlock, hard, g.Decision, g.WouldBlock, g.HardAxes)
		}
		for k, v := range g.Obs {
			if gv, ok := d.Observations[k]; !ok || math.Abs(gv-v) > 1e-9 {
				t.Fatalf("스텝 %d 관측치 %s 갈라짐: go=%v(%v) py=%v", i+1, k, gv, ok, v)
			}
		}
		if len(d.Observations) != len(g.Obs) {
			t.Fatalf("스텝 %d 관측 축 집합이 다르다: go=%v py=%v", i+1, d.Observations, g.Obs)
		}
	}
}
