#!/usr/bin/env python3
# Copyright (c) 2025 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Assert-based checks for CRD plural lookup and job-status heuristics."""

from constants import SUPPORTED_JOB_RESOURCES
from discovery import api_group, kinds_from_api_resource_list
from models import DynamicResourceConfig


def test_whitelist_is_direct_owner_only():
    assert ("batch", "Job") in SUPPORTED_JOB_RESOURCES
    assert ("apps", "ReplicaSet") not in SUPPORTED_JOB_RESOURCES
    assert ("jobset.x-k8s.io", "JobSet") not in SUPPORTED_JOB_RESOURCES


def test_server_plural_overrides_english():
    body = {
        "resources": [
            {
                "name": "trainjobs",
                "kind": "TrainJob",
                "namespaced": True
            },
            {
                "name": "trainjobs/status",
                "kind": "TrainJob",
                "namespaced": True
            },
            {
                "name": "trainings",
                "kind": "TrainingWorkload",
                "namespaced": True
            },
        ]
    }
    kinds = kinds_from_api_resource_list(body)
    assert kinds["TrainJob"] == "trainjobs"
    assert kinds["TrainingWorkload"] == "trainings"
    assert DynamicResourceConfig._pluralize("TrainingWorkload") == (
        "trainingworkloads")


def test_stopped_and_preempted_count_as_failed():
    cfg = DynamicResourceConfig("example.io/v1", "ExampleJob", plural="examplejobs")
    done, ok, failed = cfg.is_completed({"status": {"phase": "Stopped"}})
    assert done and failed and not ok
    done, ok, failed = cfg.is_completed({"status": {"phase": "Preempted"}})
    assert done and failed and not ok


def test_api_group():
    assert api_group("trainer.kubeflow.org/v1") == "trainer.kubeflow.org"
    assert api_group("v1") == ""


if __name__ == "__main__":
    test_whitelist_is_direct_owner_only()
    test_server_plural_overrides_english()
    test_stopped_and_preempted_count_as_failed()
    test_api_group()
    print("ok")
