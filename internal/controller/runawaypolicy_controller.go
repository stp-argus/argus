// SPDX-License-Identifier: Apache-2.0

/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Package controller — RunawayPolicy 를 탭이 읽을 수 있는 정책 파일로 옮긴다.
//
// # 왜 ConfigMap 인가 (spec 006 의 /v1/decide 와 다른 선택)
//
// spec 006 은 탭이 호출마다 오퍼레이터의 `/v1/decide` 에 질의하는 방식을 그렸다.
// 다시 보면 그건 **판정 상태(카운터)가 중앙에 있을 때** 의미가 있다.
// 임계값은 거의 안 바뀌는 설정이고, 그걸 호출마다 HTTP 로 물어보는 건 지연만 더한다.
//
// 지금 카운터는 탭 프로세스 안에 있으므로(중앙화는 Phase 2 의 Redis decide.lua),
// 컨트롤러는 **정책을 렌더해 두기만** 하면 된다. 탭은 파일을 읽는다.
// 나중에 카운터가 중앙으로 가면 그때 /v1/decide 가 제자리를 찾는다.
package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"

	argusv1alpha1 "github.com/stp-argus/argus/api/v1alpha1"
	"github.com/stp-argus/argus/internal/decide"
)

// toEnginePolicy — 렌더 결과를 판정 엔진 정책으로. renderedPolicy(탭 계약)와
// decide.Policy(엔진)가 별도 타입인 이유는 renderedPolicy 주석과 같다 —
// 계약과 구현이 같은 타입을 공유하면 한쪽을 고칠 때마다 다른 쪽이 깨진다.
func toEnginePolicy(rp renderedPolicy) decide.Policy {
	ax := func(name string) (float64, float64) {
		a := rp.Axes[name]
		return a.Soft, a.Hard
	}
	p := decide.DefaultPolicy()
	p.ControlLevel = rp.ControlLevel
	p.WindowS = rp.WindowS
	p.CooldownS = rp.CooldownS
	p.PrincipalCallsSoft, p.PrincipalCallsHard = ax("principal_calls")
	p.RepeatSoft, p.RepeatHard = ax("duplicate_calls")
	p.RunStepsSoft, p.RunStepsHard = ax("run_steps")
	p.RotationGapSoft, p.RotationGapHard = ax("rotation_gap")
	p.MsgBytesSoft, p.MsgBytesHard = ax("msg_bytes")
	p.ConvGrowthSoft, p.ConvGrowthHard = ax("conv_growth")
	p.TeamCallsSoft, p.TeamCallsHard = ax("team_calls")
	if rp.BudgetUSDSoft != "" {
		if v, err := strconv.ParseFloat(rp.BudgetUSDSoft, 64); err == nil {
			p.BudgetUSDSoft = v
		}
	}
	if rp.BudgetUSDHard != "" {
		if v, err := strconv.ParseFloat(rp.BudgetUSDHard, 64); err == nil {
			p.BudgetUSDHard = v
		}
	}
	// 적용된 정책이 하나도 없으면(전부 disabled 등) "default" 다.
	// "crd:?" 로 두면 운영 절차("crd: 면 선언이 판정까지 닿았다")가 거짓 양성을 낸다.
	src := "default"
	if len(rp.SourcePolicies) > 0 {
		src = "crd:" + rp.SourcePolicies[0]
		for _, s := range rp.SourcePolicies[1:] {
			src += "," + s
		}
	}
	p.Source = src
	p.FailureMode = rp.BackstopFail
	if len(rp.SourcePolicies) > 0 {
		id := rp.Identity
		p.Identity = &decide.Identity{ConversationHeaders: id.ConversationHeaders,
			TrustGatewaySession: id.TrustGatewaySession, OnUnknownConversation: id.OnUnknownConversation}
	}
	return p
}

const (
	// 탭이 마운트해 읽는 ConfigMap 과 키. 이름을 고정하는 이유 —
	// 마운트는 파드 스펙에 박히므로 동적으로 늘어나면 롤아웃 없이는 못 붙인다.
	// 여러 정책은 컨트롤러가 **합쳐서** 이 하나로 렌더한다.
	PolicyConfigMapName = "argus-policy"
	PolicyConfigMapKey  = "policy.json"

	condAccepted   = "Accepted"   // 컨트롤러가 받아 파싱했다 (GEP-713)
	condProgrammed = "Programmed" // 탭이 읽을 수 있는 형태로 렌더됐다 (GEP-713)
)

