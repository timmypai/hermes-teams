# Engineering operations runbook

## Validate this configuration product

From repository root with CPython3.14.9 and hash-verified PyYAML6.0.3 installed in an isolated environment:

```sh
python3 -m pip install --require-hashes --only-binary=:all: --no-deps -r tests/requirements-workflow.txt
timeout 600 python3 -m unittest discover -s tests -p test_workflow_safety.py -v
git diff --check
```

If pip is supplied separately, its standard --python option may target the local workspace venv. Do not install globally or inspect credential stores. Use official actionlint v1.7.7 release for the actual host architecture, validate archive against that release's published checksum before extraction/use, then:

```sh
actionlint -version
timeout 600 actionlint -color=false .github/workflows/ci.yml .github/workflows/release.yml .github/workflows/deploy.yml
```

Shellcheck integration is available only if that executable is installed; report whether actually exercised. actionlint and real PyYAML parsing are mandatory, not regex-only YAML claims. Missing tools are explicit limitations, not green results.

Unittest reads actual three workflows. Positive controls validate configuration and documentary responsibilities. Mutation controls serialize/parse isolated test-owned COPIES and reject floating refs, writable tokens, privileged untrusted events, arbitrary command/ref inputs, split mode locks, missing config, masked failures and missing bounded evidence. Actual extracted preflight shells run with a minimal nonsecret environment, no inherited credentials, fixed local data, ten-second subprocess bounds and no network/target commands. Expected exits64/78 plus absent mutation sentinel prove refusal and failure propagation only. They do not prove live credential/target isolation, merged-source provenance, version comparison or successful deployment.

Tests create disposable fixture directories ONLY under ../reports/n3-v2/fixtures and clean them after each case; per-case raw stdout/stderr/exit JSON remains under ../reports/n3-v2. This is a report directory, not a task ledger. Local command transcripts and manifest stay outside the repository. CI retains those reports (including test/lint logs and source receipt) for seven days via always-run artifact upload, without continue-on-error or masking the failed check. Do not publish private native reports or machine inventory as CI artifacts.

## Native handoff

Canonical orchestrator uses explicit native planning and supported native tasks under the actual repository contract; the sole existing Dispatcher claims workers. Developer remains codex_app_server; Reviewer owns independent QA. Read owner-bound show before work; persist heartbeat, comments and terminal lifecycle callback. A returned tool request is not proof of a completed stage; read back exact task/run or remote target.

On immutable local candidate, use same-card request_review for the independent reviewer; do not create a duplicate reviewer lane. Genuine review defects use request_changes; external missing credentials/target/decisions use a precise supported block. Never complete the repository card merely to unlock a publication child before PR/exact-head/native acceptance. Qualified forge approval is a distinct gate. No configuration/status/identity changes are delegated to Actions.

Before remote publication follow release-policy.md finite budgets and policy/actor/trigger/free eligibility checks. Persist intent and readback exact PR/head/run/attempt. Accepted dispatch is not job success; paginated latest evidence and current SHA are required. Unknown outcomes stop replay until reconciled. Never impersonate another profile, copy tokens, lower protections, alter native core gates or reopen old exhausted tasks.

## Release and deploy: disabled until target binding

Current release and deploy manual workflows deliberately fail preflight. No live environment, service, transport, publisher or knownhealthy artifact is configured. Their names are not deployment evidence. Do not dispatch them to manufacture success; a skipped off-main job is not a passed guard. Resolve D-DEPLOY-01 once with source/artifact/registry/service/environment/transport/health/knownhealthy recovery together, then implement fixed reviewed commands under a separately approved slice. There is no current live deploy or rollback command to run.

Future bound procedure: recheck exact approved source/digest/platform/config, authorization and actual current digest; acquire shared same-environment deploy/rollback lock; reject stale expected-current or superseded requests; promote identical immutable digest; read back actual version and perform bounded health plus functional success/failure smoke. Production requires explicit authorization and protected environment policy. A job success or running container alone is not service acceptance.

On health/smoke failure retain failed candidate status and recover once to approved compatible knownhealthy digest/config, then read back health/function. If rollback also fails, stop, preserve actual remaining state and ask canonical manager for a native Incident. No data wipe, down -v, live database restore or improvised transport. Report rollback success separately from the original failed deployment.

## Status reporting

For each stage record requirement version, real task/run, exact source and workflow revision, command/exit, immutable artifacts and independent verdict. Until exercised: hosted CI/native acceptance, forge approval, merge, built ARM64 artifact, release, deployment, actual rollback, voice, isolated restore and elapsed24h soak are NOT_RUN/UNVERIFIED. Retain original54 case dispositions; do not relabel removed mechanisms PASS. Stop when the authorized goal is achieved; route genuinely new work through the manager, not a new scheduler.
