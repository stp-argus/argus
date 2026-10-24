// SPDX-License-Identifier: Apache-2.0

package controller

import (
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	argusv1alpha1 "github.com/stp-argus/argus/api/v1alpha1"
)

// 여기서 시험하는 것은 **선언을 정책으로 옮기는 규칙**이다. 이 규칙이 조용히 틀리면
// 클러스터는 아무 불평 없이 잘못된 임계로 돈다 — 그게 이 프로젝트에서 가장 비싼 실패 형태다.

func i32(v int32) *int32 { return &v }

func policy(ns, name string, mut func(*argusv1alpha1.RunawayPolicySpec)) argusv1alpha1.RunawayPolicy {
	p := argusv1alpha1.RunawayPolicy{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
	}
	mut(&p.Spec)
	return p
}

func nsRef(name string) []argusv1alpha1.PolicyTargetReference {
	return []argusv1alpha1.PolicyTargetReference{{Kind: "Namespace", Name: name}}
}

// ── backstop 은 낮추기만 된다 ────────────────────────────────────────────────
//
// 이게 이 파일에서 가장 중요한 시험이다. backstop 의 존재 이유는 "정책을 위임해도
// 위임받은 쪽이 천장을 올려 회피할 수는 없다"는 보장이고, 그 보장이 깨지면
// backstop 은 이름만 남는다.

func TestBackstopCannotBeRaisedByAMoreSpecificPolicy(t *testing.T) {
	// 네임스페이스 정책이 바닥을 26 으로 깐다. 더 구체적인 정책이 100 을 요구한다.
	base := policy("argus-system", "org-backstop", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.TargetRefs = nsRef("argus-system")
		s.Backstop = &argusv1alpha1.BackstopSpec{PrincipalCalls: i32(26)}
	})
	team := policy("argus-system", "team-override", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.TargetRefs = []argusv1alpha1.PolicyTargetReference{
			{Group: "apps", Kind: "Deployment", Name: "greedy-agent"},
		}
		s.Detect = &argusv1alpha1.DetectSpec{
			PrincipalCalls: &argusv1alpha1.AxisSpec{Hard: i32(100)},
		}
	})

	// 컨트롤러가 넘기는 순서(덜 구체적 → 더 구체적)와 같게 준다.
	out, _ := render([]argusv1alpha1.RunawayPolicy{base, team})

	if got := out.Axes["principal_calls"].Hard; got != 26 {
		t.Fatalf("backstop 이 뚫렸다: principal_calls.hard = %v, 기대 26 (위임이 우회가 됐다)", got)
	}
}

func TestBackstopDoesNotRaiseAStricterDetectThreshold(t *testing.T) {
	// 반대 방향: detect 가 이미 backstop 보다 엄격하면 backstop 은 아무것도 하지 않는다.
	p := policy("argus-system", "strict", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.Detect = &argusv1alpha1.DetectSpec{
			PrincipalCalls: &argusv1alpha1.AxisSpec{Hard: i32(26)},
		}
		s.Backstop = &argusv1alpha1.BackstopSpec{PrincipalCalls: i32(200)}
	})
	out, _ := render([]argusv1alpha1.RunawayPolicy{p})

	if got := out.Axes["principal_calls"].Hard; got != 26 {
		t.Fatalf("backstop 이 임계를 느슨하게 만들었다: %v, 기대 26", got)
	}
}

func TestBackstopBudgetTakesTheLowestValue(t *testing.T) {
	loose := policy("argus-system", "loose", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.Backstop = &argusv1alpha1.BackstopSpec{BudgetUSD: "10.00"}
	})
	tight := policy("argus-system", "tight", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.Backstop = &argusv1alpha1.BackstopSpec{BudgetUSD: "1.50"}
	})
	// 순서를 바꿔도 결과는 같아야 한다 — 낮은 쪽이 이긴다.
	for _, order := range [][]argusv1alpha1.RunawayPolicy{{loose, tight}, {tight, loose}} {
		out, _ := render(order)
		if out.BudgetUSDHard != "1.50" {
			t.Fatalf("budget backstop = %q, 기대 \"1.50\"", out.BudgetUSDHard)
		}
	}
}

