#!/usr/bin/env bash
# Independent review of a kit PR (this repo) by the Hermes reviewer profile (its own GitHub account,
# not the PR author): approve -> merge; findings -> request changes. Gate paths in .github/CODEOWNERS
# still need the code owner's approval; the merge then waits for it.
#   ./review-kit-pr.sh <PR>
# Env: VM, VM_KEY, HERMES_CONTAINER, HERMES_RUNTIME_UID (same as deploy-kit.sh).
set -euo pipefail
cd "$(dirname "$0")"
pr=${1:?usage: review-kit-pr.sh <PR>}
[[ $pr =~ ^[0-9]+$ ]] || { echo "PR must be a number" >&2; exit 2; }
KEY=${VM_KEY:-/tmp/ok.key}
VM=${VM:-ubuntu@161.33.168.214}
SSH=(ssh -o IdentitiesOnly=yes -o BatchMode=yes -i "$KEY" "$VM")
CONTAINER=${HERMES_CONTAINER:-hermes-controller-r5}
RUNTIME_UID=${HERMES_RUNTIME_UID:-1004}
DOCKER="sudo -n -H -u '#$RUNTIME_UID' -- /usr/bin/docker --host unix:///run/user/$RUNTIME_UID/docker.sock"
repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)

echo "waiting for CI on $repo#$pr"
gh pr checks "$pr" -R "$repo" --watch --interval 20 >/dev/null || { echo "CI failed: not sending to review" >&2; exit 1; }

work="/tmp/kit-review-$pr"
prompt="Review kit PR #$pr of $repo per devflow-review section 4 (kit PR). Scratch directory: $work."
out=$("${SSH[@]}" "cd / && $DOCKER exec -u hermes -w /tmp \
  -e HERMES_HOME=/opt/data/profiles/reviewer -e GH_CONFIG_DIR=/opt/data/profiles/reviewer/gh \
  -e DBUS_SESSION_BUS_ADDRESS=disabled: $CONTAINER \
  timeout 3000 hermes chat -Q --oneshot --yolo -s devflow-review -q '$prompt' </dev/null 2>&1")
echo "$out" | tail -20
verdict=$(grep -oE 'VERDICT: (approved|changes_requested)' <<<"$out" | tail -1 | cut -d' ' -f2)
by=$(gh pr view "$pr" -R "$repo" --json reviews -q '[.reviews[] | select(.state != "COMMENTED")] | last | .author.login + " " + .state')
echo "reviewer verdict: ${verdict:-none}; latest review: ${by:-none}"
case $verdict in
  approved)
    [[ $by == *" APPROVED" ]] || { echo "verdict says approved but GitHub has no approval" >&2; exit 1; }
    if gh pr merge "$pr" -R "$repo" --merge; then echo "merged"
    else echo "approved; merge blocked (gate files need the CODEOWNERS approval)"; exit 3; fi ;;
  changes_requested) echo "changes requested: see the review on the PR"; exit 4 ;;
  *) echo "no verdict from the reviewer session" >&2; exit 1 ;;
esac
