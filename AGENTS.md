# AGENTS.md

Context for AI agents working in this repo. Environment lookups (Makefile
targets, module docstrings, `git log`) are authoritative; this file carries
only what they cannot tell you.

## What this repo is

One container image, two Kubernetes deployables (README.md § Architecture has
the diagram):

- **cv-job-informer** (deployment) — watches Jobs/Pods/ResourceClaims, reports
  the JobConfig and NodeConfig APIs to CloudVision. Entry point: `main.py`.
- **cv-interface-discovery** (daemonset) — discovers node interfaces into
  NodeInterfaceState CRs. Entry point: `interface_discovery.py` (own
  `__main__`).

The two components never import each other; `constants.py` is the only shared
module. Mode axes live in env vars: `JOBCONFIG_MODE` (`interface` | `node`)
and `NODECONFIG_MODE` (`discovery` | `sriovoperator` | `disabled`).

## Commands

- `make smoke PYTHON=.venv/bin/python` — full suite: two unit scripts plus the
  cluster-free integration suite. Tests are plain assert-based scripts run
  directly; there is no pytest. The integration runner takes `--jobs N`
  (default 4; `--jobs 1` is sequential and deterministic). Scenarios are
  process-isolated — each owns its fakes, kubeconfig and tmpdir — so parallel
  workers cannot interfere; constants mutation is what makes them unsafe to
  thread.
- Run a single suite from the repo root; imports need the repo root on
  `sys.path` (the Makefile sets `PYTHONPATH=.`):
  `PYTHONPATH=. python3 tests/integration/test_integration.py`

## Test design — don't fight it

- The integration suite runs the **real** `JobMonitor` against two fakes:
  `tests/integration/fake_apiserver.py` (kube-apiserver) and
  `tests/integration/cv_stub.py` (CloudVision over TLS). The seams live at
  those two clients — extend the fakes, do not mock inside handlers.
- TLS identities in tests are generated per run (`cv_stub._generate_identity`,
  via `cryptography`); PEM literals never enter source — secret scanners and
  security audits flag embedded keys even when they are throwaway.
- `test_integration.py` overrides `constants.JOB_START_*` **before** importing
  `job_monitor`, because handlers bind timing config at import time. Preserve
  that ordering when touching the harness.
- Scenario IDs (`P*`/`S*`/`N*`/`C*`) mark the axes a scenario varies:
  platform/protocol, scheduler, network allocation, NodeConfig. When a code
  change alters a pinned contract, update the affected scenarios in the same
  change.

## Gotchas

- `kubernetes` client `call_api` conventions differ by version (<36 returns a
  tuple, >=36 returns a bare HTTPResponse). `discovery.get_json` tolerates
  both; keep it that way — the Dockerfile allows `kubernetes>=28`.
- Python 3.11 is the container pin (Dockerfile); the suite also runs on newer
  CPython.
- Every source file starts with the Apache license header — copy from any
  existing module.

## Docs map

- `README.md` — product behavior, modes, deploy, API surface.
- Other files under `docs/` (scenario matrix, research notes) are local-only
  working material — they are not part of the repo.
