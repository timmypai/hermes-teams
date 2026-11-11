# Report replacement policy (WP06 F01)

Both `scripts/ci/check.sh` and `scripts/ci/test.sh` use the same report writer.
It creates an exclusive, randomly named temporary regular file in the validated
report directory, writes UTF-8 JSON, flushes and fsyncs the file, closes it, then
atomically replaces `result.json`. It never opens the existing result inode for
writing. A preexisting hardlink is detached at this directory entry; its other
names and bytes are unchanged. New reports have the temporary file's private
0600 permissions; replacement does not preserve the old inode's mode/ownership.

The existing resolved-path containment and result-file symlink checks remain.
The reports root itself cannot be a symlink. Paths resolving outside reports or
to the reports root are rejected before executing the command. As before, an
internal directory symlink that resolves to a child inside reports is allowed;
this is resolved-path containment, not a blanket prohibition on every symlink.
A result symlink observed immediately before replacement is also rejected.

## Threat model and limits

- Supported: preexisting hardlinked result files, ordinary replacement, repeated
  writes, and output errors, in a caller-controlled directory with stable trusted
  ancestors. Atomic replacement avoids truncating an existing destination alias.
- Concurrent cooperative writers may replace each other's result: last rename
  wins, with no report merging, locking or per-run ownership guarantee. No
  concurrent-writer stress test or general race-resistance claim is made here.
- A destination symlink inserted after the last check is replaced, not followed
  by rename. This does **not** make the whole path resolution race-free.
- Hostile mutation of the report directory, ancestors, or temporary pathname by
  another actor with directory/same-UID privileges is outside this guarantee.
  Such an actor can redirect paths or alias the temporary inode. Exclusive temp
  creation and application checks are not an OS sandbox or credential isolation.
- File fsync is not directory fsync: crash-durable rename is not guaranteed.
  A process kill can leave a temporary file. Neither crash nor hostile namespace
  races have been executed as acceptance tests for this change.

## Failure semantics and verification

Creation/write/fsync/rename errors cannot produce a successful command exit.
Reported write errors go to stderr; a successful underlying command becomes
exit 1, while a nonzero underlying test/check status remains nonzero. Cleanup
errors also fail the process. Before replacement the previous report may remain;
it is stale evidence, never proof that the failed invocation succeeded. No
fallback report is written to an unvalidated path. Directory/argument failures
can occur before a report is writable and do not promise a new JSON report.

Run from the source root (stdlib only):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/unit -p test_report_writer.py -v
PYTHONDONTWRITEBYTECODE=1 sh scripts/ci/check.sh --report-dir reports/report-safety-check
PYTHONDONTWRITEBYTECODE=1 sh scripts/ci/test.sh --suite unit --report-dir reports/report-safety-unit
```

The focused suite invokes real copied wrappers in isolated own-workspace
fixtures. It checks both hardlink sentinels outside each fixture's reports,
successful rewrites, genuine assertion/syntax failure propagation, path and
symlink rejection, directory creation and replacement errors. Set
`WP06_REPORT_TEST_EVIDENCE` to a private workspace directory to retain fixtures
and exact argv/cwd/exits/stdout/stderr/report bytes and runner hashes; otherwise
fixtures are cleaned up. These are synthetic local fixture tests, not native
SQLite integration or hosted CI. Existing tests and their gates remain intact.

`candidate-manifest.json` is inherited historical WP04 evidence and is unchanged;
it does not seal this new delta. WP06's current private freeze manifest is
`reports/wp06-handoff-v2/freeze.json`, pending independent DevOps custody and
same-card review. Full WP06 bootstrap/build/E2E, lint/typecheck, report schema,
redaction, deadlines and hosted checks remain separate unaccepted obligations.
