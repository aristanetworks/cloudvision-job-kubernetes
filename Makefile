# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
#
# Makefile for CV Job Informer - Kubernetes Job Monitoring Service
# Provides convenient shortcuts for common operations

# Disable built-in implicit rules to prevent Make from treating unknown targets as files to compile
MAKEFLAGS += --no-builtin-rules
.SUFFIXES:

# Configuration
# NAMESPACE: Namespaces to monitor for jobs
#   - Empty string "" = watch ALL namespaces cluster-wide (default)
#   - Single namespace, e.g. "training" = watch only that namespace
#   - Comma-separated list, e.g. "ns1,ns2,ns3" = watch all, filter to specified
NAMESPACE ?=
API_SERVER ?=
API_TOKEN ?=
LOCATION ?=
JOBCONFIG_MODE ?= interface
LOG_LEVEL ?= info
NODECONFIG_MODE ?= discovery
NODE_INTERFACE_TYPE ?= all

# Image configuration
REGISTRY ?=
IMAGE_TAG ?= latest
PUSH ?= false
SKIP_BUILD ?= false

# Deployment namespace (where the cv-job-informer pod runs)
DEPLOY_NAMESPACE := cloudvision

# Component to operate on (for logs, status, restart, describe commands)
COMPONENT ?= job

.PHONY: help
help: ## Show this help message
	@echo "CV Job Informer - Kubernetes Job Monitoring Service"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'
	@echo ""
	@echo "Variables:"
	@echo "  NAMESPACE          Namespace(s) to monitor (default: all namespaces)"
	@echo "                     - Empty = all namespaces cluster-wide"
	@echo "                     - Single namespace = watch only that namespace"
	@echo "                     - Comma-separated = watch all, filter to specified"
	@echo "  API_SERVER         API server address (REQUIRED)"
	@echo "  API_TOKEN          API authentication token (REQUIRED)"
	@echo "  LOCATION           Location identifier, e.g. cluster name (REQUIRED)"
	@echo "  JOBCONFIG_MODE     JobConfig mode: node or interface (default: interface)"
	@echo "  NODECONFIG_MODE    NodeConfig mode: discovery, sriovoperator, or disabled (default: discovery)"
	@echo "  NODE_INTERFACE_TYPE Node interfaces for NodeConfig: all, pf, or vf (default: all)"
	@echo "  LOG_LEVEL          Log level: debug, info, warning, error (default: info)"
	@echo "  REGISTRY           Container registry (e.g., docker.io/username)"
	@echo "  IMAGE_TAG          Docker image tag (default: latest)"
	@echo "  PUSH               Push image to registry: true or false (default: false)"
	@echo "  SKIP_BUILD         Skip building image, use existing: true or false (default: false)"
	@echo "  COMPONENT          Component to operate on: job or node (default: job)"
	@echo "                     - job = cv-job-informer deployment"
	@echo "                     - node = cv-interface-discovery daemonset"
	@echo "                     Used by: logs, status, restart, describe commands"

.PHONY: check-kubectl
check-kubectl:
	@command -v kubectl >/dev/null 2>&1 || (echo "Error: kubectl not found" && exit 1)

.PHONY: check-deploy-params
check-deploy-params:
	@test -n "$(API_SERVER)" || (echo "Error: API_SERVER is required. Usage: make deploy API_SERVER=... API_TOKEN=... LOCATION=..." && exit 1)
	@test -n "$(API_TOKEN)" || (echo "Error: API_TOKEN is required. Usage: make deploy API_SERVER=... API_TOKEN=... LOCATION=..." && exit 1)
	@test -n "$(LOCATION)" || (echo "Error: LOCATION is required. Usage: make deploy API_SERVER=... API_TOKEN=... LOCATION=..." && exit 1)

.PHONY: deploy
deploy: check-deploy-params ## Build and deploy to Kubernetes (requires API_SERVER, API_TOKEN, LOCATION)
	@echo "Deploying CV Job Informer"
	./build-and-deploy.sh \
		$(if $(NAMESPACE),--namespace $(NAMESPACE)) \
		$(if $(REGISTRY),--registry $(REGISTRY)) \
		$(if $(IMAGE_TAG),--image-tag $(IMAGE_TAG)) \
		$(if $(filter true,$(PUSH)),--push) \
		$(if $(filter true,$(SKIP_BUILD)),--skip-build) \
		--api-server $(API_SERVER) \
		--api-token $(API_TOKEN) \
		--location $(LOCATION) \
		--jobconfig-mode $(JOBCONFIG_MODE) \
		--log-level $(LOG_LEVEL) \
		--nodeconfig-mode $(NODECONFIG_MODE) \
		--node-interface-type $(NODE_INTERFACE_TYPE)

