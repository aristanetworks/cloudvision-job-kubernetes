#!/usr/bin/env python3
# Copyright (c) 2026 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Cluster-free integration tests: the real JobMonitor against a fake
kube-apiserver and a fake CloudVision stub.

Runs the production code path end-to-end without a cluster:

    real JobMonitor (thread) -> real kubernetes client (deserialization
    included) -> fake kube-apiserver (fake_apiserver.py)
    real api_utils HTTP calls -> fake CloudVision over TLS (cv_stub.py)

Scenario IDs: P* platform/protocol, S* scheduler, N* network allocation,
C* NodeConfig.  The cross-cutting invariants of the scenario matrix are
asserted throughout (payload shape, MAC dedup, eth0 exclusion,
stability timing, uid identity, pod-DELETE cache no-op, XOR of interfaces
and nodes by mode).

Style: plain assert-based functions runnable directly (like test_dra.py);
stdlib + kubernetes + requests (+ cryptography for the stub's per-run TLS
identity; it already arrives transitively via kubernetes -> google-auth).  No
application code is modified; the
timing constants are overridden HERE, before job_monitor/pod_handler are
imported (pod_handler binds JOB_START_* at import time).

Client-version note: the suite
is verified with both kubernetes 34.x and 36.x.  discovery.get_json tolerates
both call_api(_preload_content=False) return conventions (tuple for clients
< 36, bare HTTPResponse for >= 36), so plural resolution works across the
Dockerfile's kubernetes>=28 range; the p8 plural scenarios exercise the live
discovery path in either case.
"""

import constants

# ---------------------------------------------------------------------------
# Constant overrides MUST precede the job_monitor import (pod_handler binds
# JOB_START_STABILITY_DELAY / JOB_START_MAX_WAIT at import time).
# ---------------------------------------------------------------------------
constants.JOB_START_STABILITY_DELAY = 0.3
constants.JOB_START_MAX_WAIT = 1.5
constants.POD_INFORMER_SYNC_MAX_WAIT = 5.0
constants.POD_INFORMER_SYNC_POLL_INTERVAL = 0.02

import concurrent.futures  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from fake_apiserver import FakeApiServer, Resource, add_group, empty_profile  # noqa: E402
from cv_stub import CvStub  # noqa: E402

# ---------------------------------------------------------------------------
# KUBECONFIG must be set BEFORE the kubernetes client is imported (it freezes
# the default kubeconfig location at import time, and JobMonitor calls
# config.load_kube_config() with no arguments).  All scenarios share one
# kubeconfig file; each Harness rewrites it with its own fake-server port.
# ---------------------------------------------------------------------------
_KUBECONFIG_FD, KUBECONFIG_PATH = tempfile.mkstemp(
    prefix="cvjob-integration-kubeconfig-", suffix=".yaml")
os.close(_KUBECONFIG_FD)
os.environ["KUBECONFIG"] = KUBECONFIG_PATH

from job_monitor import JobMonitor  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixture constants
# ---------------------------------------------------------------------------
T0 = "2026-09-15T10:00:00Z"  # CR/pod start (raw Z form, as a CR would carry)
T1 = "2026-09-15T11:00:00Z"  # CR end (raw Z form)
T0_ISO = "2026-09-15T10:00:00+00:00"  # pod-derived start (client isoformat)
T1_ISO = "2026-09-15T11:00:00+00:00"  # pod-derived end (client isoformat)
T_WRONG_START = "2020-01-01T00:00:00Z"  # misleading CR time (must be unused)
T_WRONG_END = "2020-01-01T01:00:00Z"

MAC_A = "00:11:22:33:44:01"
MAC_B = "00:11:22:33:44:02"
MAC_C = "00:11:22:33:44:03"
MAC_D = "00:11:22:33:44:04"
MAC_UPPER = "AA:BB:CC:DD:EE:01"  # DRA record for the mixed-dedup scenario
MAC_LOWER = "aa:bb:cc:dd:ee:01"  # Multus case-variant of the same NIC

LOCATION = "test-cluster"
ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")

SETTLE = 0.6  # > JOB_START_STABILITY_DELAY: any stray event would fire


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
class LogCapture(logging.Handler):
    """Collects log messages so scenarios can assert on specific lines."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self._lock = threading.Lock()
        self.messages = []

    def emit(self, record):
        with self._lock:
            self.messages.append(record.getMessage())

    def has(self, substring):
        with self._lock:
            return any(substring in message for message in self.messages)


def wait_for(fn, timeout=10.0, desc="condition"):
    """Poll fn() until truthy; return its value or raise AssertionError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {desc}")


# ---------------------------------------------------------------------------
# Object fixture builders (full, deserializable k8s objects)
# ---------------------------------------------------------------------------
def owner_ref(api_version, kind, name, uid):
    return {
        "apiVersion": api_version,
        "kind": kind,
        "name": name,
        "uid": uid,
        "controller": True,
        "blockOwnerDeletion": True,
    }


def make_pod(name,
             ns="default",
             owner=None,
             node="node-a",
             phase="Running",
             start_time=T0,
             container_state="running",
             finished_at=T1,
             multus_macs=(),
             extra_annotations=None,
             spec_claims=(),
             claim_statuses=(),
             extended=None,
             uid=None):
    """Build a pod dict that deserializes cleanly into client.V1Pod.

    multus_macs: MACs rendered as a Multus network-status annotation.
    spec_claims / claim_statuses / extended: three of the four DRA
        claim->pod paths (the fourth is claim reservedFor).
    node=None omits spec.nodeName (extracts to 'Not assigned').
    """
    metadata = {"name": name, "namespace": ns, "uid": uid or f"uid-{name}"}
    if owner is not None:
        refs = owner if isinstance(owner, list) else [owner]
        metadata["ownerReferences"] = refs

    annotations = {}
    if multus_macs:
        entries = []
        for index, mac in enumerate(multus_macs, start=1):
            entries.append({
                "name": f"rdma-net-{index}",
                "interface": f"net{index}",
                "ips": [f"10.0.0.{index}"],
                "mac": mac,
                "device-info": {
                    "pci": {
                        "rdma-device": f"rdma{index}",
                        "pci-address": f"0000:0{index}:00.0",
                    }
                },
            })
        annotations["k8s.v1.cni.cncf.io/network-status"] = json.dumps(entries)
    if extra_annotations:
        annotations.update(extra_annotations)
    if annotations:
        metadata["annotations"] = annotations

    spec = {"containers": [{"name": "app", "image": "busybox:latest"}]}
    if node:
        spec["nodeName"] = node
    if spec_claims:
        spec["resourceClaims"] = [{
            "name": f"rc-{index}",
            "resourceClaimName": claim,
        } for index, claim in enumerate(spec_claims)]

    status = {"phase": phase}
    if start_time:
        status["startTime"] = start_time
    container_status = {
        "name": "app",
        "ready": phase == "Running",
        "restartCount": 0,
        "image": "busybox:latest",
        "imageID": "busybox@sha256:" + "0" * 16,
    }
    if container_state == "running":
        container_status["state"] = {"running": {"startedAt": start_time or T0}}
    elif container_state == "terminated":
        container_status["state"] = {
            "terminated": {
                "exitCode": 0,
                "startedAt": start_time or T0,
                "finishedAt": finished_at or T1,
            }
        }
    else:  # waiting
        container_status["state"] = {"waiting": {"reason": "ContainerCreating"}}
    status["containerStatuses"] = [container_status]
    if claim_statuses:
        status["resourceClaimStatuses"] = [{
            "name": f"rc-{index}",
            "resourceClaimName": claim,
        } for index, claim in enumerate(claim_statuses)]
    if extended:
        # 1.36/1.37 extended-resource DRA shape (client 36 requires
        # requestMappings on this model).
        status["extendedResourceClaimStatus"] = {
            "resourceClaimName": extended,
            "requestMappings": [],
        }

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": metadata,
        "spec": spec,
        "status": status,
    }


def nic_device(name, mac, iface=None, ip="10.10.0.9"):
    """DRA device with driver-published networkData (iface=None -> the
    device name is the interfaceName fallback)."""
    device = {
        "driver": "nic.example.com",
        "device": name,
        "pool": "nic-pool",
        "networkData": {
            "hardwareAddress": mac,
            "ips": [ip],
        },
    }
    if iface:
        device["networkData"]["interfaceName"] = iface
    return device


def gpu_device(name="gpu0"):
    """DRA device without networkData (must be skipped)."""
    return {"driver": "gpu.example.com", "device": name, "pool": "gpu-pool"}


def make_claim(name, ns="default", devices=(), reserved_for=None, uid=None):
    claim = {
        "apiVersion": "resource.k8s.io/v1",
        "kind": "ResourceClaim",
        "metadata": {
            "name": name,
            "namespace": ns,
            "uid": uid or f"uid-{name}",
        },
        "spec": {
            "devices": {}
        },
        "status": {
            "devices": list(devices)
        },
    }
    if reserved_for:
        claim["status"]["reservedFor"] = reserved_for
    return claim


def make_batch_job(name,
                   ns="default",
                   complete=False,
                   failed=False,
                   start_time=None,
                   completion_time=None,
                   uid=None):
    """batch/v1 Job with optional terminal conditions and CR times."""
    status = {}
    conditions = []
    if complete:
        conditions.append({"type": "Complete", "status": "True"})
    if failed:
        conditions.append({"type": "Failed", "status": "True"})
    if conditions:
        status["conditions"] = conditions
    if start_time:
        status["startTime"] = start_time
    if completion_time:
        status["completionTime"] = completion_time
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": ns,
            "uid": uid or f"uid-{name}",
        },
        "spec": {
            "completions": 1
        },
        "status": status,
    }


def make_custom_job(api_version, kind, name, status=None, ns="default",
                    uid=None, owner=None):
    """Generic CR job object (PyTorchJob, Workflow, RayCluster, RayJob...)."""
    metadata = {"name": name, "namespace": ns, "uid": uid or f"uid-{name}"}
    if owner is not None:
        refs = owner if isinstance(owner, list) else [owner]
        metadata["ownerReferences"] = refs
    obj = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": metadata,
        "spec": {},
    }
    if status is not None:
        obj["status"] = status
    return obj


def make_node_state(kind, api_version, name, ns, vf_mac):
    """NodeInterfaceState / SriovNetworkNodeState with two PFs and one VF."""
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": ns,
        },
        "status": {
            "interfaces": [
                {
                    "name": "ens5",
                    "mac": MAC_A,
                    "ip": "10.0.0.1",
                    "Vfs": [{
                        "name": "ens5f0v0",
                        "mac": vf_mac,
                        "ip": "10.1.0.1",
                    }],
                },
                {
                    "name": "ens6",
                    "mac": MAC_B,
                },
            ]
        },
    }


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------
def profile_with(pods=(), batch_jobs=(), pods_kwargs=None, groups=None):
    """Base profile: core/v1 pods plus optional batch/v1 jobs and groups.

    groups: iterable of (group, version, {plural: Resource}) tuples, as
    returned by claims_group().
    """
    profile = empty_profile()
    profile["core"]["v1"]["pods"] = Resource("Pod",
                                             items=list(pods),
                                             **(pods_kwargs or {}))
    if batch_jobs:
        add_group(profile, "batch", "v1",
                  {"jobs": Resource("Job", items=list(batch_jobs))})
    for group, version, resources in (groups or []):
        add_group(profile, group, version, resources)
    return profile


def claims_group(versions):
    """resource.k8s.io group serving resourceclaims on the given versions.

    Returns a list of (group, version, resources) tuples for profile_with().
    """
    return [
        ("resource.k8s.io", version,
         {"resourceclaims": Resource("ResourceClaim", items=list(items))})
        for version, items in versions.items()
    ]


def dra_profile(pods, jobs, claim_versions):
    """Profile with pods + batch jobs + resource.k8s.io claim versions."""
    return profile_with(pods=pods,
                        batch_jobs=jobs,
                        groups=claims_group(claim_versions))


# ---------------------------------------------------------------------------
# Harness: fake servers + generated kubeconfig + real JobMonitor in a thread
# ---------------------------------------------------------------------------
def _kubeconfig_yaml(port):
    return f"""apiVersion: v1
