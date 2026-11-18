#!/usr/bin/env bash
# Redo a release whose build failed or was cancelled: commit current work, move the tag, rebuild.
# Usage: bash desktop/rerelease.sh 0.1.0
set -euo pipefail
cd "$(dirname "$0")/.."
VER="${1:-$(cat VERSION)}"; VER="${VER#v}"
# Cancel runs stuck in the queue (e.g. ones asking for retired runners)
gh run list --workflow 'Desktop builds' --status queued --json databaseId -q '.[].databaseId' | xargs -r -n1 gh run cancel || true
gh release delete "v$VER" --yes 2>/dev/null || true
git push origin --delete "refs/tags/v$VER" 2>/dev/null || true
git tag -d "v$VER" 2>/dev/null || true
bash desktop/publish.sh "$VER"
