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

package main

import (
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"slices"
	"time"

	// Import all Kubernetes client auth plugins (e.g. Azure, GCP, OIDC, etc.)
	// to ensure that exec-entrypoint and run can make use of them.
	_ "k8s.io/client-go/plugin/pkg/client/auth"

	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/metrics/filters"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	argusv1alpha1 "github.com/stp-argus/argus/api/v1alpha1"
	"github.com/stp-argus/argus/internal/controller"
	"github.com/stp-argus/argus/internal/decide"
	// +kubebuilder:scaffold:imports
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))

	utilruntime.Must(argusv1alpha1.AddToScheme(scheme))
	// +kubebuilder:scaffold:scheme
}

// nolint:gocyclo
func main() {
	var metricsAddr string
	var metricsCertPath, metricsCertName, metricsCertKey string
	var webhookCertPath, webhookCertName, webhookCertKey string
	var enableLeaderElection bool
	var probeAddr string
	var secureMetrics bool
	var enableHTTP2 bool
	var tlsOpts []func(*tls.Config)
	flag.StringVar(&metricsAddr, "metrics-bind-address", "0", "The address the metrics endpoint binds to. "+
		"Use :8443 for HTTPS or :8080 for HTTP, or leave as 0 to disable the metrics service.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager. "+
			"Enabling this will ensure there is only one active controller manager.")
	flag.BoolVar(&secureMetrics, "metrics-secure", true,
		"If set, the metrics endpoint is served securely via HTTPS. Use --metrics-secure=false to use HTTP instead.")
	flag.StringVar(&webhookCertPath, "webhook-cert-path", "", "The directory that contains the webhook certificate.")
	flag.StringVar(&webhookCertName, "webhook-cert-name", "tls.crt", "The name of the webhook certificate file.")
	flag.StringVar(&webhookCertKey, "webhook-cert-key", "tls.key", "The name of the webhook key file.")
	flag.StringVar(&metricsCertPath, "metrics-cert-path", "",
		"The directory that contains the metrics server certificate.")
	flag.StringVar(&metricsCertName, "metrics-cert-name", "tls.crt", "The name of the metrics server certificate file.")
	flag.StringVar(&metricsCertKey, "metrics-cert-key", "tls.key", "The name of the metrics server key file.")
	flag.BoolVar(&enableHTTP2, "enable-http2", false,
		"If set, HTTP/2 will be enabled for the metrics and webhook servers")
	opts := zap.Options{
		Development: true,
	}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	// if the enable-http2 flag is false (the default), http/2 should be disabled
	// due to its vulnerabilities. More specifically, disabling http/2 will
	// prevent from being vulnerable to the HTTP/2 Stream Cancellation and
	// Rapid Reset CVEs. For more information see:
	// - https://github.com/advisories/GHSA-qppj-fm5r-hxr3
	// - https://github.com/advisories/GHSA-4374-p667-p6c8
	disableHTTP2 := func(c *tls.Config) {
		setupLog.Info("Disabling HTTP/2")
		c.NextProtos = []string{"http/1.1"}
	}

	if !enableHTTP2 {
		tlsOpts = append(tlsOpts, disableHTTP2)
	}

	// Initial webhook TLS options
	webhookTLSOpts := tlsOpts
	webhookServerOptions := webhook.Options{
		TLSOpts: webhookTLSOpts,
	}

	if len(webhookCertPath) > 0 {
		setupLog.Info("Initializing webhook certificate watcher using provided certificates",
			"webhook-cert-path", webhookCertPath, "webhook-cert-name", webhookCertName, "webhook-cert-key", webhookCertKey)

		webhookServerOptions.CertDir = webhookCertPath
		webhookServerOptions.CertName = webhookCertName
		webhookServerOptions.KeyName = webhookCertKey
	}

	webhookServer := webhook.NewServer(webhookServerOptions)

	// Metrics endpoint is enabled in 'config/default/kustomization.yaml'. The Metrics options configure the server.
	// More info:
	// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.23.3/pkg/metrics/server
	// - https://book.kubebuilder.io/reference/metrics.html
	metricsServerOptions := metricsserver.Options{
		BindAddress:   metricsAddr,
		SecureServing: secureMetrics,
		TLSOpts:       tlsOpts,
	}

	if secureMetrics {
		// FilterProvider is used to protect the metrics endpoint with authn/authz.
		// These configurations ensure that only authorized users and service accounts
		// can access the metrics endpoint. The RBAC are configured in 'config/rbac/kustomization.yaml'. More info:
		// https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.23.3/pkg/metrics/filters#WithAuthenticationAndAuthorization
		metricsServerOptions.FilterProvider = filters.WithAuthenticationAndAuthorization
	}

	// If the certificate is not specified, controller-runtime will automatically
	// generate self-signed certificates for the metrics server. While convenient for development and testing,
	// this setup is not recommended for production.
	//
	// TODO(user): If you enable certManager, uncomment the following lines:
	// - [METRICS-WITH-CERTS] at config/default/kustomization.yaml to generate and use certificates
	// managed by cert-manager for the metrics server.
	// - [PROMETHEUS-WITH-CERTS] at config/prometheus/kustomization.yaml for TLS certification.
	if len(metricsCertPath) > 0 {
		setupLog.Info("Initializing metrics certificate watcher using provided certificates",
			"metrics-cert-path", metricsCertPath, "metrics-cert-name", metricsCertName, "metrics-cert-key", metricsCertKey)

		metricsServerOptions.CertDir = metricsCertPath
		metricsServerOptions.CertName = metricsCertName
		metricsServerOptions.KeyName = metricsCertKey
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsServerOptions,
		WebhookServer:          webhookServer,
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "c2c5488d.stp.io",
		// LeaderElectionReleaseOnCancel defines if the leader should step down voluntarily
		// when the Manager ends. This requires the binary to immediately end when the
		// Manager is stopped, otherwise, this setting is unsafe. Setting this significantly
		// speeds up voluntary leader transitions as the new leader don't have to wait
		// LeaseDuration time first.
		//
		// In the default scaffold provided, the program ends immediately after
		// the manager stops, so would be fine to enable this option. However,
		// if you are doing or is intended to do any operation such as perform cleanups
		// after the manager stops then its usage might be unsafe.
		// LeaderElectionReleaseOnCancel: true,
	})
	if err != nil {
		setupLog.Error(err, "Failed to start manager")
		os.Exit(1)
	}

	// 판정 엔진 + /v1/decide (spec 006b). 판정이 여기로 옮겨왔다 — 탭은 신호를 보내고
	// 집행만 한다. 컨트롤러가 CR 을 렌더하면 이 엔진에 즉시 반영된다(kubelet 지연 없음).
	// 정책이 오기 전에는 DefaultPolicy = Observe·전 축 비활성이라 아무것도 차단하지 않는다.
	engine := decide.New(decide.DefaultPolicy())
	decideLog := ctrl.Log.WithName("decide")
	decideSrv := decide.NewServer(engine, func(format string, args ...any) {
		decideLog.Info(fmt.Sprintf(format, args...))
	})
	decideAddr := os.Getenv("ARGUS_DECIDE_ADDR")
	if decideAddr == "" {
		decideAddr = ":8080" // Service argus:8080 이 이미 이 포트를 가리킨다 (spec 002 FR-005)
	}
	// 호출자 인증 — 허용 SA 를 주면 켠다. 차트는 같은 릴리스의 LiteLLM SA 를 기본으로 넣는다.
	allowed, err := decide.ParseAllowedSAs(os.Getenv("ARGUS_DECIDE_ALLOWED_SA"))
	if err != nil {
		setupLog.Error(err, "판정 API 인증 설정 오류 — 인증 없이 뜨지 않는다")
		os.Exit(1)
	}
	if len(allowed) > 0 {
		audience := os.Getenv("ARGUS_DECIDE_AUDIENCE")
		if audience == "" {
			audience = "argus-decide"
		}
		cl := mgr.GetClient()
		decideSrv.Auth = &decide.Authenticator{Allowed: allowed, Review: func(ctx context.Context, token string) (string, error) {
			tr := &authenticationv1.TokenReview{Spec: authenticationv1.TokenReviewSpec{
				Token: token, Audiences: []string{audience}}}
			if err := cl.Create(ctx, tr); err != nil {
				return "", err
			}
			if !tr.Status.Authenticated || !slices.Contains(tr.Status.Audiences, audience) {
				return "", decide.ErrUnauthenticated
			}
			return tr.Status.User.Username, nil
		}}
		setupLog.Info("판정 API 인증 켬", "audience", audience, "allowed", len(allowed))
	} else {
		setupLog.Info("판정 API 인증 꺼짐 — ARGUS_DECIDE_ALLOWED_SA 가 비어 있다")
	}
	decide.StartServer(decideAddr, decideSrv)
	setupLog.Info("판정 API 시작", "addr", decideAddr, "endpoint", "/v1/decide")

	// RunawayPolicy 컨트롤러 — CR 을 탭이 읽는 정책으로 렌더한다.
	// 이게 없으면 매니저는 뜨지만 등록된 컨트롤러가 없어 아무 이벤트도 처리하지 않는다
	// (실제로 12일간 그 상태였다. status 가 비어 있는 것이 그 증거였다).
	reconciler := &controller.RunawayPolicyReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
		Engine: engine,
	}
	if err := reconciler.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "컨트롤러 등록 실패", "controller", "RunawayPolicy")
		os.Exit(1)
	}
	// 첫 렌더 — CR 이 하나도 없으면 이벤트가 없어 Reconcile 이 안 돌고, readyz 가 영영 실패한다.
	// Reconcile 은 요청과 무관하게 전체를 렌더하므로 빈 요청으로 한 번 부르면 된다.
	// 리더 선출을 켜도 모든 파드가 돈다 — 리더만 돌면 리더가 아닌 새 파드는 영영 Ready 가 안 돼 롤아웃이 멈춘다.
	if err := mgr.Add(nonLeaderRunnable(func(ctx context.Context) error {
		if !mgr.GetCache().WaitForCacheSync(ctx) {
			return nil
		}
		for !engine.Loaded() {
			if _, err := reconciler.Reconcile(ctx, ctrl.Request{}); err != nil {
				setupLog.Error(err, "첫 정책 렌더 실패 — 다시 시도한다")
			}
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(2 * time.Second):
			}
		}
		return nil
	})); err != nil {
		setupLog.Error(err, "첫 정책 렌더 등록 실패")
		os.Exit(1)
	}
	// +kubebuilder:scaffold:builder

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "Failed to set up health check")
		os.Exit(1)
	}
	// Ready 는 정책을 렌더한 뒤다. 그 전에 Service 에 붙으면 롤아웃 중 새 파드가 DefaultPolicy(Observe)로 판정한다.
	if err := mgr.AddReadyzCheck("readyz", func(*http.Request) error {
		if !engine.Loaded() {
			return errors.New("정책 렌더 전")
		}
		return nil
	}); err != nil {
		setupLog.Error(err, "Failed to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("Starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "Failed to run manager")
		os.Exit(1)
	}
}

// nonLeaderRunnable — 리더 선출과 무관하게 모든 파드에서 도는 Runnable.
type nonLeaderRunnable func(context.Context) error

func (f nonLeaderRunnable) Start(ctx context.Context) error { return f(ctx) }

func (nonLeaderRunnable) NeedLeaderElection() bool { return false }
