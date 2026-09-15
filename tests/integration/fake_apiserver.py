#!/usr/bin/env python3
# Copyright (c) 2026 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Cluster-free fake Kubernetes API server for cv-job-informer integration tests.

Implements (stdlib http.server only) the exact subset the informer uses:

- Discovery resource lists:   GET /apis/{group}/{version} and GET /api/{version}
- Cluster-scoped LIST:        GET /apis/{group}/{version}/{plural}
- Namespaced LIST:            GET /apis/{group}/{version}/namespaces/{ns}/{plural}
                              GET /api/v1/namespaces/{ns}/{plural}
- WATCH streams:              same LIST paths with ?watch=true ->
                              line-delimited {"type": ..., "object": {...}} JSON
                              over a chunked response, connection kept open.
- LIST responses:             {"kind": "<Kind>List", "apiVersion": "<gv>",
                               "metadata": {"resourceVersion": "1"},
                               "items": [...]}

FAIL CLOSED: any path that is not configured in the active profile returns 404
(never an empty list).  Which discovery docs exist and which paths 404 IS the
environment switch (e.g. which resource.k8s.io versions are served).

Environment profile: a dict of group -> version -> plural -> Resource.
Each Resource configures its kind, seeded items, and protocol-behavior knobs
(410 on watch, delayed list, close watch after N events) used by the
integration scenarios.

