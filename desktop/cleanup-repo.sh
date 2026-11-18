#!/usr/bin/env bash
# Delete old GitHub releases, tags and branches. Keeps `main` and the version in VERSION (or $1).
# Shows everything first and asks before deleting. Usage: bash desktop/cleanup-repo.sh [keep-version]
set -euo pipefail
cd "$(dirname "$0")/.."
command -v gh >/dev/null && command -v jq >/dev/null || { echo "Needs gh and jq."; exit 1; }
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
KEEP="v$(echo "${1:-$(cat VERSION)}" | sed 's/^v//')"

git fetch --prune --tags origin >/dev/null 2>&1 || true
mapfile -t RELEASES < <(gh release list --repo "$REPO" --limit 200 --json tagName -q '.[].tagName' | grep -vx "$KEEP" || true)
mapfile -t TAGS < <(git ls-remote --tags origin | awk '{print $2}' | sed 's#refs/tags/##; s#\^{}##' | sort -u | grep -vx "$KEEP" || true)
mapfile -t BRANCHES < <(git ls-remote --heads origin | awk '{print $2}' | sed 's#refs/heads/##' | grep -vx main || true)

echo "Repo: $REPO   (keeping: main, $KEEP)"
echo
echo "Releases to delete (${#RELEASES[@]}):";  printf '  %s\n' "${RELEASES[@]:-(none)}"
echo "Tags to delete (${#TAGS[@]}):";          printf '  %s\n' "${TAGS[@]:-(none)}"
echo "Branches to delete (${#BRANCHES[@]}):";  printf '  %s\n' "${BRANCHES[@]:-(none)}"
echo
if [ "${#RELEASES[@]}" -eq 0 ] && [ "${#TAGS[@]}" -eq 0 ] && [ "${#BRANCHES[@]}" -eq 0 ]; then echo "Nothing to delete."; exit 0; fi

# Make sure branches being deleted are already in main, so no work is lost.
for b in "${BRANCHES[@]}"; do
  if ! git merge-base --is-ancestor "origin/$b" origin/main 2>/dev/null; then
    echo "WARNING: branch '$b' has commits that are NOT in main. They'd be lost if deleted."
  fi
done

read -r -p "This can't be undone. Type yes to delete all of the above: " ok
[ "$ok" = "yes" ] || { echo "Cancelled."; exit 1; }

for r in "${RELEASES[@]}"; do gh release delete "$r" --repo "$REPO" --yes --cleanup-tag && echo "deleted release $r"; done
for t in "${TAGS[@]}"; do git push origin --delete "refs/tags/$t" 2>/dev/null && echo "deleted tag $t" || true; git tag -d "$t" >/dev/null 2>&1 || true; done
for b in "${BRANCHES[@]}"; do git push origin --delete "$b" && echo "deleted branch $b"; done

# Old workflow runs and their build artifacts (keeps runs for the kept version)
read -r -p "Also delete old Actions runs and build artifacts? Type yes: " ok2
if [ "$ok2" = "yes" ]; then
  gh run list --repo "$REPO" --limit 200 --json databaseId,headBranch -q ".[] | select(.headBranch != \"$KEEP\") | .databaseId" \
    | while read -r id; do gh run delete "$id" --repo "$REPO" >/dev/null 2>&1 && echo "deleted run $id"; done
fi
echo "Done."
