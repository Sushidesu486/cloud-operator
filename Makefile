GO ?= go

.PHONY: build test generate
build:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build -trimpath -o bin/cloud-operator ./cmd/cloud-operator

test:
	$(GO) test ./...

generate:
	$(GO) run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.20.1 object crd paths=./api/... output:crd:dir=config/crd
