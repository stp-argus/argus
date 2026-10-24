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

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PolicyTargetReference — GEP-713 Policy Attachment 의 부착 대상.
//
// 설계 정본(§6)은 gateway-api 의 LocalPolicyTargetReferenceWithSectionName 을 쓴다고 적어놨다.
// 여기서는 같은 모양의 로컬 타입을 둔다 — gateway-api 의존을 지금 끌어오면 계약 ① 비준 전에
// API 표면이 그쪽 버전에 묶인다. 비준 시 이 타입을 그대로 교체하면 된다(필드 이름이 같다).
type PolicyTargetReference struct {
	// group 은 대상의 API 그룹. 코어 리소스(Namespace 등)는 빈 문자열.
	// +optional
	Group string `json:"group,omitempty"`

	// kind 는 대상 종류. Namespace 면 상속(GEP-2649), HTTPRoute 면 직접 부착(GEP-2648).
	// +required
	Kind string `json:"kind"`

	// +required
	Name string `json:"name"`
}

// AxisSpec — 개수를 세는 탐지축의 임계.
//
// **soft 하나만으로는 차단하지 않는다.** 기본 차단 조건은 hard 1개다.
// 근거: 임계는 정상 트래픽 29콜(합성 대조군 하나)에서 도출됐다. 표본이 작아
// 두 축이 독립적으로 틀리는 게 아니라 같이 과소평가돼 있을 수 있다.
// 상세는 docs/thresholds.md.
//
// **0 또는 미설정 = 그 축 비활성.** 측정으로 분리력을 증명하지 못한 축은 켜지 않는다.
type AxisSpec struct {
	// enabled — 임계 숫자를 지우지 않고 축만 끈다.
	//
	// 임계값은 실측으로 도출한 **근거**라, 축을 끄려고 숫자를 지우게 하면 근거가
	// 같이 사라진다. helm values 의 `*.enabled` 와 같은 결 — 값은 보존, 스위치만 내린다.
	// 생략 = true.
	// +kubebuilder:default=true
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// +kubebuilder:validation:Minimum=0
	// +optional
	Soft *int32 `json:"soft,omitempty"`

	// +kubebuilder:validation:Minimum=0
	// +optional
	Hard *int32 `json:"hard,omitempty"`
}

// RatioAxisSpec — 비율 축의 임계. 값은 문자열이다.
//
// float64 는 쿠버네티스 API 규약이 권하지 않고(직렬화 왕복에서 정밀도가 흔들린다),
// resource.Quantity 는 `5.2` 가 `5200m` 으로 보이는 UX 함정이 있다 — 설계 정본이
// budgetUSD 를 문자열로 정한 것과 같은 이유다.
type RatioAxisSpec struct {
	// enabled — AxisSpec.Enabled 와 동일. 생략 = true.
	// +kubebuilder:default=true
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// +kubebuilder:validation:Pattern=`^\d+(\.\d{1,2})?$`
	// +optional
	Soft string `json:"soft,omitempty"`

	// +kubebuilder:validation:Pattern=`^\d+(\.\d{1,2})?$`
	// +optional
	Hard string `json:"hard,omitempty"`
}

// BudgetAxisSpec — 비용 축의 임계. 문자열인 이유는 RatioAxisSpec 과 같고, 소수 4자리인 이유는
// backstop.budgetUSD 와 같다 (실모델 호출 하나가 $0.0001 단위다 — 2자리면 센트 미만을 못 적는다).
//
// **이 값은 측정에서 도출하지 않는다.** 예산은 재는 게 아니라 정하는 것이다(폭주 분류 문서).
// 2026-09-07 실측에서 정상 코호트($0.00717)가 폭주($0.00125)보다 비쌌다 — 비용 축 단독으로는
// 폭주와 정상이 갈리지 않는다. 그래서 soft 는 다른 축과 함께 볼 때만 의미가 있다.
type BudgetAxisSpec struct {
	// enabled — AxisSpec.Enabled 와 동일. 생략 = true.
	// +kubebuilder:default=true
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// +kubebuilder:validation:Pattern=`^\d+(\.\d{1,4})?$`
	// +optional
	Soft string `json:"soft,omitempty"`

	// +kubebuilder:validation:Pattern=`^\d+(\.\d{1,4})?$`
	// +optional
	Hard string `json:"hard,omitempty"`
}

