# Copyright (c) 2026 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Kubernetes API discovery helpers for CRD plurals.

English pluralization of Kind is wrong for many CRDs. Read the server's
resource list instead.
"""

import json
import logging
from typing import Callable, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def api_group(api_version: str) -> str:
    """'batch/v1' -> 'batch'; core 'v1' -> ''."""
    if not api_version:
        return ""
    if "/" in api_version:
        return api_version.split("/")[0]
    return ""


def api_version_suffix(api_version: str) -> str:
    if not api_version:
        return ""
    if "/" in api_version:
        return api_version.rsplit("/", 1)[-1]
    return api_version


def kinds_from_api_resource_list(body: Dict) -> Dict[str, str]:
    """kind -> plural from a GET /apis/{group}/{version} body."""
    mapping: Dict[str, str] = {}
    for resource in body.get("resources") or []:
        name = resource.get("name") or ""
        kind = resource.get("kind") or ""
        if not kind or not name or "/" in name:
            continue
        mapping[kind] = name
    return mapping


def get_json(api_client, path: str) -> Optional[Dict]:
    """GET a Kubernetes discovery document. None on failure.

    Tolerates both kubernetes-client return conventions for
    call_api(..., _preload_content=False): clients < 36 return the
    (response, status, headers) tuple, clients >= 36 return the bare
    urllib3 HTTPResponse.  Both carry the body on response.data.
    """
    try:
        resp = api_client.call_api(
            path,
            "GET",
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
        response = resp[0] if isinstance(resp, tuple) else resp
        raw = response.data
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.info("[DISCOVERY] GET %s failed: %s", path, exc)
        return None


def resolve_plural(api_client,
                   group: str,
                   version: str,
                   kind: str,
                   plurals: Dict[Tuple[str, str, str], str],
                   loaded: Set[Tuple[str, str]],
                   english_plural: Callable[[str], str]) -> str:
    """Kind -> CRD plural from the API resource list, else English.

    A failed GET is not cached: the next lookup retries. English is stored
    only after a successful list that omits this kind.
    """
    key = (group, version, kind)
    if key in plurals:
        return plurals[key]
    gv = (group, version)
    if gv not in loaded:
        path = f"/apis/{group}/{version}" if group else f"/api/{version}"
        body = get_json(api_client, path)
        if not body:
            fallback = english_plural(kind)
            logger.warning(
                "[DISCOVERY] GET failed for %s %s %s, using %s (not cached)",
                group or "core", version, kind, fallback)
            return fallback
        for res_kind, plural in kinds_from_api_resource_list(body).items():
            plurals[(group, version, res_kind)] = plural
        loaded.add(gv)
    if key in plurals:
        return plurals[key]
    fallback = english_plural(kind)
    logger.warning("[DISCOVERY] No API plural for %s %s %s, using %s",
                   group or "core", version, kind, fallback)
    plurals[key] = fallback
    return fallback
