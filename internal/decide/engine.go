// SPDX-License-Identifier: Apache-2.0

// Package decide — 판정 엔진 (spec 006b/006c).
//
// hack/guardrail/argus_decide.py 의 포팅이다. 판정이 **어디서 도는가**가 바뀌었다:
// 탭(LiteLLM 프로세스 안)이 아니라 **오퍼레이터**가 든다. 이유는 둘이다.
//
//  1. 카운터는 공유돼야 한다. 탭 안에 있으면 replicas: 1 에 묶인다.
//     Envoy 가 중앙 rate limit 서비스를 유지하는 것과 같은 이유다.
//  2. 이 제품의 논지가 "선언(CRD)이 판정을 구동한다"인데, 판정이 남의 프로세스에
//     얹힌 파이썬에 있으면 오퍼레이터는 ConfigMap 복사기일 뿐이다.
//
// Mixer 의 실패(중앙 per-request 판정 → P99 2배 → 제거)는 여기 적용되지 않는다 —
// 우리 뒤에 있는 것은 6ms 마이크로서비스가 아니라 300ms~3s LLM 호출이고,
// 같은 클러스터 gRPC/HTTP 왕복 1~2ms 는 측정 오차 수준이다. 그래도 탭은
// 판정 실패 시 로컬 폴백(fail-open)을 유지한다 — 중앙 장애가 데이터플레인을 세우면 안 된다.
//
// 파이썬 판본과 **판정이 동일해야 한다.** 두 구현이 다른 답을 내면 폴백이 곧 오탐이다.
// engine_test.go 가 파이썬 테스트(test_argus_decide.py 17건)와 같은 케이스를 돈다.
package decide

