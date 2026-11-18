#!/usr/bin/env bash
# Publish a new version: bump VERSION, tag, push. GitHub Actions builds the Mac + Windows apps
# and attaches them to a release, and installed copies show "a newer version is available".
# Usage: bash desktop/release.sh 0.1.1
set -euo pipefail
cd "$(dirname "$0")/.."
NEW="${1:?Usage: bash desktop/release.sh <version, e.g. 0.1.1>}"
NEW="${NEW#v}"
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must look like 1.2.3"; exit 1; }
git check-ignore -q local-config.json || { echo "local-config.json is not ignored; stopping so the key isn't committed."; exit 1; }
if [ -f desktop/desktop.yml ]; then mkdir -p .github/workflows && mv desktop/desktop.yml .github/workflows/desktop.yml; fi
echo "$NEW" > VERSION
git add -A
git commit -m "Release v$NEW"
git tag "v$NEW"
git push origin HEAD "v$NEW"
echo "Pushed v$NEW. Builds take ~10 min:"
echo "  gh run watch \$(gh run list --workflow 'Desktop builds' --limit 1 --json databaseId -q '.[0].databaseId')"