// RunawayPolicyReconciler reconciles RunawayPolicy objects.
type RunawayPolicyReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// TapNamespace — 렌더된 정책 ConfigMap 을 둘 네임스페이스(= LiteLLM 이 있는 곳).
	TapNamespace string

	// Engine — /v1/decide 판정 엔진 (spec 006b). CR 이 바뀌면 여기로도 민다.
	// ConfigMap 경로(탭의 로컬 폴백용)와 달리 **kubelet 지연이 없다** — 렌더 즉시 반영된다.
	// nil 이면 무시한다 (테스트 등).
	Engine *decide.Engine
}

// renderedPolicy — 탭(가드레일)이 읽는 형태. 필드 이름은 가드레일의 Policy 와 1:1 이다.
//
// 굳이 별도 구조체를 두는 이유: CRD 스키마가 바뀌어도 탭이 읽는 계약은 여기서 고정된다.
// 둘이 같은 타입을 공유하면 CRD 를 손볼 때마다 탭이 깨진다.
type renderedPolicy struct {
	ControlLevel   string             `json:"control_level"`
	WindowS        float64            `json:"window_s"`
	CooldownS      float64            `json:"cooldown_s"`
	Axes           map[string]axisOut `json:"axes"`
	BudgetUSDSoft  string             `json:"budget_usd_soft,omitempty"`
	BudgetUSDHard  string             `json:"budget_usd_hard,omitempty"`
	BackstopFail   string             `json:"backstop_failure_mode,omitempty"`
	Identity       identityOut        `json:"identity"`
	SourcePolicies []string           `json:"source_policies"`
	RenderedAt     string             `json:"rendered_at"`
}

// identityOut — 탭이 읽는 신원 설정. 비어 있으면 탭의 내장 기본값이 쓰인다.
type identityOut struct {
	ConversationHeaders   []string `json:"conversation_headers,omitempty"`
	TrustGatewaySession   bool     `json:"trust_gateway_session"`
	OnUnknownConversation string   `json:"on_unknown_conversation"`
}

type axisOut struct {
	Soft float64 `json:"soft"`
	Hard float64 `json:"hard"`
}

// +kubebuilder:rbac:groups=argus.stp.io,resources=runawaypolicies,verbs=get;list;watch
// +kubebuilder:rbac:groups=argus.stp.io,resources=runawaypolicies/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=argus.stp.io,resources=runawaypolicies/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=authentication.k8s.io,resources=tokenreviews,verbs=create