import (
	"container/list"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// 제어 수준. CRD enum 과 같다.
const (
	Observe = "Observe"
	Guard   = "Guard"
	Enforce = "Enforce"
)

// Policy — 임계값. 기본값은 전부 0(=축 비활성)이다.
// **측정으로 분리력을 증명 못 한 축은 켜지지 않는다** (006c 규칙).
// 값은 컨트롤러가 CR 에서 렌더해 넣는다 — env 파싱은 여기 없다. 그건 탭의 사정이었다.
type Policy struct {
	ControlLevel string
	WindowS      float64
	CooldownS    float64

	PrincipalCallsSoft, PrincipalCallsHard float64
	RepeatWindow                           int
	RepeatSoft, RepeatHard                 float64
	RunStepsSoft, RunStepsHard             float64
	BudgetUSDSoft, BudgetUSDHard           float64
	RotationGapSoft, RotationGapHard       float64
	MsgBytesSoft, MsgBytesHard             float64 // A6 · 단일 호출 크기 (G4)
	ConvGrowthSoft, ConvGrowthHard         float64 // A7 · 대화 성장률 = 이번 ÷ 첫 호출 (G3)
	TeamCallsSoft, TeamCallsHard           float64 // A8 · 팀 단위 창 누적 (G2 backstop)

	// soft 2개로도 차단할 것인가. 기본 꺼짐 — 대조군이 작아 soft 임계들이
	// 같이 과소평가돼 있을 수 있다 (thresholds.md).
	SoftEscalation bool

	// 메모리 상한. principal 이 무한히 늘면 오퍼레이터가 죽는다.
	MaxPrincipals int

	// "env" 또는 "crd:<ns/name,...>" — 이 판정이 어느 선언에서 왔는가.
	Source string

	// backstop.failureMode ("" = Open). 엔진은 판정에 쓰지 않는다 — 응답에 실어 가드레일이 기억하게 한다.
	// 정책 파일을 마운트하지 못하는 다른 네임스페이스의 LiteLLM 도 오퍼레이터가 사라졌을 때 이 값으로 fail-closed 한다.
	FailureMode string

	// CR 의 identity 블록 (nil = 가드레일 기본값). 엔진은 쓰지 않는다 — 응답에 실어 정책 파일이 없는
	// 가드레일이 다음 호출부터 같은 대화 키 규칙을 쓰게 한다.
	Identity *Identity
}

// Identity — 가드레일이 대화 키를 만드는 규칙. 컨트롤러 identityOut·파이썬 Policy 의 같은 이름 필드와 같다.
type Identity struct {
	ConversationHeaders   []string `json:"conversation_headers,omitempty"`
	TrustGatewaySession   bool     `json:"trust_gateway_session"`
	OnUnknownConversation string   `json:"on_unknown_conversation"`
}

// DefaultPolicy — 정책이 아직 없을 때. 아무것도 차단하지 않는 Observe.
func DefaultPolicy() Policy {
	return Policy{
		ControlLevel:  Observe,
		WindowS:       60,
		CooldownS:     30,
		RepeatWindow:  20,
		MaxPrincipals: 10_000,
		Source:        "default",
	}
}

// Hit — 축 하나가 임계를 넘은 기록.
type Hit struct {
	Axis      string  `json:"axis"`
	Level     string  `json:"level"` // "soft" | "hard"
	Observed  float64 `json:"observed"`
	Threshold float64 `json:"threshold"`
	Detail    string  `json:"detail"`
}

// Decision — 판정 결과. 파이썬 Decision 과 필드가 같다 (레코드 스키마 호환).
type Decision struct {
	Decision     string             `json:"decision"` // "allow" | "block"
	WouldBlock   bool               `json:"would_block"`
	PolicyHit    []Hit              `json:"policy_hit"`
	Reason       string             `json:"reason"`
	DecisionID   string             `json:"decision_id,omitempty"`
	ControlLevel string             `json:"control_level"`
	PolicySource string             `json:"policy_source"`
	Observations map[string]float64 `json:"observations"`
	FailureMode  string             `json:"failure_mode,omitempty"` // Policy.FailureMode — 가드레일이 기억한다
	Identity     *Identity          `json:"identity,omitempty"`     // Policy.Identity — 가드레일이 기억한다
	// CostDeltaUSD — 이번 호출로 늘어난 주체 누적 지출. 메트릭(팀별 지출)용이라 응답엔 싣지 않는다.
	CostDeltaUSD float64 `json:"-"`
}

// Signals — 탭이 뽑아 보내는 신호. 파이썬 가드레일의 record 부분집합.
type Signals struct {
	Principal          string            `json:"principal"`
	RunID              string            `json:"run_id"`
	ConversationHash   string            `json:"conversation_hash"`
	ConversationSource string            `json:"conversation_source"`
	ToolName           string            `json:"tool_name"`
	ToolArgsValueHash  map[string]string `json:"tool_args_value_hashes"`
	CostUSD            float64           `json:"cost_usd"`
	// CostAuthoritative — CostUSD 가 LiteLLM 이 영속한 지출이다. 이때만 줄어든 값을 리셋으로 믿는다.
	// 탭 메모리 정산(파드마다 따로, 재기동에 0)이나 모르는 값은 max 로 보관한다.
	CostAuthoritative bool             `json:"cost_authoritative"`
	MsgBytes          float64          `json:"msg_bytes"`
	ToolArgFacets     map[string]Facet `json:"tool_arg_facets"` // 006e: 키마다 해시·모양·변화부호
	Team              string           `json:"team"`            // 팀 해시 — G2 backstop
}

// Facet — argus_signals._tool_arg_facets 의 한 키. h=값 해시, s=모양, d=변화 부호("+1","+","-","0").
type Facet struct {
	H string `json:"h"`
	S string `json:"s"`
	D string `json:"d"`
}

// ── 내부 상태 ───────────────────────────────────────────────────────────────

type principalState struct {
	calls    []float64            // 호출 시각 (창 안)
	runCalls map[string][]float64 // run별 호출 시각
	costUSD  float64
	costPeak float64 // 증분 메트릭의 기준선 — 리셋 전까지의 최고치. 뒤처진 값이 다시 올라와도 두 번 세지 않는다
	costSeen bool    // 비용을 한 번이라도 받았다 — 재기동·축출 뒤 첫 관측을 증분으로 세지 않기 위해
	// 집행 상태(blockedUntil·tripReason)는 Engine.enforce 에 있다 — LRU 축출 대상이 아니다 (STP-24).
}

// enforceState — 쿨다운. 카운터와 수명이 다르다(창이 아니라 쿨다운). 그래서 principalState 와 분리.
type enforceState struct {
	blockedUntil float64
	tripReason   string
	backstop     bool // 트립 원인에 volume·cost 범주 hard 가 있었다 — Guard 가 쿨다운 동안 막을지
}

func (s *principalState) prune(now, windowS float64) {
	cut := now - windowS
	s.calls = pruneTimes(s.calls, cut)
	for run, ts := range s.runCalls {
		if kept := pruneTimes(ts, cut); len(kept) == 0 {
			delete(s.runCalls, run)
		} else {
			s.runCalls[run] = kept
		}
	}
}

func pruneTimes(ts []float64, cut float64) []float64 {
	i := 0
	for i < len(ts) && ts[i] < cut {
		i++
	}
	return ts[i:]
}

type conversationState struct {
	fingerprints []string
	steps        int
	firstBytes   float64 // 대화 첫 호출의 msg_bytes — 성장률의 분모
	// spec 006e "파라미터 제외" 정규화 (STP-20). 파이썬 _normalized_fingerprint 와 동일 규칙.
	normFingerprints []string
	profile          map[string]*keyProfile // "tool\x00key"
}

type keyProfile struct {
	n      int
	hashes map[string]struct{}
	shapes map[string]struct{}
	mono   int
}

const profileMinObs = 4

// normalizedFingerprint — 파라미터(값은 변하는데 모양 고정, 또는 단조 증가)로 분류된 키를 빼고
// 남은 정체성 키로 지문. 남는 키가 없으면 "" — 정상 작업을 반복으로 뭉개지 않기 위해. soft 전용.
func normalizedFingerprint(cst *conversationState, tool string, facets map[string]Facet) string {
	if cst.profile == nil {
		cst.profile = map[string]*keyProfile{}
	}
	keep := map[string]string{}
	for k, v := range facets {
		id := tool + "\x00" + k
		a := cst.profile[id]
		if a == nil {
			a = &keyProfile{hashes: map[string]struct{}{}, shapes: map[string]struct{}{}}
			cst.profile[id] = a
		}
		a.n++
		a.hashes[v.H] = struct{}{}
		a.shapes[v.S] = struct{}{}
		if v.D == "+1" || v.D == "+" {
			a.mono++
		}
		isParam := a.n >= profileMinObs &&
			(a.mono >= a.n-1 || (float64(len(a.hashes)) >= float64(a.n)*0.9 && len(a.shapes) == 1))
		if !isParam {
			keep[k] = v.H
		}
	}
	if len(keep) == 0 {
		return ""
	}
	keys := make([]string, 0, len(keep))
	for k := range keep {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+keep[k])
	}
	return tool + "|" + strings.Join(parts, "|")
}