// ── 부착 범위 (GEP-713) ─────────────────────────────────────────────────────

func TestAttachmentRules(t *testing.T) {
	r := &RunawayPolicyReconciler{TapNamespace: "argus-system"}

	cases := []struct {
		name string
		p    argusv1alpha1.RunawayPolicy
		want attachKind
	}{
		{
			// 탭 네임스페이스에서 targetRefs 생략 = 플랫폼 정책
			name: "탭 네임스페이스 · refs 없음",
			p:    policy("argus-system", "a", func(s *argusv1alpha1.RunawayPolicySpec) {}),
			want: attachTap,
		},
		{
			name: "탭 네임스페이스 · 자기 자신 명시",
			p: policy("argus-system", "a", func(s *argusv1alpha1.RunawayPolicySpec) {
				s.TargetRefs = nsRef("argus-system")
			}),
			want: attachTap,
		},
		{
			// ⚠️ 이게 이 표의 핵심이다. 예전에는 이것도 붙어서, team-b 가 임계를
			// 9999 로 선언해 플랫폼 정책을 무력화할 수 있었다.
			name: "남의 네임스페이스가 탭을 겨냥 — 거부",
			p: policy("team-b", "loose", func(s *argusv1alpha1.RunawayPolicySpec) {
				s.TargetRefs = nsRef("argus-system")
			}),
			want: attachForeign,
		},
		{
			// 팀 정책. 선언은 유효하지만 탭이 아직 요청을 앱에 귀속시키지 못한다.
			name: "팀이 자기 네임스페이스를 겨냥",
			p: policy("team-b", "own", func(s *argusv1alpha1.RunawayPolicySpec) {
				s.TargetRefs = nsRef("team-b")
			}),
			want: attachOwn,
		},
		{
			name: "팀 네임스페이스 · refs 없음",
			p:    policy("team-b", "own2", func(s *argusv1alpha1.RunawayPolicySpec) {}),
			want: attachOwn,
		},
		{
			name: "제3의 네임스페이스를 겨냥 — 우리 소관 아님",
			p: policy("team-b", "other", func(s *argusv1alpha1.RunawayPolicySpec) {
				s.TargetRefs = nsRef("team-c")
			}),
			want: attachNone,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := r.attachment(&c.p); got != c.want {
				t.Fatalf("attachment = %v, 기대 %v", got, c.want)
			}
		})
	}
}

// 지금 탭의 판정을 바꾸는 것은 **플랫폼 정책뿐**이다.
func TestOnlyPlatformPolicyAppliesToTapForNow(t *testing.T) {
	r := &RunawayPolicyReconciler{TapNamespace: "argus-system"}
	platform := policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {})
	team := policy("team-b", "t", func(s *argusv1alpha1.RunawayPolicySpec) {})
	foreign := policy("team-b", "f", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.TargetRefs = nsRef("argus-system")
	})
	if !r.appliesToTap(&platform) {
		t.Fatal("플랫폼 정책이 안 붙었다")
	}
	for _, p := range []argusv1alpha1.RunawayPolicy{team, foreign} {
		if r.appliesToTap(&p) {
			t.Fatalf("%s/%s 가 탭에 붙었다 — 붙으면 안 된다", p.Namespace, p.Name)
		}
	}
}

