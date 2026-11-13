# Release policy — native-minimal 2.0 / requirement revision3

## Configuration product and trust

The product is the native seven-role engineering configuration and engineering-only workflows. Configuration CI validates these actual delivered files, not an invented business app. CI success is not built-image, ARM64 application, release or deployment acceptance. Installed Hermes is the controller, not product source. No core/Dispatcher/lifecycle/auth changes or historical patch/loader dependencies are adopted.

Default workflow permissions are contents:read. Every action uses an audited full commit SHA. Checkout never persists credentials. CI push triggers are main and zero-core-v3/**; PR types opened/synchronize/reopened target main. No pull_request_target, privileged PR code, untrusted secret expressions, arbitrary command input or operational Kanban logic is allowed. Each job and test command is bounded to ten minutes. CI concurrency separates push and PR events and does not cancel evidence.

Job/check context `ci` and known Actions app `15368` are preserved in the authoring design. Before ANY remote publication read actual Required Checks/Reviews, rule suites, merge-queue requirements, all trigger inventory and current branch/PR/run attempts; never rename/remove required checks or assume unknown policy. Merge-group support is not guessed; an actual requirement needs a bounded design/trigger-budget reconciliation before writes.

## Immutable review and finite publication

Freeze base, candidate commit/tree/sole parent, exact allowed paths, modes, SHA256 manifest, diff and test outputs. Same-card independent Reviewer inspects that exact immutable candidate, reruns syntax/safety/guard checks without editing, and returns request_changes for real defects. Author tests are not independent approval. Local safety approval is separate from independently qualified forge approval and native exact-head repository acceptance. A remote actor must not selfapprove or borrow an operator identity and call it a Bot.

The bounded slice allows one nonforce feature branch/initial PR, at most three candidate pushes, six total auto-triggered CI runs and two substantive corrections. Before each write reconcile exact branch/PR/head/run/attempt/trigger identities, record intent plus consumed/remaining budgets, actual profile-native actor, qualified approver and free standard runner eligibility. Unknown write outcomes require readback, never blind replay. No remote write occurs merely because workflows exist. Reruns and merge-main/release/deploy need their separately authorized accounting. Missing identity/protection/free eligibility blocks publication, not local authoring.

Use current PR head, tested checkout, merge-ref SHA, workflow revision, latest run attempt and complete paginated checks distinctly. Missing/failing/pending/stale checks, changed head or authentication mismatch are not PASS. No policy lowering or native gate modifications to obtain acceptance. The source-receipt and seven-day report artifact run even after a CI failure without changing failure into success.

## D-DEPLOY-01: deliberately unbound live stage

Release/deploy workflows are manual trusted-main **preflight-only**, not usable artifact publishing or live deployment implementations. Their only step validates immutable-shaped data and trusted repository/ref/workflow, then unconditionally exits78 with RELEASE_BINDINGS_UNBOUND or DEPLOY_BINDINGS_UNBOUND. Malformed or injection-shaped values exit64. They contain no credential references, protected environment attachment, checkout, build, upload, registry or target command. An off-main job may be skipped; skipped is not preflight PASS. Workflow/ref conditions are defense in depth, not proof of forge authorization.

Before enabling any privileged stage, approve one consolidated binding: accepted executable artifact/build/source and merged-source CI/reviews; exact registry/repository/publisher; project service/configuration path; closed staging/production environment mapping and protections; approved transport/endpoint identity; actual health/version/success and failure smoke scenarios; previous knownhealthy digest+configuration/data compatibility or explicit first-deploy recovery. The controller cannot be selected by inference. No mutable latest, fabricated digest, dummy secret, placeholder service or guessed environment is permitted.

The fixed unbound deploy lock is identical for deploy and rollback and is independent of mode/request. A future reviewed bound lock must key project/service/environment with cancel-in-progress:false. Concurrency is not FIFO or exactly-once. Current code does not query target version, enforce stale-request ordering, verify registry provenance, validate ARM64 behavior or actually rollback; both modes stop before any such operation. Valid-looking SHA/digest strings are not provenance.

Future live implementation must reread approved desired/current versions, reject stale expected-current mismatch, promote the same approved immutable digest from staging to production, validate ARM64 on representative architecture and retain source→artifact→environment→run readbacks. Health/functional failure must mark deployment failed even when knownhealthy recovery succeeds. Failed recovery becomes a native Incident, never an invented restored verdict. No down -v, volume deletion, live data overwrite or destructive migration is authorized.

## Evidence boundaries

Record source/PR head/checkout/merge/workflow/run/attempt, actual commands and exits, independent verdict, artifact/digest, target, health and recovery separately. Unknown fields stay NOT_RUN or UNVERIFIED. Original54 acceptance dispositions remain in the controlling mapping; removed paths are never PASS. Guard-negative exit78 is expected test success for refusal, not release/deployment success. Native runtime ownership, secret denial, qualified actors, hosted policy/CI, merge, actual build/release/target, voice, restore and 24-hour soak remain NOT_RUN until independently exercised.

## Pinned upstream provenance

Authoritative public GitHub git/ref API resolutions (tag objects have type commit):

| Action | Tag | Commit |
|---|---|---|
| actions/checkout | v4.2.2 | 11bd71901bbe5b1630ceea73d27597364c9af683 |
| actions/setup-python | v5.6.0 | a26af69be951a213d495a4c3e4e4022e16d87065 |
| actions/upload-artifact | v4.6.2 | ea165f8d65b6e75b540449e92b4886f43607fa02 |

These pins constrain fetched revisions, not a claim that upstream is risk-free. actionlint v1.7.7 official release checksums bind Linux AMD64 archive023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757 and ARM64 archive401942f9c24ed71e4fe71b76c7d638f66d8633575c4016efd2977ce7c28317d0. CI uses AMD64; local ARM64 lint is configuration validation, not target-app validation. CPython3.14.9 and PyYAML6.0.3 Linux wheels are locked to the two architecture-specific hashes in tests/requirements-workflow.txt; no source builds/global installs. Revisions or tool upgrades require normal review and revalidation.
