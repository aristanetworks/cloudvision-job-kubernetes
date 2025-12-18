# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""SR-IOV NodeConfig informer for CV Job Informer.

Watches sriovnetworknodestates in the network-operator namespace and sends
node-level interface information (PFs/VFs with MAC addresses) to the
CloudVision NodeConfig API.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

from kubernetes import client, config, watch
from api_utils import send_nodeconfig, delete_nodeconfig
from constants import (
    CV_INTERFACE_GROUP,
    CV_INTERFACE_VERSION,
    CV_INTERFACE_PLURAL,
    CV_INTERFACE_NAMESPACE,
    SRIOV_GROUP,
    SRIOV_VERSION,
    SRIOV_PLURAL,
    SRIOV_NAMESPACE,
)

logger = logging.getLogger(__name__)


def _extract_interfaces_from_state(obj: Dict,
                                   interface_type: str = "all"
                                   ) -> List[Dict[str, str]]:
    """Extract interface info (name, mac, ip) from NodeInterfaceState or SriovNetworkNodeState.

    Args:
        obj: The custom object (NodeInterfaceState or SriovNetworkNodeState).
        interface_type: "all" (PFs + VFs), "pf" (PFs only), or "vf" (VFs only).

    Returns a flat list of interfaces with keys: name, mac_address, ip_addresses (list).
    """
    status = obj.get("status", {})
    interfaces = status.get("interfaces") or []

    # Some implementations might use a single dict instead of a list
    if isinstance(interfaces, dict):
        interfaces = [interfaces]

    include_pf = interface_type in ("all", "pf")
    include_vf = interface_type in ("all", "vf")

    result: List[Dict[str, str]] = []

    for pf in interfaces:
        pf_name = pf.get("name")
        pf_mac = pf.get("mac")
        pf_ip = pf.get("ip")  # May be None

        if include_pf and pf_name and pf_mac:
            iface_info = {
                "name": pf_name,
                "mac_address": pf_mac,
            }
            # Convert single IP to list format expected by API
            if pf_ip:
                iface_info["ip_addresses"] = [pf_ip]
            else:
                iface_info["ip_addresses"] = []
            result.append(iface_info)

        # VFs are nested under "Vfs" list
        if include_vf:
            vfs = pf.get("Vfs") or []
            for vf in vfs:
                vf_name = vf.get("name")
                vf_mac = vf.get("mac")
                vf_ip = vf.get("ip")  # May be None

                if vf_name and vf_mac:
                    iface_info = {
                        "name": vf_name,
                        "mac_address": vf_mac,
                    }
                    # Convert single IP to list format expected by API
                    if vf_ip:
                        iface_info["ip_addresses"] = [vf_ip]
                    else:
                        iface_info["ip_addresses"] = []
                    result.append(iface_info)

    return result


