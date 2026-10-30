// Package v1alpha1 contains the persistent CloudVM API.
// +kubebuilder:object:generate=true
// +groupName=cloud.zjusct.io
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

var GroupVersion = schema.GroupVersion{Group: "cloud.zjusct.io", Version: "v1alpha1"}
var SchemeBuilder = runtime.NewSchemeBuilder(func(s *runtime.Scheme) error {
	s.AddKnownTypes(GroupVersion, &CloudVM{}, &CloudVMList{})
	metav1.AddToGroupVersion(s, GroupVersion)
	return nil
})
var AddToScheme = SchemeBuilder.AddToScheme

type CloudVMSpec struct {
	// +kubebuilder:validation:Pattern=`^[a-z][a-z0-9-]{0,31}$`
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="owner is immutable"
	Owner string `json:"owner"`
	// RootVolumeClaim is provisioned independently; deleting CloudVM never deletes it.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="root volume is immutable"
	RootVolumeClaim string `json:"rootVolumeClaim"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=4
	VCPUs int32 `json:"vcpus"`
	// +kubebuilder:validation:Minimum=128
	// +kubebuilder:validation:Maximum=4096
	MemoryMiB int32 `json:"memoryMiB"`
	// +kubebuilder:validation:Enum=Running;Stopped
	PowerState string `json:"powerState"`
	// BootID is an explicit, monotonically increasing boot request. Reusing it is idempotent.
	// Increase it after stop, guest shutdown, failure, or a lost Pod.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:XValidation:rule="self >= oldSelf",message="bootID must not decrease"
	BootID int64 `json:"bootID"`
}

type CloudVMStatus struct {
	Phase              string `json:"phase,omitempty"`
	Reason             string `json:"reason,omitempty"`
	Message            string `json:"message,omitempty"`
	ObservedGeneration int64  `json:"observedGeneration,omitempty"`
	LastBootID         int64  `json:"lastBootID,omitempty"`
	PodName            string `json:"podName,omitempty"`
	PodUID             string `json:"podUID,omitempty"`
	NodeName           string `json:"nodeName,omitempty"`
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Boot",type=integer,JSONPath=`.spec.bootID`
// +kubebuilder:printcolumn:name="Node",type=string,JSONPath=`.status.nodeName`
type CloudVM struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              CloudVMSpec   `json:"spec"`
	Status            CloudVMStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type CloudVMList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []CloudVM `json:"items"`
}
