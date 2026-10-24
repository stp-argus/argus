// SPDX-License-Identifier: Apache-2.0

package decide

// /v1/decide 호출자 인증 — 탭(LiteLLM 파드)의 ServiceAccount 토큰을 TokenReview 로 확인한다.
//
// 인증이 없으면 클러스터 안 누구나 남의 주체로 신호를 보낼 수 있다. 쿨다운을 걸거나
// 큰 cost_usd 를 보내 남의 예산 축을 넘기는 식이다. 공유 비밀 대신 SA 토큰을 쓰는 이유:
// 차트가 비밀을 만들면 GitOps 렌더마다 값이 바뀐다. SA 토큰은 kubelet 이 발급·회전한다.
// 탭은 전용 audience 로 투영한 토큰을 보낸다 — 이 토큰으로는 API 서버를 부를 수 없다.

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"
)

// TokenReviewFunc — 토큰이 유효하면 ServiceAccount 사용자명(system:serviceaccount:<ns>:<name>)을 돌려준다.
type TokenReviewFunc func(ctx context.Context, token string) (username string, err error)

// ErrUnauthenticated — 토큰이 무효다(만료·위조·audience 불일치).
var ErrUnauthenticated = errors.New("unauthenticated")

const authCacheTTL = time.Minute

// Authenticator — 허용된 SA 만 통과시킨다. 같은 토큰은 1분 캐시해 요청마다 API 서버를 부르지 않는다.
// 무효 토큰도 캐시한다 — 안 하면 위조 토큰 홍수가 그대로 TokenReview 홍수가 된다.
type Authenticator struct {
	Review  TokenReviewFunc
	Allowed map[string]bool // "system:serviceaccount:<ns>:<name>"

	mu    sync.Mutex
	cache map[[32]byte]authEntry
}

type authEntry struct {
	user    string // "" = 무효 토큰
	expires time.Time
}

// ParseAllowedSAs — "ns/name,ns2/name2" → 사용자명 집합. 형식이 틀린 항목이 하나라도 있으면 에러다 —
// 조용히 버리면 오타 하나로 목록이 비어 인증이 꺼진다.
func ParseAllowedSAs(s string) (map[string]bool, error) {
	out := map[string]bool{}
	if strings.TrimSpace(s) == "" {
		return out, nil
	}
	for _, part := range strings.Split(s, ",") {
		ns, name, ok := strings.Cut(strings.TrimSpace(part), "/")
		if !ok || ns == "" || name == "" || strings.Contains(name, "/") {
			return nil, fmt.Errorf("ARGUS_DECIDE_ALLOWED_SA 항목 %q 은 ns/name 형식이 아니다", part)
		}
		out["system:serviceaccount:"+ns+":"+name] = true
	}
	return out, nil
}

// Check — 통과면 0, 아니면 HTTP 상태와 실패 사유(메트릭 라벨).
func (a *Authenticator) Check(r *http.Request) (int, string) {
	token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !ok || token == "" {
		return http.StatusUnauthorized, "missing"
	}
	key := sha256.Sum256([]byte(token))
	now := time.Now()

	a.mu.Lock()
	ent, hit := a.cache[key]
	a.mu.Unlock()
	if !hit || now.After(ent.expires) {
		user, err := a.Review(r.Context(), token)
		switch {
		case errors.Is(err, ErrUnauthenticated):
			user = ""
		case err != nil:
			// API 서버 장애. 전에 본 토큰이면 만료된 캐시 판정을 그대로 쓴다 — 장애 동안 판정이 전부
			// 로컬 폴백(주체 창이 파드마다 따로)으로 빠지지 않게. 처음 보는 토큰은 503 → 탭이 폴백한다.
			if hit {
				break
			}
			return http.StatusServiceUnavailable, "review_error"
		}
		if err == nil || errors.Is(err, ErrUnauthenticated) {
			ent = authEntry{user: user, expires: now.Add(authCacheTTL)}
			a.mu.Lock()
			if a.cache == nil || len(a.cache) >= cooldownSeenMax {
				a.cache = map[[32]byte]authEntry{}
			}
			a.cache[key] = ent
			a.mu.Unlock()
		}
	}
	if ent.user == "" {
		return http.StatusUnauthorized, "invalid"
	}
	if !a.Allowed[ent.user] {
		return http.StatusForbidden, "forbidden"
	}
	return 0, ""
}