class NodeStateInformer:
    """Informer that watches node interface states and sends NodeConfig updates.

    Supports two modes:
    - 'discovery': Watch NodeInterfaceState CRs (from cv-interface-discovery daemonset)
    - 'sriovoperator': Watch SriovNetworkNodeState CRs (from SR-IOV Network Operator)
    """

    def __init__(self,
                 api_server: str,
                 api_token: str,
                 location: str,
                 node_interface_type: str = "all",
                 nodeconfig_mode: str = "discovery"):
        self.api_server = api_server
        self.api_token = api_token
        self.location = location

        # Interface type for NodeConfig payloads ("all", "pf", or "vf")
        self.node_interface_type = node_interface_type
        self.nodeconfig_mode = nodeconfig_mode

        # Determine which CR to watch based on mode
        if nodeconfig_mode == "discovery":
            self.cr_config = {
                "group": CV_INTERFACE_GROUP,
                "version": CV_INTERFACE_VERSION,
                "plural": CV_INTERFACE_PLURAL,
                "namespace": CV_INTERFACE_NAMESPACE,
                "source": "cv-interface-discovery",
            }
        elif nodeconfig_mode == "sriovoperator":
            self.cr_config = {
                "group": SRIOV_GROUP,
                "version": SRIOV_VERSION,
                "plural": SRIOV_PLURAL,
                "namespace": SRIOV_NAMESPACE,
                "source": "sriov-operator",
            }
        else:
            raise ValueError(f"Invalid nodeconfig_mode: {nodeconfig_mode}")

        # Kubernetes client
        try:
            config.load_kube_config()
        except Exception:
            config.load_incluster_config()
        self.custom_api = client.CustomObjectsApi()

        # node_name -> list of interfaces (name, mac_address, ip_address)
        self._last_interfaces: Dict[str, List[Dict[str, str]]] = {}
        # Protect internal map when accessed from watch thread
        self._lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            logger.warning("NodeConfig informer already running")
            return

        self._running = True
        logger.info(
            "[NODE-INFORMER] Starting NodeConfig informer (mode=%s, source=%s, namespace=%s, interface_type=%s)...",
            self.nodeconfig_mode,
            self.cr_config["source"],
            self.cr_config["namespace"],
            self.node_interface_type,
        )

        # Initial full sync so CV has data even if no changes occur
        self._initial_sync()

        # Start watch loop in background
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        logger.info("[NODE-INFORMER] Stopping NodeConfig informer...")
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=10)
        logger.info("[NODE-INFORMER] NodeConfig informer stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _initial_sync(self) -> None:
        """Send NodeConfig for all existing node interface states."""
        try:
            resp = self.custom_api.list_namespaced_custom_object(
                group=self.cr_config["group"],
                version=self.cr_config["version"],
                namespace=self.cr_config["namespace"],
                plural=self.cr_config["plural"],
            )
            items = resp.get("items", [])
            logger.info(
                "[SYNC] Initial sync: found %d node states (source: %s)",
                len(items), self.cr_config["source"])
            for obj in items:
                self._process_object(obj, is_deleted=False)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.warning(
                    "[SYNC] CRD not found: %s.%s/%s in namespace '%s'. "
                    "Make sure the CRD is deployed. NodeConfig informer will retry when watching.",
                    self.cr_config["plural"],
                    self.cr_config["group"],
                    self.cr_config["version"],
                    self.cr_config["namespace"],
                )
            else:
                logger.error("Initial NodeState sync failed: %s",
                             e,
                             exc_info=True)
        except Exception as e:
            logger.error("Initial NodeState sync failed: %s", e, exc_info=True)

    def _watch_loop(self) -> None:
        w = watch.Watch()
        retry_delay = 5  # Initial retry delay in seconds
        max_retry_delay = 300  # Max retry delay (5 minutes)

        while self._running:
            try:
                stream = w.stream(
                    self.custom_api.list_namespaced_custom_object,
                    group=self.cr_config["group"],
                    version=self.cr_config["version"],
                    namespace=self.cr_config["namespace"],
                    plural=self.cr_config["plural"],
                )
                # Reset retry delay on successful connection
                retry_delay = 5

                logger.info("[WATCH] NodeConfig watch connected successfully")
                for event in stream:
                    if not self._running:
                        w.stop()
                        break
                    event_type = event.get("type")
                    obj = event.get("object") or {}
                    node_name = obj.get("metadata", {}).get("name", "unknown")
                    logger.debug("[WATCH] Received %s event for node %s",
                                 event_type, node_name)
                    is_deleted = event_type == "DELETED"
                    self._process_object(obj, is_deleted=is_deleted)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    logger.warning(
                        "[WATCH] CRD not found: %s.%s/%s in namespace '%s'. "
                        "This is expected if NodeConfig mode is 'discovery' but the CRD hasn't been deployed yet. "
                        "Retrying in %d seconds...",
                        self.cr_config["plural"],
                        self.cr_config["group"],
                        self.cr_config["version"],
                        self.cr_config["namespace"],
                        retry_delay,
                    )
                else:
                    logger.error("Error in NodeState watch loop: %s",
                                 e,
                                 exc_info=True)

                # Exponential backoff with max delay
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
            except Exception as e:
                logger.error("Error in NodeState watch loop: %s",
                             e,
                             exc_info=True)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    def _process_object(self, obj: Dict, is_deleted: bool) -> None:
        """Process a node interface state object and send NodeConfig if changed."""
        metadata = obj.get("metadata", {})
        node_name = metadata.get("name")
        if not node_name:
            return

        # Handle deletion - remove NodeConfig from CloudVision
        if is_deleted:
            with self._lock:
                self._last_interfaces.pop(node_name, None)

            logger.info(
                "[NODE-DELETE] Node state deleted for %s, removing NodeConfig from CloudVision",
                node_name)
            delete_nodeconfig(self.api_server, self.api_token, node_name)
            return

        # Extract current interfaces
        interfaces = _extract_interfaces_from_state(obj,
                                                    self.node_interface_type)

        # Check if interfaces changed
        with self._lock:
            old_interfaces = self._last_interfaces.get(node_name, [])
            # Compare as sets of (name, mac, ip_addresses) tuples for change detection
            # Convert ip_addresses list to tuple for hashability
            old_set = {(i["name"], i["mac_address"],
                        tuple(i.get("ip_addresses", [])))
                       for i in old_interfaces}
            new_set = {(i["name"], i["mac_address"],
                        tuple(i.get("ip_addresses", [])))
                       for i in interfaces}

            # No change in interface set; do nothing
            if old_set == new_set:
                return

            self._last_interfaces[node_name] = interfaces

        # Send NodeConfig for this node
        send_nodeconfig(
            self.api_server,
            self.api_token,
            node_name,
            self.location,
            interfaces,
        )
