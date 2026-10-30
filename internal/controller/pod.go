package controller

import (
	"encoding/json"
	"fmt"
	cloud "github.com/Sushidesu486/cloud-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"strconv"
)

const VMLabel = "cloud.zjusct.io/vm-uid"
const OwnerLabel = "cloud.zjusct.io/owner"
const BootLabel = "cloud.zjusct.io/boot-id"

// RunnerConfig implements the versioned contract shared with cloud-launcher.
type RunnerConfig struct {
	Version      int    `json:"version"`
	ID           string `json:"id"`
	VCPUs        int32  `json:"vcpus"`
	MemoryMiB    int32  `json:"memoryMiB"`
	Firecracker  string `json:"firecracker"`
	Jailer       string `json:"jailer"`
	Kernel       string `json:"kernel"`
	RootDevice   string `json:"rootDevice"`
	WorkDir      string `json:"workDir"`
	GraceSeconds int    `json:"graceSeconds"`
	NetworkNAT   bool   `json:"networkNAT,omitempty"`
}
type RunnerOptions struct {
	Image, Node, Assets string
	Development         bool
	NetworkNAT          bool
}

func podName(vm *cloud.CloudVM) string {
	return "vm-" + string(vm.UID) + "-" + strconv.FormatInt(vm.Spec.BootID, 10)
}

func BuildPod(vm *cloud.CloudVM, options RunnerOptions) (*corev1.Pod, error) {
	if !options.Development || options.Assets == "" || options.Node == "" || options.Image == "" {
		return nil, fmt.Errorf("only explicit development runner is implemented; image, node and assets are required")
	}
	cfg := RunnerConfig{Version: 1, ID: "vm", VCPUs: vm.Spec.VCPUs, MemoryMiB: vm.Spec.MemoryMiB,
		Firecracker: "/assets/firecracker", Jailer: "/assets/jailer", Kernel: "/assets/vmlinux", RootDevice: "/dev/vm-root", WorkDir: "/work", GraceSeconds: 12}
	grace := int64(20)
	if options.NetworkNAT {
		cfg.Version = 2
		cfg.NetworkNAT = true
		cfg.GraceSeconds = 60
		grace = 75
	}
	raw, err := json.Marshal(cfg)
	if err != nil {
		return nil, err
	}
	resources := corev1.ResourceList{
		corev1.ResourceCPU:              resource.MustParse(strconv.Itoa(int(vm.Spec.VCPUs))),
		corev1.ResourceMemory:           resource.MustParse(fmt.Sprintf("%dMi", vm.Spec.MemoryMiB+128)),
		corev1.ResourceEphemeralStorage: resource.MustParse("256Mi"),
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: podName(vm), Namespace: vm.Namespace,
			Labels:          map[string]string{VMLabel: string(vm.UID), OwnerLabel: vm.Spec.Owner, BootLabel: strconv.FormatInt(vm.Spec.BootID, 10)},
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(vm, cloud.GroupVersion.WithKind("CloudVM"))}},
		Spec: corev1.PodSpec{
			RestartPolicy: corev1.RestartPolicyNever, AutomountServiceAccountToken: ptr.To(false), EnableServiceLinks: ptr.To(false),
			NodeSelector: map[string]string{"kubernetes.io/hostname": options.Node}, TerminationGracePeriodSeconds: ptr.To(grace),
			Containers: []corev1.Container{{Name: "launcher", Image: options.Image, ImagePullPolicy: corev1.PullIfNotPresent,
				Command: []string{"/assets/cloud-launcher", "run"}, Env: []corev1.EnvVar{{Name: "CLOUD_LAUNCHER_CONFIG", Value: string(raw)}},
				SecurityContext:        &corev1.SecurityContext{Privileged: ptr.To(true), ReadOnlyRootFilesystem: ptr.To(true)},
				Resources:              corev1.ResourceRequirements{Requests: resources.DeepCopy(), Limits: resources.DeepCopy()},
				VolumeMounts:           []corev1.VolumeMount{{Name: "assets", MountPath: "/assets", ReadOnly: true}, {Name: "work", MountPath: "/work"}},
				VolumeDevices:          []corev1.VolumeDevice{{Name: "root", DevicePath: "/dev/vm-root"}},
				ReadinessProbe:         &corev1.Probe{ProbeHandler: corev1.ProbeHandler{Exec: &corev1.ExecAction{Command: []string{"/assets/cloud-launcher", "ready"}}}, PeriodSeconds: 2},
				TerminationMessagePath: "/work/termination.json", TerminationMessagePolicy: corev1.TerminationMessageReadFile,
			}},
			Volumes: []corev1.Volume{
				{Name: "assets", VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: options.Assets, Type: ptr.To(corev1.HostPathDirectory)}}},
				{Name: "work", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse("192Mi"))}}},
				{Name: "root", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: vm.Spec.RootVolumeClaim}}},
			},
		},
	}, nil
}
