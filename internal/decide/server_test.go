// SPDX-License-Identifier: Apache-2.0

package decide

import (
	"fmt"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// TestConcurrentDecideIsSafe — net/http 은 요청마다 고루틴을 띄운다. 트립·쿨다운 로그 억제 상태를
// 락 없이 만지면 Go 런타임이 "concurrent map writes" 로 오퍼레이터를 통째로 죽인다.
// `go test -race` 로 돌려야 잡힌다 (make test 는 -race 없이 돈다 — 그래도 드물게 fatal 로 잡힌다).
func TestConcurrentDecideIsSafe(t *testing.T) {
	p := measured(Enforce)
	p.PrincipalCallsHard = 2 // 금방 트립 → 쿨다운 로그 경로를 탄다
	s := NewServer(New(p), func(string, ...any) {})
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	var wg sync.WaitGroup
	for w := 0; w < 16; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				body := fmt.Sprintf(`{"principal":"p%d","run_id":"r","conversation_hash":"c","conversation_source":"client","tool_name":"t"}`, w%4)
				res, err := ts.Client().Post(ts.URL+"/v1/decide", "application/json", strings.NewReader(body))
				if err != nil {
					t.Error(err)
					return
				}
				res.Body.Close()
			}
		}(w)
	}
	wg.Wait()
}