// Engine — 인메모리 판정기. 동시 요청을 받으므로 잠금이 있다.
// (파이썬 판본은 GIL + 단일 워커 전제라 잠금이 없었다.)
type Engine struct {
	mu     sync.Mutex
	policy Policy

	principals    map[string]*principalState
	principalLRU  *list.List // Front = 가장 오래됨
	principalElem map[string]*list.Element

	conversations    map[string]*conversationState
	conversationLRU  *list.List
	conversationElem map[string]*list.Element

	enforce map[string]enforceState // principal -> 쿨다운. 축출되지 않는다

	teams    map[string][]float64 // team -> 창 안 호출 시각 (G2 backstop)
	teamLRU  *list.List
	teamElem map[string]*list.Element

	Evicted int64

	loaded atomic.Bool // 컨트롤러가 정책을 한 번이라도 렌더했다 — readyz 가 본다
}

func New(p Policy) *Engine {
	if p.MaxPrincipals <= 0 {
		p.MaxPrincipals = 10_000
	}
	return &Engine{
		policy:           p,
		principals:       map[string]*principalState{},
		principalLRU:     list.New(),
		principalElem:    map[string]*list.Element{},
		conversations:    map[string]*conversationState{},
		conversationLRU:  list.New(),
		conversationElem: map[string]*list.Element{},
		enforce:          map[string]enforceState{},
		teams:            map[string][]float64{},
		teamLRU:          list.New(),
		teamElem:         map[string]*list.Element{},
	}
}

