#!/usr/bin/env bash
# One-shot: push this repo to a branch, run the Mac build on GitHub Actions, download the .dmg files.
# Needs: git, gh (logged in: `gh auth login`), jq.
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="${BRANCH:-desktop-build}"
for tool in git gh jq; do
  command -v "$tool" >/dev/null || { echo "Missing $tool. Install it first (sudo apt install $tool)."; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "Run: gh auth login"; exit 1; }

# 1. Workflow file must live under .github/workflows
if [ -f desktop/desktop.yml ]; then
  mkdir -p .github/workflows
  mv desktop/desktop.yml .github/workflows/desktop.yml
  echo "Moved workflow into .github/workflows/"
fi

# 2. Map key -> GitHub secret (never committed)
if [ -f local-config.json ] && jq -e .awsMapKey local-config.json >/dev/null; then
  jq -r .awsMapKey local-config.json | gh secret set GRIFFIES_AWS_MAP_KEY
  echo "Saved map key as repo secret GRIFFIES_AWS_MAP_KEY"
fi

# 3. Commit to the build branch and push (never commits local-config.json: it's git-ignored)
git check-ignore -q local-config.json || { echo "local-config.json is not ignored; stopping so the key isn't committed."; exit 1; }
git switch "$BRANCH" 2>/dev/null || git switch -c "$BRANCH"
git add -A
git diff --cached --quiet || git commit -m "Desktop build: Griffies redesign, AI Reply, map, filters"
git push -u origin "$BRANCH"

# 4. Run the Mac build and wait for it
gh workflow run "Desktop builds" --ref "$BRANCH" -f platform=mac
echo "Waiting for the run to start…"
sleep 8
RUN_ID="$(gh run list --workflow "Desktop builds" --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId')"
gh run watch "$RUN_ID" --exit-status

# 5. Download the .dmg files
mkdir -p dist-release
gh run download "$RUN_ID" --dir dist-release
find dist-release -name '*.dmg' -exec mv {} dist-release/ \;
echo
echo "Done:"
ls -lh dist-release/*.dmg
