#!/usr/bin/env bash
# Commit current work to main, push, and publish it as a GitHub release (Mac + Windows builds).
# Usage: bash desktop/publish.sh 0.1.0
set -euo pipefail
cd "$(dirname "$0")/.."
VER="${1:-$(cat VERSION)}"; VER="${VER#v}"

git check-ignore -q local-config.json || { echo "local-config.json is not ignored; stopping so the key isn't committed."; exit 1; }
if [ -n "$(git diff --name-only --diff-filter=U)" ] || [ -f .git/MERGE_HEAD ]; then
  echo "A merge is in progress or has conflicts. Resolve it first (see the fix steps)."; exit 1
fi
if grep -rIl '^<<<<<<< ' --exclude-dir=.git --exclude-dir=static . >/dev/null 2>&1; then
  echo "Conflict markers found in:"; grep -rIl '^<<<<<<< ' --exclude-dir=.git --exclude-dir=static .; exit 1
fi
if [ -f desktop/desktop.yml ]; then mkdir -p .github/workflows && mv desktop/desktop.yml .github/workflows/desktop.yml; fi
if [ -f local-config.json ] && jq -e .awsMapKey local-config.json >/dev/null 2>&1; then
  jq -r .awsMapKey local-config.json | gh secret set GRIFFIES_AWS_MAP_KEY
fi

[ "$(git branch --show-current)" = "main" ] || { echo "Switch to main first: git switch main"; exit 1; }
git fetch origin
if ! git merge-base --is-ancestor origin/main HEAD; then
  echo "GitHub's main has commits you don't have. Run: git pull --rebase   then try again."; exit 1
fi

echo "$VER" > VERSION
git add -A
git diff --cached --quiet || git commit -m "Griffies v$VER"
git push origin main
if git ls-remote --exit-code --tags origin "v$VER" >/dev/null 2>&1; then echo "Tag v$VER already exists on GitHub; use a new version number."; exit 1; fi
git tag -f "v$VER" && git push origin "v$VER"
echo "Building v$VER on GitHub…"
sleep 10
RUN_ID="$(gh run list --workflow 'Desktop builds' --event push --limit 1 --json databaseId -q '.[0].databaseId')"
gh run watch "$RUN_ID" --exit-status
echo; gh release view "v$VER" --json url,assets -q '.url, (.assets[].name)'
