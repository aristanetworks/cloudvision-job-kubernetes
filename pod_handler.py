# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""
Pod event handling and job event scheduling for CV Job Informer.

Handles pod lifecycle events (add/update/delete), manages pod cache,
and schedules job events (STARTED/UPDATE) based on pod state changes.

Also contains PodInformer for watching Kubernetes pod resources.
"""

import logging
import threading
import time
from typing import TYPE_CHECKING, Dict

from kubernetes import client, watch

from constants import JOB_START_STABILITY_DELAY, JOB_START_MAX_WAIT

if TYPE_CHECKING:
    from job_monitor import JobMonitor

logger = logging.getLogger(__name__)


class PodInformer:
    """Informer for Pod resources (for caching pod details)

    Supports namespace filtering when watching all namespaces but only
    processing events from specific namespaces.
    """

    def __init__(self,
                 v1_api,
                 namespace: str,
                 handlers: Dict = None,
                 filter_namespaces: set = None):
        """
        Initialize Pod informer.

        Args:
            v1_api: Kubernetes CoreV1Api client
            namespace: Namespace to watch. Empty string = all namespaces.
            handlers: Dict of event handlers (add, update, delete)
            filter_namespaces: Set of namespaces to filter events to.
                              Empty set or None = no filtering (process all events)
        """
        self.v1_api = v1_api
        self.namespace = namespace
        self.handlers = handlers or {}
        self.filter_namespaces = filter_namespaces or set()

        self.running = False
        self.watch_thread = None
        self.initial_sync_done = False

    def _should_process_pod(self, pod) -> bool:
        """Check if pod should be processed based on namespace filtering."""
        if not self.filter_namespaces:
            return True  # No filtering, process all pods
        pod_namespace = pod.metadata.namespace
        return pod_namespace in self.filter_namespaces

    def start(self):
        """Start the informer watch loop"""
        if self.running:
            logger.warning("Pod informer already running")
            return

        self.running = True
        self.watch_thread = threading.Thread(target=self._watch_loop,
                                             daemon=True)
        self.watch_thread.start()

        # Log what we're watching
        if self.namespace == "":
            if self.filter_namespaces:
                logger.info(
                    f"[POD-INFORMER] Pod informer started (watching all namespaces, filtering to: {', '.join(sorted(self.filter_namespaces))})"
                )
            else:
                logger.info(
                    "[POD-INFORMER] Pod informer started (watching all namespaces)"
                )
        else:
            logger.info(
                f"[POD-INFORMER] Pod informer started (namespace: {self.namespace})"
            )

    def _watch_loop(self):
        """Main watch loop with automatic reconnection"""
        while self.running:
            try:
                # Initial list to populate cache
                if not self.initial_sync_done:
                    logger.info(
                        "[SYNC] Pod informer: Starting initial sync...")

                    # Choose list method based on namespace setting
                    if self.namespace == "":
                        pods = self.v1_api.list_pod_for_all_namespaces()
                    else:
                        pods = self.v1_api.list_namespaced_pod(
                            namespace=self.namespace)

                    processed_count = 0
                    for pod in pods.items:
                        if self._should_process_pod(pod):
                            if 'add' in self.handlers:
                                # Pass is_initial_sync=True to defer job informer creation
                                self.handlers['add'](pod, is_initial_sync=True)
                            processed_count += 1

                    self.initial_sync_done = True
                    logger.info(
                        f"[SYNC] Pod informer: Initial sync complete ({processed_count} pods processed)"
                    )

                # Watch for changes
                w = watch.Watch()

                # Choose watch method based on namespace setting
                if self.namespace == "":
                    stream = w.stream(
                        self.v1_api.list_pod_for_all_namespaces,
                        timeout_seconds=0  # Infinite watch, only ends on error
                    )
                else:
                    stream = w.stream(
                        self.v1_api.list_namespaced_pod,
                        namespace=self.namespace,
                        timeout_seconds=0  # Infinite watch, only ends on error
                    )

                for event in stream:
                    if not self.running:
                        break

                    event_type = event['type']
                    pod = event['object']

                    # Apply namespace filtering
                    if not self._should_process_pod(pod):
                        continue

                    if event_type == 'ADDED' and 'add' in self.handlers:
                        self.handlers['add'](pod)
                    elif event_type == 'MODIFIED' and 'update' in self.handlers:
                        self.handlers['update'](pod)
                    elif event_type == 'DELETED' and 'delete' in self.handlers:
                        self.handlers['delete'](pod)

                # Watch stream ended normally, reconnect
                if self.running:
                    logger.debug("Pod watch stream ended, reconnecting...")
                    time.sleep(1)

            except client.exceptions.ApiException as e:
                if e.status == 410:  # Gone - resource version too old
                    logger.warning(
                        "Pod resource version expired (410), resyncing...")
                    self.initial_sync_done = False  # Force resync
                    time.sleep(1)
                else:
                    logger.error(
                        f"API error in Pod watch: {e}. Reconnecting in 5s...")
                    time.sleep(5)

            except Exception as e:
                if self.running:
                    logger.error(
                        f"Pod informer error: {e}, reconnecting in 5s...")
                    time.sleep(5)
                else:
                    break

    def stop(self):
        """Stop the informer"""
        self.running = False
        if self.watch_thread:
            self.watch_thread.join(timeout=10)
        logger.info("[POD-INFORMER] Pod informer stopped")


class PodEventHandler:
    """Handles pod events and schedules job events based on pod state changes."""

    def __init__(self, monitor: 'JobMonitor'):
        """Initialize with reference to parent JobMonitor."""
        self.monitor = monitor

    def on_pod_add(self, pod: client.V1Pod, is_initial_sync: bool = False):
        """Handler for Pod ADD events - discover parent resource and cache pod details

        Args:
            pod: The pod object
            is_initial_sync: True if called during pod informer initial sync.
                           When True, defers job informer creation until sync completes.
        """
        pod_name = pod.metadata.name
        namespace = pod.metadata.namespace

        # Ignore pods without ownerReferences
        if not pod.metadata.owner_references:
            logger.debug(
                f"[POD-ADD] Ignoring pod {namespace}/{pod_name}: no ownerReferences"
            )
            return

        # Extract parent resource from ownerReferences
        parent_ref = self.monitor._extract_parent_resource(pod)
        if not parent_ref:
            return  # Not a job pod

        # Use parent UID as job_key for unique identification
        job_key = parent_ref.uid

        # Get or create resource config
        # During initial sync, defer informer creation to ensure pod cache is populated first
        self.monitor._get_or_create_resource_config(
            parent_ref, defer_informer=is_initial_sync)

        self.monitor.pod_cache[pod_name] = pod

        # bidirectional mapping update
        with self.monitor.pod_to_job_lock:
            self.monitor.pod_to_job[pod_name] = job_key
            self.monitor.job_to_pods[job_key].add(pod_name)

        logger.debug(
            f"[POD-ADD] Pod {pod_name} -> {parent_ref.kind}/{parent_ref.name} (uid={job_key}), "
            f"phase={pod.status.phase}")

    def on_pod_update(self, pod: client.V1Pod):
        """Handler for Pod UPDATE events - update cache and check if job started"""
        pod_name = pod.metadata.name

        # Ignore pods without ownerReferences
        if not pod.metadata.owner_references:
            return

        # Extract parent resource from ownerReferences
        parent_ref = self.monitor._extract_parent_resource(pod)
        if not parent_ref:
            return  # Not an expected job pod

        # Use parent UID as job_key for unique identification
        job_key = parent_ref.uid

        self.monitor.pod_cache[pod_name] = pod

        # bidirectional mapping update
        with self.monitor.pod_to_job_lock:
            self.monitor.pod_to_job[pod_name] = job_key
            self.monitor.job_to_pods[job_key].add(pod_name)

        logger.debug(
            f"[POD-UPDATE] Pod {pod_name} -> {parent_ref.kind}/{parent_ref.name} (uid={job_key}), "
            f"phase={pod.status.phase}")

        # Check if this pod update should trigger a job event (STARTED or UPDATE)
        self.schedule_job_event(job_key, parent_ref.name)

    def on_pod_delete(self, pod: client.V1Pod):
        """Handler for Pod DELETE events - keep in cache for final logging"""
        pod_name = pod.metadata.name
        logger.debug(f"[POD-DELETE] Pod {pod_name}")

        # Keep pod in cache for final logging (don't remove yet)
        # It will be cleaned up when the job is removed from tracking

    def schedule_job_event(self, job_key: str, job_name: str):
        """Schedule a job event (STARTED or UPDATE) based on current state.

        Uses a stability-based approach:
        - For PENDING jobs: wait for stability before sending STARTED
        - For RUNNING jobs: check for interface changes and send UPDATE if needed
        - Reset timer on each pod update to allow stabilization
        - Use max wait as safety net for STARTED event
        """
        with self.monitor.job_locks[job_key]:
            job = self.monitor.tracked_jobs.get(job_key)
            if not job or job.status == 'FINISHED':
                # Job is not being tracked or already finished
                return

            # Get current pod info from cache
            cached_pods = self.monitor.get_cached_pods_for_job(job_key)
            for pod in cached_pods:
                pod_info = self.monitor.extract_pod_info(pod)
                if pod_info:
                    job.pods[pod.metadata.name] = pod_info
                    job.pod_objects[pod.metadata.name] = pod

            # Count running pods, pending pods, and failed pods
            running_pods = [
                p for p in job.pods.values() if p.phase == 'Running'
            ]
            pending_pods = [
                p for p in job.pods.values() if p.phase == 'Pending'
            ]
            failed_pods = [p for p in job.pods.values() if p.phase == 'Failed']

            # For PENDING jobs, need at least one running pod to proceed
            if job.status == 'PENDING' and len(running_pods) == 0:
                return

            # For PENDING jobs, don't send STARTED event if any pod is still Pending
            # This ensures all pods have started before we report the job as started
            if job.status == 'PENDING' and len(pending_pods) > 0:
                logger.debug(
                    f"[EVENT] Job {job_name}: {len(pending_pods)} pod(s) still pending, "
                    f"waiting for all pods to start (running={len(running_pods)}, pending={len(pending_pods)})"
                )
                return

            # For PENDING jobs, don't send STARTED event if any pod has failed
            # Job will transition to completion without ever being reported as started
            if job.status == 'PENDING' and len(failed_pods) > 0:
                logger.debug(
                    f"[EVENT] Job {job_name}: {len(failed_pods)} pod(s) failed, "
                    f"waiting for job completion (running={len(running_pods)}, failed={len(failed_pods)})"
                )
                return

            # For RUNNING jobs, check if interfaces actually changed
            if job.status == 'RUNNING':
                current_interfaces = {
                    iface.mac
                    for p in job.pods.values() if p.node != 'Not assigned'
                    for iface in p.network_interfaces if iface.mac
                }
                if current_interfaces == job.sent_interfaces:
                    return  # No change, skip scheduling

            now = time.time()
            job.last_pod_update_time = now

            # First time we see a running pod - record the time
            if job.first_pod_running_time is None:
                job.first_pod_running_time = now
                logger.debug(
                    f"[EVENT] Job {job_name}: first pod running, "
                    f"scheduling event (stability_delay={JOB_START_STABILITY_DELAY}s)"
                )

            # Cancel existing timer and schedule new one
            if job.event_timer:
                job.event_timer.cancel()

            job.event_timer = threading.Timer(JOB_START_STABILITY_DELAY,
                                              self._send_delayed_job_event,
                                              args=[job_key, job_name])
            job.event_timer.daemon = True
            job.event_timer.start()

    def _send_delayed_job_event(self, job_key: str, job_name: str):
        """Send job event (STARTED or UPDATE) after stability delay."""
        with self.monitor.job_locks[job_key]:
            job = self.monitor.tracked_jobs.get(job_key)
            if not job or job.status == 'FINISHED':
                return

            now = time.time()
            time_since_last_update = now - (job.last_pod_update_time or now)
            time_since_first_running = now - (job.first_pod_running_time
                                              or now)

            # Check if we should send event now (stability reached or max wait exceeded)
            # Max wait only applies to STARTED event (PENDING status)
            is_stable = time_since_last_update >= JOB_START_STABILITY_DELAY
            max_wait_exceeded = (job.status == 'PENDING'
                                 and time_since_first_running
                                 >= JOB_START_MAX_WAIT)

            if not is_stable and not max_wait_exceeded:
                # Not ready yet - reschedule
                remaining_stability = JOB_START_STABILITY_DELAY - time_since_last_update
                if job.status == 'PENDING':
                    remaining_max = JOB_START_MAX_WAIT - time_since_first_running
                    delay = min(remaining_stability, remaining_max)
                else:
                    delay = remaining_stability

                if delay > 0:
                    job.event_timer = threading.Timer(
                        delay,
                        self._send_delayed_job_event,
                        args=[job_key, job_name])
                    job.event_timer.daemon = True
                    job.event_timer.start()
                return

            # Re-extract pod info to get latest interface information
            cached_pods = self.monitor.get_cached_pods_for_job(job_key)
            for pod in cached_pods:
                pod_info = self.monitor.extract_pod_info(pod)
                if pod_info:
                    job.pods[pod.metadata.name] = pod_info
                    job.pod_objects[pod.metadata.name] = pod

            pod_infos = list(job.pods.values())
            current_interfaces = {
                iface.mac
                for p in pod_infos if p.node != 'Not assigned'
                for iface in p.network_interfaces if iface.mac
            }

            # Determine event type based on job status
            # Extract start_time from pods (earliest pod start_time)
            pod_start_times = [p.start_time for p in pod_infos if p.start_time]
            start_time = min(pod_start_times) if pod_start_times else None

            if job.status == 'PENDING':
                # Send STARTED event
                reason = "max wait exceeded" if max_wait_exceeded else f"stable for {time_since_last_update:.1f}s"
                running_pods = [p for p in pod_infos if p.phase == 'Running']
                pending_pods = [p for p in pod_infos if p.phase == 'Pending']
                failed_pods = [p for p in pod_infos if p.phase == 'Failed']

                if not start_time:
                    logger.warning(
                        f"[JOB-START] Job {job_name}: no pod is started, skipping STARTED event"
                    )
                    return

                # Don't send STARTED event if any pod is still Pending
                # This ensures all pods have started before we report the job as started
                if len(pending_pods) > 0:
                    logger.debug(
                        f"[JOB-START] Job {job_name}: {len(pending_pods)} pod(s) still pending, "
                        f"skipping STARTED event (running={len(running_pods)}, pending={len(pending_pods)})"
                    )
                    return

                # Don't send STARTED event if any pod has failed
                # Job will transition to completion without ever being reported as started
                if len(failed_pods) > 0:
                    logger.debug(
                        f"[JOB-START] Job {job_name}: {len(failed_pods)} pod(s) failed, "
                        f"skipping STARTED event (running={len(running_pods)}, failed={len(failed_pods)})"
                    )
                    return

                logger.info(
                    f"[JOB-START] Job {job_name}: sending STARTED event ({reason}), "
                    f"{len(running_pods)} running pods, start_time={start_time}"
                )

                self.monitor.process_job_event('STARTED',
                                               job_name,
                                               job.namespace,
                                               pod_infos,
                                               job_uid=job.job_key,
                                               start_time=start_time)

                job.sent_interfaces = current_interfaces
                job.status = 'RUNNING'

            elif job.status == 'RUNNING':
                # Check if interfaces still differ from what we sent
                if current_interfaces == job.sent_interfaces:
                    logger.debug(
                        f"[UPDATE] Job {job_name}: interfaces are same, skipping UPDATE"
                    )
                else:
                    if not start_time:
                        logger.warning(
                            f"[JOB-UPDATE] Job {job_name}: no pod start_time available, skipping UPDATE event"
                        )
                        return

                    logger.info(
                        f"[JOB-UPDATE] Job {job_name}: sending UPDATE event, "
                        f"interfaces changed from {len(job.sent_interfaces)} to {len(current_interfaces)}"
                    )

                    self.monitor.process_job_event('UPDATE',
                                                   job_name,
                                                   job.namespace,
                                                   pod_infos,
                                                   job_uid=job.job_key,
                                                   start_time=start_time)

                    job.sent_interfaces = current_interfaces

            job.event_timer = None
