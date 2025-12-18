# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""
CV Job Informer - Kubernetes Job Monitoring Service

Monitors Kubernetes training jobs by dynamically discovering parent resources
from pod ownerReferences and reports lifecycle events to CloudVision API.

Features:
- Dynamic parent resource discovery from pod ownerReferences
- Automatically creates informers for discovered resource types
- Tracks job lifecycle (start, completion, failure, cancellation)
- Extracts resource allocation (node names or network interface MACs)
- Reports job metadata and resource info to CloudVision API
"""

import json
import logging
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from kubernetes import client, config

import constants
from models import (
    TrackedJob,
    PodInfo,
    InterfaceInfo,
    DynamicResourceConfig,
    ParentResourceRef,
)
from api_utils import send_jobconfig
from node_monitor import NodeStateInformer
from pod_handler import PodEventHandler, PodInformer
from job_handler import JobEventHandler, CustomResourceInformer
from constants import (
    EVENT_TYPE_TO_JOB_STATE,
    TERMINATION_REASON_TO_JOB_STATE,
    SUPPORTED_JOB_RESOURCES,
)

logger = logging.getLogger(__name__)


class JobMonitor:
    """
    Dynamic job monitor that discovers parent resources from pod ownerReferences.

    Automatically creates informers for each discovered resource type (apiVersion + kind)
    and tracks job lifecycle events.

    Supports multiple namespace watching modes:
    - Empty set: Watch all namespaces cluster-wide
    - Single namespace: Watch only that namespace
    - Multiple namespaces: Watch all namespaces but filter events to specified namespaces
    """

    def __init__(self,
                 namespaces: set = None,
                 location: str = None,
                 nodeconfig_mode: str = "discovery",
                 node_interface_type: str = "all"):
        """
        Initialize monitor.

        Args:
            namespaces: Set of namespaces to monitor.
                       Empty set or None = watch all namespaces cluster-wide
                       Single namespace = watch only that namespace
                       Multiple namespaces = watch all namespaces, filter to specified ones
            location: Location identifier for API calls
            nodeconfig_mode: NodeConfig mode: "discovery", "sriovoperator", or "disabled"
            node_interface_type: Which node interfaces to include ("all", "pf", "vf")
        """
        try:
            config.load_kube_config()
        except:
            config.load_incluster_config()

        self.v1 = client.CoreV1Api()
        self.custom_api = client.CustomObjectsApi()

        # Store namespace configuration
        self.namespaces = namespaces if namespaces is not None else set()

        # Determine watch mode based on namespace configuration
        # - watch_all_namespaces: Whether to watch all namespaces (pass namespace="" to K8s API)
        # - filter_namespaces: Set of namespaces to filter events (empty = no filtering)
        if len(self.namespaces) == 0:
            # Empty set = watch all namespaces, no filtering
            self.watch_namespace = ""  # Empty string = all namespaces in K8s API
            self.filter_namespaces = set()  # No filtering needed
        elif len(self.namespaces) == 1:
            # Single namespace = watch only that namespace, no filtering
            self.watch_namespace = next(iter(self.namespaces))
            self.filter_namespaces = set()  # No filtering needed
        else:
            # Multiple namespaces = watch all namespaces, filter events
            self.watch_namespace = ""  # Watch all namespaces
            self.filter_namespaces = self.namespaces  # Filter to specified namespaces

        # location for job metadata sent to CloudVision API
        self.location = location or "default"

        # NodeConfig interface selection mode ("all", "pf", or "vf")
        self.node_interface_type = node_interface_type

        # Dynamic resource discovery
        # Discovered resource types: type_key -> DynamicResourceConfig
        self.discovered_resource_types: Dict[str, DynamicResourceConfig] = {}
        self.discovered_types_lock = threading.Lock()

        # Dynamic job informers: type_key -> CustomResourceInformer
        self.job_informers: Dict[str, CustomResourceInformer] = {}
        self.job_informers_lock = threading.Lock()

        # Tracked jobs and their associated resource config
        # job_key is the UID of the parent resource for unique identification
        self.tracked_jobs: Dict[str,
                                TrackedJob] = {}  # job_key (UID) -> TrackedJob
        # job_key (UID) -> DynamicResourceConfig (for accessing resource-specific methods)
        self.job_resource_configs: Dict[str, DynamicResourceConfig] = {}
        # job_key (UID) -> (set of pod UIDs, finish_timestamp)
        self.finished_jobs: Dict[str, Tuple[set, float]] = {}

        # Per-job locks: eliminates contention between different jobs
        # Use defaultdict to auto-create locks on first access
        self.job_locks: defaultdict[str, threading.Lock] = defaultdict(
            threading.Lock)

        # Global lock only for adding/removing jobs from tracked_jobs dict
        self.tracked_jobs_lock = threading.Lock()

        # Optional NodeConfig informer
        self.node_informer: Optional[NodeStateInformer] = None
        if nodeconfig_mode != 'disabled':
            if constants.API_SERVER and constants.API_TOKEN:
                try:
                    self.node_informer = NodeStateInformer(
                        api_server=constants.API_SERVER,
                        api_token=constants.API_TOKEN,
                        location=self.location,
                        node_interface_type=self.node_interface_type,
                        nodeconfig_mode=nodeconfig_mode)
                    logger.info(
                        "[INIT] NodeConfig informer enabled (mode=%s, interface_type=%s)",
                        nodeconfig_mode,
                        self.node_interface_type,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to initialize NodeConfig informer: {e}",
                        exc_info=True)
                    self.node_informer = None
            else:
                logger.warning(
                    "NodeConfig informer requested but API server/token are not configured; disabling."
                )

        # Pod cache: populated by pod watch, used by job handlers
        self.pod_cache: Dict[str,
                             client.V1Pod] = {}  # pod_name -> V1Pod object

        # Pod to job mapping (bidirectional for O(1) lookups)
        self.pod_to_job: Dict[str, str] = {}  # pod_name -> job_key
        self.job_to_pods: defaultdict[str, set] = defaultdict(
            set)  # job_key -> set of pod_names
        self.pod_to_job_lock = threading.Lock(
        )  # Single lock for bidirectional mapping

        # Queue for resource types discovered during pod initial sync
        # These will be processed after pod sync completes
        self.pending_resource_types: List[ParentResourceRef] = []
        self.pending_resource_types_lock = threading.Lock()

        # Create event handlers
        self.pod_handler = PodEventHandler(self)
        self.job_handler = JobEventHandler(self)

        # Create Pod informer (primary - discovers parent resources)
        pod_handlers = {
            'add': self.pod_handler.on_pod_add,
            'update': self.pod_handler.on_pod_update,
            'delete': self.pod_handler.on_pod_delete
        }

        # Watch pods based on namespace configuration
        self.pod_informer = PodInformer(
            v1_api=self.v1,
            namespace=self.watch_namespace,
            filter_namespaces=self.filter_namespaces,
            handlers=pod_handlers)

    def _extract_parent_resource(
            self, pod: client.V1Pod) -> Optional[ParentResourceRef]:
        """
        Extract parent resource information from pod ownerReferences.

        Returns the first owner reference that represents a supported job resource.
        Only resources in SUPPORTED_JOB_RESOURCES whitelist are considered.
        """
        if not pod.metadata.owner_references:
            return None

        namespace = pod.metadata.namespace

        for owner in pod.metadata.owner_references:
            # Extract API group from apiVersion (e.g., "batch/v1" -> "batch")
            # For core resources like "v1", the group is empty string
            api_version = owner.api_version
            if '/' in api_version:
                api_group = api_version.split('/')[0]
            else:
                api_group = ""

            # Only process resources in the whitelist
            if (api_group, owner.kind) not in SUPPORTED_JOB_RESOURCES:
                continue

            return ParentResourceRef(api_version=owner.api_version,
                                     kind=owner.kind,
                                     name=owner.name,
                                     uid=owner.uid,
                                     namespace=namespace)

        return None

    def _get_or_create_resource_config(
            self,
            parent_ref: ParentResourceRef,
            defer_informer: bool = False) -> DynamicResourceConfig:
        """
        Get or create a DynamicResourceConfig for the given parent resource type.

        Args:
            parent_ref: Reference to the parent resource
            defer_informer: If True, queue the resource type instead of creating informer immediately.
                           Used during pod initial sync to defer job informer creation.
        """
        type_key = f"{parent_ref.api_version}/{parent_ref.kind}"

        with self.discovered_types_lock:
            if type_key not in self.discovered_resource_types:
                resource_config = DynamicResourceConfig(
                    api_version=parent_ref.api_version, kind=parent_ref.kind)
                self.discovered_resource_types[type_key] = resource_config
                logger.info(
                    f"[DISCOVERY] New resource type discovered: {type_key} "
                    f"(group={resource_config.group}, version={resource_config.version}, "
                    f"plural={resource_config.plural})")

                if defer_informer:
                    # Queue for later processing (during pod initial sync)
                    with self.pending_resource_types_lock:
                        self.pending_resource_types.append(parent_ref)
                    logger.debug(
                        f"[DISCOVERY] Queued {type_key} for deferred informer creation"
                    )
                else:
                    # Create and start informer immediately (normal operation)
                    self._create_informer_for_resource_type(resource_config)

            return self.discovered_resource_types[type_key]

    def _create_informer_for_resource_type(
            self, resource_config: DynamicResourceConfig):
        """
        Create and start a CustomResourceInformer for a discovered resource type.
        """
        type_key = resource_config.type_key

        with self.job_informers_lock:
            if type_key in self.job_informers:
                return  # Already have an informer for this type

            job_handlers = {
                'add':
                lambda job_obj, is_initial_sync=False: self.job_handler.
                on_job_add(job_obj, resource_config, is_initial_sync),
                'update':
                lambda job_obj: self.job_handler.on_job_update(
                    job_obj, resource_config),
                'delete':
                lambda job_obj: self.job_handler.on_job_delete(
                    job_obj, resource_config)
            }

            informer = CustomResourceInformer(
                custom_api=self.custom_api,
                namespace=self.watch_namespace,
                filter_namespaces=self.filter_namespaces,
                config=resource_config,
                handlers=job_handlers)

            self.job_informers[type_key] = informer
            logger.info(f"[INFORMER] Created informer for {type_key}")

            # Start the informer
            informer.start()

    def _process_pending_resource_types(self):
        """
        Process queued resource types and create informers for them.
        Called after pod initial sync completes to ensure pod cache is populated.
        """
        with self.pending_resource_types_lock:
            if not self.pending_resource_types:
                logger.debug(
                    "[DISCOVERY] No pending resource types to process")
                return

            logger.info(
                f"[DISCOVERY] Processing {len(self.pending_resource_types)} pending resource types..."
            )

            for parent_ref in self.pending_resource_types:
                type_key = f"{parent_ref.api_version}/{parent_ref.kind}"
                with self.discovered_types_lock:
                    resource_config = self.discovered_resource_types.get(
                        type_key)
                    if resource_config:
                        logger.info(
                            f"[DISCOVERY] Creating informer for queued type: {type_key}"
                        )
                        self._create_informer_for_resource_type(
                            resource_config)

            # Clear the queue
            self.pending_resource_types.clear()
            logger.info(
                "[DISCOVERY] Finished processing pending resource types")

    def get_job_pods(self, job_key: str, job_name: str) -> List[client.V1Pod]:
        """Get pods for a specific job from cache (populated by pod watch)"""
        cached_pods = self.get_cached_pods_for_job(job_key)
        logger.debug(
            f"Found {len(cached_pods)} cached pods for job {job_name} (key={job_key})"
        )
        return cached_pods

    def extract_job_times(
            self, job_obj: Dict,
            pod_infos: List[PodInfo]) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract job start and end times from job resource and pods.

        Tries multiple sources in order of preference:
        - For start_time: job status.startTime -> earliest pod start_time -> job creationTimestamp
        - For end_time: job status.completionTime -> latest pod end_time -> current time
        """
        status = job_obj.get('status', {})
        metadata = job_obj.get('metadata', {})

        # Extract start_time
        # 1. Try job's status.startTime (batch/v1 Jobs have this)
        start_time = status.get('startTime')

        # 2. Fallback to earliest pod start_time
        if not start_time and pod_infos:
            pod_start_times = [p.start_time for p in pod_infos if p.start_time]
            if pod_start_times:
                start_time = min(pod_start_times)

        # 3. Fallback to job creation timestamp
        if not start_time:
            start_time = metadata.get('creationTimestamp')

        # Extract end_time
        # 1. Try job's status.completionTime
        end_time = status.get('completionTime')

        # 2. Fallback to latest pod end_time
        if not end_time and pod_infos:
            pod_end_times = [p.end_time for p in pod_infos if p.end_time]
            if pod_end_times:
                end_time = max(pod_end_times)

        # 3. Fallback to current time for cancelled jobs
        if not end_time:
            from datetime import datetime, timezone
            end_time = datetime.now(timezone.utc).isoformat()

        return start_time, end_time

    def _extract_network_interfaces(self,
                                    pod: client.V1Pod) -> List[InterfaceInfo]:
        """Extract network interface information from pod Multus annotations."""
        # Parse network-status annotation
        metadata = getattr(pod, "metadata", None) or {}
        annotations = getattr(metadata, "annotations", None) or {}
        net_status_raw = annotations.get("k8s.v1.cni.cncf.io/network-status")
        if not net_status_raw:
            return []

        try:
            net_status = json.loads(net_status_raw) if isinstance(
                net_status_raw, str) else net_status_raw
        except Exception as e:
            logger.error(
                "Failed to parse network-status annotation for pod %s: %s",
                getattr(metadata, "name", "<unknown>"),
                e,
            )
            return []

        # Normalize to list
        if isinstance(net_status, dict):
            net_status = [net_status]
        elif not isinstance(net_status, list):
            return []

        # Extract secondary network interfaces
        interfaces = []
        for net in net_status:
            if not isinstance(net, dict):
                continue

            # Skip default network (eth0)
            if net.get('default') or net.get('interface') == 'eth0':
                continue

            # Only process secondary networks (RDMA, SR-IOV, etc.)
            interface_name = net.get('interface', '')
            if not (interface_name.startswith('net')
                    or 'rdma' in net.get('name', '').lower()):
                continue

            # Extract device info (SR-IOV Device Plugin)
            device_info = net.get('device-info', {})
            pci_info = device_info.get('pci', {}) if device_info else {}

            ips = net.get('ips', [])
            interfaces.append(
                InterfaceInfo(interface=interface_name,
                              ip=ips[0] if ips else None,
                              mac=net.get('mac'),
                              rdma_device=pci_info.get('rdma-device'),
                              pci_address=pci_info.get('pci-address')))

        return interfaces

    def extract_pod_info(self, pod: client.V1Pod) -> PodInfo:
        """Extract relevant information from a pod for API and logging"""
        network_interfaces = self._extract_network_interfaces(pod)

        # Extract end time from container states if pod is finished
        end_time = None
        if pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                if container_status.state:
                    # Check terminated state
                    if container_status.state.terminated:
                        finished_at = container_status.state.terminated.finished_at
                        if finished_at:
                            end_time = finished_at.isoformat()
                            break

        return PodInfo(
            name=pod.metadata.name,
            node=pod.spec.node_name or 'Not assigned',
            phase=pod.status.phase,
            start_time=pod.status.start_time.isoformat()
            if pod.status.start_time else None,
            end_time=end_time,
            network_interfaces=network_interfaces,
        )

    def process_job_event(self,
                          event_type: str,
                          job_name: str,
                          namespace: str,
                          pods: List[PodInfo],
                          termination_reason: Optional[str] = None,
                          job_uid: str = None,
                          start_time: Optional[str] = None,
                          end_time: Optional[str] = None):
        """Process job event: send to CloudVision API and log details"""

        # Filter to only include pods with valid info (node assigned)
        valid_pods = [p for p in pods if p.node != 'Not assigned']

        # Send job data to API
        self._send_job_event_to_api(event_type, job_name, valid_pods,
                                    termination_reason, job_uid, start_time,
                                    end_time)

        # Log human-readable summary
        if event_type == 'STARTED':
            logger.info(
                f"[JOB-START] Job STARTED: {job_name} in {namespace}, {len(valid_pods)} running pods"
            )

            # Group pods by node and show network interfaces (debug level)
            for pod in valid_pods:
                logger.debug(f"Node {pod.node}, pod {pod.name}:")
                if pod.network_interfaces:
                    for iface in pod.network_interfaces:
                        logger.debug(
                            f"-  {iface.interface} IP={iface.ip} MAC={iface.mac} Device={iface.rdma_device} PCI={iface.pci_address}"
                        )
                else:
                    logger.debug("-  No secondary network interfaces")

        elif event_type == 'UPDATE':
            logger.info(
                f"[JOB-UPDATE] Job UPDATE: {job_name} in {namespace}, {len(valid_pods)} running pods"
            )

            # Show updated interface list (debug level)
            for pod in valid_pods:
                logger.debug(f"Node {pod.node}, pod {pod.name}:")
                if pod.network_interfaces:
                    for iface in pod.network_interfaces:
                        logger.debug(
                            f"-  {iface.interface} IP={iface.ip} MAC={iface.mac} Device={iface.rdma_device} PCI={iface.pci_address}"
                        )
                else:
                    logger.debug("-  No secondary network interfaces")

        elif event_type == 'FINISHED':
            # Determine status message based on termination reason
            if termination_reason == 'SUCCEEDED':
                status_msg = "SUCCEEDED"
            elif termination_reason == 'CANCELLED':
                status_msg = "CANCELLED"
            else:  # FAILED
                status_msg = "FAILED"

            # Count pod phases for summary
            phase_summary = {}
            for pod in valid_pods:
                phase_summary[pod.phase] = phase_summary.get(pod.phase, 0) + 1
            phase_str = ", ".join(
                [f"{count} {phase}" for phase, count in phase_summary.items()])

            logger.info(
                f"[JOB-FINISH] Job {status_msg}: {job_name} in {namespace}, pod phases: {phase_str}"
            )

    def _send_job_event_to_api(self, event_type: str, job_name: str,
                               valid_pods: List[PodInfo],
                               termination_reason: Optional[str], job_uid: str,
                               start_time: Optional[str],
                               end_time: Optional[str]):
        """
        Send job event to JobConfig API

        Args:
            event_type: 'STARTED' or 'FINISHED'
            job_name: Name of the job
            valid_pods: List of PodInfo objects
            termination_reason: Termination reason for FINISHED events
            job_uid: Job UID from CRD metadata - used as unique job_id for API
            start_time: Job start time (ISO format) - when job started running
            end_time: Job end time (ISO format) - when job finished/cancelled
        """
        # Skip if API is not configured
        if not constants.API_SERVER or not constants.API_TOKEN:
            return

        # Extract unique node names from pods
        nodes = list(set([p.node for p in valid_pods]))

        # Extract unique interface MAC addresses from pods
        # These come from Multus CNI network-status annotation
        interfaces = list({
            iface.mac
            for pod in valid_pods
            for iface in pod.network_interfaces if iface.mac
        })

        # Determine job state based on event type using mapping dictionaries
        if event_type in EVENT_TYPE_TO_JOB_STATE:
            job_state = EVENT_TYPE_TO_JOB_STATE[event_type]
        elif event_type == 'FINISHED':
            # Map termination reason to job state
            job_state = TERMINATION_REASON_TO_JOB_STATE.get(
                termination_reason, 'JOB_STATE_FAILED')
            if termination_reason not in TERMINATION_REASON_TO_JOB_STATE:
                logger.warning(
                    f"Unknown termination reason '{termination_reason}', defaulting to JOB_STATE_FAILED"
                )
        else:
            logger.warning(f"Unknown event type: {event_type}")
            return

        # Send to API (job_uid guarantees uniqueness)
        send_jobconfig(api_server=constants.API_SERVER,
                       api_token=constants.API_TOKEN,
                       job_id=job_uid,
                       job_name=job_name,
                       location=self.location,
                       job_state=job_state,
                       nodes=nodes,
                       start_time=start_time,
                       end_time=end_time,
                       interfaces=interfaces,
                       jobconfig_mode=constants.JOBCONFIG_MODE)

    # ========== Pod Cache Management ==========

    def get_cached_pods_for_job(self, job_key: str) -> List[client.V1Pod]:
        """Get all cached pods for a specific job using job_key"""
        pod_names = list(self.job_to_pods.get(job_key, set()))

        return [
            self.pod_cache[name] for name in pod_names
            if name in self.pod_cache
        ]

    def cleanup_cached_pods_for_job(self, job_key: str):
        """Remove all cached pods associated with a specific job"""
        with self.pod_to_job_lock:
            pod_names = self.job_to_pods.pop(job_key, set())
            for pod_name in pod_names:
                self.pod_to_job.pop(pod_name, None)

        # Remove pods from cache
        for pod_name in pod_names:
            self.pod_cache.pop(pod_name, None)

        if pod_names:
            logger.debug(
                f"[CLEANUP] Removed {len(pod_names)} cached pods for job {job_key}"
            )

    def cleanup_old_finished_jobs(self):
        """Remove finished jobs older than TTL to prevent memory leak"""
        cutoff_time = time.time() - constants.FINISHED_JOBS_TTL
        with self.tracked_jobs_lock:
            expired = [
                job_key
                for job_key, (_, finish_time) in self.finished_jobs.items()
                if finish_time < cutoff_time
            ]
            for job_key in expired:
                del self.finished_jobs[job_key]

        if expired:
            logger.debug(
                f"[CLEANUP] Removed {len(expired)} finished jobs older than {constants.FINISHED_JOBS_TTL}s"
            )

    # ========== Namespace Filtering Helper ==========

    def should_process_namespace(self, namespace: str) -> bool:
        """
        Check if events from the given namespace should be processed.

        Returns True if:
        - No filter is set (filter_namespaces is empty) = process all namespaces
        - The namespace is in the filter set
        """
        if not self.filter_namespaces:
            return True  # No filtering, process all
        return namespace in self.filter_namespaces

    # ========== Main Loop ==========

    def run(self):
        """Start monitoring jobs"""
        # Log namespace configuration
        if len(self.namespaces) == 0:
            logger.info(
                "[INIT] Starting job monitor for ALL namespaces (cluster-wide)"
            )
        elif len(self.namespaces) == 1:
            logger.info(
                f"[INIT] Starting job monitor for namespace: {next(iter(self.namespaces))}"
            )
        else:
            logger.info(
                f"[INIT] Starting job monitor for namespaces: {', '.join(sorted(self.namespaces))} "
                "(watching all, filtering to specified)")

        logger.info(f"[INIT] Location for API: {self.location}")
        logger.info(f"[INIT] API Server: {constants.API_SERVER}")
        logger.info(f"[INIT] JobConfig Mode: {constants.JOBCONFIG_MODE}")

        # Start NodeConfig informer if enabled
        if self.node_informer:
            self.node_informer.start()

        # Start pod informer
        self.pod_informer.start()

        # Wait for pod informer initial sync to complete
        # This ensures pod cache is populated before job informers start
        logger.info(
            "[INIT] Waiting for pod informer initial sync to complete before starting job informers..."
        )
        elapsed = 0

        while not self.pod_informer.initial_sync_done and elapsed < constants.POD_INFORMER_SYNC_MAX_WAIT:
            time.sleep(constants.POD_INFORMER_SYNC_POLL_INTERVAL)
            elapsed += constants.POD_INFORMER_SYNC_POLL_INTERVAL

        if self.pod_informer.initial_sync_done:
            logger.info(
                f"[INIT] Pod informer initial sync complete (waited {elapsed:.1f}s)"
            )
        else:
            logger.warning(
                f"Pod informer initial sync not complete after {constants.POD_INFORMER_SYNC_MAX_WAIT}s, proceeding anyway"
            )

        # Process any resource types discovered during pod initial sync
        # This creates job informers now that pod cache is populated
        self._process_pending_resource_types()

        # Keep running with periodic cleanup
        last_cleanup = time.time()
        try:
            while True:
                time.sleep(1)

                # Periodic cleanup of old finished jobs
                if time.time(
                ) - last_cleanup >= constants.FINISHED_JOBS_CLEANUP_INTERVAL:
                    self.cleanup_old_finished_jobs()
                    last_cleanup = time.time()

        except KeyboardInterrupt:
            logger.info("[SHUTDOWN] Shutting down...")
            self.pod_informer.stop()
            for informer in self.job_informers.values():
                informer.stop()
            if self.node_informer:
                self.node_informer.stop()
