# Copyright (c) 2026 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""DRA ResourceClaim helpers and informer for CV Job Informer.

Claim-name / networkData extraction is dict-based (no generated ResourceClaim
models). ResourceClaimInformer lazily imports the Kubernetes client so unit
tests can import this module without kubernetes installed.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from constants import DRA_CLAIM_PLURAL, DRA_CLAIM_VERSIONS, DRA_GROUP

logger = logging.getLogger(__name__)


def claim_key(namespace: str, name: str) -> str:
    """Cache key for a namespaced ResourceClaim."""
    return f"{namespace}/{name}"


def _get(obj: Any, *keys: str) -> Any:
    """Read an attribute or dict key, trying each name in order."""
    if obj is None:
        return None
    for key in keys:
        if isinstance(obj, dict):
            if key in obj and obj[key] is not None:
                return obj[key]
        elif hasattr(obj, key):
            value = getattr(obj, key)
            if value is not None:
                return value
    return None


def claim_names_from_pod(pod: Any) -> List[str]:
    """ResourceClaim names referenced by a pod in its own namespace.

    Collects:
    - spec.resourceClaims[].resourceClaimName (existing claims)
    - status.resourceClaimStatuses[].resourceClaimName (templates)
    - status.extendedResourceClaimStatus.resourceClaimName (DRA extended resource)
    """
    names: List[str] = []

    spec = _get(pod, "spec")
    for claim_ref in _get(spec, "resource_claims", "resourceClaims") or []:
        name = _get(claim_ref, "resource_claim_name", "resourceClaimName")
        if name:
            names.append(name)

    status = _get(pod, "status")
    for claim_status in _get(status, "resource_claim_statuses",
                             "resourceClaimStatuses") or []:
        name = _get(claim_status, "resource_claim_name", "resourceClaimName")
        if name:
            names.append(name)

    extended = _get(status, "extended_resource_claim_status",
                    "extendedResourceClaimStatus")
    name = _get(extended, "resource_claim_name", "resourceClaimName")
    if name:
        names.append(name)

    seen: Set[str] = set()
    unique: List[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def claim_reserved_for_pod(claim: Dict,
                           pod_name: str,
                           pod_uid: str = "") -> bool:
    """True if claim.status.reservedFor points at this pod.

    Lets older kubernetes clients (which drop unknown V1Pod DRA fields) still
    map generated / extended-resource claims to pods.
    """
    if not isinstance(claim, dict) or not pod_name:
        return False
    reserved = _get(claim.get("status"), "reservedFor", "reserved_for") or []
    for ref in reserved:
        resource = (_get(ref, "resource") or "pods")
        if not isinstance(resource, str) or resource.lower() not in ("pods", "pod"):
            continue
        uid = _get(ref, "uid")
        if pod_uid and uid:
            if uid == pod_uid:
                return True
            continue
        if _get(ref, "name") == pod_name:
            return True
    return False


def claims_for_pod(pod: Any, claim_cache: Dict[str, Dict]) -> List[Dict]:
    """Cached claims named by the pod spec/status or reserved for the pod."""
    metadata = _get(pod, "metadata")
    namespace = _get(metadata, "namespace") or ""
    pod_name = _get(metadata, "name") or ""
    pod_uid = _get(metadata, "uid") or ""

    seen: Set[str] = set()
    claims: List[Dict] = []

    def add(name: str, claim: Optional[Dict]) -> None:
        if not name or name in seen or not isinstance(claim, dict):
            return
        seen.add(name)
        claims.append(claim)

    for name in claim_names_from_pod(pod):
        add(name, claim_cache.get(claim_key(namespace, name)))

    prefix = f"{namespace}/"
    for key, claim in claim_cache.items():
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):]
        if claim_reserved_for_pod(claim, pod_name, pod_uid):
            add(name, claim)

    return claims


def pod_uses_claim(pod: Any, namespace: str, name: str, claim: Dict) -> bool:
    """True if this pod names or is reserved for the given claim."""
    metadata = _get(pod, "metadata")
    if (_get(metadata, "namespace") or "") != namespace:
        return False
    if name in claim_names_from_pod(pod):
        return True
    return claim_reserved_for_pod(claim, _get(metadata, "name") or "",
                                  _get(metadata, "uid") or "")