// 회귀 방지: 남의 네임스페이스 정책이 병합에 끼어 임계를 올릴 수 없다.
func TestForeignPolicyCannotLoosenThresholds(t *testing.T) {
	r := &RunawayPolicyReconciler{TapNamespace: "argus-system"}
	strict := policy("argus-system", "strict", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.Detect = &argusv1alpha1.DetectSpec{
			DuplicateCalls: &argusv1alpha1.AxisSpec{Hard: i32(20)},
		}
	})
	loose := policy("team-b", "loose", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.TargetRefs = nsRef("argus-system")
		s.Detect = &argusv1alpha1.DetectSpec{
			DuplicateCalls: &argusv1alpha1.AxisSpec{Hard: i32(9999)},
		}
	})
	var applicable []argusv1alpha1.RunawayPolicy
	for _, p := range []argusv1alpha1.RunawayPolicy{strict, loose} {
		if r.appliesToTap(&p) {
			applicable = append(applicable, p)
		}
	}
	out, _ := render(applicable)
	if got := out.Axes["duplicate_calls"].Hard; got != 20 {
		t.Fatalf("duplicate_calls.hard = %v, 기대 20 — 남의 정책이 임계를 올렸다", got)
	}
}

func TestSpecificityOrdersInheritedBeforeDirect(t *testing.T) {
	none := policy("ns", "a", func(s *argusv1alpha1.RunawayPolicySpec) {})
	inherited := policy("ns", "b", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.TargetRefs = nsRef("ns")
	})
	direct := policy("ns", "c", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.TargetRefs = []argusv1alpha1.PolicyTargetReference{
			{Group: "apps", Kind: "Deployment", Name: "x"},
		}
	})
	if !(specificity(&none) < specificity(&inherited) && specificity(&inherited) < specificity(&direct)) {
		t.Fatalf("구체성 순서가 어긋났다: %d %d %d",
			specificity(&none), specificity(&inherited), specificity(&direct))
	}
}

// ── 렌더 기본값과 활성 축 ───────────────────────────────────────────────────

func TestEmptyPolicyRendersObserveAndNoAxes(t *testing.T) {
	// 정책이 비어 있으면 **아무것도 차단하지 않는다.** 기본값이 차단이면
	// 실수로 만든 빈 CR 하나가 데이터플레인을 세운다.
	out, active := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "empty", func(s *argusv1alpha1.RunawayPolicySpec) {}),
	})
	if out.ControlLevel != "Observe" {
		t.Fatalf("빈 정책의 controlLevel = %q, 기대 Observe", out.ControlLevel)
	}
	if len(active) != 0 {
		t.Fatalf("활성 축 = %v, 기대 없음", active)
	}
}

func TestActiveAxesListsOnlyDeclaredOnes(t *testing.T) {
	out, active := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.ControlLevel = "Enforce"
			s.Window = &metav1.Duration{Duration: 90 * time.Second}
			s.Cooldown = &metav1.Duration{Duration: 45 * time.Second}
			s.Detect = &argusv1alpha1.DetectSpec{
				DuplicateCalls: &argusv1alpha1.AxisSpec{Soft: i32(2), Hard: i32(20)},
				RotationGap:    &argusv1alpha1.RatioAxisSpec{Soft: "3", Hard: "5.2"},
			}
		}),
	})
	if out.ControlLevel != "Enforce" || out.WindowS != 90 || out.CooldownS != 45 {
		t.Fatalf("스칼라 필드가 안 옮겨졌다: %+v", out)
	}
	want := []string{"duplicate_calls", "rotation_gap"}
	if len(active) != len(want) {
		t.Fatalf("활성 축 = %v, 기대 %v", active, want)
	}
	for i := range want {
		if active[i] != want[i] {
			t.Fatalf("활성 축 = %v, 기대 %v", active, want)
		}
	}
	if got := out.Axes["rotation_gap"].Hard; got != 5.2 {
		t.Fatalf("비율 축 파싱 실패: %v", got)
	}
}

