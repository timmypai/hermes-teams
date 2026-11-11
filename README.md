# Hermes Teams — WP-04 F1 corrective source v2

**Not installed, accepted or deployed.** This tree delivers the stdlib policy
package, surgical patch, guarded overlay generator, portable tests and authored
native SQLite tests. Operator native validation, Git custody, real PR/CI and
independent review remain pending. Portable success is not native acceptance.

## Exact baseline and implementation

Supported upstream commit: `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` only.
`patches/base.json` pins seven source files, including unchanged acceptance/tool
boundaries. `patches/contracts.patch` is the reviewable diff; `patches/edits.json`
contains exact replacement anchors. `patches/patched-sha256.json` identifies the
three resulting modules. Copied upstream context retains the MIT notice in
`patches/UPSTREAM-LICENSE`. No other version is claimed supported.

The managed cleaner normalizes contract/project proposals against trusted grants.
Its output is explicitly limited to title/body/parents and trusted
assignee/work-kind/contract/repository/project metadata. Model workspace, branch,
runtime, provider and other extra fields are discarded, not authorized. Direct
managed graph input rejects extra fields before normalization or any insertion;
the insertion helper also checks the field boundary before interpreting workspace
settings. No absolute-path or matching-project-slug check grants workspace access.
Children inherit the trusted root's native workspace rules; project worktrees
derive a fresh child path from the native project registry, not the model proposal.
No new workspace grant or work-kind authorization is introduced. Unmanaged
cleaner and direct graph workspace-override behavior remain unchanged. In the
unmanaged graph path the child contract remains SQL NULL (`None`), as in the
pinned upstream INSERT; this differs from the ordinary create API's `local-only`.
Fanout persists contracts inside the existing native transaction before readiness.
Single promotion sets the trusted specialist even if the root was preassigned
orchestrator. Managed create/single-update reuse native `validate_contract` and
refuse PR downgrades. Native acceptance, ownership, graph direction, idempotency,
decomposition markers and lifecycle checks are not replaced.

## Explicit trusted policy

`src/hermes_teams/policy.py` consumes policy explicitly. The supported scope is
board `seven-bot-v1`, project `hermes-teams`, repository `timmypai/hermes-teams`.
The synthetic `examples/policy.json` grants no live permission. Operation keys
`create:<idempotency-key>`, `single:<task-id>` and `child:<root-id>:<index>` must be
administrator-reviewed, with explicit work kind and role. Implementation/CI/Docker
require the repository PR contract. Research, acceptance and deploy-existing-image
require explicit non-PR grants; they do not blindly inherit a PR contract.
Contradictory model proposals and malformed policies fail closed. Existing exact
PR bindings are preserved; model-proposed exact PR URLs are conservatively rejected.

A future authorized rebuild must install the package with the patched modules and
explicitly bind the approved board's canonical native database path to reviewed
policy in administrator-owned `hermes_teams.site_policy.BINDINGS`. It defaults to
empty. Never populate it from model output, task bodies or worker-writable config.
The actual connection selects policy, not a caller-supplied board name. Unbound
boards retain legacy behavior; bound boards require grants for managed creation.
The native registry must resolve project `hermes-teams`, or creation fails.
Placing YAML in Hermes config enables nothing. These application checks are not
an OS sandbox. No automatic existing-task migration is included.

## Author-lane checks

Python 3.10+, POSIX sh, stdlib only; no dependency install:

```sh
mkdir -p reports/tmp
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$PWD/reports/tmp"
timeout 120 sh scripts/ci/check.sh --report-dir reports/f1-final-check
timeout 120 sh scripts/ci/test.sh --suite unit --report-dir reports/f1-final-unit
timeout 120 python3 scripts/validate_patch.py --base /path/to/exact/hermes-source
PYTHONPATH=src:tests/unit timeout 120 python3 -m unittest test_workspace -v
```

The validator defaults to read-only dry-run: every base hash, unique anchor,
Python syntax, agreement with the reviewable diff and patched-file hash receipt
must match before staging. Portable tests
cover policy/role/repository negatives, downgrades, graph validation, unmanaged
selection, report bounds, wrong-base refusal and new-overlay behavior. Scripts
propagate nonzero exits, enforce timeouts and restrict output to ignored reports/.
These checks do not open native databases.

