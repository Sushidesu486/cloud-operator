package controller

import (
	"encoding/json"
	"os"
	"testing"
)

func TestLauncherProtocolFixture(t *testing.T) {
	r, vm := fixture(t)
	pod, err := BuildPod(vm, r.Runner)
	if err != nil {
		t.Fatal(err)
	}
	var actual, expected any
	if err = json.Unmarshal([]byte(pod.Spec.Containers[0].Env[0].Value), &actual); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile("../../testdata/protocol-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(data, &expected); err != nil {
		t.Fatal(err)
	}
	a, _ := json.Marshal(actual)
	e, _ := json.Marshal(expected)
	if string(a) != string(e) {
		t.Fatalf("launcher contract drift: %s != %s", a, e)
	}
}
