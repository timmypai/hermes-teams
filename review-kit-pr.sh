#!/usr/bin/env bash
# Independent review of a kit PR (this repo) by the Hermes reviewer profile (its own GitHub account,
# not the PR author): approve -> merge; findings -> request changes. No human approval (user choice).
# Approval and merge are bound to the head reviewed: a push during review is never merged on it.
#   ./review-kit-pr.sh <PR>
# Env: VM, VM_KEY, HERMES_CONTAINER, HERMES_RUNTIME_UID (same as deploy-kit.sh), KIT_REVIEWER.
set -euo pipefail
cd "$(dirname "$0")"
pr=${1:?usage: review-kit-pr.sh <PR>}
[[ $pr =~ ^[0-9]+$ ]] || { echo "PR must be a number" >&2; exit 2; }
KEY=${VM_KEY:-/tmp/ok.key}
VM=${VM:-ubuntu@161.33.168.214}
SSH=(ssh -o IdentitiesOnly=yes -o BatchMode=yes -i "$KEY" "$VM")
CONTAINER=${HERMES_CONTAINER:-hermes-controller-r5}
RUNTIME_UID=${HERMES_RUNTIME_UID:-1004}
REVIEWER=${KIT_REVIEWER:-paulpai0412}
DOCKER="sudo -n -H -u '#$RUNTIME_UID' -- /usr/bin/docker --host unix:///run/user/$RUNTIME_UID/docker.sock"
repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)

echo "waiting for CI on $repo#$pr"
# Poll check buckets instead of `gh pr checks --watch`: --watch exits 1 on a network error too,
# which read as "CI failed". A query error is retried; only a fail/cancel bucket is a failure.
for ((i = 0; ; i++)); do
  (( i < 180 )) || { echo "CI still pending after 60 min: not sending to review" >&2; exit 1; }
  buckets=$(gh pr checks "$pr" -R "$repo" 2>/dev/null | cut -f2)
  grep -qxE 'fail|cancel' <<<"$buckets" && { echo "CI failed: not sending to review" >&2; exit 1; }
  [[ -n $buckets ]] && ! grep -qx pending <<<"$buckets" && break
  sleep 20
done
head=$(gh pr view "$pr" -R "$repo" --json headRefOid -q .headRefOid)
[[ $head =~ ^[0-9a-f]{40}$ ]] || { echo "no head SHA for #$pr" >&2; exit 1; }

work="/tmp/kit-review-$pr"
prompt="Review kit PR #$pr of $repo at head commit $head per devflow-review section 4 (kit PR). Scratch directory: $work."
out=$("${SSH[@]}" "cd / && $DOCKER exec -u hermes -w /tmp \
  -e HERMES_HOME=/opt/data/profiles/reviewer -e GH_CONFIG_DIR=/opt/data/profiles/reviewer/gh \
  -e DBUS_SESSION_BUS_ADDRESS=disabled: $CONTAINER \
  timeout 3000 hermes chat -Q --oneshot --yolo -s devflow-review -q '$prompt' </dev/null 2>&1")
echo "$out" | tail -20
verdict=$(grep -oE 'VERDICT: (approved|changes_requested)' <<<"$out" | tail -1 | cut -d' ' -f2)
# The reviewer's latest decisive review, and the commit it was submitted on.
review=$(gh api "repos/$repo/pulls/$pr/reviews?per_page=100" --jq \
  "[.[] | select(.user.login == \"$REVIEWER\" and .state != \"COMMENTED\")] | last | (.state // \"\") + \" \" + (.commit_id // \"\")")
echo "reviewer verdict: ${verdict:-none}; $REVIEWER review: ${review:-none}; reviewed head: $head"
case $verdict in
  approved)
    [[ $review == "APPROVED $head" ]] || { echo "no $REVIEWER approval on head $head: not merging" >&2; exit 1; }
    if gh pr merge "$pr" -R "$repo" --merge --match-head-commit "$head"; then echo "merged $head"
    else echo "approved; merge refused (head moved since review)"; exit 3; fi ;;
  changes_requested) echo "changes requested: see the review on the PR"; exit 4 ;;
  *) echo "no verdict from the reviewer session" >&2; exit 1 ;;
esac
