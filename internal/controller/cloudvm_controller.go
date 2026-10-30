package controller

import (
	"context"
	"encoding/json"
	"fmt"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"reflect"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"strconv"
	"time"
	cloud "github.com/Sushidesu486/cloud-operator/api/v1alpha1"
)

type Reconciler struct {
	client.Client
	Runner RunnerOptions
}

func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).For(&cloud.CloudVM{}).Owns(&corev1.Pod{}).Complete(r)
}
func (r *Reconciler) status(ctx context.Context, vm *cloud.CloudVM, phase, reason, message string, pod *corev1.Pod) (ctrl.Result, error) {
	before := vm.DeepCopy()
	vm.Status.Phase = phase
	vm.Status.Reason = reason
	vm.Status.Message = message
	vm.Status.ObservedGeneration = vm.Generation
	if pod != nil {
		vm.Status.PodName = pod.Name
		vm.Status.PodUID = string(pod.UID)
		vm.Status.NodeName = pod.Spec.NodeName
		n, _ := strconv.ParseInt(pod.Labels[BootLabel], 10, 64)
		if n > vm.Status.LastBootID {
			vm.Status.LastBootID = n
		}
	}
	ready := metav1.ConditionFalse
	if phase == "Running" {
		ready = metav1.ConditionTrue
	}
	meta.SetStatusCondition(&vm.Status.Conditions, metav1.Condition{Type: "Ready", Status: ready, Reason: reason, Message: message, ObservedGeneration: vm.Generation})
	if !reflect.DeepEqual(before.Status, vm.Status) {
		if err := r.Status().Patch(ctx, vm, client.MergeFrom(before)); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
}
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	vm := &cloud.CloudVM{}
	if err := r.Get(ctx, req.NamespacedName, vm); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !vm.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}
	pods := &corev1.PodList{}
	if err := r.List(ctx, pods, client.InNamespace(vm.Namespace), client.MatchingLabels{VMLabel: string(vm.UID)}); err != nil {
		return ctrl.Result{}, err
	}
	var current *corev1.Pod
	for i := range pods.Items {
		pod := &pods.Items[i]
		if !metav1.IsControlledBy(pod, vm) {
			return r.status(ctx, vm, "Blocked", "OwnershipConflict", "A matching Pod is not owned by this VM", nil)
		}
		terminal := pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed
		if pod.Name == podName(vm) {
			current = pod
		}
		// Preserve the current terminal run for diagnostics, but reap older runs
		// before a new boot so completed Pods cannot exhaust namespace quota.
		if terminal && vm.Spec.PowerState == "Running" && pod.Name != podName(vm) {
			uid := pod.UID
			if err := r.Delete(ctx, pod, &client.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}}); err != nil && !apierrors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
			return ctrl.Result{RequeueAfter: time.Second}, nil
		}
		// Never overlap runs, including a Pod stuck Terminating on a lost node.
		if !terminal && (vm.Spec.PowerState == "Stopped" || pod.Name != podName(vm)) {
			if pod.DeletionTimestamp.IsZero() {
				uid := pod.UID
				if err := r.Delete(ctx, pod, &client.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}}); err != nil && !apierrors.IsNotFound(err) {
					return ctrl.Result{}, err
				}
			}
			return r.status(ctx, vm, "Stopping", "WaitingForPreviousRun", "Waiting for the previous launcher to stop; no forced deletion", pod)
		}
	}
	if vm.Spec.PowerState == "Stopped" {
		return r.status(ctx, vm, "Stopped", "RequestedStop", "Persistent root volume retained", current)
	}
	if vm.Spec.PowerState != "Running" || vm.Spec.BootID < 1 || vm.Spec.VCPUs < 1 || vm.Spec.VCPUs > 4 || vm.Spec.MemoryMiB < 128 || vm.Spec.MemoryMiB > 4096 {
		return r.status(ctx, vm, "Blocked", "InvalidSpec", "Invalid power state, boot ID or development flavor", nil)
	}
	if current != nil {
		if !current.DeletionTimestamp.IsZero() {
			return r.status(ctx, vm, "Stopping", "PodTerminating", "Waiting for launcher deletion", current)
		}
		switch current.Status.Phase {
		case corev1.PodSucceeded:
			return r.status(ctx, vm, "Stopped", "GuestExited", "Guest exited; increase bootID explicitly to start again", current)
		case corev1.PodFailed:
			reason, message := "LauncherFailed", "Launcher failed; inspect logs before starting a new boot"
			for _, s := range current.Status.ContainerStatuses {
				if t := s.State.Terminated; t != nil {
					var result struct {
						Reason string `json:"reason"`
					}
					if json.Unmarshal([]byte(t.Message), &result) == nil && result.Reason != "" {
						reason = result.Reason
					}
					message = fmt.Sprintf("launcher exit code %d", t.ExitCode)
				}
			}
			return r.status(ctx, vm, "Failed", reason, message, current)
		case corev1.PodUnknown:
			return r.status(ctx, vm, "Unknown", "NodeStateUnknown", "Do not restart elsewhere until the old writer is fenced", current)
		}
		for _, c := range current.Status.Conditions {
			if c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue {
				return r.status(ctx, vm, "Running", "VMMRunning", "Firecracker started; guest service readiness is not implied", current)
			}
		}
		return r.status(ctx, vm, "Pending", "LauncherPending", "Waiting for scheduling, volume attachment or VMM startup", current)
	}
	// A deleted Pod does not make an already-observed boot request new again.
	if vm.Status.LastBootID >= vm.Spec.BootID {
		if vm.Status.Phase == "Stopped" || vm.Status.Phase == "Failed" {
			return r.status(ctx, vm, vm.Status.Phase, "BootConsumed", "This boot was already observed; increase bootID for a new run", nil)
		}
		return r.status(ctx, vm, "Unknown", "RunMissing", "Observed launcher disappeared; inspect and fence before requesting another boot", nil)
	}
	pvc := &corev1.PersistentVolumeClaim{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: vm.Namespace, Name: vm.Spec.RootVolumeClaim}, pvc); err != nil {
		if apierrors.IsNotFound(err) {
			return r.status(ctx, vm, "Pending", "VolumeMissing", "Root PVC must be provisioned before boot", nil)
		}
		return ctrl.Result{}, err
	}
	if pvc.Labels[OwnerLabel] != vm.Spec.Owner || pvc.Spec.VolumeMode == nil || *pvc.Spec.VolumeMode != corev1.PersistentVolumeBlock || !pvc.DeletionTimestamp.IsZero() {
		return r.status(ctx, vm, "Blocked", "VolumeRejected", "Root PVC must be an owned, non-deleting raw block volume", nil)
	}
	vms := &cloud.CloudVMList{}
	if err := r.List(ctx, vms, client.InNamespace(vm.Namespace)); err != nil {
		return ctrl.Result{}, err
	}
	for _, other := range vms.Items {
		if other.UID != vm.UID && other.Spec.RootVolumeClaim == vm.Spec.RootVolumeClaim {
			return r.status(ctx, vm, "Blocked", "VolumeInUse", "Another CloudVM references this root volume", nil)
		}
	}
	// ResourceVersion-checked update serializes claims even when the cache lags.
	// The reservation survives VM deletion; an administrator must release it explicitly.
	const claimKey = "cloud.zjusct.io/claimed-by"
	claim := pvc.Annotations[claimKey]
	if claim != "" && claim != string(vm.UID) {
		return r.status(ctx, vm, "Blocked", "VolumeClaimed", "Root volume is reserved by another VM UID", nil)
	}
	if claim == "" {
		if pvc.Annotations == nil {
			pvc.Annotations = map[string]string{}
		}
		pvc.Annotations[claimKey] = string(vm.UID)
		if err := r.Update(ctx, pvc); err != nil {
			return ctrl.Result{}, err
		}
	}
	pod, err := BuildPod(vm, r.Runner)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err = r.Create(ctx, pod); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return ctrl.Result{RequeueAfter: time.Second}, nil
		}
		return ctrl.Result{}, err
	}
	return r.status(ctx, vm, "Pending", "LauncherCreated", "Created one launcher Pod for this boot", pod)
}
