# Common CI v1 — closed Python/HTTP profile

## Actual interface and caller

`.github/workflows/reusable-ci.yml` exposes `workflow_call`. The real thin project
caller is `.github/workflows/ci.yml`; its sole project parameter is:

```yaml
jobs:
  tests:
    uses: ./.github/workflows/reusable-ci.yml
    with:
      test-profile: python-stdlib-http-v1
```

`test-profile` is a required string, with **only** `python-stdlib-http-v1` accepted.
The first step compares the value as quoted environment data before checkout.
There is no command, script, ref, repository, runner, path, secret or host input.
Unknown profiles and injection/path-shaped values fail, rather than selecting
arbitrary code. This profile deliberately fixes the project layout and commands;
it is not a general-purpose arbitrary-language runner.

The profile requires caller-owned `apps/sevenbot-flow-probe/server.py`,
`tests/test_flow_probe.py`, configuration workflows `ci.yml`, `reusable-ci.yml`,
`release.yml`, `deploy.yml`, `tests/test_workflow_safety.py`,
`tests/test_shared_ci.py`, and hash-locked `tests/requirements-workflow.txt`.
The retained safety suite also validates this repository's policy documents.
These are project contracts, not remotely fetched common helpers. The common
implementation is entirely in the reusable YAML: it does not import a shared
helper from the caller checkout or fetch a helper at a different revision.
A future project's app/test implementation must implement this closed layout,
or receive a separately reviewed new profile. Do not change names by passing
unchecked selectors.

## Required check and real execution

The caller retains the existing push/PR trigger inventory and event-separated,
noncancelling concurrency. The `tests` job calls the common workflow. A separate
ordinary job explicitly named **`ci`** has `needs: [tests]` and `if: always()`;
its shell accepts only `needs.tests.result == success`. Failure, cancellation,
skip and missing results fail. This avoids relying on a nested reusable job's
qualified check name (normally `tests / tests`) as the protected `ci` identity.
No duplicate `ci` job is created inside the reusable workflow.

GitHub Actions must actually emit that exact `ci` check with the existing verified
Actions app association `15368`. Local graph and shell tests establish neither
hosted check identity nor branch-protection acceptance. Publication must reread
current protection/check runs and confirm both fields on the new head; do not
rename required checks or alter protection to fit a mismatch.

Common CI runs, in order:

1. Closed-profile check, event checkout with credentials not persisted, fixed
   CPython 3.14.9, hash-locked binary PyYAML dependencies.
2. SHA256-qualified actionlint 1.7.7 for all four actual workflows.
3. Entire retained configuration/workflow safety suite, including real fail-closed
   release/deploy guard negatives and evidence-path missing/symlink negatives.
4. Entire `test_flow_probe*.py` suite: real loopback HTTP and foreground CLI
   subprocess tests, not mocked HTTP. Mandatory source/test existence checks
   prevent a missing main suite from becoming a zero-test success.
5. Shared-CI contract suite: real extracted app shell executed on disposable
   copies of the real app/tests, deliberate failing test propagation, missing
   tests, all gate dependency results, profile injection/path negatives,
   changed helper/workflow refs and privileged-runner rejection, shell syntax.
6. Always-run source receipt and bounded canonical absolute evidence upload,
   seven-day retention and missing-file errors, including failure logs.

Each test command is bounded by `timeout 600`; the common job has a ten-minute
**total** timeout (not ten minutes per step cumulatively). The final gate has a
one-minute timeout. A failed/timed-out common job cannot make the gate green.
Action references are full immutable commit SHAs, permissions are contents:read,
and only GitHub-hosted ubuntu-24.04 is used. No private CD calls, secrets,
privileged runner or PR-target event is introduced. Public project tests are
untrusted code with the normal hosted read-only boundary, not privileged CD.

## Local validation and immutable reuse

From the source checkout, prefix each command with
`PYTHONPYCACHEPREFIX=.cache/pycache`:

```sh
timeout 600 python3 -m unittest discover -s tests -p 'test_flow_probe*.py' -v
timeout 600 ../.venv/bin/python -m unittest discover -s tests -p test_workflow_safety.py -v
timeout 600 ../.venv/bin/python -m unittest discover -s tests -p test_shared_ci.py -v
```

The sibling venv is this task's retained local tool; hosted CI installs the
existing hash-locked requirements under its own setup-python interpreter.
No local tests claim artifact-service upload or live deployment success.

The local reusable reference intentionally selects the same commit as the
caller, without a mutable remote tag. After review and merge, an authorized
second repository can call the public workflow using the **full accepted 40-hex
commit** as `timmypai/hermes-teams/.github/workflows/reusable-ci.yml@<accepted SHA>`
and the same closed input. That repository must supply compatible app/tests and
its own reviewed caller gate/protection policy; this repository's safety suite
intentionally rejects replacing its local caller with an arbitrary repo/ref.
Do not pin a moving branch/tag, invent a future SHA, copy private CD inventory,
or assume external reuse has already passed. No helper action/script or unused
configuration schema is introduced. A named second-repo same-version trial and
actual hosted application CI remain separate required acceptance gates.

## Native delivery boundary

Freeze the exact local commit with truthful command-local Bot attribution,
record test exits and unchanged application hashes, and request independent
**same-card** review. Historical Run112 approval covers only its old source SHA.
The manager admits publication after the new review and current actor/policy/
runner/budget checks. This CI-author phase performs no push, PR, dispatch,
merge, artifact release, Oracle action or authentication configuration change.
Local GREEN is not independent review, forge approval, hosted CI or deployment.