// Reconcile 은 정책 하나가 바뀔 때마다 **전체를 다시 렌더한다.**
//
// 증분 갱신을 하지 않는 이유: 정책 개수가 적고(네임스페이스당 한 자릿수), 전체 렌더는
// 멱등이라 부분 갱신이 만들 수 있는 "지웠는데 남아 있는" 상태가 원천적으로 없다.
func (r *RunawayPolicyReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	lg := log.FromContext(ctx)

	var all argusv1alpha1.RunawayPolicyList
	if err := r.List(ctx, &all); err != nil {
		return ctrl.Result{}, fmt.Errorf("list policies: %w", err)
	}

	applicable := make([]argusv1alpha1.RunawayPolicy, 0, len(all.Items))
	// 붙지 않는 정책도 **왜 안 붙는지**를 자기 status 에 적어준다. 조용히 무시하면
	// "선언했는데 아무 일도 안 일어나는" 상태가 되고, 빈 status 는 "컨트롤러가 안 돈다"와
	// 구별되지 않는다. 이유가 셋이라 메시지도 셋이다.
	type unattached struct {
		p      argusv1alpha1.RunawayPolicy
		reason string
		msg    string
	}
	var skipped []unattached
	for _, p := range all.Items {
		switch r.attachment(&p) {
		case attachTap:
			if p.Spec.Enabled != nil && !*p.Spec.Enabled {
				// helm values 의 enabled: false 와 같은 결 — CR 을 지우지 않고 내린다.
				// 조용히 무시하면 "선언했는데 아무 일도 안 일어나는" 상태가 되므로 status 에 남긴다.
				skipped = append(skipped, unattached{p, "Disabled",
					"정책이 enabled: false 다. 임계·대상 선언은 보존된 채 판정에서만 빠져 있다"})
				continue
			}
			applicable = append(applicable, p)
		case attachForeign:
			// ⚠️ 여기가 테넌트 격리 지점이다. 남의 네임스페이스가 탭을 겨냥해
			// 임계를 올리는 것을 막는다 (2026-08-16 확인된 구멍).
			skipped = append(skipped, unattached{p, "CrossNamespaceNotPermitted",
				fmt.Sprintf("네임스페이스 %q 의 정책은 탭(%s)을 겨냥할 수 없다. "+
					"플랫폼 정책은 탭 네임스페이스 안에서 선언한다", p.Namespace, r.TapNamespace)})
		case attachOwn:
			// 팀 정책. 선언은 유효하지만 **탭이 아직 요청을 앱에 귀속시키지 못한다**.
			skipped = append(skipped, unattached{p, "AwaitingWorkloadIdentity",
				"정책은 유효하지만 아직 적용되지 않는다 — 탭이 요청을 워크로드에 귀속시키는 " +
					"배선(XFCC/SPIFFE)이 없다. 지금 적용되는 것은 탭 네임스페이스의 플랫폼 정책뿐이다"})
		case attachNone:
			if p.Namespace == r.TapNamespace {
				skipped = append(skipped, unattached{p, "NotAttached",
					fmt.Sprintf("이 정책은 탭(%s)에 붙지 않는다. 지금 지원하는 부착은 "+
						"targetRefs 생략(자기 네임스페이스) 또는 Kind=Namespace 뿐이다", r.TapNamespace)})
			}
			// 그 밖(남의 네임스페이스에서 또 남을 겨냥)은 우리 소관이 아니다. 건드리지 않는다.
		}
	}
	// 상속(Namespace 대상)을 먼저, 구체적인 것을 나중에 — 뒤가 앞을 덮어쓴다.
	// 단 backstop 은 낮추기만 된다 (mergeInto 참고).
	sort.SliceStable(applicable, func(i, j int) bool {
		return specificity(&applicable[i]) < specificity(&applicable[j])
	})

	rendered, active := render(applicable)
	// 판정 엔진에 즉시 반영. ConfigMap 은 탭의 로컬 폴백용으로 계속 쓰고(kubelet ~85초),
	// 중앙 판정은 이 줄로 **렌더 즉시** 새 정책을 탄다.
	if r.Engine != nil {
		r.Engine.SetPolicy(toEnginePolicy(rendered))
	}
	if err := r.writeConfigMap(ctx, rendered); err != nil {
		// 렌더 실패는 정책이 반영되지 않았다는 뜻이므로 status 에 남긴다.
		for i := range applicable {
			_ = r.setConditions(ctx, &applicable[i], true, false, active, err)
		}
		return ctrl.Result{}, fmt.Errorf("write policy configmap: %w", err)
	}

	for i := range applicable {
		if err := r.setConditions(ctx, &applicable[i], true, true, active, nil); err != nil {
			lg.Error(err, "status 갱신 실패", "policy", applicable[i].Name)
		}
	}
	for i := range skipped {
		s := &skipped[i]
		if err := r.setNotAttached(ctx, &s.p, s.reason, s.msg); err != nil {
			lg.Error(err, "status 갱신 실패", "policy", s.p.Name)
		}
		lg.Info("정책이 적용되지 않는다", "policy", s.p.Namespace+"/"+s.p.Name,
			"reason", s.reason, "targetRefs", s.p.Spec.TargetRefs)
	}

	if len(active) == 0 {
		// **선언은 했는데 축이 다 꺼진 상태.** 조용히 넘기면 "정책이 있으니 지켜지겠지"
		// 라는 오해가 생긴다. 로그와 status 양쪽에 남긴다.
		lg.Info("정책이 적용됐지만 활성 축이 없다 — 아무것도 탐지하지 않는다",
			"policies", len(applicable))
	}
	lg.Info("정책 렌더 완료", "policies", len(applicable), "activeAxes", active,
		"configMap", PolicyConfigMapName, "namespace", r.TapNamespace)
	return ctrl.Result{}, nil
}

