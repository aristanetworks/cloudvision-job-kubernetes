#!/usr/bin/env python3
# Copyright (c) 2026 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
"""Fake CloudVision JobConfig/NodeConfig API stub (HTTPS, self-signed).

api_utils.py hardcodes "https://{server}" and verify=False, so the stub serves
TLS with the embedded self-signed certificate below.

Behavior:
- POST /api/resources/computejob/v1/JobConfig  -> validated, recorded, 200
- POST /api/resources/computejob/v1/NodeConfig -> validated, recorded, 200
- DELETE /api/resources/computejob/v1/NodeConfig?key.id=<node> -> recorded, 200
- Any invalid payload -> 400 with a diagnostic body (regressions fail loudly).
- Any other path -> 404 (fail closed).

Validation contract (grounded in api_utils.py payload building):
- key.id: non-empty string
- name, location: non-empty strings
- state: one of the four JOB_STATE_* values
- start_time: ISO-8601-ish timestamp
- exactly one of interfaces/nodes (XOR by JOBCONFIG_MODE)
- interfaces.values: non-empty list of MAC-address strings
- nodes.values: non-empty list of node-name strings
- end_time: required for terminal states, forbidden for RUNNING
- NodeConfig: hostname == key.id, data_interfaces.values[{name, mac_address,
  ip_addresses.values[str]}] with well-formed MACs.
"""

import json
import logging
import re
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

JOBCONFIG_PATH = "/api/resources/computejob/v1/JobConfig"
NODECONFIG_PATH = "/api/resources/computejob/v1/NodeConfig"

JOB_STATES = {
    "JOB_STATE_RUNNING",
    "JOB_STATE_COMPLETED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
}
TERMINAL_STATES = {
    "JOB_STATE_COMPLETED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
}
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")