func (e *Engine) team(key string) []float64 {
	if ts, ok := e.teams[key]; ok {
		e.teamLRU.MoveToBack(e.teamElem[key])
		return ts
	}
	e.teams[key] = nil
	e.teamElem[key] = e.teamLRU.PushBack(key)
	for len(e.teams) > e.policy.MaxPrincipals {
		front := e.teamLRU.Front()
		old := front.Value.(string)
		e.teamLRU.Remove(front)
		delete(e.teams, old)
		delete(e.teamElem, old)
		e.Evicted++
	}
	return nil
}

// SetPolicy — 컨트롤러가 CR 을 다시 렌더하면 부른다. 카운터는 유지된다 —
// 정책이 바뀌었다고 창을 비우면 폭주 중에 정책을 고치는 순간 폭주가 리셋된다.
func (e *Engine) SetPolicy(p Policy) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if p.MaxPrincipals <= 0 {
		p.MaxPrincipals = 10_000
	}
	e.policy = p
	e.loaded.Store(true)
}

// Loaded — 컨트롤러가 정책을 한 번이라도 넣었는가. 그 전에는 DefaultPolicy(Observe·전 축 꺼짐)라
// 이 파드가 트래픽을 받으면 롤아웃 중 새 파드가 아무것도 막지 않는다.
func (e *Engine) Loaded() bool { return e.loaded.Load() }

func (e *Engine) Policy() Policy {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.policy
}

