#!/usr/bin/env bash
# Runs inside the GitHub job (.github/workflows/history.yml).
# GitHub starts timed jobs late or not at all (often only a few times a day), so instead one run stays
# up for about 5.5 hours and records the spot price itself at about 2 minutes past every hour; the
# workflow then starts the next run. The first record is made straight away.
set -u
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

stop=$(( $(date +%s) + 340 * 60 ))     # leave time before GitHub's 6-hour limit
first=1
while :; do
  if [ "$first" -eq 0 ]; then
    now=$(date +%s)
    next=$(( (now / 3600 + 1) * 3600 + 120 ))    # next hour, 2 minutes past
    [ "$next" -gt "$stop" ] && break
    sleep $(( next - now ))
  fi
  first=0
  git pull --rebase -q || true                   # pick up any changes pushed meanwhile
  # 'daily' also fetches yesterday's daily history once a day (it skips when already stored).
  python scripts/update_history.py daily || echo "update failed; trying again next hour"
  if git diff --quiet -- history.json; then
    echo "history.json unchanged"
    continue
  fi
  git add history.json
  git commit -q -m "Gold history $(date -u +%Y-%m-%dT%H:%MZ)"
  git pull --rebase -q || git rebase --abort
  git push -q || echo "push failed; will push with the next record"
  # Pushes made with the workflow token may not start a Pages build on their own.
  curl -sS -o /dev/null -X POST -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$GITHUB_REPOSITORY/pages/builds" || true
done
echo "collector finished"