func TestStepCapOnlyFillsAnEmptyRunStepsAxis(t *testing.T) {
	// deprecated stepCap 은 이미 배포된 CR 을 위한 다리일 뿐이다.
	// 명시된 runSteps 를 덮어쓰면 안 된다.
	out, _ := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.StepCap = i32(50)
			s.Detect = &argusv1alpha1.DetectSpec{
				RunSteps: &argusv1alpha1.AxisSpec{Hard: i32(27)},
			}
		}),
	})
	if got := out.Axes["run_steps"].Hard; got != 27 {
		t.Fatalf("stepCap 이 명시된 runSteps 를 덮었다: %v, 기대 27", got)
	}

	out2, _ := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.StepCap = i32(50)
		}),
	})
	if got := out2.Axes["run_steps"].Hard; got != 50 {
		t.Fatalf("stepCap 폴백이 안 먹었다: %v, 기대 50", got)
	}
}

// ── identity (spec 006f) ────────────────────────────────────────────────────
//
// 신원은 backstop 과 규칙이 다르다. backstop 은 안전 천장이라 **낮추기만** 되지만,
// 신원은 "어떤 클라이언트가 들어오는가"라는 **환경 사실**의 선언이고 그 사실은
// 대상이 좁을수록 정확하다 — 그래서 더 구체적인 정책이 이긴다.

func boolp(v bool) *bool { return &v }

func TestIdentityDefaultsAreTheSafeOnes(t *testing.T) {
	// 대화를 모르면 세지 않는다. 가짜 버킷에 넣으면 무관한 작업이 서로의 반복이 된다.
	out, _ := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {}),
	})
	if out.Identity.OnUnknownConversation != "Skip" {
		t.Fatalf("기본 onUnknownConversation = %q, 기대 Skip", out.Identity.OnUnknownConversation)
	}
	if !out.Identity.TrustGatewaySession {
		t.Fatal("기본은 게이트웨이 세션을 신뢰해야 한다 (LiteLLM 이 우리가 모르는 벤더까지 덮는다)")
	}
	if len(out.Identity.ConversationHeaders) != 0 {
		t.Fatalf("기본 헤더 목록은 비어야 한다(탭 내장 사다리 사용): %v", out.Identity.ConversationHeaders)
	}
}

func TestIdentityIsRenderedFromTheCR(t *testing.T) {
	out, _ := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Identity = &argusv1alpha1.IdentitySpec{
				ConversationHeaders:   []string{"x-acme-session", "session-id"},
				TrustGatewaySession:   boolp(false),
				OnUnknownConversation: "Derive",
			}
		}),
	})
	if out.Identity.TrustGatewaySession {
		t.Fatal("trustGatewaySession=false 가 반영되지 않았다")
	}
	if out.Identity.OnUnknownConversation != "Derive" {
		t.Fatalf("onUnknownConversation = %q", out.Identity.OnUnknownConversation)
	}
	if len(out.Identity.ConversationHeaders) != 2 ||
		out.Identity.ConversationHeaders[0] != "x-acme-session" {
		t.Fatalf("헤더 순서가 보존되지 않았다: %v", out.Identity.ConversationHeaders)
	}
}

func TestMoreSpecificPolicyWinsOnIdentity(t *testing.T) {
	ns := policy("argus-system", "ns-wide", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.TargetRefs = nsRef("argus-system")
		s.Identity = &argusv1alpha1.IdentitySpec{ConversationHeaders: []string{"x-generic"}}
	})
	specific := policy("argus-system", "team", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.Identity = &argusv1alpha1.IdentitySpec{ConversationHeaders: []string{"x-team-session"}}
	})
	// 컨트롤러가 넘기는 순서(덜 구체적 → 더 구체적)
	out, _ := render([]argusv1alpha1.RunawayPolicy{ns, specific})
	if out.Identity.ConversationHeaders[0] != "x-team-session" {
		t.Fatalf("더 구체적인 정책이 이기지 않았다: %v", out.Identity.ConversationHeaders)
	}
}

// ── enabled 스위치 (helm values 의 *.enabled 와 같은 결) ─────────────────────