def interfaces_from_claim(claim: Dict) -> List[Dict[str, Optional[str]]]:
    """MAC/IP/iface rows from ResourceClaim.status.devices[].networkData.

    Devices without networkData (GPUs, accelerators) are skipped.
    """
    if not isinstance(claim, dict):
        return []

    devices = _get(claim.get("status"), "devices") or []
    interfaces: List[Dict[str, Optional[str]]] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        network = _get(device, "networkData", "network_data") or {}
        if not isinstance(network, dict):
            continue
        mac = _get(network, "hardwareAddress", "hardware_address")
        if not mac:
            continue
        ips = _get(network, "ips") or []
        ip = ips[0] if ips else None
        interface_name = (_get(network, "interfaceName", "interface_name")
                          or _get(device, "device") or "")
        interfaces.append({
            "interface": interface_name,
            "ip": ip,
            "mac": mac,
        })
    return interfaces


def interfaces_for_pod(
        pod: Any,
        claim_cache: Dict[str, Dict]) -> List[Dict[str, Optional[str]]]:
    """DRA network interfaces for a pod from cached ResourceClaims."""
    interfaces: List[Dict[str, Optional[str]]] = []
    for claim in claims_for_pod(pod, claim_cache):
        interfaces.extend(interfaces_from_claim(claim))
    return interfaces


def select_interfaces(
        dra_ifaces: List[Dict[str, Optional[str]]],
        multus_ifaces: List[Dict[str, Optional[str]]]
) -> List[Dict[str, Optional[str]]]:
    """Union DRA and Multus MACs. DRA wins on MAC collision; Multus is kept.

    Multus-only clusters therefore behave as before. Mixed DRA+Multus pods
    keep secondary NICs that only appear in network-status.
    """
    by_mac: Dict[str, Dict[str, Optional[str]]] = {}
    for iface in list(dra_ifaces) + list(multus_ifaces):
        mac = iface.get("mac")
        if not mac:
            continue
        key = mac.lower()
        if key not in by_mac:
            by_mac[key] = iface
    return list(by_mac.values())