kind: Config
clusters:
- name: fake
  cluster:
    server: http://127.0.0.1:{port}
contexts:
- name: fake
  context:
    cluster: fake
    user: fake
current-context: fake
users:
- name: fake
  user:
    token: test-token
"""


# The harness running in this worker process (set by Harness.__init__); used
# by _run_scenario to dump captured logs when a scenario fails.
ACTIVE_HARNESS = None


class Harness:
    """One scenario's fake environment plus a running real JobMonitor."""

    def __init__(self,
                 profile,
                 namespaces=None,
                 location=LOCATION,
                 jobconfig_mode="interface",
                 nodeconfig_mode="disabled",
                 node_interface_type="all",
                 pod_sync_max_wait=None,
                 pod_sync_poll_interval=None):
        # Registered first: if __init__ itself fails (e.g. JobMonitor's
        # kubeconfig load), _run_scenario still dumps THIS harness's logs
        # instead of whatever ran before it in this worker.
        global ACTIVE_HARNESS
        ACTIVE_HARNESS = self
        self._saved = {
            "API_SERVER": constants.API_SERVER,
            "API_TOKEN": constants.API_TOKEN,
            "JOBCONFIG_MODE": constants.JOBCONFIG_MODE,
            "POD_INFORMER_SYNC_MAX_WAIT": constants.POD_INFORMER_SYNC_MAX_WAIT,
            "POD_INFORMER_SYNC_POLL_INTERVAL":
            constants.POD_INFORMER_SYNC_POLL_INTERVAL,
        }
        self.logs = LogCapture()
        logging.getLogger().addHandler(self.logs)
        self._saved_levels = {}
        # Application loggers log their lifecycle at INFO, but a NOTSET
        # logger inherits the root level (WARNING by default).  Raise the
        # module loggers to INFO so scenarios can assert on those lines.
        for name in ("pod_handler", "kubernetes", "urllib3", "dra",
                     "job_handler", "job_monitor", "node_monitor",
                     "api_utils", "discovery"):
            logger_obj = logging.getLogger(name)
            self._saved_levels[name] = logger_obj.level
            if logger_obj.level in (logging.NOTSET, logging.WARNING):
                logger_obj.setLevel(logging.INFO)
        logging.getLogger("pod_handler").setLevel(logging.DEBUG)
        logging.getLogger("kubernetes").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

        self.fake = FakeApiServer(profile)
        self.fake.start()
        self.cv = CvStub()
        self.cv.start()

        constants.API_SERVER = f"127.0.0.1:{self.cv.port}"
        constants.API_TOKEN = "test-token"
        constants.JOBCONFIG_MODE = jobconfig_mode
        if pod_sync_max_wait is not None:
            constants.POD_INFORMER_SYNC_MAX_WAIT = pod_sync_max_wait
        if pod_sync_poll_interval is not None:
            constants.POD_INFORMER_SYNC_POLL_INTERVAL = pod_sync_poll_interval

        self.tmpdir = tempfile.mkdtemp(prefix="cvjob-integration-")
        with open(KUBECONFIG_PATH, "w", encoding="utf-8") as handle:
            handle.write(_kubeconfig_yaml(self.fake.port))

        self.monitor = JobMonitor(namespaces=namespaces or set(),
                                  location=location,
                                  nodeconfig_mode=nodeconfig_mode,
                                  node_interface_type=node_interface_type)
        self.thread = threading.Thread(target=self.monitor.run,
                                       name="job-monitor-under-test",
                                       daemon=True)
        self.thread.start()

    # -- event push helpers ------------------------------------------------
    def add_pod(self, pod):
        self.fake.push_core("pods", "ADDED", pod)

    def update_pod(self, pod):
        self.fake.push_core("pods", "MODIFIED", pod)

    def delete_pod(self, pod):
        self.fake.push_core("pods", "DELETED", pod)

    def update_job(self, job, group="batch", version="v1", plural="jobs"):
        self.fake.push_group(group, version, plural, "MODIFIED", job)

    def delete_job(self, job, group="batch", version="v1", plural="jobs"):
        self.fake.push_group(group, version, plural, "DELETED", job)

    def push_custom(self, group, version, plural, event_type, obj):
        self.fake.push_group(group, version, plural, event_type, obj)

    # -- observation helpers -------------------------------------------------
    def jobconfigs_for(self, job_uid):
        return self.cv.jobconfigs_for(job_uid)

    def tracked(self):
        return dict(self.monitor.tracked_jobs)

    # -- teardown ------------------------------------------------------------
    def stop(self):
        monitor = self.monitor
        # 1. Cancel pending stability timers and drop tracked jobs so no
        #    stray event can fire into a later scenario's CV stub.
        for job in list(monitor.tracked_jobs.values()):
            if job.event_timer is not None:
                job.event_timer.cancel()
                job.event_timer = None
        with monitor.tracked_jobs_lock:
            monitor.tracked_jobs.clear()
        # 2. Signal every informer to stop watching BEFORE closing the fake.
        #    When the fake's shutdown then ends the watch streams, the loops
        #    see running=False and exit immediately; closing the fake first
        #    instead sends each thread through connection-refused retries and
        #    its 5s reconnect backoff inside the joins below - a teardown
        #    storm that slows every scenario and leaves zombie threads that
        #    bleed into the next scenario under CI contention.
        monitor.pod_informer.running = False
        monitor.claim_informer.running = False
        with monitor.job_informers_lock:
            job_informers = list(monitor.job_informers.values())
        for informer in job_informers:
            informer.running = False
        if monitor.node_informer is not None:
            monitor.node_informer._running = False
        # 3. Close the fake apiserver: ends the open watch streams so the
        #    signalled threads unblock and exit.
        self.fake.stop()
        # 4. Stop every informer (running already False; this just joins).
        monitor.pod_informer.stop()
        monitor.claim_informer.stop()
        for informer in job_informers:
            informer.stop()
        if monitor.node_informer is not None:
            monitor.node_informer.stop()
        # 4. Let in-flight handler callbacks finish their POSTs, then close CV.
        time.sleep(0.2)
        self.cv.stop()
        # 5. Restore process-global state.
        logging.getLogger().removeHandler(self.logs)
        for name, level in self._saved_levels.items():
            logging.getLogger(name).setLevel(level)
        for key, value in self._saved.items():
            setattr(constants, key, value)
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def stop_and_check(harness, desc):
    """Stop the harness and assert the CV stub rejected nothing (invariant:
    every payload the integration produced was valid)."""
    harness.stop()
    rejected = harness.cv.rejected
    assert rejected == [], (
        f"[{desc}] CV stub rejected invalid payloads (regression): {rejected}")


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------
def wait_jobconfig(harness, job_uid, state=None, count=1, timeout=10.0,
                   desc=""):
    def check():
        posts = harness.jobconfigs_for(job_uid)
        if state is not None:
            posts = [p for p in posts if p.get("state") == state]
        return posts if len(posts) >= count else None

    return wait_for(check,
                    timeout=timeout,
                    desc=f"JobConfig POST uid={job_uid} state={state} "
                         f"count>={count} {desc}")


def interfaces_of(payload):
    return set(payload["interfaces"]["values"])


def nodes_of(payload):
    return set(payload["nodes"]["values"])


def no_dra_probes_404(harness, versions=("v1", "v1beta2", "v1beta1",
                                         "v1alpha3")):
    for version in versions:
        requests = harness.fake.requests_for(
            f"/apis/resource.k8s.io/{version}/resourceclaims")
        assert requests and all(r["status"] == 404 for r in requests), (
            f"expected only 404s for resource.k8s.io/{version} probes, "
            f"got {requests}")


def assert_lifecycle(posts, macs, start, end):
    """STARTED + FINISHED with a stable MAC set (invariants 1/2/3)."""
    assert len(posts) == 2, posts
    started, finished = posts
    assert started["state"] == "JOB_STATE_RUNNING"
    assert finished["state"] == "JOB_STATE_COMPLETED"
    for payload in posts:
        assert interfaces_of(payload) == set(macs), payload
        assert "nodes" not in payload  # interface mode: XOR by mode
        assert payload["location"] == LOCATION
    assert finished["start_time"] == start
    assert finished["end_time"] == end


# ===========================================================================
# Platform / protocol scenarios
# ===========================================================================
def test_p1_no_dra_api_multus_fallback():
    """P1/P0: resource.k8s.io absent -> claim informer self-disables (all four
    probes 404), the Multus fallback is logged, and the full STARTED/FINISHED
    flow still works."""
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   multus_macs=(MAC_A, MAC_B))
    harness = Harness(profile_with(pods=[pod], batch_jobs=[job]))
    try:
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_RUNNING")
        harness.update_job(
            make_batch_job("job-m",
                           uid="uid-job-m",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_COMPLETED")

        assert_lifecycle(harness.jobconfigs_for("uid-job-m"), (MAC_A, MAC_B),
                         T0, T1)

        # All four DRA probes 404'd and the informer disabled itself.
        no_dra_probes_404(harness)
        assert harness.logs.has("using Multus fallback")
        assert harness.logs.has("DRA ResourceClaim watch disabled")
    finally:
        stop_and_check(harness, "P1")


def test_p2_dra_v1():
    """P2/P0: resource.k8s.io/v1 served -> the probe locks onto v1 without
    trying other versions, and claim networkData MACs reach the payload."""
    claim = make_claim("c1",
                       devices=[nic_device("nic0", MAC_A, iface="ens5",
                                           ip="10.10.0.1")])
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   spec_claims=["c1"])
    harness = Harness(dra_profile([pod], [job], {"v1": [claim]}))
    try:
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}

        # Probe hit v1 first: no 404s, no fallthrough to other versions.
        assert harness.fake.requests_for(
            "/apis/resource.k8s.io/v1/resourceclaims", status=200)
        for version in ("v1beta2", "v1beta1", "v1alpha3"):
            assert not harness.fake.requests_for(
                f"/apis/resource.k8s.io/{version}/resourceclaims"), (
                    f"probe should have stopped at v1, but {version} was tried")
    finally:
        stop_and_check(harness, "P2")


