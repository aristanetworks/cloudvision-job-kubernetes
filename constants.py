# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""
Constants and configuration for CV Job Informer.

This module contains all constants used across the application:
- API configuration (set via command-line arguments)
- Job event timing configuration
- Job state mappings for CloudVision API
- SR-IOV Network Operator configuration
- Interface discovery configuration
"""

from pathlib import Path
from typing import Optional

# ============================================================================
# API Configuration (set via command-line arguments in main.py)
# ============================================================================
API_SERVER: Optional[str] = None
API_TOKEN: Optional[str] = None
JOBCONFIG_MODE: str = 'interface'  # 'node' or 'interface'

# ============================================================================
# Informer Initialization Configuration
# ============================================================================
# Maximum time to wait for pod informer initial sync to complete before
# starting job informers. This ensures pod cache is populated before
# processing completed jobs during initial sync.
POD_INFORMER_SYNC_MAX_WAIT: float = 60.0  # seconds

# Polling interval when waiting for pod informer initial sync
POD_INFORMER_SYNC_POLL_INTERVAL: float = 0.5  # seconds

# ============================================================================
# Memory Management Configuration
# ============================================================================
# Time to keep finished job records in memory (for deduplication)
# After this period, finished jobs are removed to prevent memory leak
FINISHED_JOBS_TTL: float = 3600.0 * 24.0 * 7.0  # seconds (1 week)

# Interval for periodic cleanup of old finished jobs
FINISHED_JOBS_CLEANUP_INTERVAL: float = 600.0  # seconds (10 minutes)

# ============================================================================
# Job Event Stability Configuration
# ============================================================================
# Wait this many seconds after the last pod update before sending job events
# (STARTED or UPDATE). This allows time for all pods to be scheduled and
# Multus to attach interfaces, and reduces churn during pod restarts.
JOB_START_STABILITY_DELAY: float = 10.0  # seconds

# Maximum time to wait for pod updates to stabilize before sending STARTED anyway.
# This is a safety net to prevent indefinitely waiting if pods keep updating.
# Only applies to STARTED event (not UPDATE).
JOB_START_MAX_WAIT: float = 60.0  # seconds

# ============================================================================
# Job State Mapping for CloudVision API
# ============================================================================
# Map event types to CloudVision job states
EVENT_TYPE_TO_JOB_STATE = {
    'STARTED': 'JOB_STATE_RUNNING',
    'UPDATE': 'JOB_STATE_RUNNING',
}

# Map termination reasons to CloudVision job states
TERMINATION_REASON_TO_JOB_STATE = {
    'SUCCEEDED': 'JOB_STATE_COMPLETED',
    'CANCELLED': 'JOB_STATE_CANCELLED',
    'FAILED': 'JOB_STATE_FAILED',
}

# ============================================================================
# Supported Job Resource Types (Whitelist)
# ============================================================================
# Only these resource types will be monitored as job resources.
# The key is (apiGroup, kind) tuple, matching the RBAC permissions in deployment.yaml.
# To add support for additional resource types:
#   1. Add RBAC permissions in deployment.yaml
#   2. Add the (apiGroup, kind) tuple to this set
#
# Note: apiGroup is the API group (e.g., "batch", "kubeflow.org"), not the full apiVersion.
#       For core resources, use empty string "" as the apiGroup.
SUPPORTED_JOB_RESOURCES = {
    # Kubernetes batch Jobs (also created by JobSet, so JobSet jobs are tracked via their child Jobs)
    ("batch", "Job"),
    # Kubeflow Training Operator
    ("kubeflow.org", "PyTorchJob"),
    ("kubeflow.org", "TFJob"),
    ("kubeflow.org", "MPIJob"),
    ("kubeflow.org", "XGBoostJob"),
    ("kubeflow.org", "PaddleJob"),
    # Kubeflow Trainer v2
    ("trainer.kubeflow.org", "TrainJob"),
    # Argo Workflows
    ("argoproj.io", "Workflow"),
    ("argoproj.io", "WorkflowTemplate"),
    ("argoproj.io", "CronWorkflow"),
    # Run:ai Workloads
    ("run.ai", "RunaiJob"),
    ("run.ai", "TrainingWorkload"),
    ("run.ai", "InferenceWorkload"),
    ("run.ai", "InteractiveWorkload"),
    # Volcano batch scheduler
    ("batch.volcano.sh", "Job"),
    # KubeRay
    ("ray.io", "RayJob"),
    ("ray.io", "RayCluster"),
}

# ============================================================================
# NodeConfig CR Configuration
# ============================================================================
# NodeInterfaceState CR (from cv-interface-discovery daemonset)
CV_INTERFACE_GROUP = "cloudvision.arista.io"
CV_INTERFACE_VERSION = "v1"
CV_INTERFACE_PLURAL = "nodeinterfacestates"
CV_INTERFACE_NAMESPACE = "cloudvision"

# SriovNetworkNodeState CR (from SR-IOV Network Operator)
SRIOV_GROUP = "sriovnetwork.openshift.io"
SRIOV_VERSION = "v1"
SRIOV_PLURAL = "sriovnetworknodestates"
SRIOV_NAMESPACE = "network-operator"

# ============================================================================
# Interface Discovery Configuration
# ============================================================================
# Discovery interval in seconds (how often to scan for interface changes)
DISCOVERY_INTERVAL = 60  # seconds

# Path to sysfs network interfaces
SYSFS_NET_PATH = Path("/sys/class/net")