Watch events are broadcast per resource to every open watcher.  Events pushed
while no watcher is attached are buffered and delivered on the next watch
connect, so tests can push events without racing informer startup.
"""

import copy
import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

RESOURCE_VERSION = "1"


class Resource:
    """One servable resource (plural) inside a group/version discovery doc.

    Args:
        kind: Kubernetes kind (used for the "<Kind>List" wrapper).
        items: Seeded objects (deep-copied on server start).
        namespaced: Whether objects are namespaced (affects list filtering).
        watch_status: If set (e.g. 410), the NEXT watch request returns this
            HTTP status instead of streaming (one-shot; consumed on use).
        list_delay: Seconds to sleep before answering a LIST (slow-sync tests).
        close_watch_after: If set, a watch stream closes after delivering this
            many events (reconnect tests).
        hide_from_discovery: If True, list/watch still work but the resource
            is omitted from the discovery document (English-pluralize
            fallback tests).
    """

    def __init__(self,
                 kind,
                 items=None,
                 namespaced=True,
                 watch_status=None,
                 list_delay=0.0,
                 close_watch_after=None,
                 hide_from_discovery=False):
        self.kind = kind
        self.items = items or []
        self.namespaced = namespaced
        self.watch_status = watch_status
        self.list_delay = list_delay
        self.close_watch_after = close_watch_after
        self.hide_from_discovery = hide_from_discovery


def empty_profile():
    """Profile with the core v1 pods resource and no groups."""
    return {"core": {"v1": {"pods": Resource("Pod")}}, "groups": {}}


def add_group(profile, group, version, resources):
    """Register {plural: Resource} entries under a group/version."""
    profile["groups"].setdefault(group, {}).setdefault(version,
                                                      {}).update(resources)
    return profile


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route through logging, keep quiet
        logger.debug("[fake-apiserver] %s %s", self.address_string(), fmt
                     % args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self, path, why="not configured"):
        logger.error("[fake-apiserver] FAIL-CLOSED 404 %s (%s)", path, why)
        self._send_json(
            404, {
                "kind": "Status",
                "apiVersion": "v1",
                "metadata": {},
                "status": "Failure",
                "message": f"the server could not find the requested resource: {path} ({why})",
                "reason": "NotFound",
                "code": 404,
            })

    def _send_status(self, status, reason, message):
        self._send_json(status, {
            "kind": "Status",
            "apiVersion": "v1",
            "metadata": {},
            "status": "Failure",
            "message": message,
            "reason": reason,
            "code": status,
        })

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        server = self.server  # FakeApiServer instance

        if query.get("watch", ["false"])[0].lower() == "true":
            self._handle_watch(server, path)
            return
        self._handle_list(server, path)

    def do_POST(self):  # noqa: N802
        # Nothing in cv-job-informer writes to the API server: fail closed.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.record(self.path, "POST", 404)
        self._not_found(self.path, "POST not supported by fake apiserver")

    def _resolve(self, path):
        """Parse an API path into (group, version, plural, namespace|None).

        Returns (None, ...) entries when the path shape is recognized but the
        resource is not configured, and raises ValueError for unrecognized
        shapes (both end up as fail-closed 404s at the caller).
        """
        parts = [p for p in path.split("/") if p]
        namespace = None
        if parts and parts[0] == "api":
            # /api/{version}[/{plural}] or /api/{version}/namespaces/{ns}/{plural}
            rest = parts[1:]
            if not rest:
                raise ValueError("core discovery root not served")
            if len(rest) == 1:
                return ("core", rest[0], None, None)
            if len(rest) == 2:
                return ("core", rest[0], rest[1], None)
            if len(rest) >= 3 and rest[1] == "namespaces":
                namespace = rest[2]
                plural = rest[3] if len(rest) > 3 else None
                return ("core", rest[0], plural, namespace)
            raise ValueError(f"unrecognized core path {path}")
        if parts and parts[0] == "apis":
            # /apis/{group}/{version}[/{plural}] or
            # /apis/{group}/{version}/namespaces/{ns}/{plural}
            rest = parts[1:]
            if not rest:
                raise ValueError("group discovery root not served")
            if len(rest) == 1:
                raise ValueError(f"group root not served: {path}")
            if len(rest) == 2:
                return (rest[0], rest[1], None, None)
            if len(rest) == 3:
                return (rest[0], rest[1], rest[2], None)
            if len(rest) >= 4 and rest[2] == "namespaces":
                namespace = rest[3]
                plural = rest[4] if len(rest) > 4 else None
                return (rest[0], rest[1], plural, namespace)
            raise ValueError(f"unrecognized group path {path}")
        raise ValueError(f"unrecognized path {path}")

    def _resource_for(self, server, group, version, plural):
        if group == "core":
            table = server.profile.get("core", {})
        else:
            table = server.profile.get("groups", {}).get(group, {})
        versions = table.get(version)
        if versions is None:
            return None
        if plural is None:
            return None  # caller handles discovery-doc case
        return versions.get(plural)

    def _handle_list(self, server, path):
        try:
            group, version, plural, namespace = self._resolve(path)
        except ValueError as exc:
            server.record(path, "GET", 404)
            self._not_found(path, str(exc))
            return

        if plural is None:
            # Discovery document request.
            if group == "core":
                versions = server.profile.get("core", {})
            else:
                versions = server.profile.get("groups", {}).get(group, {})
            resources = versions.get(version)
            if resources is None:
                server.record(path, "GET", 404)
                self._not_found(path, "group/version not configured")
                return
            gv = version if group == "core" else f"{group}/{version}"
            body = {
                "kind": "APIResourceList",
                "apiVersion": "v1",
                "groupVersion": gv,
                "resources": [
                    {
                        "name": plural_name,
                        "singularName": "",
                        "namespaced": res.namespaced,
                        "kind": res.kind,
                        "verbs": ["get", "list", "watch"],
                    } for plural_name, res in sorted(resources.items())
                    if not res.hide_from_discovery
                ],
            }
            server.record(path, "GET", 200)
            self._send_json(200, body)
            return

        resource = self._resource_for(server, group, version, plural)
        if resource is None:
            server.record(path, "GET", 404)
            self._not_found(path, "resource not configured")
            return

        if resource.list_delay:
            time.sleep(resource.list_delay)

        items = resource.items
        if namespace is not None:
            items = [
                item for item in items
                if (item.get("metadata") or {}).get("namespace") == namespace
            ]
        body = {
            "kind": f"{resource.kind}List",
            "apiVersion": version if group == "core" else f"{group}/{version}",
            "metadata": {
                "resourceVersion": RESOURCE_VERSION
            },
            "items": copy.deepcopy(items),
        }
        server.record(path, "GET", 200)
        self._send_json(200, body)

    def _handle_watch(self, server, path):
        try:
            group, version, plural, namespace = self._resolve(path)
        except ValueError as exc:
            server.record(path, "GET", 404)
            self._not_found(path, str(exc))
            return

        if plural is None:
            server.record(path, "GET", 404)
            self._not_found(path, "cannot watch a discovery document")
            return

        resource = self._resource_for(server, group, version, plural)
        if resource is None:
            server.record(path, "GET", 404)
            self._not_found(path, "resource not configured")
            return

        # One-shot protocol switch: e.g. first watch on a resource 410s.
        with server.lock:
            status = resource.watch_status
            if status is not None:
                resource.watch_status = None
        if status is not None:
            server.record(path, "GET", status)
            self._send_status(status, "Expired",
                              "too old resource version (fake 410)")
            return

        key = (group, version, plural)
        events_in = queue.Queue()
        with server.lock:
            pending = server.pending.pop(key, [])
            server.watchers.setdefault(key, []).append(events_in)
        server.record(path, "GET", 200)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        delivered = 0
        close_after = resource.close_watch_after

        def deliver(event):
            line = json.dumps(event,
                              separators=(",", ":")).encode("utf-8") + b"\n"
            self.wfile.write(f"{len(line):X}\r\n".encode("ascii"))
            self.wfile.write(line)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        closed = False
        try:
            for event in pending:
                deliver(event)
                delivered += 1
                if close_after is not None and delivered >= close_after:
                    closed = True
                    break
            while not closed and server.running:
                try:
                    event = events_in.get(timeout=0.25)
                except queue.Empty:
                    continue
                deliver(event)
                delivered += 1
                if close_after is not None and delivered >= close_after:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("[fake-apiserver] watch client went away: %s", path)
        finally:
            with server.lock:
                watchers = server.watchers.get(key, [])
                if events_in in watchers:
                    watchers.remove(events_in)
                # Keep undelivered live events for the next watcher.
                leftovers = []
                while True:
                    try:
                        leftovers.append(events_in.get_nowait())
                    except queue.Empty:
                        break
                server.pending.setdefault(key, []).extend(leftovers)
        try:
            # Terminate the chunked body so urllib3 sees a clean end when the
            # server (not the client) ends the stream.
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            self.close_connection = True
        except OSError:
            pass


class FakeApiServer(ThreadingHTTPServer):
    """Runnable fake kube-apiserver.  Start with start(); stop with stop()."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Clients routinely drop pooled keep-alive connections mid-read;
        # that is normal lifecycle noise, not a server bug.
        logger.debug("[fake-apiserver] connection error from %s",
                     client_address)

    def __init__(self, profile):
        self.profile = profile
        self.lock = threading.Lock()
        self.watchers = {}  # (group, version, plural) -> [Queue, ...]
        self.pending = {}  # (group, version, plural) -> [event, ...]
        self.requests = []  # [{path, method, status}]
        self.running = True
        super().__init__(("127.0.0.1", 0), _Handler)

    # ------------------------------------------------------------------
    def record(self, path, method, status):
        with self.lock:
            self.requests.append({
                "path": path,
                "method": method,
                "status": status,
                "ts": time.time(),
            })

    def requests_for(self, substring, status=None):
        with self.lock:
            reqs = list(self.requests)
        return [
            r for r in reqs
            if substring in r["path"] and (status is None
                                           or r["status"] == status)
        ]

    # ------------------------------------------------------------------
    def push(self, group, version, plural, event_type, obj):
        """Broadcast a watch event to open watchers (or buffer it)."""
        event = {"type": event_type, "object": obj}
        key = (group, version, plural)
        with self.lock:
            watchers = list(self.watchers.get(key, []))
            if not watchers:
                self.pending.setdefault(key, []).append(event)
        for events_in in watchers:
            events_in.put(event)

    # Convenience wrappers -------------------------------------------------
    def push_core(self, plural, event_type, obj):
        self.push("core", "v1", plural, event_type, obj)

    def push_group(self, group, version, plural, event_type, obj):
        self.push(group, version, plural, event_type, obj)

    # ------------------------------------------------------------------
    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        self.thread = threading.Thread(target=self.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        logger.info("[fake-apiserver] serving on 127.0.0.1:%s", self.port)

    def stop(self):
        self.running = False
        super().shutdown()
        super().server_close()
        logger.info("[fake-apiserver] stopped")

    # ------------------------------------------------------------------
    # Test helpers over the request log
    # ------------------------------------------------------------------
    def has_request(self, substring, status=None):
        return bool(self.requests_for(substring, status=status))
