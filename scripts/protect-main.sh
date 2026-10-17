#!/usr/bin/env bash
# Protect main: PRs only, CI "check" must pass, no force-push, no deletion.
# Requires: gh auth with repo admin on kudratsingh/soundings.
set -euo pipefail

REPO="${1:-kudratsingh/soundings}"

gh api -X PUT "repos/${REPO}/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["check"] },
  "enforce_admins": false,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": true
}
JSON

echo "Protection applied. Verify:"
gh api "repos/${REPO}/branches/main/protection" --jq '{checks: .required_status_checks.contexts, force: .allow_force_pushes.enabled, linear: .required_linear_history.enabled}'