.PHONY: logs
logs: check-kubectl ## View logs (COMPONENT=job for cv-job-informer, COMPONENT=node for cv-interface-discovery)
	@if [ "$(COMPONENT)" = "node" ]; then \
		echo "Viewing logs from cv-interface-discovery daemonset..."; \
		kubectl logs -f daemonset/cv-interface-discovery -n $(DEPLOY_NAMESPACE); \
	else \
		echo "Viewing logs from cv-job-informer deployment..."; \
		kubectl logs -f deployment/cv-job-informer -n $(DEPLOY_NAMESPACE); \
	fi

.PHONY: status
status: check-kubectl ## Check status (COMPONENT=job for cv-job-informer, COMPONENT=node for cv-interface-discovery)
	@if [ "$(COMPONENT)" = "node" ]; then \
		echo "CV Interface Discovery Status"; \
		echo ""; \
		echo "Pods:"; \
		kubectl get pods -n $(DEPLOY_NAMESPACE) -l app=cv-interface-discovery; \
		echo ""; \
		echo "DaemonSet:"; \
		kubectl get daemonset cv-interface-discovery -n $(DEPLOY_NAMESPACE); \
		echo ""; \
		echo "Recent logs (from one pod):"; \
		POD=$$(kubectl get pods -n $(DEPLOY_NAMESPACE) -l app=cv-interface-discovery -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
		if [ -n "$$POD" ]; then \
			kubectl logs --tail=20 $$POD -n $(DEPLOY_NAMESPACE) 2>&1 || echo "Unable to retrieve logs (pod may not be ready)"; \
		else \
			echo "No cv-interface-discovery pods found"; \
		fi; \
	else \
		echo "CV Job Informer Status"; \
		echo ""; \
		echo "Pods:"; \
		kubectl get pods -n $(DEPLOY_NAMESPACE) -l app=cv-job-informer; \
		echo ""; \
		echo "Deployment:"; \
		kubectl get deployment cv-job-informer -n $(DEPLOY_NAMESPACE); \
		echo ""; \
		echo "Recent logs:"; \
		kubectl logs --tail=20 deployment/cv-job-informer -n $(DEPLOY_NAMESPACE) 2>&1 || echo "Unable to retrieve logs (pod may not be ready)"; \
	fi

.PHONY: describe
describe: check-kubectl ## Describe pod (COMPONENT=job for cv-job-informer, COMPONENT=node for cv-interface-discovery)
	@if [ "$(COMPONENT)" = "node" ]; then \
		kubectl describe pod -l app=cv-interface-discovery -n $(DEPLOY_NAMESPACE); \
	else \
		kubectl describe pod -l app=cv-job-informer -n $(DEPLOY_NAMESPACE); \
	fi

.PHONY: restart
restart: check-kubectl ## Restart component (COMPONENT=job for cv-job-informer, COMPONENT=node for cv-interface-discovery)
	@if [ "$(COMPONENT)" = "node" ]; then \
		echo "Restarting CV Interface Discovery daemonset"; \
		kubectl rollout restart daemonset/cv-interface-discovery -n $(DEPLOY_NAMESPACE); \
		kubectl rollout status daemonset/cv-interface-discovery -n $(DEPLOY_NAMESPACE); \
	else \
		echo "Restarting CV Job Informer deployment"; \
		kubectl rollout restart deployment/cv-job-informer -n $(DEPLOY_NAMESPACE); \
		kubectl rollout status deployment/cv-job-informer -n $(DEPLOY_NAMESPACE); \
	fi

.PHONY: delete
delete: check-kubectl ## Delete cv-job-informer from Kubernetes
	@echo "Deleting CV Job Informer from namespace $(DEPLOY_NAMESPACE)"
	@bash -c 'read -p "Are you sure? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		kubectl delete -f job_informer.yaml || true; \
		kubectl delete -f interface_discovery.yaml || true; \
		kubectl delete secret cv-job-informer-api-credentials -n $(DEPLOY_NAMESPACE) || true; \
		echo "✓ CV Job Informer deleted"; \
	else \
		echo "Cancelled"; \
	fi'

.DEFAULT_GOAL := help

# Catch-all rule to provide helpful error message for unknown targets
%:
	@echo "Error: Unknown target or invalid argument '$@'"
	@echo "Run 'make help' to see available targets and variables"
	@exit 1