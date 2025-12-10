# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""
Data models and resource configuration for CV Job Informer.

This module contains:
- Data classes for job and pod tracking (InterfaceInfo, PodInfo, TrackedJob)
- Dynamic resource configuration (ParentResourceRef, DynamicResourceConfig)
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from kubernetes import client

logger = logging.getLogger(__name__)

# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class InterfaceInfo:
    """Network interface information extracted from pod annotations"""
    interface: str
    ip: Optional[str]
    mac: Optional[str]
    rdma_device: Optional[
        str] = None  # Logging only, from SR-IOV Device Plugin
    pci_address: Optional[
        str] = None  # Logging only, from SR-IOV Device Plugin


@dataclass
class PodInfo:
    """Pod information for tracking and API reporting"""
    name: str
    node: str
    phase: str
    start_time: Optional[str]
    end_time: Optional[str]
    network_interfaces: List[InterfaceInfo] = field(default_factory=list)


@dataclass
class TrackedJob:
    """
    Tracked job information

    Status values:
    - 'PENDING': Job created, waiting for first pod to start running
    - 'RUNNING': At least one pod running, job is running (STARTED event sent to API)
    - 'FINISHED': Job completed/failed/cancelled (FINISHED event sent to API)
    """
    job_key: str
    job_name: str
    namespace: str
    pods: Dict[str,
               PodInfo] = field(default_factory=dict)  # pod_name -> PodInfo
    pod_objects: Dict[str, client.V1Pod] = field(
        default_factory=dict)  # pod_name -> V1Pod
    status: str = 'PENDING'
    # Timing fields for stability-based events (STARTED/UPDATE)
    first_pod_running_time: Optional[
        float] = None  # When first pod became Running
    last_pod_update_time: Optional[float] = None  # Time of last pod update
    event_timer: Optional[threading.Timer] = None  # Timer for delayed event
    # Interfaces sent to API (set of MAC addresses) - for detecting changes
    sent_interfaces: Set[str] = field(default_factory=set)


# ============================================================================
# Dynamic Resource Configuration
# ============================================================================


@dataclass
class ParentResourceRef:
    """Information about a parent resource discovered from pod ownerReferences"""
    api_version: str  # e.g., "batch/v1", "run.ai/v1", "argoproj.io/v1alpha1"
    kind: str  # e.g., "Job", "RunaiJob", "Workflow"
    name: str  # Resource name
    uid: str  # Resource UID
    namespace: str  # Namespace where the resource exists


class DynamicResourceConfig:
    """
    Dynamic configuration for any parent resource type discovered from pod ownerReferences.

    Parses apiVersion and kind to determine the CRD group, version, and plural name.
    Works with any resource type without requiring code changes.
    """

    def __init__(self, api_version: str, kind: str):
        """
        Initialize from apiVersion and kind (extracted from pod ownerReferences).

        Args:
            api_version: API version string (e.g., "batch/v1", "run.ai/v1")
            kind: Resource kind (e.g., "Job", "RunaiJob", "Workflow")
        """
        self.api_version = api_version
        self.kind = kind

        # Parse apiVersion into group and version
        if '/' in api_version:
            self._group, self._version = api_version.rsplit('/', 1)
        else:
            self._group = ""
            self._version = api_version

        self._plural = self._pluralize(kind)
        self._type_key = f"{api_version}/{kind}"

    @staticmethod
    def _pluralize(kind: str) -> str:
        """Convert kind to plural resource name."""
        lower = kind.lower()
        if lower.endswith('s') or lower.endswith('x') or lower.endswith(
                'ch') or lower.endswith('sh'):
            return lower + 'es'
        elif lower.endswith('y') and len(
                lower) > 1 and lower[-2] not in 'aeiou':
            return lower[:-1] + 'ies'
        else:
            return lower + 's'

    @property
    def name(self) -> str:
        """Human-readable name of the resource type"""
        return self.kind

    @property
    def group(self) -> str:
        """API group (e.g., 'batch', 'run.ai', 'argoproj.io')"""
        return self._group

    @property
    def version(self) -> str:
        """API version (e.g., 'v1', 'v1alpha1')"""
        return self._version

    @property
    def plural(self) -> str:
        """Resource plural name (e.g., 'jobs', 'runaijobs', 'workflows')"""
        return self._plural

    @property
    def type_key(self) -> str:
        """Unique key identifying this resource type (apiVersion/kind)"""
        return self._type_key

    def get_job_key(self, job_obj: Dict) -> Optional[str]:
        """Extract job key from resource object - uses UID for unique identification"""
        try:
            return job_obj['metadata']['uid']
        except Exception as e:
            logger.error(f"Error getting job key from {self.kind}: {e}")
            return None

    def is_completed(self, job_obj: Dict) -> Tuple[bool, bool, bool]:
        """
        Check if the resource is completed and determine its final state.

        Returns:
            Tuple of (is_completed, is_succeeded, is_failed)
        """
        if not job_obj:
            return (False, False, False)

        status = job_obj.get('status')
        if not status:
            return (False, False, False)

        # 1. Check Conditions (Most reliable for K8s Jobs, JobSets)
        conditions = status.get('conditions', [])

        success_condition_types = {
            'complete', 'completed', 'succeeded', 'jobcomplete'
        }
        fail_condition_types = {'failed', 'jobfailed', 'error'}

        for condition in conditions:
            cond_type = condition.get('type', '').lower()
            cond_status = str(condition.get('status', '')).lower()

            if cond_status == 'true':
                if cond_type in success_condition_types:
                    return (True, True, False)
                if cond_type in fail_condition_types:
                    return (True, False, True)

        # 2. Check Phase/State (Most reliable for Argo, Spark, Pods)
        phase = status.get('phase') or status.get('state') or ''
        phase = phase.lower()

        success_phases = {'succeeded', 'completed', 'complete'}
        fail_phases = {'failed', 'error', 'nodeadline'}

        if phase in success_phases:
            return (True, True, False)
        if phase in fail_phases:
            return (True, False, True)

        return (False, False, False)
