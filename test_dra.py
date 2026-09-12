#!/usr/bin/env python3
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Assert-based checks for DRA ResourceClaim interface extraction."""

from types import SimpleNamespace

from dra import (
    claim_key,
    claim_names_from_pod,
    interfaces_for_pod,
    interfaces_from_claim,
    select_interfaces,
)

MULTUS = [{
    "interface": "net1",
    "ip": "10.0.0.9",
    "mac": "aa:aa:aa:aa:aa:aa",
}]


def _pod(namespace="default",
         spec_claims=None,
         status_claims=None,
         extended=None):
    spec = SimpleNamespace(resource_claims=[
        SimpleNamespace(resource_claim_name=name)
        for name in (spec_claims or [])
    ])
    status = SimpleNamespace(
        resource_claim_statuses=[
            SimpleNamespace(resource_claim_name=name)
            for name in (status_claims or [])
        ],
        extended_resource_claim_status=SimpleNamespace(
            resource_claim_name=extended) if extended else None,
    )
    return SimpleNamespace(metadata=SimpleNamespace(namespace=namespace,
                                                    name="worker-0"),
                           spec=spec,
                           status=status)


def _nic_claim(mac, interface="net1", ip="10.10.1.2/24"):
    return {
        "status": {
            "devices": [{
                "device": "eth0",
                "driver": "nic.example.com",
                "pool": "nic-worker-a",
                "networkData": {
                    "hardwareAddress": mac,
                    "interfaceName": interface,
                    "ips": [ip],
                },
            }]
        }
    }


def _gpu_claim():
    return {
        "status": {
            "devices": [{
                "device": "gpu0",
                "driver": "gpu.example.com",
                "pool": "gpu-worker-a",
            }]
        }
    }


def test_claim_with_network_data_wins():
    pod = _pod(spec_claims=["rdma-claim"])
    cache = {claim_key("default", "rdma-claim"): _nic_claim("00:01:ec:84:fb:51")}
    dra = interfaces_for_pod(pod, cache)
    same_mac_multus = [{
        "interface": "net1",
        "ip": "10.0.0.9",
        "mac": "00:01:ec:84:fb:51",
    }]
    chosen = select_interfaces(dra, same_mac_multus)
    assert len(chosen) == 1, chosen
    assert chosen[0]["mac"] == "00:01:ec:84:fb:51"
    assert chosen[0]["interface"] == "net1"
    assert chosen[0]["ip"] == "10.10.1.2/24"


def test_union_keeps_extra_multus_macs():
    dra = [{
        "interface": "net1",
        "ip": "10.10.1.2/24",
        "mac": "00:01:ec:84:fb:51",
    }]
    chosen = select_interfaces(dra, MULTUS)
    macs = {iface["mac"] for iface in chosen}
    assert macs == {"00:01:ec:84:fb:51", "aa:aa:aa:aa:aa:aa"}


def test_gpu_only_claim_falls_back_to_multus():
    pod = _pod(spec_claims=["gpu-claim"])
    cache = {claim_key("default", "gpu-claim"): _gpu_claim()}
    dra = interfaces_for_pod(pod, cache)
    assert dra == []
    chosen = select_interfaces(dra, MULTUS)
    assert chosen == MULTUS


def test_no_claims_uses_multus():
    pod = _pod()
    assert claim_names_from_pod(pod) == []
    dra = interfaces_for_pod(pod, {})
    chosen = select_interfaces(dra, MULTUS)
    assert chosen == MULTUS


def test_extended_resource_claim_status_resolved():
    pod = _pod(extended="ccc-gpu-57999b9c4c-vpq68-gpu-8s27z")
    assert claim_names_from_pod(pod) == ["ccc-gpu-57999b9c4c-vpq68-gpu-8s27z"]
    cache = {
        claim_key("default", "ccc-gpu-57999b9c4c-vpq68-gpu-8s27z"):
        _nic_claim("bb:bb:bb:bb:bb:bb", interface="ens20np0")
    }
    dra = interfaces_for_pod(pod, cache)
    chosen = select_interfaces(dra, MULTUS)
    assert chosen[0]["mac"] == "bb:bb:bb:bb:bb:bb"
    assert chosen[0]["interface"] == "ens20np0"


def test_template_status_and_spec_deduped():
    pod = _pod(spec_claims=["shared"], status_claims=["shared", "generated"])
    assert claim_names_from_pod(pod) == ["shared", "generated"]


def test_interfaces_from_claim_skips_devices_without_mac():
    claim = {
        "status": {
            "devices": [
                {
                    "device": "gpu0"
                },
                {
                    "device": "nic0",
                    "networkData": {
                        "hardwareAddress": "cc:cc:cc:cc:cc:cc",
                        "interfaceName": "net2",
                    },
                },
            ]
        }
    }
    ifaces = interfaces_from_claim(claim)
    assert len(ifaces) == 1
    assert ifaces[0]["mac"] == "cc:cc:cc:cc:cc:cc"


def test_reserved_for_without_pod_claim_fields():
    """Old kubernetes clients drop V1Pod DRA fields; reservedFor still maps."""
    pod = _pod()
    pod.metadata.uid = "pod-uid-1"
    claim = _nic_claim("dd:dd:dd:dd:dd:dd", interface="ens21np0")
    claim["metadata"] = {"name": "generated-claim", "namespace": "default"}
    claim["status"]["reservedFor"] = [{
        "resource": "pods",
        "name": "worker-0",
        "uid": "pod-uid-1",
    }]
    cache = {claim_key("default", "generated-claim"): claim}
    assert claim_names_from_pod(pod) == []
    dra = interfaces_for_pod(pod, cache)
    assert dra[0]["mac"] == "dd:dd:dd:dd:dd:dd"
    assert dra[0]["interface"] == "ens21np0"


if __name__ == "__main__":
    test_claim_with_network_data_wins()
    test_union_keeps_extra_multus_macs()
    test_gpu_only_claim_falls_back_to_multus()
    test_no_claims_uses_multus()
    test_extended_resource_claim_status_resolved()
    test_template_status_and_spec_deduped()
    test_interfaces_from_claim_skips_devices_without_mac()
    test_reserved_for_without_pod_claim_fields()
    print("ok")
