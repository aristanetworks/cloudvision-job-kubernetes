#!/bin/bash
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
#
# Build and deployment script for CV Job Informer
# This script builds the Docker image and deploys to Kubernetes

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
DEPLOY_NAMESPACE="cloudvision"  # Where to deploy the cv-job-informer pod
WATCH_NAMESPACE=""              # Namespaces to watch (empty = all namespaces)
IMAGE_NAME="cloudvision-job-kubernetes"
IMAGE_TAG="latest"
REGISTRY=""
API_SERVER=""
API_TOKEN=""
LOCATION=""
JOBCONFIG_MODE="interface"
LOG_LEVEL="info"
NODECONFIG_MODE="discovery"
NODE_INTERFACE_TYPE="all"

BUILD_IMAGE=true
PUSH_IMAGE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    -n|--namespace)
      WATCH_NAMESPACE="$2"
      shift 2
      ;;
    --image-name)
      IMAGE_NAME="$2"
      shift 2
      ;;
    --image-tag)
      IMAGE_TAG="$2"
      shift 2
      ;;
    --registry)
      REGISTRY="$2"
      shift 2
      ;;
    --api-server)
      API_SERVER="$2"
      shift 2
      ;;
    --api-token)
      API_TOKEN="$2"
      shift 2
      ;;
    --location)
      LOCATION="$2"
      shift 2
      ;;
    --jobconfig-mode)
      JOBCONFIG_MODE="$2"
      shift 2
      ;;
    --log-level)
      LOG_LEVEL="$2"
      shift 2
      ;;
    --nodeconfig-mode)
      NODECONFIG_MODE="$2"
      shift 2
      ;;
    --node-interface-type)
      NODE_INTERFACE_TYPE="$2"
      shift 2
      ;;
    --skip-build)
      BUILD_IMAGE=false
      shift
      ;;
    --push)
      PUSH_IMAGE=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Build and deployment options:"
      echo "  --image-name NAME               Docker image name (default: cloudvision-job-kubernetes)"
      echo "  --image-tag TAG                 Docker image tag (default: latest)"
      echo "  --registry REGISTRY             Container registry (e.g., ghcr.io/aristanetworks)"
      echo "  --skip-build                    Skip Docker image build"
      echo "  --push                          Push image to registry after build"
      echo ""
      echo "Deployment options:"
      echo "  --api-server SERVER             External API server address (REQUIRED)"
      echo "  --api-token TOKEN               API authentication token (REQUIRED)"
      echo "  --location LOCATION             Location identifier (REQUIRED)"
      echo "  -n, --namespace NAMESPACE       Namespaces to watch (default: all namespaces)"
      echo "                                  Empty or unspecified = watch all namespaces"
      echo "                                  Comma-separated list = watch specified namespaces"
      echo "  --jobconfig-mode MODE           JobConfig mode: node or interface (default: interface)"
      echo "  --log-level LEVEL               Log level: debug, info, warning, error (default: info)"
      echo "  --nodeconfig-mode MODE          NodeConfig mode: discovery, sriovoperator, or disabled (default: discovery)"
      echo "  --node-interface-type TYPE      Node interfaces to send in NodeConfig: all, pf, or vf (default: all)"
      echo ""
      echo "  -h, --help                      Show this help message"
      echo ""
      echo "Examples:"
      echo "  # Build and deploy locally (watch all namespaces)"
      echo "  $0 --api-server api.example.com --api-token mytoken --location mycluster"
      echo ""
      echo "  # Build, push to registry, and deploy"
      echo "  $0 --registry myregistry.io --push --api-server api.example.com --api-token mytoken --location mycluster"
      echo ""
      echo "  # Deploy only (skip build, use existing image)"
      echo "  $0 --skip-build --api-server api.example.com --api-token mytoken --location mycluster"
      echo ""
      echo "  # Watch specific namespaces only"
      echo "  $0 --api-server api.example.com --api-token mytoken --location mycluster --namespace training,ml-jobs"
      exit 0
      ;;
    *)
      echo -e "${RED}Error: Unknown option $1${NC}"
      exit 1
      ;;
  esac