// Decide — 판정. now 는 테스트 주입용; 0 이면 현재 시각.
func (e *Engine) Decide(sig Signals, now float64) Decision {
	if now == 0 {
		now = float64(time.Now().UnixNano()) / 1e9
	}
	e.mu.Lock()
	defer e.mu.Unlock()

	p := e.policy
	principal := sig.Principal
	if principal == "" {
		principal = "(unknown)"
	}
	runID := sig.RunID
	if runID == "" {
		runID = "(no-run)"
	}
	// 대화 키를 못 만든 요청은 대화 스코프 축을 평가하지 않는다 (spec 006f).
	// 가짜 버킷에 넣어 세면 무관한 작업이 서로의 반복으로 세어진다 = 오탐.
	convKnown := sig.ConversationHash != "" && sig.ConversationSource != "unavailable"

	pst := e.principal(principal)
	pst.prune(now, p.WindowS)

	// 쿨다운 중인가 — 판정은 유지하되(히스테리시스) **누적은 계속한다** (argus_decide.py 와 동일).
	// 조기 반환하면 임계가 낮은 축 하나가 먼저 걸린 순간 나머지 축은 누적조차 못 한다.
	en, cooling := e.enforce[principal]
	if cooling && en.blockedUntil <= now {
		delete(e.enforce, principal)
		cooling = false
	}

	// 차단된 호출은 나가지 않았으므로 창에 남기지 않는다 — 끝에서 되돌릴 수 있게 지금 상태를 잡아 둔다.
	// 슬라이스 헤더만 잡으면 된다: append 는 기존 원소를 덮어쓰지 않는다.
	if pst.runCalls == nil {
		pst.runCalls = map[string][]float64{}
	}
	prevCalls := pst.calls
	prevRun, hadRun := pst.runCalls[runID]
	pst.calls = append(pst.calls, now)
	pst.runCalls[runID] = append(pst.runCalls[runID], now)
	// CostUSD 는 절대 누적값 — += 가 아니라 보관 (argus_decide.py 와 동일 의미론).
	// LiteLLM 이 영속한 값이면 최신값을 따른다: 줄어든 값은 budget_duration 리셋이다(예전 max 는 리셋 뒤에도
	// 옛 최고치로 계속 막았다). 탭 메모리 정산은 파드마다 따로이고 재기동에 0 이 되므로 max 로 둔다.
	if sig.CostAuthoritative {
		pst.costUSD = sig.CostUSD
	} else {
		pst.costUSD = max(pst.costUSD, sig.CostUSD)
	}
	// 증분은 **이전 값을 알 때만** 센다 — 재기동·LRU 축출 뒤 첫 관측에 0 에서 재면
	// 그 주체의 과거 누적 전체가 한 번에 "지출 급증"으로 찍힌다. 기준선은 최고치라 동시 요청이
	// 뒤처진 값을 보내도 두 번 세지 않는다. 영속 값이 절반 아래로 떨어지면 리셋으로 보고 기준선을 내린다.
	var costDelta float64
	switch {
	case !pst.costSeen:
		pst.costPeak = sig.CostUSD
	case sig.CostUSD > pst.costPeak:
		costDelta = sig.CostUSD - pst.costPeak
		pst.costPeak = sig.CostUSD
	case sig.CostAuthoritative && sig.CostUSD < pst.costPeak/2:
		pst.costPeak = sig.CostUSD
	}
	pst.costSeen = true

	var hits []Hit
	obs := map[string]float64{}

	// A1 · principal 누적 (핑퐁 backstop)
	nCalls := float64(len(pst.calls))
	add(&hits, obs, "principal_calls", nCalls, p.PrincipalCallsSoft, p.PrincipalCallsHard,
		fmt.Sprintf("%d콜/%.0f초", len(pst.calls), p.WindowS))

	// 대화 상태 — 대화를 아는 요청만 (파이썬과 같은 조건). 지문이 없어도 성장률(A7)엔 필요하다.
	var cst *conversationState
	var prevConv conversationState
	if convKnown {
		cst = e.conversation(sig.ConversationHash)
		prevConv = *cst
		cst.steps++
	}

	// A2 · 동일 (tool, args) 반복 — 대화 안에서, 최근 w 호출 중
	if cst != nil {
		if fp := fingerprint(sig); fp != "" {
			cst.fingerprints = append(cst.fingerprints, fp)
			w := p.RepeatWindow
			if w <= 0 {
				w = 20
			}
			if len(cst.fingerprints) > w {
				cst.fingerprints = cst.fingerprints[len(cst.fingerprints)-w:]
			}
			repeats := 0
			for _, x := range cst.fingerprints {
				if x == fp {
					repeats++
				}
			}
			obs["duplicate_calls"] = float64(repeats)
			// hard 는 완전일치, soft 는 정규화(파라미터 제외) 지문도 본다 (STP-20) — 근사라 단독 차단 금지.
			nrep := 0
			if len(sig.ToolArgFacets) > 0 {
				if nfp := normalizedFingerprint(cst, sig.ToolName, sig.ToolArgFacets); nfp != "" {
					cst.normFingerprints = append(cst.normFingerprints, nfp)
					if len(cst.normFingerprints) > w {
						cst.normFingerprints = cst.normFingerprints[len(cst.normFingerprints)-w:]
					}
					for _, x := range cst.normFingerprints {
						if x == nfp {
							nrep++
						}
					}
					obs["duplicate_calls_norm"] = float64(nrep)
				}
			}
			detail := fmt.Sprintf("최근 %d콜 중 동일 (tool,args) %d회", len(cst.fingerprints), repeats)
			switch {
			case p.RepeatHard > 0 && float64(repeats) >= p.RepeatHard:
				hits = append(hits, Hit{Axis: "duplicate_calls", Level: "hard", Observed: float64(repeats), Threshold: p.RepeatHard, Detail: detail})
			case p.RepeatSoft > 0 && float64(max(repeats, nrep)) >= p.RepeatSoft:
				if nrep > repeats {
					detail = fmt.Sprintf("파라미터 제외 지문 %d회 (완전일치 %d회)", nrep, repeats)
				}
				hits = append(hits, Hit{Axis: "duplicate_calls", Level: "soft", Observed: float64(max(repeats, nrep)), Threshold: p.RepeatSoft, Detail: detail})
			}
		}
	}

	// A3 · run 스텝 수. run ID 가 없으면 평가하지 않는다 — "(no-run)" 버킷은 주체의 창 안 호출 수와
	// 같아서, 사용량 문제를 "run 스텝 폭증"(행동 축)으로 잘못 이름 붙여 principal_calls 보다 먼저 트립한다.
	if sig.RunID != "" {
		runN := float64(len(pst.runCalls[runID]))
		add(&hits, obs, "run_steps", runN, p.RunStepsSoft, p.RunStepsHard,
			fmt.Sprintf("run %s %d콜", runID, len(pst.runCalls[runID])))
	} // 관측치도 남기지 않는다 — 대화를 모를 때의 duplicate_calls 와 같다(도출 분포에 0 이 섞이지 않게)

	// A4 · 누적 비용
	add(&hits, obs, "budget_usd", round8(pst.costUSD), p.BudgetUSDSoft, p.BudgetUSDHard,
		fmt.Sprintf("$%.6f", pst.costUSD))

	// A5 · 회전 격차 — run 을 돌려 per-run 상한을 피하는 서명
	perRunMax := 1
	for _, ts := range pst.runCalls {
		if len(ts) > perRunMax {
			perRunMax = len(ts)
		}
	}
	if len(pst.runCalls) > 1 {
		gap := nCalls / float64(perRunMax)
		add(&hits, obs, "rotation_gap", round2(gap), p.RotationGapSoft, p.RotationGapHard,
			fmt.Sprintf("누적 %d ÷ run당최대 %d = ×%.1f", len(pst.calls), perRunMax, gap))
	}

	// A8 · 팀 단위 창 누적 (G2 backstop) — 주체를 여러 개 쥔 한 조직은 여기서 다시 묶인다
	var prevTeam []float64
	if sig.Team != "" {
		prevTeam = pruneTimes(e.team(sig.Team), now-p.WindowS)
		ts := append(prevTeam, now)
		e.teams[sig.Team] = ts
		add(&hits, obs, "team_calls", float64(len(ts)), p.TeamCallsSoft, p.TeamCallsHard,
			fmt.Sprintf("팀 %d콜/%.0f초", len(ts), p.WindowS))
	}

	// A6 · 단일 호출 크기 (G4)
	msgBytes := sig.MsgBytes
	add(&hits, obs, "msg_bytes", msgBytes, p.MsgBytesSoft, p.MsgBytesHard, fmt.Sprintf("%.0fB", msgBytes))

	// A7 · 대화 성장률 (G3) — 첫 호출 대비. 첫 호출·크기 모름·대화 모름이면 평가하지 않는다
	if cst != nil && msgBytes > 0 {
		if cst.firstBytes <= 0 {
			cst.firstBytes = msgBytes
		} else if cst.steps > 1 {
			growth := msgBytes / cst.firstBytes
			add(&hits, obs, "conv_growth", round2(growth), p.ConvGrowthSoft, p.ConvGrowthHard,
				fmt.Sprintf("%.0fB ÷ 첫 %.0fB = ×%.1f", msgBytes, cst.firstBytes, growth))
		}
	}

	var d Decision
	if cooling {
		// 쿨다운 중에도 축은 계속 잰다. 행동 축으로 트립한 뒤 사용량·비용 상한까지 넘으면 Guard 도 막아야 한다.
		if !en.backstop && backstopHit(hits) {
			en.backstop = true
			e.enforce[principal] = en
		}
		// Guard 는 쿨다운 중에 넘은 사용량·비용 hard 도 싣는다 — 안 실으면 막은 이유가 레코드에 안 남는다.
		// (Observe·Enforce 는 예전처럼 cooldown 하나 — 기존 소비자가 그 모양에 기댄다)
		ch := []Hit{{
			Axis: "cooldown", Level: "hard",
			Observed: en.blockedUntil - now, Threshold: 0, Detail: en.tripReason,
		}}
		for _, h := range hits {
			if e.policy.ControlLevel == Guard && backstopHit([]Hit{h}) {
				ch = append(ch, h)
			}
		}
		d = e.render(ch, principal, now, true, obs)
	} else {
		d = e.render(hits, principal, now, false, obs)
	}
	// 쿨다운 중에도 Observe 면 호출이 나가 돈이 든다 — 두 경로 모두 증분을 싣는다.
	d.CostDeltaUSD = costDelta
	if d.Decision == "block" {
		// 막힌 호출은 창에 남기지 않는다 — 나가지 않은 호출이다. 남기면 재시도가 창(60초)을 계속 채워
		// 재시도를 멈출 때까지 풀리지 않는다. 안 남기면 창 안의 통과 호출이 빠지는 대로 풀린다.
		// 정규화 지문 프로파일은 되돌리지 않는다(soft 전용).
		pst.calls = prevCalls
		if hadRun {
			pst.runCalls[runID] = prevRun
		} else {
			delete(pst.runCalls, runID)
		}
		if cst != nil {
			cst.fingerprints, cst.normFingerprints = prevConv.fingerprints, prevConv.normFingerprints
			cst.steps, cst.firstBytes = prevConv.steps, prevConv.firstBytes
		}
		if sig.Team != "" {
			e.teams[sig.Team] = prevTeam
		}
	}
	return d
}