// appliesToTap — 이 정책이 탭에 적용되는가.
//
// targetRefs 가 비어 있으면 **자기 네임스페이스**에 적용된다(가장 흔한 경우).
// Kind=Namespace 면 그 네임스페이스에 상속된다(GEP-2649).
// 그 외 Kind(HTTPRoute 등)는 아직 라우트 단위 배선이 없어 인식만 하고 넘긴다.
// appliesToTap — 지금 이 정책이 **실제로 탭의 판정을 바꾸는가.**
//
// 탭은 아직 "이 요청이 어느 앱에서 왔는가"를 모른다(설계 C단계, XFCC/SPIFFE 소비).
// 그래서 지금 적용되는 것은 **탭 네임스페이스의 플랫폼 정책뿐**이다.
// 자기 네임스페이스를 겨냥한 팀 정책은 **받아두되 적용되지 않고**, 그 사실이 status 에 남는다 —
// 조용히 무시하면 "선언했는데 아무 일도 안 일어나는" 상태가 된다.
func (r *RunawayPolicyReconciler) appliesToTap(p *argusv1alpha1.RunawayPolicy) bool {
	return r.attachment(p) == attachTap
}

type attachKind int

const (
	attachNone    attachKind = iota
	attachOwn                // 자기 네임스페이스 (targetRefs 생략 또는 자기 자신 지정)
	attachTap                // 탭 네임스페이스를 겨냥 — **플랫폼 정책만 허용**
	attachForeign            // 남의 네임스페이스를 겨냥 — 거부
)

// attachment — 이 정책이 어떤 자격으로 붙는가.
//
// **테넌트 격리가 여기서 결정된다.** 예전에는 `Kind=Namespace, name=<탭>` 이면 **어느
// 네임스페이스에서 선언했든** 붙었다. 그러면 team-b 가 탭을 겨냥해 duplicate_calls 임계를
// 9999 로 선언하고, 병합에서 나중 것이 이겨 **플랫폼 정책을 무력화**할 수 있다
// (2026-08-16 확인. backstop 은 principal_calls·budget 만 막는다).
//
// Gateway API 의 ReferenceGrant 와 같은 입장을 취한다 — **크로스 네임스페이스 참조는
// 받는 쪽이 허용해야 한다.** 여기서 "받는 쪽"은 탭 네임스페이스이고, 허용의 표시는
// **그 네임스페이스 안에 있다는 것** 자체다. 플랫폼 팀만 거기에 쓸 수 있기 때문이다.
func (r *RunawayPolicyReconciler) attachment(p *argusv1alpha1.RunawayPolicy) attachKind {
	if len(p.Spec.TargetRefs) == 0 {
		// 생략 = 자기 네임스페이스. 그게 탭이면 플랫폼 정책이다.
		if p.Namespace == r.TapNamespace {
			return attachTap
		}
		return attachOwn
	}
	for _, t := range p.Spec.TargetRefs {
		if t.Kind != "Namespace" {
			continue // Deployment·HTTPRoute 등은 아직 배선이 없다 (설계 C단계)
		}
		switch {
		case t.Name == p.Namespace:
			// 자기 네임스페이스를 명시적으로 겨냥 — 언제나 정당하다.
			if p.Namespace == r.TapNamespace {
				return attachTap
			}
			return attachOwn
		case t.Name == r.TapNamespace:
			// 탭을 겨냥했다. **탭 네임스페이스 안에서 선언한 것만** 받는다.
			if p.Namespace == r.TapNamespace {
				return attachTap
			}
			return attachForeign
		}
	}
	return attachNone
}

// specificity — 낮을수록 먼저(= 덜 구체적). Namespace 상속이 가장 낮다.
func specificity(p *argusv1alpha1.RunawayPolicy) int {
	if len(p.Spec.TargetRefs) == 0 {
		return 0
	}
	for _, t := range p.Spec.TargetRefs {
		if t.Kind == "Namespace" {
			return 1
		}
	}
	return 2
}