// DetectSpec — 탐지축들. 현행 임계와 재도출 근거는 DETECTION.ko.md §5 (thresholds.md 는 08-12 최초 기록).
type DetectSpec struct {
	// duplicateCalls — 같은 대화 안에서 동일 (도구, 인자값) 지문이 반복된 횟수.
	//
	// **대화 전체를 해싱하면 안 된다.** 폭주 대화는 스텝마다 자라서 매번 다른 해시가 나온다.
	// 도구 지문은 그 성장에 오염되지 않는다.
	// 실측(2026-09-20 재도출): 정상 최대 3 · soft 5 / hard 20 · 탐지 핑퐁 92%·재시도 87% · 오탐 0%.
	// +optional
	DuplicateCalls *AxisSpec `json:"duplicateCalls,omitempty"`

	// principalCalls — principal 별 윈도우 누적 호출. 핑퐁 재귀의 backstop.
	//
	// 실측(2026-09-07 도출, 09-20 재확인 정상 최대 12): 현행 soft 17 / hard 19 · 오탐 0%.
	// ⚠️ **호출 간격에 의존한다.** 대조군의 간격은 설계값이라 이 임계는 부분적으로 설계다.
	// 실사용 트래픽으로 재측정이 필요하다.
	// +optional
	PrincipalCalls *AxisSpec `json:"principalCalls,omitempty"`

	// runSteps — run 하나의 호출 수.
	//
	// ⚠️ **핑퐁 재귀에 탐지율 0% 다.** runId 를 회전시키면 카운터가 매번 리셋돼
	// 정상의 최대값(5)을 넘지 못한다. 이 축을 단독으로 쓰면 핑퐁을 통째로 놓친다.
	// 제품이 principal 축을 필요로 하는 이유가 이 숫자다.
	// +optional
	RunSteps *AxisSpec `json:"runSteps,omitempty"`

	// rotationGap — principal 누적 ÷ run 당 최대. run 을 잘게 쪼갤수록 커진다.
	//
	// per-run 상한을 피하려는 시도 자체가 신호가 된다.
	// 실측: 정상 최대 2 · 폭주 5.8. run 이 하나뿐이면 정의상 1.0 이라 신호가 아니다.
	// +optional
	RotationGap *RatioAxisSpec `json:"rotationGap,omitempty"`

	// budgetUSD — principal 누적 지출. 엔진은 8/25 부터 이 축을 세고 있었는데 선언 표면이 없어
	// hard 는 backstop.budgetUSD 로만, soft 는 어디서도 켤 수 없었다 (2차 검증 피드백
	// "Agent 단위 비용 항목이 거버넌스 정책에 필요", Linear STP-14).
	// backstop.budgetUSD 는 여전히 천장이다 — 여기 hard 가 backstop 보다 크면 backstop 이 이긴다.
	// +optional
	BudgetUSD *BudgetAxisSpec `json:"budgetUSD,omitempty"`

	// messageBytes — 한 호출의 메시지 크기(바이트). 단일 초대형 호출 (G4, Linear STP-19).
	// ⚠️ 임계는 실트래픽에서 도출 전까지 비운다. 정상 사용에도 큰 호출은 있다 — soft 부터.
	// +optional
	MessageBytes *AxisSpec `json:"messageBytes,omitempty"`

	// conversationGrowth — 이번 호출 크기 ÷ 그 대화의 첫 호출 크기. 호출 수는 적은데 매 턴 부푸는
	// 폭주 (G3, STP-19) — 컨텍스트 누적은 비용을 O(N²) 로 키우는데 호출 카운터는 조용하다.
	// ⚠️ 위와 같은 이유로 임계는 도출 전까지 비운다.
	// +optional
	ConversationGrowth *RatioAxisSpec `json:"conversationGrowth,omitempty"`
}

