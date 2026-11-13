# sevenbot-flow-probe

Python standard-library, read-only foreground delivery probe. No package dependencies,
filesystem serving, subprocess execution, configuration mutation or external APIs.

```sh
python3 apps/sevenbot-flow-probe/server.py --source-sha "$APPROVED_SOURCE_SHA"
```

Supply the exact approved immutable build-source SHA (40 lowercase hex characters).
The service validates its shape; it cannot authenticate provenance. Deployment must
bind this value to reviewed immutable build metadata. Do not use `latest`, invent a
SHA, or treat the test fixture SHA as deployment evidence.

Options: `--host` defaults to `127.0.0.1`; `--port` defaults to `8080` (0 selects an
ephemeral port); `--response-revision` defaults to `flow-probe-v1`, with only
`flow-probe-v1` and `flow-probe-v2` accepted. Invalid configuration exits 2 before
binding. Bind failure exits 2 with `BIND_FAILED`. Startup prints one JSON object
containing the actual listening host and port. SIGTERM/SIGINT close the listener
and exit 0; no daemon or work receiver is installed.

| GET path | Status | JSON |
| --- | --- | --- |
| `/version` | 200 | `schema_version: 1`, `source_sha`, `response_revision` |
| `/healthz` | 200 | `healthy: true` |
| `/healthz` with invalid running version | 503 | `healthy: false`, `error: INVALID_VERSION` |
| `/` | 200 | `message: sevenbot-flow-probe-v1` (or `sevenbot-flow-probe-v2`) |
| Any other exact path, including query strings | 404 | `error: NOT_FOUND` |

Unsupported methods return 405 with `Allow: GET`; HEAD has no response body.
JSON responses declare `application/json` and byte Content-Length. Each request
closes its connection; bodies are never consumed or evaluated. The stdlib parser
bounds request-line/header sizes; each socket read times out after one second.
Malformed/oversized protocol requests use stdlib HTTP error responses. The service
is deliberately single-request-at-a-time and intended for isolated loopback smoke
checks, not public ingress or production traffic. No request/environment logging.
Health revalidates the actual in-memory version on every call; tests inject faults
in process, never through a mutation endpoint.

## Reproduce verification

From the checkout:

```sh
PYTHONPYCACHEPREFIX=.cache/pycache timeout 600 python3 -m unittest discover -s tests -p 'test_flow_probe*.py' -v
PYTHONPYCACHEPREFIX=.cache/pycache timeout 600 ../.venv/bin/python -m unittest discover -s tests -p test_workflow_safety.py -v
```

The retained workflow suite needs the unchanged hash-locked
`tests/requirements-workflow.txt` installed into an owned virtual environment.
Application tests use real ephemeral loopback HTTP, bounded subprocesses and
workspace-local temporary fixtures with cleanup. They test both revisions,
version identity, health fault/recovery, 404/405, malformed configuration before
bind, bounded/oversized requests and foreground SIGTERM with a partial request.

This application slice does not modify workflows or add Docker/host installation.
Independent review, publication, CI, deployment A/B provenance and rollback remain
separate gates; local test success does not satisfy them.