func render(policies []argusv1alpha1.RunawayPolicy) (renderedPolicy, []string) {
	out := renderedPolicy{
		ControlLevel: "Observe", // 안전한 기본값 — 정책이 비어도 차단하지 않는다
		WindowS:      60,
		CooldownS:    30,
		Axes:         map[string]axisOut{},
		RenderedAt:   time.Now().UTC().Format(time.RFC3339),
		// 신원 기본값도 **안전한 쪽**이다. 대화를 모르면 세지 않는다 —
		// 가짜 버킷에 넣으면 무관한 작업이 서로의 반복이 되어 오탐이 난다.
		Identity: identityOut{TrustGatewaySession: true, OnUnknownConversation: "Skip"},
	}
	// backstop 은 **전부 병합한 뒤에** 조인다.
	//
	// 처음엔 정책마다 detect → backstop 순으로 접었는데, 그러면 덜 구체적인 정책의
	// backstop 이 먼저 반영되고 뒤따르는 구체적인 정책의 detect 가 그 위를 덮어써서
	// 천장이 올라갔다. backstop 의 존재 이유가 "위임해도 우회는 못 한다"인데
	// 정확히 그게 뚫렸다 — TestBackstopCannotBeRaisedByAMoreSpecificPolicy 가 고정한다.
	var backstops []*argusv1alpha1.BackstopSpec
	for i := range policies {
		mergeInto(&out, &policies[i])
		if b := policies[i].Spec.Backstop; b != nil {
			backstops = append(backstops, b)
		}
		out.SourcePolicies = append(out.SourcePolicies,
			policies[i].Namespace+"/"+policies[i].Name)
	}
	for _, b := range backstops {
		applyBackstop(&out, b)
	}
	active := make([]string, 0, len(out.Axes)+1)
	for name, a := range out.Axes {
		if a.Hard > 0 || a.Soft > 0 {
			active = append(active, name)
		}
	}
	// 비용 축은 Axes 맵이 아니라 별도 키다 (탭 계약 budget_usd_soft/hard). 활성 목록엔 같이 싣는다 —
	// verify-cluster.sh 가 이 목록으로 "무엇이 켜져 있나"를 보여주기 때문이다.
	if out.BudgetUSDSoft != "" || out.BudgetUSDHard != "" {
		active = append(active, "budget_usd")
	}
	sort.Strings(active)
	return out, active
}

func mergeInto(out *renderedPolicy, p *argusv1alpha1.RunawayPolicy) {
	s := p.Spec
	if s.ControlLevel != "" {
		out.ControlLevel = s.ControlLevel
	}
	if s.Window != nil && s.Window.Duration > 0 {
		out.WindowS = s.Window.Duration.Seconds()
	}
	if s.Cooldown != nil && s.Cooldown.Duration > 0 {
		out.CooldownS = s.Cooldown.Duration.Seconds()
	}
	if d := s.Detect; d != nil {
		setAxis(out.Axes, "duplicate_calls", d.DuplicateCalls)
		setAxis(out.Axes, "principal_calls", d.PrincipalCalls)
		setAxis(out.Axes, "run_steps", d.RunSteps)
		setRatioAxis(out.Axes, "rotation_gap", d.RotationGap)
		setBudgetAxis(out, d.BudgetUSD)
		setAxis(out.Axes, "msg_bytes", d.MessageBytes)
		setRatioAxis(out.Axes, "conv_growth", d.ConversationGrowth)
	}
	// Deprecated stepCap → runSteps.hard. 이미 배포된 CR 이 이 필드를 쓰고 있다.
	if s.StepCap != nil && *s.StepCap > 0 {
		a := out.Axes["run_steps"]
		if a.Hard == 0 {
			a.Hard = float64(*s.StepCap)
			out.Axes["run_steps"] = a
		}
	}
	// identity — 더 구체적인 정책이 이긴다. backstop 과 달리 "낮추기만" 규칙이 없다:
	// 신원은 안전 천장이 아니라 **환경 사실**(어떤 클라이언트가 들어오는가)의 선언이고,
	// 그 사실은 대상이 좁을수록 정확하다.
	if id := s.Identity; id != nil {
		if len(id.ConversationHeaders) > 0 {
			out.Identity.ConversationHeaders = append([]string(nil), id.ConversationHeaders...)
		}
		if id.TrustGatewaySession != nil {
			out.Identity.TrustGatewaySession = *id.TrustGatewaySession
		}
		if id.OnUnknownConversation != "" {
			out.Identity.OnUnknownConversation = id.OnUnknownConversation
		}
	}
}