// BackstopSpec — 덮어쓸 수 없는 천장 (설계 §6).
//
// 하위(더 구체적인 대상)의 정책은 이 값을 **낮추기만** 할 수 있다.
// 그러지 않으면 "자식 라우트가 상한을 올려버리는" 회피가 생긴다 —
// 앱 팀이 자기 라우트에 큰 값을 선언하면 플랫폼 정책이 무력화된다.
type BackstopSpec struct {
	// +kubebuilder:validation:Minimum=1
	// +optional
	PrincipalCalls *int32 `json:"principalCalls,omitempty"`

	// teamCalls — 팀(가상 키의 team_id) 단위 창 누적 호출 상한 (G2 identity exhaustion 의 부분 해소, STP-16).
	// 주체를 여러 개 **합법적으로** 획득한 공격(키 다중 발급·SA 다중 보유)은 사다리로 못 막는다 —
	// 상위 묶음이 있어야 잡힌다. 팀이 없는 호출(header/api_key 티어)엔 평가하지 않는다.
	// +kubebuilder:validation:Minimum=1
	// +optional
	TeamCalls *int32 `json:"teamCalls,omitempty"`

	// budgetUSD — principal 누적 지출 상한. 문자열인 이유는 RatioAxisSpec 주석과 같다.
	//
	// **이 값은 측정에서 도출하지 않는다.** 상한 자체가 정책이다(폭주 분류 문서).
	// 시험 데이터로 뽑으면 $0.0009 같은 값이 나오고, 켜면 정상 사용자가 막힌다.
	// +kubebuilder:validation:Pattern=`^\d+(\.\d{1,4})?$`
	// +optional
	BudgetUSD string `json:"budgetUSD,omitempty"`

	// failureMode — backstop 은 전역 기본값과 **독립적으로** fail-closed 가 될 수 있다.
	// +kubebuilder:validation:Enum=Open;Closed
	// +kubebuilder:default=Open
	// +optional
	FailureMode string `json:"failureMode,omitempty"`
}

// IdentitySpec — 대화 축을 **무엇에서** 얻을 것인가 (설계 §6 identity).
//
// 이 블록은 설계에 처음부터 있었지만 "근거 있는 필드만 연다"는 규칙 때문에 닫아두었다.
// 2026-08-16 조사가 근거를 만들었다 — 근거표는
// docs/research/2026-08-16_competitor-and-session-protocols.md.
//
// 왜 정책이어야 하나: 대화 축을 어디서 얻을지는 **클러스터에 어떤 클라이언트가 들어오는가**에
// 달렸고, 그건 운영자가 아는 사실이지 우리가 코드에 박을 사실이 아니다. 실측으로 확인된 차이:
//
//	Claude Code  x-claude-code-session-id  → LiteLLM 이 이미 정규화해 준다
//	Codex CLI    session-id / thread-id    → LiteLLM 정규식에 안 걸린다 (직접 봐야 한다)
//
// 와이어 파싱(도구 호출이 본문 어디에 있는가)은 여기 없다 — 그건 프로토콜 어댑터지 정책이 아니다.
type IdentitySpec struct {
	// conversationHeaders — 대화 축으로 쓸 헤더. **순서가 우선순위다.**
	// 비우면 내장 기본 사다리(Claude Code → Codex → 일반)를 쓴다.
	//
	// 사내 에이전트가 자체 헤더(`x-acme-session`)를 쓴다면 여기 한 줄로 붙인다 —
	// 가드레일 파이썬을 고칠 일이 아니다.
	// +optional
	ConversationHeaders []string `json:"conversationHeaders,omitempty"`

	// trustGatewaySession — 게이트웨이가 이미 정규화한 세션을 신뢰할 것인가.
	//
	// LiteLLM 은 `x-<vendor>-session-id` 를 훑어 `litellm_session_id` 를 채워 준다.
	// 우리가 열거하지 못한 벤더까지 덮어 주므로 기본은 **신뢰(true)** 다.
	// 끄는 경우: 그 로직이 `x-litellm-trace-id` 를 최우선으로 쓰는데, trace 를 요청마다
	// 새로 발급하는 클라이언트가 있으면 대화가 매 호출 쪼개진다. 그때 끄고 헤더를 직접 지정한다.
	// +kubebuilder:default=true
	// +optional
	TrustGatewaySession *bool `json:"trustGatewaySession,omitempty"`

	// onUnknownConversation — 대화 축을 끝내 못 정했을 때.
	//
	//	Skip   (기본) 대화 스코프 축(duplicate_calls)을 **평가하지 않는다.**
	//	              모르는 것을 세지 않는다 — 가짜 버킷에 넣으면 무관한 작업이
	//	              서로의 반복으로 세어져 오탐이 된다.
	//	Derive        (principal, model, 첫 메시지)로 파생한다. 대화가 짧고 시작 메시지가
	//	              충분히 다른 환경에서만 켠다.
	// +kubebuilder:validation:Enum=Skip;Derive
	// +kubebuilder:default=Skip
	// +optional
	OnUnknownConversation string `json:"onUnknownConversation,omitempty"`
}

