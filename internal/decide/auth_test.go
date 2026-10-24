// SPDX-License-Identifier: Apache-2.0

package decide

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestDecideAuth(t *testing.T) {
	reviews, apiDown := 0, false
	allowed, err := ParseAllowedSAs("argus-system/litellm")
	if err != nil {
		t.Fatal(err)
	}
	auth := &Authenticator{
		Allowed: allowed,
		Review: func(_ context.Context, token string) (string, error) {
			reviews++
			if apiDown {
				return "", errors.New("connection refused")
			}
			switch token {
			case "tap":
				return "system:serviceaccount:argus-system:litellm", nil
			case "other-pod":
				return "system:serviceaccount:default:default", nil
			case "apiserver-down":
				return "", errors.New("connection refused")
			}
			return "", ErrUnauthenticated
		},
	}
	srv := NewServer(New(DefaultPolicy()), nil)
	srv.Auth = auth
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	call := func(header string) int {
		req, _ := http.NewRequest(http.MethodPost, ts.URL+"/v1/decide", strings.NewReader(`{"principal":"victim"}`))
		if header != "" {
			req.Header.Set("Authorization", header)
		}
		res, err := ts.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		return res.StatusCode
	}

	for _, c := range []struct {
		name, header string
		want         int
	}{
		{"토큰 없음", "", http.StatusUnauthorized},
		{"위조 토큰", "Bearer forged", http.StatusUnauthorized},
		{"허용 안 된 SA", "Bearer other-pod", http.StatusForbidden},
		{"API 서버 장애", "Bearer apiserver-down", http.StatusServiceUnavailable},
		{"탭", "Bearer tap", http.StatusOK},
	} {
		if got := call(c.header); got != c.want {
			t.Errorf("%s: status %d, want %d", c.name, got, c.want)
		}
	}

	before := reviews
	if got := call("Bearer tap"); got != http.StatusOK {
		t.Fatalf("캐시된 탭 토큰: status %d", got)
	}
	if got := call("Bearer forged"); got != http.StatusUnauthorized {
		t.Fatalf("캐시된 위조 토큰: status %d", got)
	}
	if reviews != before {
		t.Fatalf("같은 토큰을 1분 안에 다시 검증했다(위조 토큰 홍수 = TokenReview 홍수): reviews %d → %d", before, reviews)
	}

	// API 서버 장애 중에는 전에 확인한 토큰을 만료된 캐시로 계속 받는다. 처음 보는 토큰은 503.
	for k, v := range auth.cache {
		v.expires = time.Now().Add(-time.Second)
		auth.cache[k] = v
	}
	apiDown = true
	if got := call("Bearer tap"); got != http.StatusOK {
		t.Fatalf("장애 중 확인된 탭 토큰: status %d", got)
	}
	if got := call("Bearer forged"); got != http.StatusUnauthorized {
		t.Fatalf("장애 중 위조 토큰: status %d", got)
	}
	if got := call("Bearer never-seen"); got != http.StatusServiceUnavailable {
		t.Fatalf("장애 중 처음 보는 토큰: status %d", got)
	}
	apiDown = false

	// 인증은 판정만 막는다 — 메트릭·정책 조회는 스크레이퍼와 verify-cluster 가 토큰 없이 본다.
	for _, path := range []string{"/metrics", "/v1/policy", "/healthz"} {
		res, err := ts.Client().Get(ts.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		if res.StatusCode != http.StatusOK {
			t.Errorf("GET %s: status %d", path, res.StatusCode)
		}
	}
}

func TestParseAllowedSAsRejectsTypos(t *testing.T) {
	// 오타 항목을 조용히 버리면 목록이 비어 인증이 꺼진다.
	for _, bad := range []string{"litellm", "argus-system/", "/litellm", "a/b/c", "argus-system/litellm,oops"} {
		if _, err := ParseAllowedSAs(bad); err == nil {
			t.Errorf("%q 를 받아들였다", bad)
		}
	}
	got, err := ParseAllowedSAs(" argus-system/litellm , gateway/litellm ")
	if err != nil || !got["system:serviceaccount:argus-system:litellm"] || !got["system:serviceaccount:gateway:litellm"] {
		t.Fatalf("got %v, %v", got, err)
	}
	if got, err := ParseAllowedSAs(""); err != nil || len(got) != 0 {
		t.Fatalf("빈 값은 인증 꺼짐: %v, %v", got, err)
	}
}
