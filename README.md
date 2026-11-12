# Hermes Teams

Native seven-role engineering-team integration for Hermes Agent. This repository's product is engineering configuration, workflows and operating procedures, not an unspecified business application.

## Architecture

Intake (`hermes`, existing default profile) → orchestrator explicit native planning → native Kanban → sole existing Dispatcher → researcher / architect / developer (`codex_app_server`) / independent reviewer+QA / devops.

The seven existing profiles are default, orchestrator, researcher, architect, developer, reviewer and devops. Reviewer owns independent QA; no extra QA profile or worker pool is introduced. Supported `kanban.auto_decompose=false` disables automatic decomposition at the controlling Gateway scope; it is not a board-local setting and does not disable Dispatcher. Preserve unrelated project holds. The installed Hermes controller is not a delivery artifact or deployment target.

Kanban owns task lifecycle; GitHub owns source, PR and CI evidence; target readback owns deployment truth. Actions never changes task status, starts workers or chooses the next Epic. No second harness, dispatcher, task database or scheduler belongs here. Profile directories and role prompts are not OS or credential isolation.

## Implemented configuration scope

- `.github/workflows/ci.yml`: meaningful YAML/actionlint and configuration safety validation, including invalid-mutation controls and exact extracted preflight guard negatives. Job/check name remains `ci`; observed contract associates it with Actions app `15368`, subject to fresh policy reconciliation before publication.
- `.github/workflows/release.yml`: manual trusted-main preflight only; rejects unbound release with nonzero exit before credentials, checkout, build or publication.
- `.github/workflows/deploy.yml`: manual deploy/rollback preflight only; both modes share one non-cancelling lock and reject unbound targets before credentials or target effects.
- `tests/`: standard unittest plus hash-locked PyYAML dependency for this configuration product, not an application or operational runner.

These definitions and local tests are authored controls. Their presence does not claim hosted CI, native GitHub acceptance, qualified forge approval, merge, image build, ARM64 functionality, release, deployment, rollback, voice or soak PASS. Configuration CI can pass independently of missing live artifact/target bindings. No fake empty build, placeholder image or controller redeployment is provided.

## Delivery policy

Repository edits require the native repository completion contract before dispatch, immutable candidate custody and same-card independent review. Exact-head CI, independent forge review, merge, release and healthy deployment are different gates. Production requires its own explicit approval and protected trusted execution; production credentials stay outside general workers.

Use immutable digests and knownhealthy rollback only after the consolidated D-DEPLOY-01 bindings are approved. Preserve original acceptance-case dispositions; removed design paths are not PASS. Machine-specific inventory, credentials, local reports and task databases remain private.

See [release policy](docs/engineering/release-policy.md) and [operations runbook](docs/engineering/operations-runbook.md) for exact validation commands, provenance and remaining gates.