// RunawayPolicySpec defines the desired state of RunawayPolicy.
//
// 형태는 설계 정본 docs/design/2026-06-13_v0.1-design.md §6 (GEP-713 Policy Attachment)를 따른다.
// 다만 **실측 근거가 있는 축만** 필드로 열었다 — 근거 없는 필드를 미리 열면 API 표면만 넓어지고,
// 쿠버네티스 API 는 한 번 나가면 되돌리기 어렵다.
//
// 아직 열지 않은 것 (설계에는 있으나 근거 부족): evasion · scope ·
// inputHashDedup 의 세부 옵션. 측정 근거가 생기면 연다.
// `identity` 는 2026-08-16 실측 조사로 근거가 생겨 열었다.
type RunawayPolicySpec struct {
	// enabled — 정책 전체를 **지우지 않고** 끈다. 생략 = true.
	//
	// CR 을 지우면 GitOps diff 와 함께 선언의 역사가 사라진다. 잠깐 내리고 싶은 것뿐이라면
	// (사고 조사, 데모 준비, 임계 재측정) 스위치가 맞다. 꺼진 정책은 status 에
	// `Programmed=False reason=Disabled` 로 정직하게 남는다 — 조용히 무시되지 않는다.
	// +kubebuilder:default=true
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// targetRefs — 이 정책이 붙는 대상. 비어 있으면 **자기 네임스페이스 전체**에 적용된다.
	//
	// Namespace 를 가리키면 상속(GEP-2649), HTTPRoute 등을 가리키면 직접 부착(GEP-2648).
	// +optional
	// +listType=atomic
	TargetRefs []PolicyTargetReference `json:"targetRefs,omitempty"`

	// controlLevel — 단계적 적용 사다리. **기본값이 Observe 인 것이 중요하다.**
	//
	// 쿠버네티스 정책 생태계(Gatekeeper/Kyverno)의 합의가 audit → enforce 이고,
	// "갑작스러운 강제가 플랫폼 팀이 앱 팀의 신뢰를 잃는 방식"이라는 경고가 그 근거다.
	//   Observe — 판정은 돌지만 통과. would_block 만 기록
	//   Guard   — 사용량·비용(volume·cost) 축 hard 만 차단, 행동 축은 기록만.
	//             backstop 상한뿐 아니라 detect.principalCalls hard 도 volume 이라 막힌다
	//   Enforce — 모든 축 hard 차단
	// +kubebuilder:validation:Enum=Observe;Guard;Enforce
	// +kubebuilder:default=Observe
	// +optional
	ControlLevel string `json:"controlLevel,omitempty"`

	// window — 슬라이딩 윈도우 크기. 고정 윈도우는 경계에서 2배 버스트를 허용한다.
	// +kubebuilder:default="60s"
	// +optional
	Window *metav1.Duration `json:"window,omitempty"`

	// cooldown — 한 번 걸린 principal 을 유지하는 시간 (서킷 브레이커식 히스테리시스).
	// 윈도우가 미끄러지며 걸렸다 풀렸다 하는 플래핑을 막는다.
	// +kubebuilder:default="30s"
	// +optional
	Cooldown *metav1.Duration `json:"cooldown,omitempty"`

	// detect — 탐지축별 임계.
	// +optional
	Detect *DetectSpec `json:"detect,omitempty"`

	// backstop — 덮어쓸 수 없는 천장.
	// +optional
	Backstop *BackstopSpec `json:"backstop,omitempty"`

	// identity — 대화 축을 무엇에서 얻을 것인가. 비우면 내장 기본값.
	// +optional
	Identity *IdentitySpec `json:"identity,omitempty"`

	// stepCap — **Deprecated.** `detect.runSteps.hard` 를 쓴다.
	// 이미 배포된 CR 이 이 필드를 쓰고 있어 하위호환으로 남긴다. 설정되면 runSteps.hard 로 옮긴다.
	// +kubebuilder:validation:Minimum=1
	// +optional
	StepCap *int32 `json:"stepCap,omitempty"`
}