def test_p3_dra_v1beta1_fallback():
    """P3/named: only v1beta1 served (1.32/1.33 gate-on shape) -> v1 and
    v1beta2 probes 404, v1beta1 is picked, MACs still reported."""
    claim = make_claim("c1",
                       devices=[nic_device("nic0", MAC_A, iface="ens5")])
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   spec_claims=["c1"])
    harness = Harness(dra_profile([pod], [job], {"v1beta1": [claim]}))
    try:
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}

        assert harness.fake.requests_for(
            "/apis/resource.k8s.io/v1/resourceclaims", status=404)
        assert harness.fake.requests_for(
            "/apis/resource.k8s.io/v1beta2/resourceclaims", status=404)
        assert harness.fake.requests_for(
            "/apis/resource.k8s.io/v1beta1/resourceclaims", status=200)
        assert not harness.fake.requests_for(
            "/apis/resource.k8s.io/v1alpha3/resourceclaims"), (
                "probe must stop at v1beta1 and never try v1alpha3")
    finally:
        stop_and_check(harness, "P3")


def test_p5_watch_stream_closed_mid_session():
    """P5/P2: the fake apiserver closes the pod watch after two events ->
    the informer reconnects and job tracking continues without loss."""
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   phase="Pending",
                   container_state="waiting",
                   start_time=None)
    harness = Harness(
        profile_with(pods=[pod],
                     batch_jobs=[job],
                     pods_kwargs={"close_watch_after": 2}))
    try:
        # Two live pod updates fill the first watch, which then closes.
        running = copy.deepcopy(pod)
        running["status"]["phase"] = "Running"
        running["status"]["startTime"] = T0
        running["status"]["containerStatuses"][0]["ready"] = True
        running["status"]["containerStatuses"][0]["state"] = {
            "running": {
                "startedAt": T0
            }
        }
        running["metadata"]["annotations"] = {
            "k8s.v1.cni.cncf.io/network-status":
            json.dumps([{
                "name": "rdma-net-1",
                "interface": "net1",
                "ips": ["10.0.0.1"],
                "mac": MAC_A,
            }])
        }
        harness.update_pod(running)
        harness.update_pod(copy.deepcopy(running))  # second event -> close

        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}

        # Finish the job after the reconnect to prove tracking survived.
        harness.update_job(
            make_batch_job("job-m",
                           uid="uid-job-m",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_COMPLETED")

        # Pod GETs: initial list + first watch + post-close re-watch.  The
        # informer waits ~1s before reconnecting, so poll for it.
        wait_for(
            lambda: len(harness.fake.requests_for("/api/v1/pods",
                                                  status=200)) >= 3,
            desc="pod watch re-established after the stream closed")
        assert harness.logs.has("Pod watch stream ended, reconnecting")
    finally:
        stop_and_check(harness, "P5")


def test_p6_watch_410_resync():
    """P6/P0: first pod watch returns 410 Gone -> warn + full relist, events
    continue, and the resync does not duplicate job events."""
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   multus_macs=(MAC_A, ))
    harness = Harness(
        profile_with(pods=[pod], batch_jobs=[job],
                     pods_kwargs={"watch_status": 410}))
    try:
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_RUNNING")
        harness.update_job(
            make_batch_job("job-m",
                           uid="uid-job-m",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_COMPLETED")

        # 410 handled: warning logged, relist happened, watch re-established.
        assert harness.logs.has("resource version expired (410), resyncing")
        pod_gets = harness.fake.requests_for("/api/v1/pods", status=200)
        assert len(pod_gets) >= 2, pod_gets  # initial list + post-410 relist

        # Resync must not duplicate events: exactly one STARTED, one FINISHED.
        posts = harness.jobconfigs_for("uid-job-m")
        assert len(posts) == 2, posts
        assert posts[0]["state"] == "JOB_STATE_RUNNING"
        assert posts[1]["state"] == "JOB_STATE_COMPLETED"
    finally:
        stop_and_check(harness, "P6")


def test_p7_slow_pod_initial_sync():
    """P7/P2: pod LIST delayed beyond POD_INFORMER_SYNC_MAX_WAIT -> the
    'proceeding anyway' warning fires, the late list still populates the pod
    cache, and the late-discovered parent type still gets its informer (the
    run loop re-drains the deferred queue) so the job IS reported to
    CloudVision with a full lifecycle."""
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   multus_macs=(MAC_A, ))
    harness = Harness(profile_with(pods=[pod],
                                   batch_jobs=[job],
                                   pods_kwargs={"list_delay": 2.5}),
                      pod_sync_max_wait=1.0,
                      # The run loop accounts elapsed by POLL_INTERVAL per
                      # iteration; small sleeps oversleep on some platforms,
                      # so use an accurate 0.25s poll to hit the 1.0s cap
                      # before the delayed list lands at 2.5s.
                      pod_sync_poll_interval=0.25)
    try:
        wait_for(
            lambda: harness.logs.has(
                "Pod informer initial sync not complete after 1.0s, "
                "proceeding anyway"),
            desc="'proceeding anyway' warning")

        # The delayed list lands and populates the pod cache...
        wait_for(lambda: "p1" in harness.monitor.pod_cache,
                 desc="late pod list processed into the cache")

        # ...and with NO further pod events, the deferred-queue re-drain
        # creates the stranded type's informer and the job is reported.
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}
        assert "batch/v1/Job" in harness.monitor.job_informers

        # The full lifecycle flows for the late-discovered job.
        harness.update_job(
            make_batch_job("job-m",
                           uid="uid-job-m",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_COMPLETED")
        assert_lifecycle(harness.jobconfigs_for("uid-job-m"), (MAC_A, ), T0,
                         T1)
    finally:
        stop_and_check(harness, "P7")


def test_p8_plural_discovery():
    """P8/named: a non-English CRD plural is resolved from live discovery and
    used for list+watch; the English plural is never requested."""
    workflow = make_custom_job("argoproj.io/v1alpha1",
                               "Workflow",
                               "w1",
                               status={"phase": "Running"},
                               uid="uid-w1")
    pod = make_pod("step-1",
                   owner=owner_ref("argoproj.io/v1alpha1", "Workflow", "w1",
                                   "uid-w1"),
                   multus_macs=(MAC_A, ))
    profile = profile_with(pods=[pod])
    add_group(profile, "argoproj.io", "v1alpha1",
              {"workflowz": Resource("Workflow", items=[workflow])})
    harness = Harness(profile)
    try:
        started = wait_jobconfig(harness, "uid-w1",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}

        # Discovery doc fetched; list/watch used the advertised plural.
        assert harness.fake.requests_for("/apis/argoproj.io/v1alpha1",
                                         status=200)
        assert harness.fake.requests_for(
            "/apis/argoproj.io/v1alpha1/workflowz", status=200)
        assert not harness.fake.requests_for(
            "/apis/argoproj.io/v1alpha1/workflows"), (
                "informer must use the discovered plural 'workflowz'")
    finally:
        stop_and_check(harness, "P8-workflowz")


def test_p8_plural_fallback_doc404():
    """P8 variant: discovery does not advertise the kind -> English-pluralize
    fallback + warning; list/watch still reach the English plural."""
    workflow = make_custom_job("argoproj.io/v1alpha1",
                               "Workflow",
                               "w1",
                               status={"phase": "Running"},
                               uid="uid-w1")
    pod = make_pod("step-1",
                   owner=owner_ref("argoproj.io/v1alpha1", "Workflow", "w1",
                                   "uid-w1"),
                   multus_macs=(MAC_A, ))
    profile = profile_with(pods=[pod])
    add_group(
        profile, "argoproj.io", "v1alpha1",
        {
            # Served at the English plural but hidden from the discovery doc,
            # which only advertises a different resource of the same group.
            "workflows":
            Resource("Workflow",
                     items=[workflow],
                     hide_from_discovery=True),
            "others":
            Resource("Other", items=[]),
        })
    harness = Harness(profile)
    try:
        started = wait_jobconfig(harness, "uid-w1",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}
        assert harness.fake.requests_for(
            "/apis/argoproj.io/v1alpha1/workflows", status=200)
        assert harness.logs.has(
            "No API plural for argoproj.io v1alpha1 Workflow, using workflows")
    finally:
        stop_and_check(harness, "P8-fallback")


def test_p11_late_claim_initial_sync_reschedules():
    """P11/P1: the claim informer's initial sync completes AFTER pods were
    processed with a cold claim cache (slow claim LIST past the startup
    barrier) -> the late cache warm-up must reschedule the tracked job so
    STARTED still fires.  Pins the reschedule-on-initial-sync behavior of
    _on_resource_claim_change; without it the job is never reported."""
    claim = make_claim("c1",
                       devices=[nic_device("nic0", MAC_A, iface="ens5")])
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   spec_claims=["c1"])
    # Claim LIST answers 6.0s in; the claim barrier (pod_sync_max_wait=0.5)
    # expires first, so the empty-STARTED attempt (POST skipped, status set
    # to RUNNING with an empty interface set) precedes the cache warm-up.
    group = [("resource.k8s.io", "v1beta1", {
        "resourceclaims":
        Resource("ResourceClaim", items=[claim], list_delay=6.0)
    })]
    harness = Harness(profile_with(pods=[pod], batch_jobs=[job], groups=group),
                      pod_sync_max_wait=0.5)
    try:
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}
        assert harness.logs.has("Using resource.k8s.io/v1beta1")
    finally:
        stop_and_check(harness, "P11")


# ===========================================================================
# Scheduler / operator scenarios
# ===========================================================================
def test_s1_batch_job_success():
    """S1/P0: batch Job success -> STARTED then FINISHED/SUCCEEDED with the
    exact CR times; identity, location and MAC list correct."""
    job = make_batch_job("train-1", uid="uid-train-1")
    pod = make_pod("worker-0",
                   owner=owner_ref("batch/v1", "Job", "train-1",
                                   "uid-train-1"),
                   multus_macs=(MAC_A, ))
    harness = Harness(profile_with(pods=[pod], batch_jobs=[job]))
    try:
        started = wait_jobconfig(harness, "uid-train-1",
                                 state="JOB_STATE_RUNNING")[0]
        assert started["name"] == "train-1"
        assert started["location"] == LOCATION
        assert started["start_time"] == T0_ISO  # pod-derived start
        assert interfaces_of(started) == {MAC_A}

        harness.update_job(
            make_batch_job("train-1",
                           uid="uid-train-1",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-train-1", state="JOB_STATE_COMPLETED")
        assert_lifecycle(harness.jobconfigs_for("uid-train-1"), (MAC_A, ),
                         T0, T1)
    finally:
        stop_and_check(harness, "S1")


def test_s3_deletion_of_tracked_job():
    """S3/P0: deleting a tracked unfinished job reports CANCELLED; deleting an
    already-finished job is a no-op."""
    job_del = make_batch_job("job-del", uid="uid-job-del")
    pod_del = make_pod("pod-del",
                       owner=owner_ref("batch/v1", "Job", "job-del",
                                       "uid-job-del"),
                       multus_macs=(MAC_A, ))
    job_done = make_batch_job("job-done", uid="uid-job-done")
    pod_done = make_pod("pod-done",
                        owner=owner_ref("batch/v1", "Job", "job-done",
                                        "uid-job-done"),
                        node="node-b",
                        multus_macs=(MAC_B, ))
    harness = Harness(profile_with(pods=[pod_del, pod_done],
                                   batch_jobs=[job_del, job_done]))
    try:
        wait_jobconfig(harness, "uid-job-del", state="JOB_STATE_RUNNING")
        wait_jobconfig(harness, "uid-job-done", state="JOB_STATE_RUNNING")

        # Finish job-done normally.
        harness.update_job(
            make_batch_job("job-done",
                           uid="uid-job-done",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-job-done", state="JOB_STATE_COMPLETED")

        # Delete the still-running job -> CANCELLED.
        harness.delete_job(make_batch_job("job-del", uid="uid-job-del"))
        cancelled = wait_jobconfig(harness,
                                   "uid-job-del",
                                   state="JOB_STATE_CANCELLED")[0]
        assert ISO_RE.match(cancelled["end_time"]), cancelled
        assert cancelled["start_time"] == T0_ISO
        assert interfaces_of(cancelled) == {MAC_A}

        # Delete the already-finished job -> no additional POST.
        harness.delete_job(make_batch_job("job-done", uid="uid-job-done"))
        time.sleep(SETTLE)
        assert len(harness.jobconfigs_for("uid-job-done")) == 2, (
            harness.jobconfigs_for("uid-job-done"))
    finally:
        stop_and_check(harness, "S3")


def test_s4_pytorchjob_lifecycle():
    """S4/P0: Kubeflow PyTorchJob with master+worker pods -> one tracked job,
    global MAC dedup, FINISHED/SUCCEEDED using the CR times."""
    ptj = make_custom_job("kubeflow.org/v1",
                          "PyTorchJob",
                          "ptj-1",
                          status={"conditions": [{
                              "type": "Running",
                              "status": "True"
                              }]},
                          uid="uid-ptj-1")
    master = make_pod("master",
                      owner=owner_ref("kubeflow.org/v1", "PyTorchJob", "ptj-1",
                                      "uid-ptj-1"),
                      multus_macs=(MAC_A, ))
    worker = make_pod("worker",
                      owner=owner_ref("kubeflow.org/v1", "PyTorchJob", "ptj-1",
                                      "uid-ptj-1"),
                      node="node-b",
                      multus_macs=(MAC_B, ))
    profile = profile_with(pods=[master, worker])
    add_group(profile, "kubeflow.org", "v1",
              {"pytorchjobs": Resource("PyTorchJob", items=[ptj])})
    harness = Harness(profile)
    try:
        started = wait_jobconfig(harness, "uid-ptj-1",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A, MAC_B}  # dedup across pods

        done = copy.deepcopy(ptj)
        done["status"] = {
            "conditions": [{
                "type": "Succeeded",
                "status": "True"
            }],
            "startTime": T0,
            "completionTime": T1,
        }
        harness.push_custom("kubeflow.org", "v1", "pytorchjobs", "MODIFIED",
                            done)
        wait_jobconfig(harness, "uid-ptj-1", state="JOB_STATE_COMPLETED")
        assert_lifecycle(harness.jobconfigs_for("uid-ptj-1"),
                         (MAC_A, MAC_B), T0, T1)

        # Plural came from live discovery.
        assert harness.fake.requests_for("/apis/kubeflow.org/v1", status=200)
        assert harness.fake.requests_for(
            "/apis/kubeflow.org/v1/pytorchjobs", status=200)
    finally:
        stop_and_check(harness, "S4")


def test_s6_trainer_v2_child_jobs():
    """S6/P0: TrainJob -> JobSet -> child batch Jobs -> pods.  Exactly the
    child batch Jobs are tracked; the trainjobs/jobsets informers stay
    dormant (F1/F2)."""
    trainjob = make_custom_job("trainer.kubeflow.org/v1alpha1",
                               "TrainJob",
                               "tj-1",
                               uid="uid-tj-1")
    jobset = make_custom_job("jobset.x-k8s.io/v1alpha2",
                             "JobSet",
                             "js-1",
                             uid="uid-js-1")
    cj1 = make_batch_job("cj-1", uid="uid-cj-1")
    cj2 = make_batch_job("cj-2", uid="uid-cj-2")
    pod1 = make_pod("pod-1",
                    owner=owner_ref("batch/v1", "Job", "cj-1", "uid-cj-1"),
                    multus_macs=(MAC_A, ))
    pod2 = make_pod("pod-2",
                    owner=owner_ref("batch/v1", "Job", "cj-2", "uid-cj-2"),
                    node="node-b",
                    multus_macs=(MAC_B, ))
    profile = profile_with(pods=[pod1, pod2], batch_jobs=[cj1, cj2])
    add_group(profile, "trainer.kubeflow.org", "v1alpha1",
              {"trainjobs": Resource("TrainJob", items=[trainjob])})
    add_group(profile, "jobset.x-k8s.io", "v1alpha2",
              {"jobsets": Resource("JobSet", items=[jobset])})
    harness = Harness(profile)
    try:
        wait_jobconfig(harness, "uid-cj-1", state="JOB_STATE_RUNNING")
        wait_jobconfig(harness, "uid-cj-2", state="JOB_STATE_RUNNING")

        harness.update_job(
            make_batch_job("cj-1",
                           uid="uid-cj-1",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        harness.update_job(
            make_batch_job("cj-2", uid="uid-cj-2", failed=True, start_time=T0))
        wait_jobconfig(harness, "uid-cj-1", state="JOB_STATE_COMPLETED")
        wait_jobconfig(harness, "uid-cj-2", state="JOB_STATE_FAILED")

        # Exactly the two child batch Jobs are tracked.
        uids = {payload["key"]["id"] for payload in harness.cv.jobconfigs()}
        assert uids == {"uid-cj-1", "uid-cj-2"}, uids
        states1 = [p["state"] for p in harness.jobconfigs_for("uid-cj-1")]
        assert states1 == ["JOB_STATE_RUNNING", "JOB_STATE_COMPLETED"], states1
        states2 = [p["state"] for p in harness.jobconfigs_for("uid-cj-2")]
        assert states2 == ["JOB_STATE_RUNNING", "JOB_STATE_FAILED"], states2

        # Dormant informers: neither CR type was ever listed or watched.
        all_requests = harness.fake.requests
        assert not any("/trainjobs" in r["path"] for r in all_requests)
        assert not any("/jobsets" in r["path"] for r in all_requests)
    finally:
        stop_and_check(harness, "S6")


def test_s8_argo_workflow_pod_gc():
    """S8/P0: Argo Workflow with podGC -> tracked as Workflow; FINISHED/
    SUCCEEDED uses pod times (startedAt/finishedAt are not read); GC'd pods
    keep their end times (pod DELETE is a cache no-op)."""
    workflow = make_custom_job("argoproj.io/v1alpha1",
                               "Workflow",
                               "w1",
                               status={
                                   "phase": "Running",
                                   "startedAt": T_WRONG_START,
                                   "finishedAt": T_WRONG_END,
                               },
                               uid="uid-w1")
    pod = make_pod("step-1",
                   owner=owner_ref("argoproj.io/v1alpha1", "Workflow", "w1",
                                   "uid-w1"),
                   multus_macs=(MAC_A, ))
    profile = profile_with(pods=[pod])
    add_group(profile, "argoproj.io", "v1alpha1",
              {"workflows": Resource("Workflow", items=[workflow])})
    harness = Harness(profile)
    try:
        started = wait_jobconfig(harness, "uid-w1",
                                 state="JOB_STATE_RUNNING")[0]
        assert started["start_time"] == T0_ISO

        # Pod finishes, then Argo's podGC deletes it (cache no-op keeps it).
        finished_pod = copy.deepcopy(pod)
        finished_pod["status"]["phase"] = "Succeeded"
        finished_pod["status"]["containerStatuses"][0]["ready"] = False
        finished_pod["status"]["containerStatuses"][0]["state"] = {
            "terminated": {
                "exitCode": 0,
                "startedAt": T0,
                "finishedAt": T1,
            }
        }
        harness.update_pod(finished_pod)
        # Cross-informer ordering is not guaranteed: wait until the pod
        # informer has cached the terminated state before finishing the
        # workflow, otherwise end_time falls back to now().
        wait_for(
            lambda: harness.monitor.pod_cache["step-1"].status.
            container_statuses[0].state.terminated is not None,
            desc="terminated container state cached")
        harness.delete_pod(finished_pod)

        # Workflow succeeds; its misleading CR times must NOT be used.
        done = copy.deepcopy(workflow)
        done["status"] = {
            "phase": "Succeeded",
            "startedAt": T_WRONG_START,
            "finishedAt": T_WRONG_END,
        }
        harness.push_custom("argoproj.io", "v1alpha1", "workflows", "MODIFIED",
                            done)
        finished = wait_jobconfig(harness,
                                  "uid-w1",
                                  state="JOB_STATE_COMPLETED")[0]
        assert finished["start_time"] == T0_ISO, finished
        assert finished["end_time"] == T1_ISO, finished
        assert T_WRONG_START not in json.dumps(finished)
        assert T_WRONG_END not in json.dumps(finished)
        assert interfaces_of(finished) == {MAC_A}
        assert len(harness.jobconfigs_for("uid-w1")) == 2
    finally:
        stop_and_check(harness, "S8")


def test_s13_raycluster_never_succeeds():
    """S13/P0: RayCluster is a long-running service -> never SUCCEEDED; a
    non-terminal condition does not finish it; state=failed -> FAILED; CR
    deletion -> CANCELLED (F4)."""
    rc_ready = make_custom_job("ray.io/v1",
                               "RayCluster",
                               "rc-ready",
                               status={"state": "ready"},
                               uid="uid-rc-ready")
    rc_fail = make_custom_job("ray.io/v1",
                              "RayCluster",
                              "rc-fail",
                              status={"state": "ready"},
                              uid="uid-rc-fail")
    head = make_pod("head",
                    owner=owner_ref("ray.io/v1", "RayCluster", "rc-ready",
                                    "uid-rc-ready"),
                    multus_macs=(MAC_A, ))
    ray_worker = make_pod("ray-worker",
                          owner=owner_ref("ray.io/v1", "RayCluster", "rc-fail",
                                          "uid-rc-fail"),
                          node="node-b",
                          multus_macs=(MAC_B, ))
    profile = profile_with(pods=[head, ray_worker])
    add_group(profile, "ray.io", "v1",
              {"rayclusters": Resource("RayCluster", items=[rc_ready,
                                                            rc_fail])})
    harness = Harness(profile)
    try:
        wait_jobconfig(harness, "uid-rc-ready", state="JOB_STATE_RUNNING")
        wait_jobconfig(harness, "uid-rc-fail", state="JOB_STATE_RUNNING")

        # Non-terminal condition (ReplicaFailure is not in the fail value
        # set) must not finish the job.
        cond = copy.deepcopy(rc_ready)
        cond["status"]["conditions"] = [{
            "type": "ReplicaFailure",
            "status": "True"
        }]
        harness.push_custom("ray.io", "v1", "rayclusters", "MODIFIED", cond)
        time.sleep(SETTLE)
        assert len(harness.jobconfigs_for("uid-rc-ready")) == 1

        # state=failed -> FAILED (pods were Running, so not CANCELLED).
        failed = copy.deepcopy(rc_fail)
        failed["status"]["state"] = "failed"
        harness.push_custom("ray.io", "v1", "rayclusters", "MODIFIED", failed)
        failed_post = wait_jobconfig(harness,
                                     "uid-rc-fail",
                                     state="JOB_STATE_FAILED")[0]
        assert failed_post["end_time"], failed_post

        # Deletion of the still-running cluster -> CANCELLED.
        harness.push_custom("ray.io", "v1", "rayclusters", "DELETED",
                            copy.deepcopy(rc_ready))
        cancelled = wait_jobconfig(harness,
                                   "uid-rc-ready",
                                   state="JOB_STATE_CANCELLED")[0]
        assert ISO_RE.match(cancelled["end_time"])

        # Never SUCCEEDED anywhere in the scenario.
        assert not any(payload["state"] == "JOB_STATE_COMPLETED"
                       for payload in harness.cv.jobconfigs())
    finally:
        stop_and_check(harness, "S13")


def test_s14_rayjob_collapse():
    """S14/P0: the RayJob K8sJobMode chain collapses to RayCluster + submitter
    batch Job; the rayjobs informer stays dormant; RayJob success is invisible
    (no FINISHED for it); cascade delete -> CANCELLED (F3/F4 documented)."""
    rayjob = make_custom_job("ray.io/v1",
                             "RayJob",
                             "rj-1",
                             status={"jobStatus": "SUCCEEDED"},
                             uid="uid-rj-1")
    raycluster = make_custom_job("ray.io/v1",
                                 "RayCluster",
                                 "rc-1",
                                 status={"state": "ready"},
                                 uid="uid-rc-1",
                                 owner=owner_ref("ray.io/v1", "RayJob", "rj-1",
                                                 "uid-rj-1"))
    submitter = make_batch_job("rj-1-submit", uid="uid-submit-1")
    head = make_pod("head",
                    owner=owner_ref("ray.io/v1", "RayCluster", "rc-1",
                                    "uid-rc-1"),
                    multus_macs=(MAC_A, ))
    ray_worker = make_pod("ray-worker",
                          owner=owner_ref("ray.io/v1", "RayCluster", "rc-1",
                                          "uid-rc-1"),
                          node="node-b",
                          multus_macs=(MAC_B, ))
    submit_pod = make_pod("submitter-pod",
                          owner=owner_ref("batch/v1", "Job", "rj-1-submit",
                                          "uid-submit-1"),
                          node="node-c",
                          multus_macs=(MAC_C, ))
    profile = profile_with(pods=[head, ray_worker, submit_pod],
                           batch_jobs=[submitter])
    add_group(profile, "ray.io", "v1", {
        "rayclusters": Resource("RayCluster", items=[raycluster]),
        "rayjobs": Resource("RayJob", items=[rayjob]),
    })
    harness = Harness(profile)
    try:
        wait_jobconfig(harness, "uid-rc-1", state="JOB_STATE_RUNNING")
        wait_jobconfig(harness, "uid-submit-1", state="JOB_STATE_RUNNING")

        # Submitter Job completes -> SUCCEEDED.
        harness.update_job(
            make_batch_job("rj-1-submit",
                           uid="uid-submit-1",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-submit-1", state="JOB_STATE_COMPLETED")

        # Exactly two tracked jobs; the RayJob itself never reports.
        uids = {payload["key"]["id"] for payload in harness.cv.jobconfigs()}
        assert uids == {"uid-rc-1", "uid-submit-1"}, uids

        # The rayjobs informer was never created.
        assert not any("/rayjobs" in r["path"]
                       for r in harness.fake.requests), (
                           "rayjobs informer must stay dormant")

        # Deleting the RayJob cascades to the RayCluster -> CANCELLED.
        harness.push_custom("ray.io", "v1", "rayclusters", "DELETED",
                            copy.deepcopy(raycluster))
        wait_jobconfig(harness, "uid-rc-1", state="JOB_STATE_CANCELLED")

        # The RayJob CR itself is never tracked (no pod references it), so
        # no post ever carries its uid; detection of a *tracked* RayJob's
        # jobStatus success is covered by test_s14b below.
        assert harness.jobconfigs_for("uid-rj-1") == []
    finally:
        stop_and_check(harness, "S14")


def test_s14b_rayjob_jobstatus_success_detection():
    """S14b: RayJob completion via status.jobStatus (F3 fix).

    A tracked RayJob whose status.jobStatus reaches a terminal value now
    reports through the normal lifecycle.  Values are the REAL KubeRay enum
    (verified from ray-project/kuberay rayjob_types.go: jobStatus is
    ""/PENDING/RUNNING/STOPPED/SUCCEEDED/FAILED with no conditions/phase/
    state fields on RayJobStatus): a live RUNNING -> SUCCEEDED transition
    yields STARTED then FINISHED/JOB_STATE_COMPLETED, a RUNNING -> FAILED
    transition yields FINISHED/JOB_STATE_FAILED, and deleting the
    terminal CRs afterwards is a no-op (no more CANCELLED mislabel).  A
    RayJob already SUCCEEDED at initial sync reports COMPLETED once via the
    pre-existing path.  RayCluster keeps its documented no-success-signal
    limitation (see test_s13)."""
    # -- RayJob that completes live
    rj_live = make_custom_job("ray.io/v1",
                              "RayJob",
                              "rj-live",
                              status={
                                  "jobStatus": "RUNNING",
                                  "startTime": T0
                              },
                              uid="uid-rj-live")
    pod_live = make_pod("rj-live-head",
                        owner=owner_ref("ray.io/v1", "RayJob", "rj-live",
                                        "uid-rj-live"),
                        multus_macs=(MAC_A, ))
    # -- RayJob already SUCCEEDED before the informer started
    rj_pre = make_custom_job("ray.io/v1",
                             "RayJob",
                             "rj-pre",
                             status={"jobStatus": "SUCCEEDED"},
                             uid="uid-rj-pre")
    pod_pre = make_pod("rj-pre-head",
                       owner=owner_ref("ray.io/v1", "RayJob", "rj-pre",
                                       "uid-rj-pre"),
                       node="node-b",
                       container_state="terminated",
                       multus_macs=(MAC_B, ))
    # -- RayJob that fails live (application-level FAILED)
    rj_fail = make_custom_job("ray.io/v1",
                              "RayJob",
                              "rj-fail",
                              status={
                                  "jobStatus": "RUNNING",
                                  "startTime": T0
                              },
                              uid="uid-rj-fail")
    pod_fail = make_pod("rj-fail-head",
                        owner=owner_ref("ray.io/v1", "RayJob", "rj-fail",
                                        "uid-rj-fail"),
                        node="node-c",
                        multus_macs=(MAC_C, ))
    profile = profile_with(pods=[pod_live, pod_pre, pod_fail])
    add_group(profile, "ray.io", "v1",
              {"rayjobs": Resource("RayJob",
                                   items=[rj_live, rj_pre, rj_fail])})
    harness = Harness(profile)
    try:
        # Pre-existing completed RayJob -> exactly one COMPLETED via the
        # initial-sync path, with pod-derived times (no CR completionTime).
        pre_posts = wait_jobconfig(harness, "uid-rj-pre",
                                   state="JOB_STATE_COMPLETED")
        assert len(pre_posts) == 1, pre_posts
        assert pre_posts[0]["start_time"] == T0_ISO
        assert pre_posts[0]["end_time"] == T1_ISO
        assert interfaces_of(pre_posts[0]) == {MAC_B}

        # Live RayJob: STARTED first.
        started = wait_jobconfig(harness, "uid-rj-live",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A}

        # Pods finish, then the driver reports jobStatus=Completed.
        finished_pod = copy.deepcopy(pod_live)
        finished_pod["status"]["phase"] = "Succeeded"
        finished_pod["status"]["containerStatuses"][0]["ready"] = False
        finished_pod["status"]["containerStatuses"][0]["state"] = {
            "terminated": {
                "exitCode": 0,
                "startedAt": T0,
                "finishedAt": T1,
            }
        }
        harness.update_pod(finished_pod)
        wait_for(lambda: harness.monitor.pod_cache["rj-live-head"].status.
                 container_statuses[0].state.terminated is not None,
                 desc="terminated container state cached")
        completed = copy.deepcopy(rj_live)
        completed["status"]["jobStatus"] = "SUCCEEDED"
        harness.push_custom("ray.io", "v1", "rayjobs", "MODIFIED", completed)
        finished = wait_jobconfig(harness,
                                  "uid-rj-live",
                                  state="JOB_STATE_COMPLETED")[0]
        assert finished["start_time"] == T0, finished  # CR status.startTime
        assert finished["end_time"] == T1_ISO, finished  # pod end fallback

        # Failed RayJob: RUNNING -> FAILED reports FINISHED/JOB_STATE_FAILED
        # (pod-derived end time from the failed container).
        failed_pod = copy.deepcopy(pod_fail)
        failed_pod["status"]["phase"] = "Failed"
        failed_pod["status"]["containerStatuses"][0]["ready"] = False
        failed_pod["status"]["containerStatuses"][0]["state"] = {
            "terminated": {
                "exitCode": 1,
                "startedAt": T0,
                "finishedAt": T1,
            }
        }
        harness.update_pod(failed_pod)
        wait_for(lambda: harness.monitor.pod_cache["rj-fail-head"].status.
                 container_statuses[0].state.terminated is not None,
                 desc="failed container state cached")
        failed_cr = copy.deepcopy(rj_fail)
        failed_cr["status"]["jobStatus"] = "FAILED"
        harness.push_custom("ray.io", "v1", "rayjobs", "MODIFIED", failed_cr)
        failed_post = wait_jobconfig(harness,
                                     "uid-rj-fail",
                                     state="JOB_STATE_FAILED")[0]
        assert failed_post["start_time"] == T0, failed_post  # CR startTime
        assert failed_post["end_time"] == T1_ISO, failed_post  # pod end
        assert interfaces_of(failed_post) == {MAC_C}

        # Deleting the terminal RayJob CRs must not add CANCELLED posts.
        harness.push_custom("ray.io", "v1", "rayjobs", "DELETED",
                            copy.deepcopy(rj_live))
        harness.push_custom("ray.io", "v1", "rayjobs", "DELETED",
                            copy.deepcopy(rj_pre))
        harness.push_custom("ray.io", "v1", "rayjobs", "DELETED",
                            copy.deepcopy(rj_fail))
        time.sleep(SETTLE)
        live_states = [
            p["state"] for p in harness.jobconfigs_for("uid-rj-live")
        ]
        assert live_states == ["JOB_STATE_RUNNING", "JOB_STATE_COMPLETED"], (
            live_states)
        fail_states = [
            p["state"] for p in harness.jobconfigs_for("uid-rj-fail")
        ]
        assert fail_states == ["JOB_STATE_RUNNING", "JOB_STATE_FAILED"], (
            fail_states)
        assert len(harness.jobconfigs_for("uid-rj-pre")) == 1
        assert not any(p["state"] == "JOB_STATE_CANCELLED"
                       for p in harness.cv.jobconfigs())
    finally:
        stop_and_check(harness, "S14b")


def test_s10_runai_runaijob_status_contract():
    """S10/P2: Run:ai RunaiJob (run.ai/v1) owning pods directly with
    status.phase-based completion -> tracked via live discovery of the run.ai
    group, full lifecycle, FINISHED/SUCCEEDED.

    Contract pin, not an implementation mirror: the run.ai operator is
    closed-source.  VERIFIED against NVIDIA's official docs
    (run-ai-docs.nvidia.com, checked 2026-09): the V2 CRD kinds named in the
    whitelist exist (TrainingWorkload/InferenceWorkload/InteractiveWorkload/
    DistributedWorkload/DistributedInferenceWorkload/ExternalWorkload;
    RunaiJob is the legacy kind), workloads report phase-style status, and
    this repo's RBAC grants runaijobs plus the legacy plurals.  NOT publicly
    verifiable: whether the controller sets pod ownerReferences to the run.ai
    CR - that is this integration's discovery premise (doc U3).  If a real
    cluster shows pods owned by intermediate controllers instead, this
    scenario and the run.ai whitelist entries need revisiting together."""
    runai_job = make_custom_job("run.ai/v1",
                                "RunaiJob",
                                "rj-1",
                                status={"phase": "Running"},
                                uid="uid-rj-1")
    pod = make_pod("rj-pod",
                   owner=owner_ref("run.ai/v1", "RunaiJob", "rj-1",
                                   "uid-rj-1"),
                   multus_macs=(MAC_A, ))
    profile = profile_with(pods=[pod])
    add_group(profile, "run.ai", "v1",
              {"runaijobs": Resource("RunaiJob", items=[runai_job])})
    harness = Harness(profile)
    try:
        started = wait_jobconfig(harness, "uid-rj-1",
                                 state="JOB_STATE_RUNNING")[0]
        assert started["location"] == LOCATION
        assert started["start_time"] == T0_ISO  # pod-derived start
        assert interfaces_of(started) == {MAC_A}

        # Pods finish, then the operator flips status.phase to 'completed'
        # (lowercase member of the shared success value set).
        finished_pod = copy.deepcopy(pod)
        finished_pod["status"]["phase"] = "Succeeded"
        finished_pod["status"]["containerStatuses"][0]["ready"] = False
        finished_pod["status"]["containerStatuses"][0]["state"] = {
            "terminated": {
                "exitCode": 0,
                "startedAt": T0,
                "finishedAt": T1,
            }
        }
        harness.update_pod(finished_pod)
        wait_for(lambda: harness.monitor.pod_cache["rj-pod"].status.
                 container_statuses[0].state.terminated is not None,
                 desc="terminated container state cached")

        done = copy.deepcopy(runai_job)
        done["status"]["phase"] = "completed"
        harness.push_custom("run.ai", "v1", "runaijobs", "MODIFIED", done)
        finished = wait_jobconfig(harness,
                                  "uid-rj-1",
                                  state="JOB_STATE_COMPLETED")[0]
        assert finished["start_time"] == T0_ISO
        assert finished["end_time"] == T1_ISO  # pod-derived end
        assert interfaces_of(finished) == {MAC_A}
        assert len(harness.jobconfigs_for("uid-rj-1")) == 2

        # Plural came from live run.ai discovery (group resolved, kind
        # whitelisted).
        assert harness.fake.requests_for("/apis/run.ai/v1", status=200)
        assert harness.fake.requests_for("/apis/run.ai/v1/runaijobs",
                                         status=200)
    finally:
        stop_and_check(harness, "S10")


def test_s11_runai_legacy_kind_ignored():
    """S11/P2: a pod owned by the legacy run.ai kind 'Training' (whose RBAC
    plural 'trainings' is granted but whose kind is NOT whitelisted) is
    ignored entirely: not cached, no discovery fetch for run.ai, no informer,
    no job, no CloudVision events."""
    legacy_pod = make_pod("legacy-pod",
                          owner=owner_ref("run.ai/v1", "Training", "tr-1",
                                          "uid-tr-1"),
                          multus_macs=(MAC_A, ))
    profile = profile_with(pods=[legacy_pod])
    # The group is served (RBAC would allow listing it) but nothing should
    # ever request it: the whitelist check short-circuits before discovery.
    add_group(profile, "run.ai", "v1",
              {"trainings": Resource("Training", items=[])})
    harness = Harness(profile)
    try:
        wait_for(
            lambda: harness.logs.has(
                "Pod informer: Initial sync complete"),  # noqa: 501 (match)
            desc="pod informer initial sync")
        time.sleep(SETTLE)
        # Ignored before caching: the pod never enters the pod cache.
        assert "legacy-pod" not in harness.monitor.pod_cache
        assert harness.cv.jobconfigs() == []
        assert not any("/apis/run.ai" in r["path"]
                       for r in harness.fake.requests), (
                           "non-whitelisted legacy kind must be ignored "
                           "before any run.ai discovery or list")
    finally:
        stop_and_check(harness, "S11")


# ===========================================================================
# Network allocation scenarios
# ===========================================================================
def test_n1_multus_only_macs():
    """N1/P0: no DRA API, Multus annotation with two secondary NICs ->
    interfaces.values carries exactly both MACs on every post (payload shape
    is allocation-source-agnostic)."""
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   multus_macs=(MAC_A, MAC_B))
    harness = Harness(profile_with(pods=[pod], batch_jobs=[job]))
    try:
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_RUNNING")
        harness.update_job(
            make_batch_job("job-m",
                           uid="uid-job-m",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_jobconfig(harness, "uid-job-m", state="JOB_STATE_COMPLETED")
        assert_lifecycle(harness.jobconfigs_for("uid-job-m"),
                         (MAC_A, MAC_B), T0, T1)
    finally:
        stop_and_check(harness, "N1")


def test_n2_malformed_annotation():
    """N2/P1: an unparseable network-status annotation -> logged error, no
    crash; zero-MAC posts are skipped in interface mode; the job lifecycle
    continues."""
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod(
        "p1",
        owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
        extra_annotations={
            "k8s.v1.cni.cncf.io/network-status": "not-json{{"
        })
    harness = Harness(profile_with(pods=[pod], batch_jobs=[job]))
    try:
        # STARTED is attempted (job goes RUNNING internally) but skipped.
        wait_for(lambda: any(job.job_key == "uid-job-m"
                             and job.status == "RUNNING"
                             for job in harness.tracked().values()),
                 desc="job tracked and RUNNING with malformed annotation")
        assert harness.cv.jobconfigs_for("uid-job-m") == []

        # Completion is processed; still nothing posted (no MACs).
        harness.update_job(
            make_batch_job("job-m",
                           uid="uid-job-m",
                           complete=True,
                           start_time=T0))
        wait_for(lambda: "uid-job-m" not in harness.tracked(),
                 desc="finished job removed from tracking")
        assert harness.cv.jobconfigs_for("uid-job-m") == []
        assert harness.logs.has("Failed to parse network-status annotation")
    finally:
        stop_and_check(harness, "N2")


def test_n3_annotation_filter_rules():
    """N3/P1: eth0/default dropped; net1 kept; an rdma-named network kept even
    with a non-net interface name; a plain sriov-net with iface 'foo'
    dropped."""
    entries = [
        {  # dropped: default + eth0
            "name": "net-eth0",
            "interface": "eth0",
            "default": True,
            "ips": ["10.0.0.1"],
            "mac": MAC_C,
        },
        {  # kept: interface net1
            "name": "sriov-net",
            "interface": "net1",
            "ips": ["10.0.0.2"],
            "mac": MAC_A,
        },
        {  # kept: network name contains 'rdma'
            "name": "rdma-net",
            "interface": "foo0",
            "ips": ["10.0.0.3"],
            "mac": MAC_B,
        },
        {  # dropped: neither net* iface nor rdma in the network name
            "name": "sriov-net",
            "interface": "foo1",
            "ips": ["10.0.0.4"],
            "mac": MAC_D,
        },
    ]
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   extra_annotations={
                       "k8s.v1.cni.cncf.io/network-status":
                       json.dumps(entries)
                   })
    harness = Harness(profile_with(pods=[pod], batch_jobs=[job]))
    try:
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A, MAC_B}, started
    finally:
        stop_and_check(harness, "N3")


def test_n4_dra_claim_devices():
    """N4/P0: a claim with two networkData devices (one missing interfaceName
    -> device-name fallback) and one GPU device -> exactly the two NIC MACs;
    the GPU device is skipped."""
    claim = make_claim(
        "c1",
        devices=[
            nic_device("nic0", MAC_A, iface="net1"),
            nic_device("ens20np0", MAC_B, iface=None, ip="10.10.0.2"),
            gpu_device(),
        ])
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   spec_claims=["c1"])
    harness = Harness(dra_profile([pod], [job], {"v1": [claim]}))
    try:
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A, MAC_B}, started

        # interfaceName fallback lands on the device name; ip is ips[0].
        cached = harness.monitor.pod_cache["p1"]
        ifaces = harness.monitor._extract_network_interfaces(cached)
        by_mac = {iface.mac: iface for iface in ifaces}
        assert by_mac[MAC_A].interface == "net1"
        assert by_mac[MAC_B].interface == "ens20np0"
        assert by_mac[MAC_B].ip == "10.10.0.2"
        assert len(ifaces) == 2  # GPU device contributed nothing
    finally:
        stop_and_check(harness, "N4")


def test_n5_claim_pod_association_paths():
    """N5/P0: all four claim->pod association paths map the claim's MACs:
    spec.resourceClaims, status.resourceClaimStatuses,
    status.extendedResourceClaimStatus, and claim reservedFor (uid match)."""
    claim = make_claim(
        "shared",
        devices=[nic_device("nic0", MAC_A, iface="ens5")],
        reserved_for=[{
            "resource": "pods",
            "name": "p-d",
            "uid": "uid-p-d",
        }])
    owner = owner_ref("batch/v1", "Job", "job-n5", "uid-job-n5")
    pods = [
        make_pod("p-a", owner=owner, spec_claims=["shared"]),
        make_pod("p-b", owner=owner, claim_statuses=["shared"]),
        make_pod("p-c", owner=owner, extended="shared"),
        make_pod("p-d", owner=owner),  # reservedFor path only
    ]
    job = make_batch_job("job-n5", uid="uid-job-n5")
    harness = Harness(dra_profile(pods, [job], {"v1": [claim]}))
    try:
        started = wait_jobconfig(harness, "uid-job-n5",
                                 state="JOB_STATE_RUNNING")[0]
        # Four pods share one MAC: the global per-job dedup yields one entry.
        assert interfaces_of(started) == {MAC_A}, started

        # Each pod individually resolved the claim through its own path.
        for name in ("p-a", "p-b", "p-c", "p-d"):
            cached = harness.monitor.pod_cache[name]
            ifaces = harness.monitor._extract_network_interfaces(cached)
            macs = {iface.mac for iface in ifaces}
            assert MAC_A in macs, (name, ifaces)
    finally:
        stop_and_check(harness, "N5")


def test_n6_reservedfor_matching_rules():
    """N6/P2: reservedFor matching contract as implemented (dra.py, since the
    reservedFor UID-matching fix): uid match wins even across names; resource
    != pods is skipped; when BOTH uids are present and differ the name is
    ignored (a replaced pod must not inherit the old pod's NICs); the name
    fallback applies only when the reservation carries no uid (old-client
    compatibility).

    Contract note: an earlier revision of the scenario matrix expected
    same-name/different-uid to map; the code is deliberately uid-strict.
    This scenario pins the implemented contract so a behavior change here
    fails loudly.
    """
    claim = make_claim(
        "c1",
        devices=[nic_device("nic0", MAC_A, iface="ens5")],
        reserved_for=[
            # uid match -> maps (uid-first, name mismatch is irrelevant).
            {
                "resource": "pods",
                "name": "some-other-pod",
                "uid": "uid-p1"
            },
            # Non-pod resource -> never maps.
            {
                "resource": "other",
                "name": "p1",
                "uid": "unrelated"
            },
        ])
    claim_replaced_pod = make_claim(
        "c2",
        devices=[nic_device("nic3", MAC_D, iface="ens7")],
        reserved_for=[
            # Same name, different uid -> does NOT map (uid-strict): the
            # replaced pod's reservation stays with the old pod.
            {
                "resource": "pods",
                "name": "p1",
                "uid": "older-uid"
            },
        ])
    claim_name_fallback = make_claim(
        "c3",
        devices=[nic_device("nic1", MAC_B, iface="ens6")],
        reserved_for=[
            # Reservation without uid -> maps via the name fallback
            # (old-client compatibility path).
            {
                "resource": "pods",
                "name": "p1",
            },
        ])
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   multus_macs=(MAC_C, ))
    harness = Harness(
        dra_profile([pod], [job],
                    {"v1": [claim, claim_replaced_pod, claim_name_fallback]}))
    try:
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        # uid-mapped MAC_A + no-uid name-fallback MAC_B; the non-pod
        # reservation of c1 and the replaced-pod claim c2 (MAC_D) contribute
        # nothing; Multus MAC_C is independent.
        assert interfaces_of(started) == {MAC_A, MAC_B, MAC_C}, started
    finally:
        stop_and_check(harness, "N6")


def test_n7_mixed_dra_multus_dedup():
    """N7/named: the same NIC seen via DRA (uppercase MAC) and Multus
    (lowercase) is reported once with the DRA record; Multus-only and
    DRA-only MACs are kept (case-insensitive dedup, DRA wins)."""
    claim = make_claim(
        "c1",
        devices=[
            nic_device("ens5", MAC_UPPER, iface="ens5", ip="10.10.0.1"),
            nic_device("ens6", MAC_C, iface="ens6", ip="10.10.0.3"),
        ])
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   spec_claims=["c1"],
                   multus_macs=(MAC_LOWER, MAC_B))
    harness = Harness(dra_profile([pod], [job], {"v1": [claim]}))
    try:
        started = wait_jobconfig(harness, "uid-job-m",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_UPPER, MAC_B, MAC_C}, started

        # DRA wins the collision: the kept record for the shared NIC carries
        # the DRA interface name and IP, not the Multus ones.
        cached = harness.monitor.pod_cache["p1"]
        ifaces = harness.monitor._extract_network_interfaces(cached)
        shared = [i for i in ifaces if i.mac.lower() == MAC_LOWER]
        assert len(shared) == 1, ifaces
        assert shared[0].interface == "ens5"
        assert shared[0].ip == "10.10.0.1"
    finally:
        stop_and_check(harness, "N7")


def test_n8_late_arriving_macs():
    """N8/P0: the driver publishes networkData after the pod is Running ->
    the zero-MAC STARTED is skipped and an UPDATE carrying the MACs is posted
    after the claim update; unrelated claim updates do not re-trigger."""
    claim = make_claim("c1", devices=[])
    other_claim = make_claim("c2", devices=[])
    job = make_batch_job("job-m", uid="uid-job-m")
    pod = make_pod("p1",
                   owner=owner_ref("batch/v1", "Job", "job-m", "uid-job-m"),
                   spec_claims=["c1"])
    harness = Harness(dra_profile([pod], [job],
                                  {"v1": [claim, other_claim]}))
    try:
        # STARTED attempted with zero MACs -> skipped (job is RUNNING though).
        wait_for(lambda: any(job.job_key == "uid-job-m"
                             and job.status == "RUNNING"
                             for job in harness.tracked().values()),
                 desc="job RUNNING with zero MACs")
        time.sleep(SETTLE)
        assert harness.cv.jobconfigs_for("uid-job-m") == []

        # The driver publishes the MAC on the claim -> UPDATE with MACs.
        updated = copy.deepcopy(claim)
        updated["status"]["devices"] = [nic_device("nic0", MAC_A,
                                                   iface="ens5")]
        harness.push_custom("resource.k8s.io", "v1", "resourceclaims",
                            "MODIFIED", updated)
        post = wait_jobconfig(harness, "uid-job-m")[0]
        assert post["state"] == "JOB_STATE_RUNNING"  # UPDATE maps to RUNNING
        assert interfaces_of(post) == {MAC_A}
        assert len(harness.cv.jobconfigs_for("uid-job-m")) == 1

        # An unrelated claim update must not re-trigger this job.
        unrelated = copy.deepcopy(other_claim)
        unrelated["status"]["devices"] = [gpu_device()]
        harness.push_custom("resource.k8s.io", "v1", "resourceclaims",
                            "MODIFIED", unrelated)
        time.sleep(SETTLE)
        assert len(harness.cv.jobconfigs_for("uid-job-m")) == 1
    finally:
        stop_and_check(harness, "N8")


def test_n9_interface_vs_node_mode():
    """N9/P0: the same fixture under both JOBCONFIG_MODEs -> interface mode
    posts deduped MACs, node mode posts unique node names; pods without a
    nodeName are filtered in both modes."""
    owner = owner_ref("batch/v1", "Job", "job-n9", "uid-job-n9")
    pods = [
        make_pod("p1", owner=owner, node="node-a", multus_macs=(MAC_A, )),
        make_pod("p2", owner=owner, node="node-a", multus_macs=(MAC_B, )),
        make_pod("p3", owner=owner, node=None, multus_macs=(MAC_C, )),
    ]
    job = make_batch_job("job-n9", uid="uid-job-n9")

    # -- interface mode
    harness = Harness(profile_with(pods=copy.deepcopy(pods),
                                   batch_jobs=[copy.deepcopy(job)]),
                      jobconfig_mode="interface")
    try:
        started = wait_jobconfig(harness, "uid-job-n9",
                                 state="JOB_STATE_RUNNING")[0]
        assert interfaces_of(started) == {MAC_A, MAC_B}, started
        assert "nodes" not in started
    finally:
        stop_and_check(harness, "N9-interface")

    # -- node mode
    harness = Harness(profile_with(pods=copy.deepcopy(pods),
                                   batch_jobs=[copy.deepcopy(job)]),
                      jobconfig_mode="node")
    try:
        started = wait_jobconfig(harness, "uid-job-n9",
                                 state="JOB_STATE_RUNNING")[0]
        assert nodes_of(started) == {"node-a"}, started
        assert "interfaces" not in started
    finally:
        stop_and_check(harness, "N9-node")


def test_n10_neither_dra_nor_multus():
    """N10/P0: eth0-only pods -> interface mode never posts (zero-MAC skip);
    node mode reports the node inventory."""
    owner = owner_ref("batch/v1", "Job", "job-n10", "uid-job-n10")
    pods = [
        make_pod("p1", owner=owner, node="node-a"),
        make_pod("p2", owner=owner, node="node-b"),
    ]
    job = make_batch_job("job-n10", uid="uid-job-n10")

    # -- interface mode: no MACs -> JobConfig POST skipped entirely
    harness = Harness(profile_with(pods=copy.deepcopy(pods),
                                   batch_jobs=[copy.deepcopy(job)]),
                      jobconfig_mode="interface")
    try:
        # Let the STARTED attempt happen first (job goes RUNNING internally
        # with zero MACs, so the POST is skipped) before completing the job.
        wait_for(lambda: any(job.job_key == "uid-job-n10"
                             and job.status == "RUNNING"
                             for job in harness.tracked().values()),
                 desc="job RUNNING with zero MACs (STARTED attempted)")
        harness.update_job(
            make_batch_job("job-n10",
                           uid="uid-job-n10",
                           complete=True,
                           start_time=T0,
                           completion_time=T1))
        wait_for(lambda: "uid-job-n10" not in harness.tracked(),
                 desc="job completed without any POST")
        assert harness.cv.jobconfigs() == []
        assert harness.logs.has("no secondary interfaces found")
    finally:
        stop_and_check(harness, "N10-interface")

    # -- node mode: normal node-name payload
    harness = Harness(profile_with(pods=copy.deepcopy(pods),
                                   batch_jobs=[copy.deepcopy(job)]),
                      jobconfig_mode="node")
    try:
        started = wait_jobconfig(harness, "uid-job-n10",
                                 state="JOB_STATE_RUNNING")[0]
        assert nodes_of(started) == {"node-a", "node-b"}, started
    finally:
        stop_and_check(harness, "N10-node")


# ===========================================================================
# NodeConfig scenarios
# ===========================================================================
def _run_nodeconfig_mode(mode):
    """Shared fixture for C1x: one node-state CR, one NodeConfig mode run."""
    if mode == "discovery":
        group = "cloudvision.arista.io"
        plural = "nodeinterfacestates"
        ns = "cloudvision"
        kind = "NodeInterfaceState"
        nodeconfig_mode = "discovery"
    else:
        group = "sriovnetwork.openshift.io"
        plural = "sriovnetworknodestates"
        ns = "network-operator"
        kind = "SriovNetworkNodeState"
        nodeconfig_mode = "sriovoperator"

    state = make_node_state(kind, f"{group}/v1", "node-1", ns, vf_mac=MAC_C)
    profile = empty_profile()
    add_group(profile, group, "v1", {plural: Resource(kind, items=[state])})
    harness = Harness(profile, nodeconfig_mode=nodeconfig_mode)
    try:
        wait_for(lambda: harness.cv.nodeconfigs(),
                 desc=f"NodeConfig POST ({mode})")
        posts = harness.cv.nodeconfigs()
        assert len(posts) == 1, posts
        return posts[0]
    finally:
        stop_and_check(harness, f"C1x-{mode}")


def test_c1x_nodeconfig_discovery_vs_sriovoperator():
    """C1x/P0: discovery and sriovoperator NodeConfig modes produce identical
    NodeConfig payloads for equivalent CR bodies (PFs + VFs, ip mapping)."""
    discovery_payload = _run_nodeconfig_mode("discovery")
    sriov_payload = _run_nodeconfig_mode("sriovoperator")

    assert discovery_payload == sriov_payload
    assert discovery_payload["key"] == {"id": "node-1"}
    assert discovery_payload["hostname"] == "node-1"
    assert discovery_payload["location"] == LOCATION
    ifaces = discovery_payload["data_interfaces"]["values"]
    by_name = {iface["name"]: iface for iface in ifaces}
    assert set(by_name) == {"ens5", "ens5f0v0", "ens6"}
    assert by_name["ens5"]["mac_address"] == MAC_A
    assert by_name["ens5"]["ip_addresses"]["values"] == ["10.0.0.1"]
    assert by_name["ens5f0v0"]["mac_address"] == MAC_C
    assert by_name["ens5f0v0"]["ip_addresses"]["values"] == ["10.1.0.1"]
    assert by_name["ens6"]["mac_address"] == MAC_B
    assert by_name["ens6"]["ip_addresses"]["values"] == []


def test_c5x_nodeconfig_change_detection_and_delete():
    """C5x/P0: identical re-delivery posts nothing; a changed VF MAC posts
    once; CR deletion issues a NodeConfig DELETE for the node."""
    state = make_node_state("NodeInterfaceState", "cloudvision.arista.io/v1",
                            "node-1", "cloudvision", vf_mac=MAC_C)
    profile = empty_profile()
    add_group(profile, "cloudvision.arista.io", "v1",
              {"nodeinterfacestates": Resource("NodeInterfaceState",
                                               items=[state])})
    harness = Harness(profile, nodeconfig_mode="discovery")
    try:
        wait_for(lambda: len(harness.cv.nodeconfigs()) >= 1,
                 desc="initial NodeConfig sync")
        assert len(harness.cv.nodeconfigs()) == 1

        # Identical body -> change detection suppresses the POST.
        harness.push_custom("cloudvision.arista.io", "v1",
                            "nodeinterfacestates", "MODIFIED",
                            copy.deepcopy(state))
        time.sleep(SETTLE)
        assert len(harness.cv.nodeconfigs()) == 1

        # One VF MAC changes -> exactly one more POST with the new MAC.
        changed = copy.deepcopy(state)
        changed["status"]["interfaces"][0]["Vfs"][0]["mac"] = MAC_D
        harness.push_custom("cloudvision.arista.io", "v1",
                            "nodeinterfacestates", "MODIFIED", changed)
        wait_for(lambda: len(harness.cv.nodeconfigs()) >= 2,
                 desc="NodeConfig POST after VF MAC change")
        assert len(harness.cv.nodeconfigs()) == 2
        latest = harness.cv.nodeconfigs()[-1]
        vf = [
            iface for iface in latest["data_interfaces"]["values"]
            if iface["name"] == "ens5f0v0"
        ][0]
        assert vf["mac_address"] == MAC_D

        # CR deletion -> NodeConfig DELETE for the node.
        harness.push_custom("cloudvision.arista.io", "v1",
                            "nodeinterfacestates", "DELETED",
                            copy.deepcopy(state))
        wait_for(lambda: harness.cv.nodeconfig_deletes() == ["node-1"],
                 desc="NodeConfig DELETE for node-1")
        assert len(harness.cv.nodeconfigs()) == 2  # no further POSTs
    finally:
        stop_and_check(harness, "C5x")


# ===========================================================================
# Runner
# ===========================================================================
SCENARIOS = [
    # Platform / protocol (6.1)
    test_p1_no_dra_api_multus_fallback,
    test_p2_dra_v1,
    test_p3_dra_v1beta1_fallback,
    test_p5_watch_stream_closed_mid_session,
    test_p6_watch_410_resync,
    test_p7_slow_pod_initial_sync,
    test_p8_plural_discovery,
    test_p8_plural_fallback_doc404,
    test_p11_late_claim_initial_sync_reschedules,
    # Scheduler / operator (6.2)
    test_s1_batch_job_success,
    test_s3_deletion_of_tracked_job,
    test_s4_pytorchjob_lifecycle,
    test_s6_trainer_v2_child_jobs,
    test_s8_argo_workflow_pod_gc,
    test_s13_raycluster_never_succeeds,
    test_s14_rayjob_collapse,
    test_s14b_rayjob_jobstatus_success_detection,
    test_s10_runai_runaijob_status_contract,
    test_s11_runai_legacy_kind_ignored,
    # Network allocation (6.3)
    test_n1_multus_only_macs,
    test_n2_malformed_annotation,
    test_n3_annotation_filter_rules,
    test_n4_dra_claim_devices,
    test_n5_claim_pod_association_paths,
    test_n6_reservedfor_matching_rules,
    test_n7_mixed_dra_multus_dedup,
    test_n8_late_arriving_macs,
    test_n9_interface_vs_node_mode,
    test_n10_neither_dra_nor_multus,
    # NodeConfig (6.4)
    test_c1x_nodeconfig_discovery_vs_sriovoperator,
    test_c5x_nodeconfig_change_detection_and_delete,
]


DEFAULT_JOBS = min(4, os.cpu_count() or 1)


def _parse_jobs(argv):
    """--jobs N (or --jobs=N) selects the worker process count; default is
    4 parallel workers (1 on single-core boxes).  Every scenario owns its
    fake apiserver, CV stub, kubeconfig and tmpdir, so scenarios are safe
    to run in separate processes; constants mutation makes them unsafe to
    thread within one process."""
    jobs = DEFAULT_JOBS
    for index, arg in enumerate(argv):
        if arg == "--jobs" and index + 1 < len(argv):
            jobs = int(argv[index + 1])
        elif arg.startswith("--jobs="):
            jobs = int(arg.split("=", 1)[1])
    return max(1, jobs)


def _run_scenario(scenario):
    """Pool worker body: one scenario, isolated in this process."""
    name = scenario.__name__
    begin = time.time()
    try:
        scenario()
        return name, time.time() - begin, None
    except Exception:
        import traceback
        error = traceback.format_exc()
        harness = ACTIVE_HARNESS
        if harness is not None:
            tail = harness.logs.messages[-40:]
            if tail:
                error += (f"\n--- last {len(tail)} harness log lines "
                          f"(oldest first) ---\n" + "\n".join(tail))
        return name, time.time() - begin, error


def main():
    jobs = _parse_jobs(sys.argv[1:])
    started = time.time()
    results = {}

    def record(name, elapsed, error):
        results[name] = (elapsed, error)
        if error is None:
            print(f"{name} ok ({elapsed:.1f}s)", flush=True)
        else:
            print(f"{name} FAILED ({elapsed:.1f}s)", flush=True)
            print(error, flush=True)

    if jobs == 1:
        for scenario in SCENARIOS:
            record(*_run_scenario(scenario))
    else:
        print(f"running {len(SCENARIOS)} scenarios with {jobs} workers",
              flush=True)
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=jobs) as pool:
            futures = [pool.submit(_run_scenario, s) for s in SCENARIOS]
            for future in concurrent.futures.as_completed(futures):
                record(*future.result())

    elapsed = time.time() - started
    failures = [s.__name__ for s in SCENARIOS if results[s.__name__][1]]
    if failures:
        print(f"FAILED {len(failures)}/{len(SCENARIOS)} "
              f"({elapsed:.1f}s): {failures}")
        raise SystemExit(1)
    axes = {}
    for scenario in SCENARIOS:
        axis = scenario.__name__.split("_")[1][0].upper()
        axes[axis] = axes.get(axis, 0) + 1
    coverage = " ".join(f"{axis}={count}" for axis, count in sorted(axes.items()))
    slowest_name, slowest = max(
        ((name, e) for name, (e, _) in results.items()), key=lambda i: i[1])
    print(f"all {len(SCENARIOS)} integration scenarios passed "
          f"({elapsed:.1f}s, jobs={jobs})")
    print(f"coverage: {coverage} | slowest: {slowest_name} ({slowest:.1f}s)")
    print("ok")


if __name__ == "__main__":
    main()