done

# Validate required parameters
if [ -z "$API_SERVER" ]; then
    echo -e "${RED}Error: --api-server is required${NC}"
    echo "Use --help for usage information"
    exit 1
fi

if [ -z "$API_TOKEN" ]; then
    echo -e "${RED}Error: --api-token is required${NC}"
    echo "Use --help for usage information"
    exit 1
fi

if [ -z "$LOCATION" ]; then
    echo -e "${RED}Error: --location is required${NC}"
    echo "Use --help for usage information"
    exit 1
fi

# Validate NODECONFIG_MODE
if [[ ! "$NODECONFIG_MODE" =~ ^(discovery|sriovoperator|disabled)$ ]]; then
    echo -e "${RED}Error: --nodeconfig-mode must be one of: discovery, sriovoperator, disabled${NC}"
    echo "Use --help for usage information"
    exit 1
fi

# Construct full image name
if [ -n "$REGISTRY" ]; then
    FULL_IMAGE_NAME="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
else
    FULL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"
fi

echo -e "${BLUE}=== CV Job Informer Build and Deployment ===${NC}"
echo ""
echo "Configuration:"
echo "  Image:                     $FULL_IMAGE_NAME"
echo "  Deploy namespace:          $DEPLOY_NAMESPACE"
echo "  Watch namespaces:          ${WATCH_NAMESPACE:-all (cluster-wide)}"
echo "  API Server:                $API_SERVER"
echo "  Location:                  $LOCATION"
echo "  JobConfig Mode:            $JOBCONFIG_MODE"
echo "  NodeConfig mode:           $NODECONFIG_MODE"
echo "  Node interface type:       $NODE_INTERFACE_TYPE"
echo "  Log Level:                 $LOG_LEVEL"
echo ""

# Check prerequisites
if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}Error: kubectl not found. Please install kubectl first.${NC}"
    exit 1
fi

if [ "$BUILD_IMAGE" = true ]; then
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}Error: docker not found. Please install Docker first.${NC}"
        exit 1
    fi
fi

# Check required files
if [ "$BUILD_IMAGE" = true ]; then
    if [ ! -f "job_monitor.py" ]; then
        echo -e "${RED}Error: job_monitor.py not found in current directory${NC}"
        exit 1
    fi
    if [ ! -f "Dockerfile" ]; then
        echo -e "${RED}Error: Dockerfile not found in current directory${NC}"
        exit 1
    fi
fi

# Check for deployment YAML file
if [ ! -f "job_informer.yaml" ]; then
    echo -e "${RED}Error: job_informer.yaml not found in current directory${NC}"
    exit 1
fi

# Build Docker image
if [ "$BUILD_IMAGE" = true ]; then
    echo -e "${GREEN}Step 1: Building Docker image${NC}"
    docker build -t "$FULL_IMAGE_NAME" .
    echo -e "${GREEN}✓ Image built: $FULL_IMAGE_NAME${NC}"
    echo ""

    # Push to registry if requested
    if [ "$PUSH_IMAGE" = true ]; then
        echo -e "${GREEN}Step 2: Pushing image to registry${NC}"
        docker push "$FULL_IMAGE_NAME"
        echo -e "${GREEN}✓ Image pushed to registry${NC}"
        echo ""
    else
        # If not pushing to registry, import to containerd for local k8s
        echo -e "${GREEN}Step 2: Importing image to containerd${NC}"

        # Check if containerd is being used
        if command -v ctr &> /dev/null; then
            TEMP_TAR="/tmp/${IMAGE_NAME}-${IMAGE_TAG}.tar"

            echo "  Saving Docker image to tar..."
            docker save "$FULL_IMAGE_NAME" -o "$TEMP_TAR"

            echo "  Importing to containerd (k8s.io namespace)..."
            sudo ctr -n k8s.io images import "$TEMP_TAR"

            echo "  Cleaning up temporary tar file..."
            rm -f "$TEMP_TAR"

            echo -e "${GREEN}✓ Image imported to containerd${NC}"

            # Verify the image is available
            if sudo crictl images | grep -q "$IMAGE_NAME.*$IMAGE_TAG"; then
                echo -e "${GREEN}✓ Image verified in containerd${NC}"
            else
                echo -e "${YELLOW}Warning: Could not verify image in containerd${NC}"
            fi
        else
            echo -e "${YELLOW}Warning: ctr command not found. Skipping containerd import.${NC}"
            echo -e "${YELLOW}If using containerd, you may need to manually import the image.${NC}"
        fi
        echo ""
    fi