def _generate_identity(certfile: str, keyfile: str) -> None:
    """Generate a throwaway self-signed leaf (CN=localhost) for this run.

    The production client (api_utils) connects with verify=False, so the stub
    only needs A certificate, not a trusted one.  Generating per run keeps
    private-key material out of version control entirely (embedded PEM keys
    trip secret scanners and security audits even in tests) and needs no
    openssl binary.
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256()))
    with open(certfile, "w", encoding="utf-8") as handle:
        handle.write(
            cert.public_bytes(serialization.Encoding.PEM).decode("ascii"))
    with open(keyfile, "w", encoding="utf-8") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()).decode("ascii"))


def _require(cond, errors, message):
    if not cond:
        errors.append(message)


def _nonempty_str(value):
    return isinstance(value, str) and value != ""


def _validate_jobconfig(payload):
    errors = []
    _require(isinstance(payload, dict), errors, "payload must be a JSON object")
    if not isinstance(payload, dict):
        return errors
    allowed = {
        "key", "name", "state", "start_time", "location", "interfaces",
        "nodes", "end_time", "type"
    }
    unknown = set(payload) - allowed
    _require(not unknown, errors,
             f"unknown JobConfig fields: {sorted(unknown)}")

    key = payload.get("key")
    _require(isinstance(key, dict) and _nonempty_str(key.get("id")), errors,
             "key.id must be a non-empty string")
    _require(_nonempty_str(payload.get("name")), errors,
             "name must be a non-empty string")
    _require(payload.get("state") in JOB_STATES, errors,
             f"state must be one of {sorted(JOB_STATES)}, "
             f"got {payload.get('state')!r}")
    _require(_nonempty_str(payload.get("start_time"))
             and ISO_RE.match(payload.get("start_time") or ""), errors,
             f"start_time must be an ISO-8601 timestamp, "
             f"got {payload.get('start_time')!r}")
    _require(_nonempty_str(payload.get("location")), errors,
             "location must be a non-empty string")

    has_interfaces = "interfaces" in payload
    has_nodes = "nodes" in payload
    _require(has_interfaces != has_nodes, errors,
             "exactly one of interfaces/nodes must be present "
             f"(interfaces={has_interfaces}, nodes={has_nodes})")
    if has_interfaces:
        values = (payload.get("interfaces") or {}).get("values")
        _require(isinstance(values, list) and len(values) > 0, errors,
                 "interfaces.values must be a non-empty list")
        if isinstance(values, list):
            for mac in values:
                _require(isinstance(mac, str) and MAC_RE.match(mac), errors,
                         f"interfaces.values entries must be MACs, got {mac!r}")
    if has_nodes:
        values = (payload.get("nodes") or {}).get("values")
        _require(isinstance(values, list) and len(values) > 0, errors,
                 "nodes.values must be a non-empty list")
        if isinstance(values, list):
            for node in values:
                _require(_nonempty_str(node), errors,
                         f"nodes.values entries must be strings, got {node!r}")

    if payload.get("state") in TERMINAL_STATES:
        end_time = payload.get("end_time")
        _require(_nonempty_str(end_time) and ISO_RE.match(end_time or ""),
                 errors,
                 f"end_time is required for terminal state "
                 f"{payload.get('state')}, got {end_time!r}")
    else:
        _require("end_time" not in payload, errors,
                 "end_time must be absent for non-terminal state "
                 f"{payload.get('state')}")

    if "type" in payload:
        _require(payload.get("type") == "JOB_TYPE_TENANT", errors,
                 f"type must be JOB_TYPE_TENANT, got {payload.get('type')!r}")
    return errors


def _validate_nodeconfig(payload):
    errors = []
    _require(isinstance(payload, dict), errors, "payload must be a JSON object")
    if not isinstance(payload, dict):
        return errors
    allowed = {"key", "location", "hostname", "data_interfaces"}
    unknown = set(payload) - allowed
    _require(not unknown, errors,
             f"unknown NodeConfig fields: {sorted(unknown)}")

    key = payload.get("key")
    _require(isinstance(key, dict) and _nonempty_str(key.get("id")), errors,
             "key.id must be a non-empty string")
    _require(_nonempty_str(payload.get("location")), errors,
             "location must be a non-empty string")
    _require(_nonempty_str(payload.get("hostname")), errors,
             "hostname must be a non-empty string")
    if isinstance(key, dict) and _nonempty_str(key.get("id")):
        _require(payload.get("hostname") == key.get("id"), errors,
                 "hostname must equal key.id "
                 f"({payload.get('hostname')!r} != {key.get('id')!r})")

    data = payload.get("data_interfaces")
    _require(isinstance(data, dict), errors,
             "data_interfaces must be an object")
    if isinstance(data, dict):
        values = data.get("values")
        _require(isinstance(values, list) and len(values) > 0, errors,
                 "data_interfaces.values must be a non-empty list")
        if isinstance(values, list):
            for iface in values:
                _require(isinstance(iface, dict), errors,
                         "each data_interface must be an object")
                if not isinstance(iface, dict):
                    continue
                _require(_nonempty_str(iface.get("name")), errors,
                         f"interface name must be non-empty, got {iface.get('name')!r}")
                _require(
                    isinstance(iface.get("mac_address"), str)
                    and MAC_RE.match(iface.get("mac_address") or ""), errors,
                    f"interface mac_address must be a MAC, "
                    f"got {iface.get('mac_address')!r}")
                ips = iface.get("ip_addresses")
                _require(isinstance(ips, dict), errors,
                         "interface ip_addresses must be an object")
                if isinstance(ips, dict):
                    ip_values = ips.get("values")
                    _require(isinstance(ip_values, list), errors,
                             "ip_addresses.values must be a list")
                    if isinstance(ip_values, list):
                        for ip in ip_values:
                            _require(_nonempty_str(ip), errors,
                                     f"ip_addresses.values entries must be "
                                     f"strings, got {ip!r}")
    return errors


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.debug("[cv-stub] %s %s", self.address_string(), fmt % args)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # malformed JSON
            return {"__malformed__": f"{exc.__class__.__name__}: {exc}"}

    def _reply(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        server = self.server
        path = urlparse(self.path).path
        payload = self._read_body()
        if path == JOBCONFIG_PATH:
            errors = _validate_jobconfig(payload)
            kind = "JobConfig"
        elif path == NODECONFIG_PATH:
            errors = _validate_nodeconfig(payload)
            kind = "NodeConfig"
        else:
            server.record({"type": "UnknownPath", "path": path})
            self._reply(404, {"errors": [f"unknown path {path}"]})
            return

        if errors:
            logger.error("[cv-stub] rejected %s payload: %s (%s)", kind,
                         payload, errors)
            server.reject(kind, payload, errors)
            self._reply(400, {"errors": errors})
            return

        server.record({"type": kind, "payload": payload, "ts": time.time()})
        self._reply(200, {"key": payload.get("key"), "revision": "1"})

    def do_DELETE(self):  # noqa: N802
        server = self.server
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path != NODECONFIG_PATH:
            server.record({"type": "UnknownPath", "path": path})
            self._reply(404, {"errors": [f"unknown path {path}"]})
            return
        node = (query.get("key.id") or [""])[0]
        if not node:
            server.reject("NodeConfigDelete", None,
                          ["DELETE NodeConfig requires key.id"])
            self._reply(400, {"errors": ["key.id query param required"]})
            return
        server.record({"type": "NodeConfigDelete", "node": node,
                       "ts": time.time()})
        self._reply(200, {})

    def do_GET(self):  # noqa: N802
        self.server.record({
            "type": "UnknownPath",
            "path": urlparse(self.path).path
        })
        self._reply(404, {"errors": ["GET not supported by CV stub"]})


class CvStub(ThreadingHTTPServer):
    """Runnable fake CloudVision.  Records and strictly validates payloads."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Clients dropping pooled keep-alive connections is normal noise.
        logger.debug("[cv-stub] connection error from %s", client_address)

    def __init__(self):
        self._lock = threading.Lock()
        self._recorded = []
        self._rejected = []
        super().__init__(("127.0.0.1", 0), _Handler)
        # Wrap the listening socket in TLS with a per-run self-signed cert.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # load_cert_chain needs file paths; generate once per server run into
        # a tempdir kept alive for the server's lifetime.
        self._certdir = tempfile.mkdtemp(prefix="cvjob-cvstub-")
        certfile = f"{self._certdir}/cert.pem"
        keyfile = f"{self._certdir}/key.pem"
        _generate_identity(certfile, keyfile)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        self.socket = context.wrap_socket(self.socket, server_side=True)

    # ------------------------------------------------------------------
    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        self.thread = threading.Thread(target=self.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        logger.info("[cv-stub] serving TLS on 127.0.0.1:%s", self.port)

    def stop(self):
        super().shutdown()
        super().server_close()
        logger.info("[cv-stub] stopped")

    # ------------------------------------------------------------------
    def record(self, entry):
        with self._lock:
            self._recorded.append(entry)

    def reject(self, kind, payload, errors):
        with self._lock:
            self._rejected.append({
                "type": kind,
                "payload": payload,
                "errors": errors,
            })

    def snapshot(self):
        with self._lock:
            return list(self._recorded), list(self._rejected)

    @property
    def recorded(self):
        return self.snapshot()[0]

    @property
    def rejected(self):
        return self.snapshot()[1]

    # Convenience accessors ---------------------------------------------
    def jobconfigs(self):
        return [
            entry["payload"] for entry in self.recorded
            if entry.get("type") == "JobConfig"
        ]

    def nodeconfigs(self):
        return [
            entry["payload"] for entry in self.recorded
            if entry.get("type") == "NodeConfig"
        ]

    def nodeconfig_deletes(self):
        return [
            entry["node"] for entry in self.recorded
            if entry.get("type") == "NodeConfigDelete"
        ]

    def jobconfigs_for(self, job_id):
        return [
            payload for payload in self.jobconfigs()
            if payload.get("key", {}).get("id") == job_id
        ]