// render — 결정 규칙. 기본은 hard 1개. soft 하나만으로는 절대 차단하지 않는다.
func (e *Engine) render(hits []Hit, principal string, now float64,
	cooling bool, obs map[string]float64) Decision {

	p := e.policy
	var hard, soft []Hit
	for _, h := range hits {
		if h.Level == "hard" {
			hard = append(hard, h)
		} else {
			soft = append(soft, h)
		}
	}

	tripped := len(hard) > 0 || (p.SoftEscalation && len(soft) >= 2)
	reason := ""
	if tripped {
		drivers := hard
		if len(drivers) == 0 {
			drivers = soft
		}
		parts := make([]string, 0, len(drivers))
		for _, h := range drivers {
			parts = append(parts, fmt.Sprintf("%s=%s>=%s", h.Axis, trimFloat(h.Observed), trimFloat(h.Threshold)))
		}
		reason = strings.Join(parts, " · ")
		if !cooling {
			e.enforce[principal] = enforceState{blockedUntil: now + p.CooldownS, tripReason: reason, backstop: backstopHit(hard)}
			// 상한: 넘칠 때만 만료분을 훑는다. ponytail: 활성 쿨다운이 MaxPrincipals 를 넘으면 그대로 둔다.
			if len(e.enforce) > p.MaxPrincipals {
				for k, v := range e.enforce {
					if v.blockedUntil <= now {
						delete(e.enforce, k)
					}
				}
			}
		}
	}

	did := ""
	if tripped {
		seed := fmt.Sprintf("%s|%.3f|%s", principal, now, reason)
		sum := sha256.Sum256([]byte(seed))
		did = "d-" + hex.EncodeToString(sum[:])[:16]
	}

	// Guard — 설계의 중간 단계: 사용량·비용(volume·cost) 범주의 hard 만 막고, 행동 축은 기록만 한다.
	// 범주로 가르므로 backstop 상한뿐 아니라 detect.principalCalls hard 도 여기서 막힌다.
	guardBlocks := backstopHit(hard)
	if cooling {
		guardBlocks = e.enforce[principal].backstop
	}
	decision := "allow"
	if tripped && (p.ControlLevel == Enforce || (p.ControlLevel == Guard && guardBlocks)) {
		decision = "block"
	}
	if hits == nil {
		hits = []Hit{}
	}
	return Decision{
		Decision:     decision,
		WouldBlock:   tripped,
		PolicyHit:    hits,
		Reason:       reason,
		DecisionID:   did,
		ControlLevel: p.ControlLevel,
		PolicySource: p.Source,
		Observations: obs,
		FailureMode:  p.FailureMode,
		Identity:     p.Identity,
	}
}

