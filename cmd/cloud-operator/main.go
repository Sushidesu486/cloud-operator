package main

import (
	"flag"
	"fmt"
	cloud "github.com/Sushidesu486/cloud-operator/api/v1alpha1"
	"github.com/Sushidesu486/cloud-operator/internal/controller"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"os"
	"path/filepath"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metrics "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

func main() {
	var ns string
	var options controller.RunnerOptions
	flag.StringVar(&ns, "namespace", "", "Required: exactly one development namespace")
	flag.StringVar(&options.Image, "runner-image", "", "Pinned development container image")
	flag.StringVar(&options.Node, "node", "", "Allowed development node hostname")
	flag.StringVar(&options.Assets, "assets", "", "Read-only node path containing development assets")
	flag.BoolVar(&options.Development, "development", false, "Explicitly enable privileged experimental launcher Pods")
	flag.BoolVar(&options.NetworkNAT, "network-nat", false, "Enable Pod-local VM NAT and SSH readiness")
	flag.Parse()
	if ns == "" || ns == "default" || ns == "kube-system" || !options.Development || !filepath.IsAbs(options.Assets) || options.Assets == "/" {
		fmt.Fprintln(os.Stderr, "requires an isolated --namespace, --development and an absolute --assets directory")
		os.Exit(2)
	}
	ctrl.SetLogger(zap.New())
	scheme := runtime.NewScheme()
	must(corev1.AddToScheme(scheme))
	must(cloud.AddToScheme(scheme))
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{Scheme: scheme,
		Cache:   cache.Options{DefaultNamespaces: map[string]cache.Config{ns: {}}},
		Metrics: metrics.Options{BindAddress: "0"}, HealthProbeBindAddress: "0",
		LeaderElection: true, LeaderElectionNamespace: ns, LeaderElectionID: "cloud-operator", LeaderElectionReleaseOnCancel: true,
	})
	must(err)
	must((&controller.Reconciler{Client: mgr.GetClient(), Runner: options}).SetupWithManager(mgr))
	must(mgr.Start(ctrl.SetupSignalHandler()))
}
func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
