# Hermes Teams

Native seven-role engineering-team integration for Hermes Agent.

## Architecture

Intake (`hermes`, existing default profile) → orchestrator → native Kanban decomposition and Dispatcher → researcher / architect / Codex App-Server developer / independent reviewer+QA / devops.

Kanban owns task lifecycle. GitHub owns source, PR and CI evidence. Docker owns deployed service state. This repository must not introduce another harness, dispatcher, task database, worker pool or Supabase.

## Delivery policy

- Repository changes require a native PR completion contract before dispatch.
- Exact-head CI, independent review, merge, release and deployment are distinct gates.
- Worker prompts and profile directories are not OS or credential isolation.
- Production credentials stay outside developer workers.
- Deployment uses immutable digests, health and functional smoke tests, and known-good rollback.
- Never claim an unexecuted acceptance test passed.

## Bootstrap status

This initial commit is administrative documentation only. It does not claim the integration, CI, release or deployment has been implemented or accepted. Runtime evidence and machine-specific inventory remain private; do not commit credentials, raw logs, local backup databases or host identifiers.