class ResourceClaimInformer:
    """Watch ResourceClaims (v1, then beta/alpha) cluster- or namespace-wide.

    On 404 for every known version (no DRA API), logs once and disables.
    """

    def __init__(self,
                 custom_api,
                 namespace: str,
                 on_change: Callable[..., None],
                 filter_namespaces: Optional[set] = None):
        self.custom_api = custom_api
        self.namespace = namespace
        self.on_change = on_change
        self.filter_namespaces = filter_namespaces or set()
        self.running = False
        self.watch_thread: Optional[threading.Thread] = None
        self.initial_sync_done = False
        self.disabled = False
        self.api_version: Optional[str] = None

    def _should_process(self, obj: Dict) -> bool:
        if not self.filter_namespaces:
            return True
        ns = (obj.get("metadata") or {}).get("namespace", "")
        return ns in self.filter_namespaces

    def start(self) -> None:
        if self.running:
            logger.warning("ResourceClaim informer already running")
            return
        self.running = True
        self.watch_thread = threading.Thread(target=self._watch_loop,
                                             daemon=True)
        self.watch_thread.start()
        if self.namespace == "":
            logger.info(
                "[CLAIM-INFORMER] ResourceClaim informer started (all namespaces)"
            )
        else:
            logger.info(
                "[CLAIM-INFORMER] ResourceClaim informer started (namespace: %s)",
                self.namespace)

    def stop(self) -> None:
        self.running = False
        if self.watch_thread:
            self.watch_thread.join(timeout=10)
        logger.info("[CLAIM-INFORMER] ResourceClaim informer stopped")

    def _list_claims(self) -> Dict:
        version = self.api_version
        if self.namespace == "":
            return self.custom_api.list_cluster_custom_object(
                group=DRA_GROUP,
                version=version,
                plural=DRA_CLAIM_PLURAL)
        return self.custom_api.list_namespaced_custom_object(
            group=DRA_GROUP,
            version=version,
            namespace=self.namespace,
            plural=DRA_CLAIM_PLURAL)

    def _watch_stream(self, watch_cls):
        w = watch_cls()
        version = self.api_version
        if self.namespace == "":
            return w.stream(self.custom_api.list_cluster_custom_object,
                            group=DRA_GROUP,
                            version=version,
                            plural=DRA_CLAIM_PLURAL,
                            timeout_seconds=0)
        return w.stream(self.custom_api.list_namespaced_custom_object,
                        group=DRA_GROUP,
                        version=version,
                        namespace=self.namespace,
                        plural=DRA_CLAIM_PLURAL,
                        timeout_seconds=0)

    def _disable(self, reason: str) -> None:
        logger.warning(
            "[CLAIM-INFORMER] %s; DRA ResourceClaim watch disabled "
            "(Multus fallback still used)", reason)
        self.disabled = True
        self.initial_sync_done = True
        self.running = False

    def _emit(self,
              obj: Dict,
              deleted: bool,
              is_initial_sync: bool = False) -> None:
        metadata = obj.get("metadata") or {}
        name = metadata.get("name")
        namespace = metadata.get("namespace", "")
        if not name:
            return
        self.on_change(namespace,
                       name,
                       obj,
                       deleted,
                       is_initial_sync=is_initial_sync)

    def _sync_cache(self, is_initial_sync: bool = True) -> None:
        from kubernetes import client

        try:
            logger.info(
                "[SYNC] ResourceClaim informer: %s...",
                "Starting initial sync" if is_initial_sync else "Resyncing")
            resp = None
            if not self.api_version:
                tried = []
                for version in DRA_CLAIM_VERSIONS:
                    tried.append(version)
                    self.api_version = version
                    try:
                        resp = self._list_claims()
                        logger.info(
                            "[CLAIM-INFORMER] Using %s/%s", DRA_GROUP, version)
                        break
                    except client.exceptions.ApiException as e:
                        self.api_version = None
                        if e.status == 404:
                            continue
                        raise
                else:
                    self._disable(
                        f"{DRA_CLAIM_PLURAL}.{DRA_GROUP} not found "
                        f"(tried {', '.join(tried)})")
                    return
            else:
                resp = self._list_claims()

            items = resp.get("items", [])
            processed = 0
            for obj in items:
                if self._should_process(obj):
                    self._emit(obj,
                               deleted=False,
                               is_initial_sync=is_initial_sync)
                    processed += 1
            self.initial_sync_done = True
            logger.info(
                "[SYNC] ResourceClaim informer: Initial sync complete (%d claims, %s/%s)",
                processed, DRA_GROUP, self.api_version)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                self.api_version = None
                self._disable(
                    f"{DRA_CLAIM_PLURAL}.{DRA_GROUP} not found")
            else:
                logger.error("Initial ResourceClaim sync failed: %s",
                             e,
                             exc_info=True)
                self.initial_sync_done = True
        except Exception as e:
            logger.error("Initial ResourceClaim sync failed: %s",
                         e,
                         exc_info=True)
            self.initial_sync_done = True

    def _watch_loop(self) -> None:
        from kubernetes import client, watch

        self._sync_cache()
        if self.disabled:
            return

        while self.running:
            try:
                stream = self._watch_stream(watch.Watch)
                for event in stream:
                    if not self.running:
                        break
                    obj = event.get("object") or {}
                    if not self._should_process(obj):
                        continue
                    deleted = event.get("type") == "DELETED"
                    self._emit(obj, deleted=deleted)
                if self.running:
                    logger.debug(
                        "ResourceClaim watch stream ended, reconnecting...")
                    time.sleep(1)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    self.api_version = None
                    self._disable(
                        f"{DRA_CLAIM_PLURAL}.{DRA_GROUP} not found")
                    return
                if e.status == 410:
                    logger.warning(
                        "ResourceClaim resource version expired (410), resyncing..."
                    )
                    self.initial_sync_done = False
                    self._sync_cache(is_initial_sync=False)
                    time.sleep(1)
                else:
                    logger.error(
                        "API error in ResourceClaim watch: %s. Reconnecting in 5s...",
                        e)
                    time.sleep(5)
            except Exception as e:
                if self.running:
                    logger.error(
                        "ResourceClaim informer error: %s, reconnecting in 5s...",
                        e)
                    time.sleep(5)
                else:
                    break