func TestDisabledAxisKeepsItsNumbersButDoesNotRender(t *testing.T) {
	// 임계값은 실측으로 도출한 근거다. 축을 끄려고 숫자를 지우게 하면 근거가 같이 사라진다.
	off := false
	out, active := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Detect = &argusv1alpha1.DetectSpec{
				DuplicateCalls: &argusv1alpha1.AxisSpec{Enabled: &off, Soft: i32(2), Hard: i32(20)},
				PrincipalCalls: &argusv1alpha1.AxisSpec{Soft: i32(11), Hard: i32(26)},
			}
		}),
	})
	if _, ok := out.Axes["duplicate_calls"]; ok {
		t.Fatal("enabled: false 축이 렌더됐다")
	}
	if len(active) != 1 || active[0] != "principal_calls" {
		t.Fatalf("활성 축 = %v, 기대 [principal_calls]", active)
	}
}

func TestDisabledPolicyIsSkippedNotDeleted(t *testing.T) {
	// CR 은 남고 판정에서만 빠진다. Reconcile 분류는 attachTap 에서 걸러지므로
	// 여기서는 render 에 안 들어간다는 것만 — 분류 자체는 kind 관통이 확인한다.
	off := false
	p := policy("argus-system", "paused", func(s *argusv1alpha1.RunawayPolicySpec) {
		s.Enabled = &off
		s.ControlLevel = "Enforce"
		s.Detect = &argusv1alpha1.DetectSpec{
			DuplicateCalls: &argusv1alpha1.AxisSpec{Hard: i32(20)},
		}
	})
	// 꺼진 정책이 실수로 render 에 들어가면 Enforce 가 살아난다 — 그 사고를 고정
	if p.Spec.Enabled == nil || *p.Spec.Enabled {
		t.Fatal("선행 조건: 정책이 꺼져 있어야 한다")
	}
}

func TestAxisEnabledNilMeansOn(t *testing.T) {
	// 생략 = true. 기존 CR(enabled 필드가 없던 시절)이 조용히 꺼지면 안 된다.
	out, active := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Detect = &argusv1alpha1.DetectSpec{
				DuplicateCalls: &argusv1alpha1.AxisSpec{Soft: i32(2), Hard: i32(20)},
			}
		}),
	})
	if out.Axes["duplicate_calls"].Hard != 20 || len(active) != 1 {
		t.Fatalf("enabled 생략이 켜짐으로 안 읽혔다: %v %v", out.Axes, active)
	}
}

func TestDetectBudgetUSDRendersSoftAndBackstopStillCapsHard(t *testing.T) {
	// 엔진은 budget_usd 를 이미 세고 있었는데 CR 에 선언 표면이 없어 soft 를 켤 수 없었다 (STP-14).
	// detect.budgetUSD 가 soft/hard 를 렌더하고, backstop.budgetUSD 는 여전히 천장이다.
	out, active := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Detect = &argusv1alpha1.DetectSpec{
				BudgetUSD: &argusv1alpha1.BudgetAxisSpec{Soft: "0.5", Hard: "5"},
			}
			s.Backstop = &argusv1alpha1.BackstopSpec{BudgetUSD: "1"}
		}),
	})
	if out.BudgetUSDSoft != "0.5" {
		t.Fatalf("budget soft = %q, 기대 \"0.5\"", out.BudgetUSDSoft)
	}
	if out.BudgetUSDHard != "1" {
		t.Fatalf("budget hard = %q, 기대 backstop 이 이긴 \"1\"", out.BudgetUSDHard)
	}
	if len(active) != 1 || active[0] != "budget_usd" {
		t.Fatalf("활성 축 = %v, 기대 [budget_usd]", active)
	}
	ep := toEnginePolicy(out)
	if ep.BudgetUSDSoft != 0.5 || ep.BudgetUSDHard != 1 {
		t.Fatalf("엔진 정책 budget = %v/%v, 기대 0.5/1", ep.BudgetUSDSoft, ep.BudgetUSDHard)
	}

	// enabled:false 는 detect 쪽 값을 지우고, backstop 천장은 그대로 남는다.
	out, _ = render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			f := false
			s.Detect = &argusv1alpha1.DetectSpec{
				BudgetUSD: &argusv1alpha1.BudgetAxisSpec{Enabled: &f, Soft: "0.5", Hard: "5"},
			}
			s.Backstop = &argusv1alpha1.BackstopSpec{BudgetUSD: "1"}
		}),
	})
	if out.BudgetUSDSoft != "" || out.BudgetUSDHard != "1" {
		t.Fatalf("enabled:false 후 budget = %q/%q, 기대 \"\"/\"1\"", out.BudgetUSDSoft, out.BudgetUSDHard)
	}
}