// applyBackstop — **backstop 은 낮추기만 된다.** 더 구체적인 정책이 천장을 올려
// 빠져나가는 것을 막는다. render 가 모든 병합을 마친 뒤에만 부른다.
func applyBackstop(out *renderedPolicy, b *argusv1alpha1.BackstopSpec) {
	if b.PrincipalCalls != nil && *b.PrincipalCalls > 0 {
		a := out.Axes["principal_calls"]
		v := float64(*b.PrincipalCalls)
		if a.Hard == 0 || v < a.Hard {
			a.Hard = v
			out.Axes["principal_calls"] = a
		}
	}
	if b.TeamCalls != nil && *b.TeamCalls > 0 {
		a := out.Axes["team_calls"] // backstop 전용 축 — detect 에는 없다. hard 만.
		v := float64(*b.TeamCalls)
		if a.Hard == 0 || v < a.Hard {
			a.Hard = v
			out.Axes["team_calls"] = a
		}
	}
	if b.BudgetUSD != "" {
		if out.BudgetUSDHard == "" || lessUSD(b.BudgetUSD, out.BudgetUSDHard) {
			out.BudgetUSDHard = b.BudgetUSD
		}
	}
	if b.FailureMode != "" {
		out.BackstopFail = b.FailureMode
	}
}

func setAxis(m map[string]axisOut, name string, a *argusv1alpha1.AxisSpec) {
	if a == nil {
		return
	}
	// enabled: false — 임계 숫자는 CR 에 보존한 채 축만 끈다. 렌더에서 빼면
	// 엔진 입장에선 임계 0 = 비활성과 같다. 엔진 코드는 이 스위치를 모른다 —
	// 선언의 의미론은 렌더에서 끝나야 파이썬↔Go 동등성이 유지된다.
	if a.Enabled != nil && !*a.Enabled {
		delete(m, name)
		return
	}
	cur := m[name]
	if a.Soft != nil {
		cur.Soft = float64(*a.Soft)
	}
	if a.Hard != nil {
		cur.Hard = float64(*a.Hard)
	}
	m[name] = cur
}

func setRatioAxis(m map[string]axisOut, name string, a *argusv1alpha1.RatioAxisSpec) {
	if a == nil {
		return
	}
	if a.Enabled != nil && !*a.Enabled {
		delete(m, name)
		return
	}
	cur := m[name]
	if v, err := strconv.ParseFloat(a.Soft, 64); err == nil {
		cur.Soft = v
	}
	if v, err := strconv.ParseFloat(a.Hard, 64); err == nil {
		cur.Hard = v
	}
	m[name] = cur
}

// setBudgetAxis — detect.budgetUSD. hard 는 뒤에서 applyBackstop 이 더 낮은 backstop 으로 덮을 수 있다.
func setBudgetAxis(out *renderedPolicy, a *argusv1alpha1.BudgetAxisSpec) {
	if a == nil {
		return
	}
	if a.Enabled != nil && !*a.Enabled {
		out.BudgetUSDSoft, out.BudgetUSDHard = "", ""
		return
	}
	if a.Soft != "" {
		out.BudgetUSDSoft = a.Soft
	}
	if a.Hard != "" {
		out.BudgetUSDHard = a.Hard
	}
}

func lessUSD(a, b string) bool {
	fa, ea := strconv.ParseFloat(a, 64)
	fb, eb := strconv.ParseFloat(b, 64)
	if ea != nil || eb != nil {
		return false
	}
	return fa < fb
}

func (r *RunawayPolicyReconciler) writeConfigMap(ctx context.Context, p renderedPolicy) error {
	blob, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return err
	}
	cm := &corev1.ConfigMap{}
	key := types.NamespacedName{Name: PolicyConfigMapName, Namespace: r.TapNamespace}
	err = r.Get(ctx, key, cm)
	if apierrors.IsNotFound(err) {
		cm = &corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{
				Name:      PolicyConfigMapName,
				Namespace: r.TapNamespace,
				Labels: map[string]string{
					"app.kubernetes.io/managed-by": "argus-operator",
					"app.kubernetes.io/component":  "policy",
				},
			},
			Data: map[string]string{PolicyConfigMapKey: string(blob)},
		}
		return r.Create(ctx, cm)
	}
	if err != nil {
		return err
	}
	if cm.Data[PolicyConfigMapKey] == string(blob) {
		return nil // 내용이 같으면 쓰지 않는다 — 불필요한 롤아웃·이벤트를 만들지 않는다
	}
	if cm.Data == nil {
		cm.Data = map[string]string{}
	}
	cm.Data[PolicyConfigMapKey] = string(blob)
	return r.Update(ctx, cm)
}