## Operator/CI-only native validation

Managed Workers must not execute native SQLite tests, retry Git metadata writes,
clear runtime markers, alter fences or switch executors. Tests refuse managed
Worker contexts before importing native DB modules. The approved unbound operator
must inspect source and verify the frozen manifest, then run against a disposable
exact source baseline with its existing dependencies:

```sh
mkdir -p reports/tmp
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/reports/tmp" timeout 120 \
  python3 scripts/ci/native.py --base /path/to/exact/hermes-source
```

This creates a NEW reports/native-overlay without changing the baseline, verifies
loaded module hashes and exercises actual native SQLite in disposable databases,
never the live board. Existing overlays cause refusal: use a fresh candidate
checkout for another authorized validation. Authored tests cover cleaner, fanout,
single routing, atomic rollback, cycles, project resolution, idempotency, durable
decomposition markers, explicit non-PR kinds and unmanaged compatibility.
F1 regressions additionally cover a malicious dir/path proposal with an approved
project, trusted fresh-worktree derivation, direct normalized-looking graph input
and private insertion bypass, unchanged tasks/events/links on rejection, and
unmanaged legacy overrides. Fresh fixture initialization uses native `connect`,
which runs both the current base schema and additive migrations; partial synthetic
schemas are not substituted. The new source needs NEW operator validation:
earlier candidate results do not cover F1. Do not rerun or reuse an old overlay.
Connection redirection and injected failures are isolation, not simulated GitHub
or model evidence. Native integration is **NOT_RUN by the author**, awaiting
operator/CI results; baseline regression failures are not candidate success.

The final F1 test-oracle correction retains all prior tests and changes no
production code. The earlier operator suite failed because its unmanaged control
incorrectly expected `local-only` instead of the upstream graph path's NULL.
That failure remains evidence, not a pass. For a separately attributed unpatched
comparison, the approved operator can run the added test in a fresh stage BEFORE
running the full candidate suite above (no overlay path/environment):

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/reports/tmp" \
  PYTHONPATH=/path/to/exact/hermes-source timeout 120 python3 -m unittest \
  discover -s tests/integration -p test_unmanaged_baseline.py -v
```

This fixture verifies loaded native hashes and uses native schema plus migrations;
the same test is included in the full patched suite. Both actual executions remain
operator/CI responsibilities, not author results. A failed comparison stops custody.

The bounded Linux ci job runs syntax, portable and mandatory native tests inside
a digest-pinned official Hermes image. Checkout uses an operator-verified official
Action SHA, exact PR head, persist-credentials:false and contents:read only.
`patches/runtime-image.json` records registry-manifest verification only, not
verification of image contents/dependencies. Missing or mismatched native source
must FAIL ci, not skip into green. No actual hosted run, PR, Required Checks or
head/check association has been verified. Release/deploy workflows are out of scope.

## Future authorized rebuild and rollback

After native validation and independent review, a rebuild owner can stage files:

```sh
python3 scripts/validate_patch.py --base /path/to/exact/hermes-source --apply-to /path/to/new-overlay
```

This emits three patched modules and a hash receipt only. It never edits the base,
installs or restarts anything. The rebuild owner must integrate these files and
src/hermes_teams, protect explicit bindings, run native tests and obtain separate
release authorization. Preserve original source/image and operator-managed backups.
Code rollback does not undo persisted contracts/graph rows: verify compatibility
separately without erasing or downgrading contracts.

`candidate-manifest.json` freezes public source hashes, excluding itself and ignored
reports. It records the explicit public allowlist, file modes, deletions, exact
base and rejected-source lineage; the manifest does not hash itself. This source
freeze is not immutable storage. Operator custody must preserve the exact tree
and record the resulting commit/PR head. Local RED/GREEN and final evidence remain
separate in reports/. Original rejection and historical evidence remain unchanged
at their original owner; no Git metadata writes or publication occur in this lane.
Remaining acceptance: operator native validation, exact-tree Git custody, real
matching PR/current-head Required Checks, approved assignee identity acceptance,
and independent same-card review. None is replaced by this manifest or unit tests.
