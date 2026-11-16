#!/usr/bin/env bash
# ub-mode "Then Make it Right" metrics, CI only (GitHub-hosted runner, no secrets):
#   crapper  CRAP per function (target < 10)
#   dryer    structural duplication (target: nothing >= 0.8)
#   mutator  mutation testing on the source files this PR changed (cover the uncovered, kill survivors)
# Evidence for developer and reviewer, never a merge gate: always exits 0.
# UB_TOOLS = dir with bootstrapped crapper/ dryer/ mutator/ checkouts (mutator needs ../crapper).
# QUALITY_BASE = git ref the PR is compared against.
# Coverage: if the tools' default test discovery does not find this repo's tests, add an executable
# .devflow/coverage.sh that writes LCOV to target/coverage/<language>/lcov.info (Go: coverage.out);
# the tools then read it instead of running their own.
set -uo pipefail
TOOLS=${UB_TOOLS:?UB_TOOLS not set}
BASE=${QUALITY_BASE:-origin/main}
OUT=.metrics
mkdir -p "$OUT"

cov=()
if [[ -x .devflow/coverage.sh ]]; then
  if .devflow/coverage.sh >"$OUT/coverage.txt" 2>&1; then cov=(--use-existing-coverage)
  else echo "coverage.sh failed; tools fall back to their own coverage run" >>"$OUT/coverage.txt"; fi
fi

"$TOOLS/crapper/crapper" "${cov[@]}" >"$OUT/crap.txt" 2>&1 \
  || { echo "(coverage run failed; complexity only)" >"$OUT/crap.txt"; "$TOOLS/crapper/crapper" --no-coverage >>"$OUT/crap.txt" 2>&1; }
"$TOOLS/dryer/dryer" --threshold 0.8 >"$OUT/dry.txt" 2>&1

mapfile -t changed < <(git diff --name-only --diff-filter=AM "$BASE"...HEAD \
  | grep -E '\.(py|ts|tsx|js|jsx|mjs|cjs|go|rs|java|clj|cljs|cljc)$' \
  | grep -vE '(^|/)(tests?|spec|vendor|node_modules|target)/|(^|/)(test_[^/]*|[^/]*_test\.[a-z]+|[^/]*\.(test|spec)\.[a-z]+)$')
if ((${#changed[@]})); then
  test_cmd=()
  [[ -f .devflow/test.sh ]] && test_cmd=(--test-command "bash .devflow/test.sh")
  "$TOOLS/mutator/mutator" "${cov[@]}" "${test_cmd[@]}" "${changed[@]}" >"$OUT/mutate.txt" 2>&1
  echo "exit $? (0 all killed, 2 baseline tests failed, 3 survivors)" >>"$OUT/mutate.txt"
else
  echo "no changed source files" >"$OUT/mutate.txt"
fi

if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
  {
    echo "## ub-mode quality (evidence, not a gate)"
    for f in crap dry mutate; do
      echo "### $f"; echo '```'; grep -v '^+ (' "$OUT/$f.txt" | tail -n 40; echo '```'
    done
  } >>"$GITHUB_STEP_SUMMARY"
fi
exit 0