// backstopHit — volume·cost 범주의 hard 가 있는가 (Guard 의 차단 대상).
func backstopHit(hits []Hit) bool {
	for _, h := range hits {
		if c := Category(h.Axis); h.Level == "hard" && (c == "volume" || c == "cost") {
			return true
		}
	}
	return false
}

// add — 임계 0 = 그 축 비활성. 관측치는 임계와 무관하게 항상 기록한다 —
// 축을 끈 상태로도 분포가 쌓여야 나중에 근거를 만들 수 있다 (FR-005).
func add(hits *[]Hit, obs map[string]float64, axis string,
	observed, soft, hard float64, detail string) {
	obs[axis] = observed
	switch {
	case hard != 0 && observed >= hard:
		*hits = append(*hits, Hit{axis, "hard", observed, hard, detail})
	case soft != 0 && observed >= soft:
		*hits = append(*hits, Hit{axis, "soft", observed, soft, detail})
	}
}

// fingerprint — 행동 해싱. (도구, 인자값 해시들)이 한 지문.
// 대화 전체를 해싱하면 안 된다 — 폭주 대화는 스텝마다 자라 매번 다른 해시가 나온다.
// **파이썬 _fingerprint 와 바이트 단위로 같아야 한다** — 두 구현이 같은 입력에
// 다른 지문을 내면 원격↔폴백 전환 순간 반복 카운트가 끊긴다.
func fingerprint(sig Signals) string {
	if sig.ToolName == "" {
		return ""
	}
	keys := make([]string, 0, len(sig.ToolArgsValueHash))
	for k := range sig.ToolArgsValueHash {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+sig.ToolArgsValueHash[k])
	}
	seed := sig.ToolName + "|" + strings.Join(parts, "|")
	sum := sha256.Sum256([]byte(seed))
	return hex.EncodeToString(sum[:])[:16]
}