// setNotAttached — 이 탭에 붙지 않는 정책임을 CR 자신에게 적는다.
//
// 지금 붙는 것은 targetRefs 가 비었거나(자기 네임스페이스) Kind=Namespace 인 경우뿐이다.
// Deployment·HTTPRoute 같은 더 좁은 부착은 배선이 없어 **적용되지 않는데**, 그걸 조용히
// 넘기면 사용자는 선언한 임계가 도는 줄 안다. 빈 status 는 "컨트롤러가 죽었다"로도
// 읽히므로 더 나쁘다 — 못 붙였다는 사실 자체를 신호로 만든다.
func (r *RunawayPolicyReconciler) setNotAttached(
	ctx context.Context, p *argusv1alpha1.RunawayPolicy, reason, msg string,
) error {
	now := metav1.Now()
	for _, t := range []struct {
		typ, reason string
	}{{condAccepted, reason}, {condProgrammed, reason}} {
		found := false
		for i := range p.Status.Conditions {
			c := &p.Status.Conditions[i]
			if c.Type != t.typ {
				continue
			}
			if c.Status != metav1.ConditionFalse {
				c.LastTransitionTime = now
			}
			c.Status, c.Reason, c.Message, c.ObservedGeneration =
				metav1.ConditionFalse, t.reason, msg, p.Generation
			found = true
			break
		}
		if !found {
			p.Status.Conditions = append(p.Status.Conditions, metav1.Condition{
				Type: t.typ, Status: metav1.ConditionFalse, Reason: t.reason, Message: msg,
				LastTransitionTime: now, ObservedGeneration: p.Generation,
			})
		}
	}
	p.Status.ObservedGeneration = p.Generation
	p.Status.ActiveAxes = nil
	p.Status.RenderedConfigMap = ""
	return r.Status().Update(ctx, p)
}

func (r *RunawayPolicyReconciler) setConditions(
	ctx context.Context, p *argusv1alpha1.RunawayPolicy,
	accepted, programmed bool, active []string, renderErr error,
) error {
	now := metav1.Now()
	set := func(t string, ok bool, reason, msg string) {
		st := metav1.ConditionFalse
		if ok {
			st = metav1.ConditionTrue
		}
		for i := range p.Status.Conditions {
			c := &p.Status.Conditions[i]
			if c.Type != t {
				continue
			}
			if c.Status != st {
				c.LastTransitionTime = now
			}
			c.Status, c.Reason, c.Message, c.ObservedGeneration = st, reason, msg, p.Generation
			return
		}
		p.Status.Conditions = append(p.Status.Conditions, metav1.Condition{
			Type: t, Status: st, Reason: reason, Message: msg,
			LastTransitionTime: now, ObservedGeneration: p.Generation,
		})
	}

	set(condAccepted, accepted, "Parsed", "컨트롤러가 정책을 받아 파싱했다")
	if renderErr != nil {
		set(condProgrammed, false, "RenderFailed", renderErr.Error())
	} else if len(active) == 0 {
		// 렌더는 됐지만 켜진 축이 없다 = 아무것도 탐지하지 않는다. Programmed 로 보면 안 된다.
		set(condProgrammed, false, "NoActiveAxes",
			"렌더는 됐지만 활성 축이 없다 — 이 정책은 아무것도 탐지하지 않는다")
	} else {
		set(condProgrammed, programmed, "Rendered",
			fmt.Sprintf("탭이 읽을 정책으로 렌더됨. 활성 축: %v", active))
	}

	p.Status.ObservedGeneration = p.Generation
	p.Status.ActiveAxes = active
	p.Status.RenderedConfigMap = r.TapNamespace + "/" + PolicyConfigMapName
	return r.Status().Update(ctx, p)
}

// SetupWithManager registers the controller.
func (r *RunawayPolicyReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.TapNamespace == "" {
		r.TapNamespace = os.Getenv("ARGUS_TAP_NAMESPACE")
	}
	if r.TapNamespace == "" {
		r.TapNamespace = os.Getenv("POD_NAMESPACE")
	}
	if r.TapNamespace == "" {
		r.TapNamespace = "argus-system"
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&argusv1alpha1.RunawayPolicy{}).
		// 렌더된 ConfigMap 이 손으로 지워지거나 바뀌면 다시 맞춘다.
		Watches(&corev1.ConfigMap{}, handler.EnqueueRequestsFromMapFunc(
			func(ctx context.Context, o client.Object) []ctrl.Request {
				if o.GetName() != PolicyConfigMapName || o.GetNamespace() != r.TapNamespace {
					return nil
				}
				return []ctrl.Request{{}}
			})).
		Named("runawaypolicy").
		Complete(r)
}
