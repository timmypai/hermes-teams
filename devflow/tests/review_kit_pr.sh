#!/usr/bin/env bash
# review-kit-pr.sh merges only the head the reviewer approved: fake gh/ssh, no network.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
script="$here/../../review-kit-pr.sh"
H1=1111111111111111111111111111111111111111
H2=2222222222222222222222222222222222222222
t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
mkdir -p "$t/bin"
cat >"$t/bin/ssh" <<'EOF'
#!/usr/bin/env bash
echo "VERDICT: approved"
EOF
cat >"$t/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "repo view") echo o/r ;;
  "pr checks") printf 'ci\tpass\t1s\thttps://example\n' ;;   # non-tty: name, state, ...
  "pr view") echo "$HEAD" ;;
  "api repos/o/r/pulls/7/reviews?per_page=100") echo "$REVIEW" ;;
  "pr merge") echo "$*" >>"$LOG" ;;
  *) echo "unexpected gh $*" >&2; exit 9 ;;
esac
EOF
chmod +x "$t/bin/"*
fail=0
case_() { # name head review want_exit want_merge
  local name=$1 rc=0; : >"$t/log"
  HEAD=$2 REVIEW=$3 LOG="$t/log" PATH="$t/bin:$PATH" bash "$script" 7 >"$t/out" 2>&1 || rc=$?
  local merged; merged=$(cat "$t/log")
  if [[ $rc == "$4" && $merged == "$5" ]]; then echo "ok   $name"
  else echo "FAIL $name: exit $rc (want $4), merge '$merged' (want '$5')"; cat "$t/out"; fail=1; fi
}
case_ "approved head is merged on that head" $H1 "APPROVED $H1" 0 "pr merge 7 -R o/r --merge --match-head-commit $H1"
case_ "approval of an older head is not merged" $H1 "APPROVED $H2" 1 ""
case_ "no reviewer approval is not merged" $H1 "" 1 ""
exit $fail
