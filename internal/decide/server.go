// SPDX-License-Identifier: Apache-2.0

package decide

// /v1/decide — 판정 API (spec 006b O1).
//
// 탭(가드레일)이 요청마다 신호를 보내고 판정을 받아 **집행만** 한다.
// 차단 여부를 정하는 곳이 여기다 — "선언(CRD)이 판정을 구동한다"의 '판정'이 이 프로세스다.
//
// 계약은 파이썬 엔진의 decide(signals) → Decision 과 같은 모양이다. 그래서 탭은
// 원격이 죽으면 로컬 파이썬 엔진으로 폴백해도 **같은 판정**을 받는다(동등성 테스트로 고정).
//
// 지연 예산: 같은 클러스터 HTTP 왕복 ~1-2ms. 뒤에 있는 것이 300ms~3s LLM 호출이라
// 측정 오차 수준이다 (Mixer 의 6ms 마이크로서비스 상황과 다르다 — 설계 문서 §2).
// 그래도 탭 쪽 타임아웃(기본 500ms)과 fail-open 은 유지한다.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"time"
)

// Server — Engine 을 HTTP 로 노출한다. 컨트롤러와 같은 프로세스에서 돌며
// 같은 Engine 인스턴스를 공유한다 (컨트롤러가 SetPolicy 로 정책을 갱신한다).
type Server struct {
	Engine *Engine
	// 탐지 로그. 판정이 여기로 옮겨왔으니 "지금 뭐가 걸렸나"도 여기 로그에 남아야 한다.
	// kubectl logs deploy/argus 가 탐지 화면이 된다.
	Log func(format string, args ...any)
	// Auth — nil 이면 인증 없음(로컬 실행·시험). 오퍼레이터 main 이 ARGUS_DECIDE_ALLOWED_SA 로 켠다.
	Auth *Authenticator

	decisions atomic.Int64
	blocks    atomic.Int64
	// 쿨다운 유지 줄 억제 — 파이썬 _log_decision 과 같은 규칙 (첫 줄 + N번째마다).
	// net/http 은 요청마다 고루틴이라 락이 필요하다 — 없으면 동시 map 쓰기로 프로세스가 죽는다.
	mu           sync.Mutex
	cooldownSeen map[string]int
}

// cooldownSeenMax — 다시 트립하지 않는 주체의 항목은 지워지지 않는다. 넘치면 통째로 비운다
// (억제 카운터일 뿐이라 비워도 로그 한 줄이 더 찍히는 게 전부다).
const cooldownSeenMax = 10000

func NewServer(e *Engine, log func(string, ...any)) *Server {
	if log == nil {
		log = func(string, ...any) {}
	}
	return &Server{Engine: e, Log: log, cooldownSeen: map[string]int{}}
}

// Handler — mux 를 돌려준다. 오퍼레이터 main 이 :8080 에 단다.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/decide", s.handleDecide)
	mux.HandleFunc("GET /v1/policy", s.handlePolicy)
	mux.Handle("GET /metrics", metricsHandler())
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	return mux
}

func (s *Server) handleDecide(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	defer func() { mDecideDuration.Observe(time.Since(start).Seconds()) }()
	if s.Auth != nil {
		if code, reason := s.Auth.Check(r); code != 0 {
			mAuthFailures.WithLabelValues(reason).Inc()
			http.Error(w, fmt.Sprintf(`{"error":"auth: %s"}`, reason), code)
			return
		}
	}
	var sig Signals
	// 한도: 신호는 작다. 큰 본문은 실수(대화 전문을 보냈다든가)다 — 받지 않는다.
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&sig); err != nil {
		// 판정 불능은 400 으로 정직하게. 탭이 이걸 받으면 로컬 폴백으로 간다(fail-open).
		http.Error(w, fmt.Sprintf(`{"error":"bad signals: %s"}`, err), http.StatusBadRequest)
		return
	}

	d := s.Engine.Decide(sig, 0)
	s.decisions.Add(1)
	if d.Decision == "block" {
		s.blocks.Add(1)
	}
	s.logDecision(sig, d)
	observe(sig, d)

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(d)
}

// handlePolicy — 지금 어떤 정책으로 판정 중인가. verify-cluster.sh 가 본다.
// "선언이 판정까지 닿았는가"를 레코드 없이도 즉시 물을 수 있는 창구다.
func (s *Server) handlePolicy(w http.ResponseWriter, _ *http.Request) {
	p := s.Engine.Policy()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"control_level": p.ControlLevel,
		"source":        p.Source,
		"window_s":      p.WindowS,
		"cooldown_s":    p.CooldownS,
		"decisions":     s.decisions.Load(),
		"blocks":        s.blocks.Load(),
	})
}

// logDecision — 탐지·차단을 오퍼레이터 로그에 한 줄로.
// 규칙은 파이썬 _log_decision 과 같다: hard 는 찍고, soft 는 안 찍고(도배 방지),
// 쿨다운 연장은 첫 줄 + 10번째마다. 억제 근거는 dev 실측(soft 36/90, 쿨다운 18/20).
func (s *Server) logDecision(sig Signals, d Decision) {
	var hard []Hit
	for _, h := range d.PolicyHit {
		if h.Level == "hard" {
			hard = append(hard, h)
		}
	}
	if len(hard) == 0 {
		return
	}

	onlyCooldown := true
	for _, h := range hard {
		if h.Axis != "cooldown" {
			onlyCooldown = false
		}
	}
	s.mu.Lock()
	if onlyCooldown {
		if len(s.cooldownSeen) >= cooldownSeenMax {
			clear(s.cooldownSeen)
		}
		s.cooldownSeen[sig.Principal]++
		n := s.cooldownSeen[sig.Principal]
		s.mu.Unlock()
		if n != 1 && n%10 != 0 {
			return
		}
	} else {
		delete(s.cooldownSeen, sig.Principal) // 새 발화 — 다음 쿨다운의 첫 줄을 놓치지 않게
		s.mu.Unlock()
	}

	head := "관측"
	switch {
	case d.Decision == "block":
		head = "차단"
	case d.WouldBlock:
		head = fmt.Sprintf("탐지 (would_block · %s — 통과시킴)", d.ControlLevel)
	}
	axes := ""
	for i, h := range hard {
		if i > 0 {
			axes += " "
		}
		axes += fmt.Sprintf("%s/%s=%s>=%s", h.Axis, h.Level, trimFloat(h.Observed), trimFloat(h.Threshold))
	}
	s.Log("[argus] %s principal=%s run=%s | %s | id=%s policy=%s",
		head, sig.Principal, sig.RunID, axes, d.DecisionID, d.PolicySource)
}

// StartServer — 오퍼레이터 main 에서 부른다. controller-runtime 매니저와 별개 리스너.
func StartServer(addr string, s *Server) *http.Server {
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
	}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			s.Log("[argus] /v1/decide 서버 종료: %v", err)
		}
	}()
	return srv
}