else
    echo -e "${YELLOW}Skipping image build (--skip-build specified)${NC}"
    echo ""
fi

# Create deployment namespace if it doesn't exist
if ! kubectl get namespace "$DEPLOY_NAMESPACE" &> /dev/null; then
    echo -e "${YELLOW}Warning: Namespace '$DEPLOY_NAMESPACE' does not exist.${NC}"
    read -p "Do you want to create it? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        kubectl create namespace "$DEPLOY_NAMESPACE"
        echo -e "${GREEN}✓ Created namespace: $DEPLOY_NAMESPACE${NC}"
    else
        echo -e "${RED}Deployment cancelled.${NC}"
        exit 1
    fi
fi

# Create API credentials secret FIRST (before deployment)
# The deployment references this secret, so it must exist first
STEP_NUM=3
if [ "$BUILD_IMAGE" = false ]; then
    STEP_NUM=1
elif [ "$PUSH_IMAGE" = false ]; then
    STEP_NUM=2
fi

echo -e "${GREEN}Step $STEP_NUM: Creating/updating API credentials secret${NC}"
kubectl create secret generic cv-job-informer-api-credentials \
  --from-literal=api-server="$API_SERVER" \
  --from-literal=api-token="$API_TOKEN" \
  -n "$DEPLOY_NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -
echo -e "${GREEN}✓ Secret created/updated with actual credentials${NC}"
echo ""

# Apply deployment YAML (RBAC, ServiceAccount, Deployment)
((STEP_NUM++))
echo -e "${GREEN}Step $STEP_NUM: Applying deployment from job_informer.yaml${NC}"

# Apply the deployment YAML file directly
kubectl apply -f job_informer.yaml -n "$DEPLOY_NAMESPACE"

echo -e "${GREEN}✓ Deployment applied${NC}"
echo ""

# Update deployment with script parameters if they differ from defaults
# Note: Updating env vars will trigger a rollout restart automatically
NEEDS_UPDATE=false

if [ "$FULL_IMAGE_NAME" != "cloudvision-job-kubernetes:latest" ]; then
    echo "Updating image to: $FULL_IMAGE_NAME"
    kubectl set image deployment/cv-job-informer cv-job-informer="$FULL_IMAGE_NAME" -n "$DEPLOY_NAMESPACE"
    NEEDS_UPDATE=true
fi

# Always update env vars to ensure they match script parameters
echo "Updating deployment env vars (watch-namespace=${WATCH_NAMESPACE:-all}, log-level=$LOG_LEVEL, location=$LOCATION, jobconfig-mode=$JOBCONFIG_MODE, nodeconfig-mode=$NODECONFIG_MODE, node-interface-type=$NODE_INTERFACE_TYPE)"
kubectl set env deployment/cv-job-informer \
    NAMESPACE="$WATCH_NAMESPACE" \
    LOG_LEVEL="$LOG_LEVEL" \
    LOCATION="$LOCATION" \
    JOBCONFIG_MODE="$JOBCONFIG_MODE" \
    NODECONFIG_MODE="$NODECONFIG_MODE" \
    NODE_INTERFACE_TYPE="$NODE_INTERFACE_TYPE" \
    -n "$DEPLOY_NAMESPACE"
