#!/usr/bin/env python3
# Copyright (c) 2026 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Assert-based checks for CRD plural lookup and job-status heuristics."""

from constants import SUPPORTED_JOB_RESOURCES
from discovery import api_group, get_json, kinds_from_api_resource_list, resolve_plural
from models import DynamicResourceConfig

DISCOVERY_BODY = b"""{
  "kind": "APIResourceList",
  "apiVersion": "v1",
  "groupVersion": "run.ai/v1",
  "resources": [
    {"name": "trainings", "singularName": "", "namespaced": true,
     "kind": "TrainingWorkload", "verbs": ["get", "list", "watch"]}
  ]
}"""


class _FakeResponse:
    """Minimal stand-in for urllib3.HTTPResponse (exposes .data)."""

    def __init__(self, body):
        self.data = body if isinstance(body, bytes) else body.encode("utf-8")


class _TupleConventionClient:
    """Old clients (<36): call_api(_preload_content=False) returns the
    (response, status, headers) tuple."""

    def __init__(self, body):
        self._response = _FakeResponse(body)

    def call_api(self, path, method, **kwargs):
        assert kwargs.get("_preload_content") is False
        return (self._response, 200, {})


class _BareResponseClient:
    """New clients (>=36): call_api(_preload_content=False) returns the bare
    urllib3 HTTPResponse."""

    def __init__(self, body):
        self._response = _FakeResponse(body)

    def call_api(self, path, method, **kwargs):
        assert kwargs.get("_preload_content") is False
        return self._response


class _ExplodingClient:
    """Any client erroring inside call_api."""

    def call_api(self, path, method, **kwargs):
        raise OSError("connection refused")


class _FailingThenOkClient:
    def __init__(self, body):
        self._fails = 1
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def call_api(self, path, method, **kwargs):
        if self._fails:
            self._fails -= 1
            raise OSError("connection refused")

        class _Resp:
            pass

        resp = _Resp()
        resp.data = self._body
        return resp


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


def test_plural_for_retries_after_failed_get():
    """A failed discovery GET must not pin the English plural forever."""
    body = b"""{
      "kind": "APIResourceList",
      "resources": [
        {"name": "trainings", "kind": "TrainingWorkload", "namespaced": true}
      ]
    }"""
    plurals = {}
    loaded = set()
    client = _FailingThenOkClient(body)
    first = resolve_plural(client, "run.ai", "v1", "TrainingWorkload",
                           plurals, loaded,
                           DynamicResourceConfig._pluralize)
    assert first == "trainingworkloads"
    assert ("run.ai", "v1") not in loaded
    assert ("run.ai", "v1", "TrainingWorkload") not in plurals
    second = resolve_plural(client, "run.ai", "v1", "TrainingWorkload",
                            plurals, loaded,
                            DynamicResourceConfig._pluralize)
    assert second == "trainings"
    assert plurals[("run.ai", "v1", "TrainingWorkload")] == "trainings"


def test_get_json_tuple_convention_old_clients():
    """kubernetes<36 wraps the response in (resp, status, headers)."""
    parsed = get_json(_TupleConventionClient(DISCOVERY_BODY), "/apis/run.ai/v1")
    assert parsed is not None
    assert parsed["groupVersion"] == "run.ai/v1"
    assert kinds_from_api_resource_list(parsed)["TrainingWorkload"] == (
        "trainings")


def test_get_json_bare_response_convention_new_clients():
    """kubernetes>=36 returns the bare HTTPResponse from call_api."""
    parsed = get_json(_BareResponseClient(DISCOVERY_BODY), "/apis/run.ai/v1")
    assert parsed is not None
    assert parsed["groupVersion"] == "run.ai/v1"
    assert kinds_from_api_resource_list(parsed)["TrainingWorkload"] == (
        "trainings")


def test_get_json_str_body_and_error_paths():
    # str bodies decode fine too.
    parsed = get_json(_BareResponseClient('{"kind": "X"}'), "/apis/x/v1")
    assert parsed == {"kind": "X"}
    # call_api raising -> None (never propagates).
    assert get_json(_ExplodingClient(), "/apis/x/v1") is None
    # Non-dict JSON -> None.
    assert get_json(_BareResponseClient(b'["list"]'), "/apis/x/v1") is None


if __name__ == "__main__":
    checks = [
        test_whitelist_is_direct_owner_only,
        test_server_plural_overrides_english,
        test_stopped_and_preempted_count_as_failed,
        test_api_group,
        test_plural_for_retries_after_failed_get,
        test_get_json_tuple_convention_old_clients,
        test_get_json_bare_response_convention_new_clients,
        test_get_json_str_body_and_error_paths,
    ]
    for check in checks:
        check()
        print(f"  {check.__name__} ok")
    print(f"{len(checks)}/{len(checks)} discovery checks passed")
