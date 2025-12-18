# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""
Job lifecycle event handling for CV Job Informer.

Handles job resource events (add/update/delete) and manages job state transitions,
completion detection, and termination reason determination.

Also contains CustomResourceInformer for watching Kubernetes custom resources.
"""

import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, List

from kubernetes import client, watch

from models import TrackedJob, PodInfo, DynamicResourceConfig

if TYPE_CHECKING:
    from job_monitor import JobMonitor

logger = logging.getLogger(__name__)


class CustomResourceInformer:
    """
    Generic informer for custom resources (Job, RunaiJob, Workflow, etc.)

    Uses DynamicResourceConfig to handle resource-specific details.
    Created dynamically when a new parent resource type is discovered.

    Supports namespace filtering when watching all namespaces but only
    processing events from specific namespaces.
    """

    def __init__(self,
                 custom_api,
                 namespace: str,
                 config: DynamicResourceConfig,
                 handlers: Dict,
                 filter_namespaces: set = None):
        """
        Initialize CustomResourceInformer.

        Args:
            custom_api: Kubernetes CustomObjectsApi client
            namespace: Namespace to watch. Empty string = all namespaces.
            config: DynamicResourceConfig for the resource type
            handlers: Dict of event handlers (add, update, delete)
            filter_namespaces: Set of namespaces to filter events to.
                              Empty set or None = no filtering (process all events)
        """
        self.custom_api = custom_api
        self.namespace = namespace
        self.config = config
        self.handlers = handlers
        self.filter_namespaces = filter_namespaces or set()

        self.running = False
        self.watch_thread = None
        self.initial_sync_done = False

    def _should_process_job(self, job_obj: Dict) -> bool:
        """Check if job should be processed based on namespace filtering."""
        if not self.filter_namespaces:
            return True  # No filtering, process all jobs
        job_namespace = job_obj.get('metadata', {}).get('namespace', '')
        return job_namespace in self.filter_namespaces

    def _sync_cache(self):
        """Initial sync - process all existing jobs through handlers"""
        try:
            logger.info(f"[SYNC] Syncing {self.config.name}...")

            # Choose list method based on namespace setting
            if self.namespace == "":
                jobs = self.custom_api.list_cluster_custom_object(
                    group=self.config.group,
                    version=self.config.version,
                    plural=self.config.plural)
            else:
                jobs = self.custom_api.list_namespaced_custom_object(
                    group=self.config.group,
                    version=self.config.version,
                    namespace=self.namespace,
                    plural=self.config.plural)

            job_items = jobs.get('items', [])

            # Process each job through handlers (with filtering if needed)
            processed_count = 0
            for job_obj in job_items:
                if self._should_process_job(job_obj):
                    if 'add' in self.handlers:
                        self.handlers['add'](job_obj, is_initial_sync=True)
                    processed_count += 1

            logger.info(
                f"[SYNC] {self.config.name} synced: {processed_count} jobs processed"
            )
            self.initial_sync_done = True
        except Exception as e:
            logger.error(f"Error syncing {self.config.name}: {e}")

    def _watch_loop(self):
        """Main watch loop with automatic reconnection"""
        # Log what we're watching
        if self.namespace == "":
            if self.filter_namespaces:
                logger.info(
                    f"[INFORMER] Starting {self.config.name} watch loop (all namespaces, filtering to: {', '.join(sorted(self.filter_namespaces))})"
                )
            else:
                logger.info(
                    f"[INFORMER] Starting {self.config.name} watch loop (all namespaces)"
                )
        else:
            logger.info(
                f"[INFORMER] Starting {self.config.name} watch loop for namespace: {self.namespace}"
            )

        while self.running:
            try:
                w = watch.Watch()

                # Choose watch method based on namespace setting
                if self.namespace == "":
                    stream = w.stream(
                        self.custom_api.list_cluster_custom_object,
                        group=self.config.group,
                        version=self.config.version,
                        plural=self.config.plural,
                        timeout_seconds=0)
                else:
                    stream = w.stream(
                        self.custom_api.list_namespaced_custom_object,
                        group=self.config.group,
                        version=self.config.version,
                        namespace=self.namespace,
                        plural=self.config.plural,
                        timeout_seconds=0)

                for event in stream:
                    if not self.running:
                        break

                    event_type = event['type']
                    job_obj = event['object']

                    # Apply namespace filtering
                    if not self._should_process_job(job_obj):
                        continue

                    job_key = self.config.get_job_key(job_obj)

                    if not job_key:
                        continue  # Skip jobs without valid job key

                    # Trigger handlers (handlers maintain their own state)
                    if event_type == 'ADDED':
                        if 'add' in self.handlers:
                            self.handlers['add'](job_obj)

                    elif event_type == 'MODIFIED':
                        if 'update' in self.handlers:
                            self.handlers['update'](job_obj)

                    elif event_type == 'DELETED':
                        if 'delete' in self.handlers:
                            self.handlers['delete'](job_obj)

                if self.running:
                    logger.debug(
                        f"{self.config.name} watch stream ended, reconnecting..."
                    )
                    time.sleep(1)

            except client.exceptions.ApiException as e:
                if e.status == 410:  # Gone - resource version too old
                    logger.warning(
                        f"{self.config.name} resource version expired (410), resyncing..."
                    )
                    self._sync_cache()
                    time.sleep(1)
                else:
                    logger.error(
                        f"API error in {self.config.name} watch: {e}. Reconnecting in 5s..."
                    )
                    time.sleep(5)

            except Exception as e:
                if self.running:
                    logger.error(
                        f"Error in {self.config.name} watch loop: {e}. Reconnecting in 5s..."
                    )
                    time.sleep(5)

    def start(self):
        """Start the informer"""
        if self.running:
            logger.warning(f"{self.config.name} informer already running")
            return

        self.running = True

        # Initial cache sync
        self._sync_cache()

        # Start watch thread
        self.watch_thread = threading.Thread(target=self._watch_loop,
                                             daemon=True)
        self.watch_thread.start()

        logger.info(f"[INFORMER] {self.config.name} informer started")

    def stop(self):
        """Stop the informer"""
        logger.info(f"[INFORMER] Stopping {self.config.name} informer...")
        self.running = False
        if self.watch_thread:
            self.watch_thread.join(timeout=10)
        logger.info(f"[INFORMER] {self.config.name} informer stopped")


class JobEventHandler:
    """Handles job lifecycle events and state transitions."""

    def __init__(self, monitor: 'JobMonitor'):
        """Initialize with reference to parent JobMonitor."""
        self.monitor = monitor

    def _determine_termination_reason(self, is_succeeded: bool,
                                      is_failed: bool,
                                      pod_infos: List[PodInfo],
                                      pod_objects: Dict) -> str:
        """
        Determine the termination reason for a finished job."""
        if is_succeeded:
            return 'SUCCEEDED'

        if is_failed:
            if len(pod_infos) > 0:
                # Check if all pods failed
                all_failed = all(p.phase == 'Failed' for p in pod_infos)
                if all_failed:
                    # Check if pods were cancelled (deletionTimestamp set)
                    was_cancelled = any(
                        pod.metadata.deletion_timestamp is not None
                        for pod in pod_objects.values())
                    return 'CANCELLED' if was_cancelled else 'FAILED'
                return 'FAILED'
            return 'FAILED'

        return 'UNKNOWN'

    def _handle_preexisting_completed_job(
            self, job_obj: Dict, job_key: str, name: str, namespace: str,
            is_succeeded: bool, is_failed: bool,
            resource_config: DynamicResourceConfig):
        """
        Handle a completed job found during initial sync.

        This method processes jobs that were already completed before the informer started.
        It extracts timing information from the job resource and pods, then logs a FINISHED event.
        """
        # Get cached pods for this job
        pods = self.monitor.get_cached_pods_for_job(job_key)
        logger.info(
            f"[SYNC] Processing pre-existing completed {resource_config.name}: "
            f"{name}, found {len(pods)} cached pods")

        # Extract pod info
        pod_infos = []
        for pod in pods:
            pod_info = self.monitor.extract_pod_info(pod)
            if pod_info:
                pod_infos.append(pod_info)

        if len(pod_infos) == 0:
            logger.warning(
                f"[INITIAL-SYNC] No pod info available for completed job {name}, "
                "skipping API notification")
            return

        # Determine termination reason using helper method
        # Note: For pre-existing jobs, we don't have pod_objects, so pass empty dict
        termination_reason = self._determine_termination_reason(
            is_succeeded, is_failed, pod_infos, {})

        # Extract job times from job resource and pods
        start_time, end_time = self.monitor.extract_job_times(
            job_obj, pod_infos)

        # Skip sending if we couldn't extract proper timestamps
        if not start_time or not end_time:
            logger.warning(
                f"[SYNC] Skipping job {name}: "
                f"unable to extract timestamps (start_time={start_time}, end_time={end_time})"
            )
            return

        logger.info(
            f"[SYNC] Job {name}: start_time={start_time}, end_time={end_time}, "
            f"termination_reason={termination_reason}")

        # Send FINISHED event to API
        self.monitor.process_job_event('FINISHED',
                                       name,
                                       namespace,
                                       pod_infos,
                                       termination_reason,
                                       job_uid=job_key,
                                       start_time=start_time,
                                       end_time=end_time)

        # Mark as finished to prevent duplicate processing
        pod_uids = set(pod.metadata.uid for pod in pods)
        self.monitor.finished_jobs[job_key] = (pod_uids, time.time())

    def on_job_add(self,
                   job_obj: Dict,
                   resource_config: DynamicResourceConfig,
                   is_initial_sync: bool = False):
        """Handler for custom job resource ADD events"""
        name = job_obj['metadata']['name']
        namespace = job_obj['metadata']['namespace']

        # Use resource config to extract job key
        job_key = resource_config.get_job_key(job_obj)

        if not job_key:
            logger.warning(
                f"[ADD] {resource_config.name} {name} missing job key, skipping"
            )
            return

        # Check if this job is already tracked (watch reconnect case)
        with self.monitor.tracked_jobs_lock:
            if job_key in self.monitor.tracked_jobs:
                logger.debug(
                    f"[ADD] {resource_config.name} {name} already tracked, skipping (watch reconnect)"
                )
                return
            # Also check if already finished
            if job_key in self.monitor.finished_jobs:
                logger.debug(
                    f"[ADD] {resource_config.name} {name} already finished, skipping (watch reconnect)"
                )
                return

        # Check if job is already completed using resource config
        is_completed, is_succeeded, is_failed = resource_config.is_completed(
            job_obj)

        # Handle completed jobs found during initial sync
        # These are jobs that completed before the informer started
        if is_completed and is_initial_sync:
            logger.info(
                f"[JOB-ADD] {resource_config.name} {name} (uid={job_key}, "
                f"initial_sync={is_initial_sync}, completed={is_completed})")
            self._handle_preexisting_completed_job(job_obj, job_key, name,
                                                   namespace, is_succeeded,
                                                   is_failed, resource_config)
            return

        # Job completed after informer started but before we could track it
        # This can happen for very fast jobs or after watch reconnect
        if is_completed and not is_initial_sync:
            logger.debug(
                f"[JOB-ADD] {resource_config.name} {name} already completed, skipping (watch reconnect or fast job)"
            )
            return

        # New job to track - log at INFO level
        logger.info(f"[JOB-ADD] {resource_config.name} {name} (uid={job_key})")

        # Track in-progress jobs (both new jobs and jobs found during initial sync)
        with self.monitor.tracked_jobs_lock:
            self.monitor.tracked_jobs[job_key] = TrackedJob(
                job_key=job_key,
                job_name=name,
                namespace=namespace,
                pods={},
                pod_objects={},
                status='PENDING')
            # Store resource config for this job
            self.monitor.job_resource_configs[job_key] = resource_config

        if is_initial_sync:
            logger.info(
                f"[JOB-ADD] Tracking pre-existing in-progress job: {name}")

        # Check if pods are already running (they may have started before job was added)
        self.monitor.pod_handler.schedule_job_event(job_key, name)

    def on_job_update(self, job_obj: Dict,
                      resource_config: DynamicResourceConfig):
        """Handler for custom job resource UPDATE events"""
        name = job_obj['metadata']['name']
        namespace = job_obj['metadata']['namespace']

        # Use resource config to extract job key
        job_key = resource_config.get_job_key(job_obj)

        if not job_key:
            return

        if job_key not in self.monitor.tracked_jobs:
            logger.debug(
                f"[UPDATE] {resource_config.name} {name} not in tracked_jobs, skipping"
            )
            return

        with self.monitor.job_locks[job_key]:
            tracked_job = self.monitor.tracked_jobs.get(job_key)
            if not tracked_job:
                # Job was removed between check and lock acquisition
                return

            # Get current pods from cache
            pods = self.monitor.get_job_pods(job_key, name)

            status = job_obj.get('status', {})
            conditions = status.get('conditions', [])
            logger.debug(
                f"[UPDATE] {resource_config.name} {name}: status={tracked_job.status}, "
                f"pods={len(pods)}, conditions={conditions}")

            # Update pod info cache (keep accumulating, don't overwrite)
            for pod in pods:
                pod_info = self.monitor.extract_pod_info(pod)
                if pod_info:
                    tracked_job.pods[pod.metadata.name] = pod_info
                    tracked_job.pod_objects[pod.metadata.name] = pod

            # Check for completion using resource config
            is_completed, is_succeeded, is_failed = resource_config.is_completed(
                job_obj)

            logger.debug(
                f"[UPDATE] {resource_config.name} {name}: is_completed={is_completed}, is_succeeded={is_succeeded}, is_failed={is_failed}, status={tracked_job.status}"
            )

            # Log completion
            if is_completed and tracked_job.status != 'FINISHED':
                logger.debug(f"[UPDATE] Logging FINISHED for job {name}")

                # Cancel any pending event timer
                if tracked_job.event_timer is not None:
                    tracked_job.event_timer.cancel()
                    tracked_job.event_timer = None

                # Check if job ever fully started (all pods running)
                # If job is still in PENDING status, it means not all pods started
                if tracked_job.status == 'PENDING':
                    logger.info(
                        f"[JOB-UPDATE] Job {name} completed before it fully started (status=PENDING), "
                        "skipping FINISHED event")
                    tracked_job.status = 'FINISHED'
                    pod_uids = set(
                        pod_obj.metadata.uid
                        for pod_obj in tracked_job.pod_objects.values())
                    self.monitor.finished_jobs[job_key] = (pod_uids,
                                                           time.time())
                else:
                    # Job was RUNNING, send FINISHED event
                    # Use cached pod info (already updated above)
                    pod_infos = list(tracked_job.pods.values())
                    logger.debug(
                        f"[UPDATE] Using {len(pod_infos)} cached pod infos for job {name}"
                    )

                    # If we have no pod info at all, log a warning but still record the event
                    if len(pod_infos) == 0:
                        logger.warning(
                            f"{resource_config.name} {name} completed but no pod info available (pods may have been deleted quickly)"
                        )
                        pod_infos = []

                    # Determine termination reason using helper method
                    termination_reason = self._determine_termination_reason(
                        is_succeeded, is_failed, pod_infos,
                        tracked_job.pod_objects)

                    # Extract times from job resource and pods
                    start_time, end_time = self.monitor.extract_job_times(
                        job_obj, pod_infos)

                    # Skip sending if no start_time available
                    if not start_time:
                        logger.warning(
                            f"[JOB-UPDATE] Job {name} completed but no start_time available, "
                            "skipping FINISHED event")
                        # Still clean up tracking below
                    else:
                        logger.info(
                            f"[JOB-FINISH] Job {name}: start_time={start_time}, "
                            f"end_time={end_time}, reason={termination_reason}"
                        )
                        self.monitor.process_job_event(
                            'FINISHED',
                            name,
                            namespace,
                            pod_infos,
                            termination_reason,
                            job_uid=tracked_job.job_key,
                            start_time=start_time,
                            end_time=end_time)

                    tracked_job.status = 'FINISHED'

                    pod_uids = set(
                        pod_obj.metadata.uid
                        for pod_obj in tracked_job.pod_objects.values())
                    self.monitor.finished_jobs[job_key] = (pod_uids,
                                                           time.time())

        # Remove from tracking
        if job_key in self.monitor.tracked_jobs and self.monitor.tracked_jobs[
                job_key].status == 'FINISHED':
            with self.monitor.tracked_jobs_lock:
                logger.debug(f"Removing completed job {job_key} from tracking")
                self.monitor.tracked_jobs.pop(job_key, None)
                self.monitor.job_resource_configs.pop(job_key, None)

            # Clean up cached pods for this finished job
            self.monitor.cleanup_cached_pods_for_job(job_key)

    def on_job_delete(self, job_obj: Dict,
                      resource_config: DynamicResourceConfig):
        """Handler for custom job resource DELETE events"""
        name = job_obj['metadata']['name']
        namespace = job_obj['metadata']['namespace']
        job_key = resource_config.get_job_key(job_obj)
        if not job_key:
            return

        logger.info(
            f"[JOB-DELETE] {resource_config.name}: {name}, uid={job_key}")

        if job_key in self.monitor.finished_jobs:
            logger.info(
                f"[JOB-DELETE] {resource_config.name} {name} was already logged as finished, cleaning up cached pods"
            )
            # Clean up cached pods
            self.monitor.cleanup_cached_pods_for_job(job_key)
            self.monitor.job_resource_configs.pop(job_key, None)
            return

        # If job was being tracked and not finished, log as cancelled
        if job_key in self.monitor.tracked_jobs:
            with self.monitor.job_locks[job_key]:
                tracked_job = self.monitor.tracked_jobs.get(job_key)
                if not tracked_job:
                    # Job was removed between check and lock acquisition
                    return
                logger.info(
                    f"[JOB-DELETE] {resource_config.name} {name} was being tracked, status={tracked_job.status}"
                )

                # Cancel any pending event timer
                if tracked_job.event_timer is not None:
                    tracked_job.event_timer.cancel()
                    tracked_job.event_timer = None

                if tracked_job.status != 'FINISHED':
                    # Check if job ever started (all pods running)
                    if tracked_job.status == 'PENDING':
                        logger.info(
                            f"[JOB-DELETE] Job {name} cancelled before it fully started (status=PENDING), skipping API call"
                        )
                    else:
                        # Job started and then cancelled
                        # Get final pod state
                        pods = self.monitor.get_job_pods(job_key, name)
                        pod_infos = []
                        for pod in pods:
                            pod_info = self.monitor.extract_pod_info(pod)
                            if pod_info:
                                pod_infos.append(pod_info)

                        # Extract job start and end times
                        start_time, end_time = self.monitor.extract_job_times(
                            job_obj, pod_infos)

                        # Skip sending if no start_time available
                        if not start_time:
                            logger.warning(
                                f"[JOB-DELETE] Job {name} cancelled but no start_time available, "
                                "skipping CANCELLED event")
                        else:
                            self.monitor.process_job_event(
                                'FINISHED',
                                name,
                                namespace,
                                pod_infos,
                                'CANCELLED',
                                job_uid=tracked_job.job_key,
                                start_time=start_time,
                                end_time=end_time)

            # Remove from tracking
            with self.monitor.tracked_jobs_lock:
                self.monitor.tracked_jobs.pop(job_key, None)
                self.monitor.job_resource_configs.pop(job_key, None)

            # Clean up cached pods
            self.monitor.cleanup_cached_pods_for_job(job_key)