// ── LRU ─────────────────────────────────────────────────────────────────────

func (e *Engine) principal(key string) *principalState {
	if st, ok := e.principals[key]; ok {
		e.principalLRU.MoveToBack(e.principalElem[key])
		return st
	}
	st := &principalState{runCalls: map[string][]float64{}}
	e.principals[key] = st
	e.principalElem[key] = e.principalLRU.PushBack(key)
	for len(e.principals) > e.policy.MaxPrincipals {
		front := e.principalLRU.Front()
		old := front.Value.(string)
		e.principalLRU.Remove(front)
		delete(e.principals, old)
		delete(e.principalElem, old)
		e.Evicted++
	}
	return st
}

func (e *Engine) conversation(key string) *conversationState {
	if st, ok := e.conversations[key]; ok {
		e.conversationLRU.MoveToBack(e.conversationElem[key])
		return st
	}
	st := &conversationState{}
	e.conversations[key] = st
	e.conversationElem[key] = e.conversationLRU.PushBack(key)
	for len(e.conversations) > e.policy.MaxPrincipals {
		front := e.conversationLRU.Front()
		old := front.Value.(string)
		e.conversationLRU.Remove(front)
		delete(e.conversations, old)
		delete(e.conversationElem, old)
		e.Evicted++
	}
	return st
}

// roundPy — 파이썬 round(x, n) 과 같은 값. 두 엔진 모두 반올림한 값을 임계와 비교하므로, 안 맞추면 경계에서
// 판정이 갈라진다. 파이썬은 이진값 그대로 정확한 10진 반올림(동률은 짝수)이고 strconv 'f' 도 같다.
// 예전 int(v*100+0.5) 는 1.625 → 1.63 (파이썬 1.62) 이었다 — 무작위 동등성 테스트가 찾았다.
func roundPy(v float64, n int) float64 {
	r, _ := strconv.ParseFloat(strconv.FormatFloat(v, 'f', n, 64), 64)
	return r
}

func round8(v float64) float64 { return roundPy(v, 8) }

func round2(v float64) float64 { return roundPy(v, 2) }

// trimFloat — 파이썬 판본의 f"{v:.6g}" 과 **바이트 단위로 같은** 모양 (5 → "5", 96.98 → "96.98",
// 0.0023826 → "0.0023826"). reason 문자열이 레코드·로그·차단 메시지에 남으므로 두 구현이 같은
// 문장을 만들어야 한다. 예전 %.2f 는 센트 미만 예산을 "budget_usd=0>0" 으로 뭉갰다.
func trimFloat(v float64) string {
	return strconv.FormatFloat(v, 'g', 6, 64)
}