func TestConversationSizeAxesRender(t *testing.T) {
	// G3/G4 (STP-19): detect.messageBytes · detect.conversationGrowth → msg_bytes · conv_growth
	out, active := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "p", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Detect = &argusv1alpha1.DetectSpec{
				MessageBytes:       &argusv1alpha1.AxisSpec{Soft: i32(50000)},
				ConversationGrowth: &argusv1alpha1.RatioAxisSpec{Soft: "4"},
			}
		}),
	})
	if out.Axes["msg_bytes"].Soft != 50000 || out.Axes["conv_growth"].Soft != 4 {
		t.Fatalf("axes = %+v", out.Axes)
	}
	if len(active) != 2 || active[0] != "conv_growth" || active[1] != "msg_bytes" {
		t.Fatalf("활성 축 = %v", active)
	}
	ep := toEnginePolicy(out)
	if ep.MsgBytesSoft != 50000 || ep.ConvGrowthSoft != 4 {
		t.Fatalf("엔진 정책 = %+v", ep)
	}
}

func TestTeamCallsBackstopRendersHardOnlyAndLowersOnly(t *testing.T) {
	out, active := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "org", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Backstop = &argusv1alpha1.BackstopSpec{TeamCalls: i32(300)}
		}),
		policy("argus-system", "team", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Backstop = &argusv1alpha1.BackstopSpec{TeamCalls: i32(900)} // 올리려는 시도 — 무시돼야
		}),
	})
	if out.Axes["team_calls"].Hard != 300 || out.Axes["team_calls"].Soft != 0 {
		t.Fatalf("team_calls = %+v, 기대 hard 300 / soft 0", out.Axes["team_calls"])
	}
	if len(active) != 1 || active[0] != "team_calls" {
		t.Fatalf("활성 축 = %v", active)
	}
	if ep := toEnginePolicy(out); ep.TeamCallsHard != 300 {
		t.Fatalf("엔진 정책 = %+v", ep)
	}
}

func TestFailureModeReachesEngine(t *testing.T) {
	out, _ := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "org", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Backstop = &argusv1alpha1.BackstopSpec{TeamCalls: i32(300), FailureMode: "Closed"}
		}),
	})
	if ep := toEnginePolicy(out); ep.FailureMode != "Closed" {
		t.Fatalf("엔진 정책 FailureMode = %q, want Closed", ep.FailureMode)
	}
}

func TestIdentityReachesEngine(t *testing.T) {
	out, _ := render([]argusv1alpha1.RunawayPolicy{
		policy("argus-system", "org", func(s *argusv1alpha1.RunawayPolicySpec) {
			s.Backstop = &argusv1alpha1.BackstopSpec{TeamCalls: i32(300)}
			s.Identity = &argusv1alpha1.IdentitySpec{ConversationHeaders: []string{"x-agent-session"}}
		}),
	})
	ep := toEnginePolicy(out)
	if ep.Identity == nil || len(ep.Identity.ConversationHeaders) != 1 || ep.Identity.ConversationHeaders[0] != "x-agent-session" {
		t.Fatalf("엔진 정책 Identity = %+v", ep.Identity)
	}
}