// RunawayPolicyStatus defines the observed state of RunawayPolicy.
type RunawayPolicyStatus struct {
	// INSERT ADDITIONAL STATUS FIELD - define observed state of cluster
	// Important: Run "make" to regenerate code after modifying this file

	// For Kubernetes API conventions, see:
	// https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#typical-status-properties

	// conditions represent the current state of the RunawayPolicy resource.
	// Each condition has a unique type and reflects the status of a specific aspect of the resource.
	//
	// Standard condition types include:
	// - "Available": the resource is fully functional
	// - "Progressing": the resource is being created or updated
	// - "Degraded": the resource failed to reach or maintain its desired state
	//
	// The status of each condition is one of True, False, or Unknown.
	//
	// ARGUS 가 쓰는 조건 타입:
	//   Accepted   — 컨트롤러가 이 정책을 받아 파싱했다 (GEP-713 규약)
	//   Programmed — 탭이 실제로 이 정책으로 돌고 있다 (GEP-713 규약)
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`

	// observedGeneration — 컨트롤러가 마지막으로 처리한 spec 세대.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// activeAxes — 실제로 켜진(임계가 0 이 아닌) 탐지축 이름.
	//
	// **이게 비어 있으면 이 정책은 아무것도 탐지하지 않는다.** 선언은 했는데 축이 다 꺼져 있는
	// 상태를 조용히 넘기지 않으려고 status 에 드러낸다.
	// +optional
	// +listType=atomic
	ActiveAxes []string `json:"activeAxes,omitempty"`

	// renderedConfigMap — 탭이 읽는 정책 파일을 담은 ConfigMap 이름.
	// +optional
	RenderedConfigMap string `json:"renderedConfigMap,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// RunawayPolicy is the Schema for the runawaypolicies API
type RunawayPolicy struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitzero"`

	// spec defines the desired state of RunawayPolicy
	// +required
	Spec RunawayPolicySpec `json:"spec"`

	// status defines the observed state of RunawayPolicy
	// +optional
	Status RunawayPolicyStatus `json:"status,omitzero"`
}

// +kubebuilder:object:root=true

// RunawayPolicyList contains a list of RunawayPolicy
type RunawayPolicyList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []RunawayPolicy `json:"items"`
}

func init() {
	SchemeBuilder.Register(&RunawayPolicy{}, &RunawayPolicyList{})
}
