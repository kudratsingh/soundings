#!/usr/bin/env bash
# Fail if any denylisted word appears in code, UI, or docs outside the
# three places that define the list. Case-insensitive, prefix match.
set -euo pipefail

ALLOW_RE='^(config/denylist.txt|docs/SPEC-LADDER.md|CLAUDE.md|scripts/denylist-check.sh)$'

hits=$(grep -rinEf config/denylist.txt \
  --include='*.py' --include='*.ts' --include='*.tsx' --include='*.md' --include='*.yaml' --include='*.yml' --include='*.json' \
  --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=dist --exclude-dir=.venv \
  . 2>/dev/null | grep -vE "^\./($(echo "$ALLOW_RE" | sed 's/[()^$]//g; s/|/|/g'))" || true)

# filter allowed files precisely
filtered=$(echo "$hits" | awk -F: -v allow="$ALLOW_RE" '{ f=$1; sub(/^\.\//,"",f); if (f !~ allow) print }' || true)

if [[ -n "${filtered// }" ]]; then
  echo "Denylisted vocabulary found (the work stops at the handoff; see CLAUDE.md §2.1, §2.5):"
  echo "$filtered"
  exit 1
fi
echo "denylist: clean"