NEEDS_UPDATE=true

# Wait for rollout if we made updates
if [ "$NEEDS_UPDATE" = true ]; then
    echo "Waiting for deployment to update..."
    kubectl rollout status deployment/cv-job-informer -n "$DEPLOY_NAMESPACE" --timeout=60s
fi

# Wait for deployment to be ready
((STEP_NUM++))
echo -e "${GREEN}Step $STEP_NUM: Waiting for deployment to be ready...${NC}"
kubectl rollout status deployment/cv-job-informer -n "$DEPLOY_NAMESPACE" --timeout=120s

# Deploy interface discovery components if mode is 'discovery'
if [ "$NODECONFIG_MODE" = "discovery" ]; then
    ((STEP_NUM++))
    echo ""
    echo -e "${GREEN}Step $STEP_NUM: Deploying interface discovery components (NodeInterfaceState CRD + DaemonSet)${NC}"

    # Create a temporary YAML with the correct image and log level
    echo "Applying interface_discovery.yaml with image=$FULL_IMAGE_NAME and log-level=$LOG_LEVEL..."

    # Use sed to replace placeholders in interface_discovery.yaml
    sed -e "s|image: cloudvision-job-kubernetes:latest|image: $FULL_IMAGE_NAME|g" \
        -e "s|value: \"info\"|value: \"$LOG_LEVEL\"|g" \
        interface_discovery.yaml | kubectl apply -f - -n "$DEPLOY_NAMESPACE"

    echo -e "${GREEN}✓ CRD and DaemonSet deployed${NC}"
    echo "Waiting for DaemonSet to be ready..."
    kubectl rollout status daemonset/cv-interface-discovery -n "$DEPLOY_NAMESPACE" --timeout=60s || true
    echo ""
fi

echo ""
echo -e "${GREEN}=== Deployment Complete ===${NC}"
echo ""
echo "Deployment Information:"
echo "  Image:                     $FULL_IMAGE_NAME"
echo "  Deploy namespace:          $DEPLOY_NAMESPACE"
echo "  Watch namespaces:          ${WATCH_NAMESPACE:-all (cluster-wide)}"
echo "  API Server:                $API_SERVER"
echo "  Location:                  $LOCATION"
echo "  JobConfig Mode:            $JOBCONFIG_MODE"
echo "  NodeConfig mode:           $NODECONFIG_MODE"
echo "  Node interface type:       $NODE_INTERFACE_TYPE"
echo "  Log Level:                 $LOG_LEVEL"
echo ""
echo "Useful commands:"
echo "  View job-informer logs:    kubectl logs -f deployment/cv-job-informer -n $DEPLOY_NAMESPACE"
echo "  Check job-informer status: kubectl get pods -n $DEPLOY_NAMESPACE -l app=cv-job-informer"
echo "  Describe job-informer pod: kubectl describe pod -l app=cv-job-informer -n $DEPLOY_NAMESPACE"

if [ "$NODECONFIG_MODE" = "discovery" ]; then
echo "  View discovery logs:       kubectl logs -f daemonset/cv-interface-discovery -n $DEPLOY_NAMESPACE"
echo "  Check discovery status:    kubectl get pods -n $DEPLOY_NAMESPACE -l app=cv-interface-discovery"
echo "  List NodeInterfaceStates:  kubectl get nodeinterfacestates -n $DEPLOY_NAMESPACE"
fi

echo "  Delete deployment:         kubectl delete deployment,daemonset,sa,secret -l app=cv-job-informer -n $DEPLOY_NAMESPACE && kubectl delete clusterrole,clusterrolebinding cv-job-informer"
echo ""
echo -e "${GREEN}✓ CV Job Informer is now running!${NC}"

