package controller

import (
	"context"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"testing"
	cloud "github.com/Sushidesu486/cloud-operator/api/v1alpha1"
)

func fixture(t *testing.T) (*Reconciler, *cloud.CloudVM) {
	t.Helper()
	s := runtime.NewScheme()
	_ = corev1.AddToScheme(s)
	_ = cloud.AddToScheme(s)
	vm := &cloud.CloudVM{ObjectMeta: metav1.ObjectMeta{Name: "alice", Namespace: "test", UID: types.UID("test-vm-uid")}, Spec: cloud.CloudVMSpec{Owner: "alice", RootVolumeClaim: "alice-root", VCPUs: 1, MemoryMiB: 128, PowerState: "Running", BootID: 1}}
	pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: "alice-root", Namespace: "test", Labels: map[string]string{OwnerLabel: "alice"}}, Spec: corev1.PersistentVolumeClaimSpec{VolumeMode: ptr.To(corev1.PersistentVolumeBlock)}}
	c := fake.NewClientBuilder().WithScheme(s).WithStatusSubresource(&cloud.CloudVM{}, &corev1.Pod{}).WithObjects(vm, pvc).Build()
	return &Reconciler{Client: c, Runner: RunnerOptions{Image: "test@sha256:abc", Node: "m600", Assets: "/var/tmp/test", Development: true}}, vm
}
func reconcile(t *testing.T, r *Reconciler, vm *cloud.CloudVM) {
	t.Helper()
	_, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(vm)})
	if err != nil {
		t.Fatal(err)
	}
}
func getVM(t *testing.T, r *Reconciler, vm *cloud.CloudVM) *cloud.CloudVM {
	t.Helper()
	out := &cloud.CloudVM{}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(vm), out); err != nil {
		t.Fatal(err)
	}
	return out
}
func getPods(t *testing.T, r *Reconciler) []corev1.Pod {
	t.Helper()
	out := &corev1.PodList{}
	if err := r.List(context.Background(), out); err != nil {
		t.Fatal(err)
	}
	return out.Items
}

func TestBootIdempotencyAndGuestShutdown(t *testing.T) {
	r, vm := fixture(t)
	reconcile(t, r, vm)
	reconcile(t, r, vm)
	pods := getPods(t, r)
	if len(pods) != 1 {
		t.Fatalf("created %d Pods", len(pods))
	}
	p := &pods[0]
	if p.Spec.NodeName != "" || p.Spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Fatal("must use scheduler and operator-managed restarts")
	}
	p.Status.Phase = corev1.PodSucceeded
	if err := r.Status().Update(context.Background(), p); err != nil {
		t.Fatal(err)
	}
	reconcile(t, r, vm)
	if got := getVM(t, r, vm); got.Status.Phase != "Stopped" {
		t.Fatalf("guest shutdown: %+v", got.Status)
	}
	if err := r.Delete(context.Background(), p); err != nil {
		t.Fatal(err)
	}
	reconcile(t, r, vm)
	if len(getPods(t, r)) != 0 {
		t.Fatal("guest shutdown caused unintended reboot")
	}
	v := getVM(t, r, vm)
	v.Spec.BootID++
	if err := r.Update(context.Background(), v); err != nil {
		t.Fatal(err)
	}
	reconcile(t, r, vm)
	if len(getPods(t, r)) != 1 {
		t.Fatal("explicit boot did not start")
	}
}
func TestNoOverlapWithTerminatingPod(t *testing.T) {
	r, vm := fixture(t)
	reconcile(t, r, vm)
	p := getPods(t, r)[0]
	p.Finalizers = []string{"test/hold"}
	if err := r.Update(context.Background(), &p); err != nil {
		t.Fatal(err)
	}
	v := getVM(t, r, vm)
	v.Spec.BootID++
	if err := r.Update(context.Background(), v); err != nil {
		t.Fatal(err)
	}
	reconcile(t, r, vm)
	reconcile(t, r, vm)
	pods := getPods(t, r)
	if len(pods) != 1 || pods[0].DeletionTimestamp.IsZero() {
		t.Fatal("new boot overlapped old terminating Pod")
	}
	if getVM(t, r, vm).Status.Phase != "Stopping" {
		t.Fatal("must wait for old writer")
	}
}
func TestRootVolumeOwnershipAndReservation(t *testing.T) {
	for _, mode := range []string{"foreign-owner", "foreign-claim"} {
		t.Run(mode, func(t *testing.T) {
			r, vm := fixture(t)
			pvc := &corev1.PersistentVolumeClaim{}
			_ = r.Get(context.Background(), types.NamespacedName{Namespace: "test", Name: "alice-root"}, pvc)
			if mode == "foreign-owner" {
				pvc.Labels[OwnerLabel] = "bob"
			} else {
				pvc.Annotations = map[string]string{"cloud.zjusct.io/claimed-by": "another-vm"}
			}
			if err := r.Update(context.Background(), pvc); err != nil {
				t.Fatal(err)
			}
			reconcile(t, r, vm)
			if len(getPods(t, r)) != 0 || getVM(t, r, vm).Status.Phase != "Blocked" {
				t.Fatal("unowned disk accepted")
			}
		})
	}
}
func TestMissingRunIsUnknownAndDoesNotReboot(t *testing.T) {
	r, vm := fixture(t)
	reconcile(t, r, vm)
	p := getPods(t, r)[0]
	_ = r.Delete(context.Background(), &p)
	reconcile(t, r, vm)
	if len(getPods(t, r)) != 0 || getVM(t, r, vm).Status.Phase != "Unknown" {
		t.Fatal("lost run was restarted or incorrectly declared stopped")
	}
}
func TestStopRetainsRootVolume(t *testing.T) {
	r, vm := fixture(t)
	reconcile(t, r, vm)
	v := getVM(t, r, vm)
	v.Spec.PowerState = "Stopped"
	_ = r.Update(context.Background(), v)
	reconcile(t, r, vm)
	reconcile(t, r, vm)
	pvc := &corev1.PersistentVolumeClaim{}
	if err := r.Get(context.Background(), types.NamespacedName{Namespace: "test", Name: "alice-root"}, pvc); err != nil {
		t.Fatal(err)
	}
	if len(pvc.OwnerReferences) != 0 || len(getPods(t, r)) != 0 || getVM(t, r, vm).Status.Phase != "Stopped" {
		t.Fatal("stop did not preserve independent volume")
	}
}

func TestNewBootReapsCompletedPodBeforeCreatingAnother(t *testing.T) {
	r, vm := fixture(t)
	reconcile(t, r, vm)
	pod := getPods(t, r)[0]
	pod.Status.Phase = corev1.PodSucceeded
	if err := r.Status().Update(context.Background(), &pod); err != nil {
		t.Fatal(err)
	}
	reconcile(t, r, vm)
	v := getVM(t, r, vm)
	v.Spec.BootID++
	if err := r.Update(context.Background(), v); err != nil {
		t.Fatal(err)
	}
	reconcile(t, r, vm)
	if len(getPods(t, r)) != 0 {
		t.Fatal("old terminal Pod was not reaped before a new boot")
	}
	reconcile(t, r, vm)
	pods := getPods(t, r)
	if len(pods) != 1 || pods[0].Labels[BootLabel] != "2" {
		t.Fatal("new boot did not proceed after cleanup")
	}
}
