#!/usr/bin/env bash
# Apply this run's signals to the newest status.json and push the result.
#
# Why apply and push are one loop: apply_signals.py computes status.json from
# (the file as it is now) + (this run's alarm payload and /livez probe). When
# the push is rejected, another run has changed status.json since this one
# read it, and replaying our commit on top (git pull --rebase) conflicts on
# `updated_at` and `signals` every time two runs touch the same lines. The
# commit is not worth keeping: drop it, take the new tip, and apply this run's
# signals to that file again. Every run's signals are therefore merged into the
# file that is on main when its push succeeds, in whatever order the pushes land.
#
# Environment (all optional): LIVEZ_URL and DISPATCH_CLIENT_PAYLOAD are read by
# apply_signals.py; REMOTE, BRANCH, MAX_ATTEMPTS and RETRY_SLEEP_MAX (seconds,
# 0 for none) are for the tests. The caller sets git user.name and user.email.
set -euo pipefail

remote=${REMOTE:-origin}
branch=${BRANCH:-main}
max_attempts=${MAX_ATTEMPTS:-10}
sleep_max=${RETRY_SLEEP_MAX:-3}

for attempt in $(seq 1 "$max_attempts"); do
  python3 scripts/apply_signals.py

  if git diff --quiet -- status.json; then
    echo "status.json unchanged"
    exit 0
  fi

  git add status.json
  git commit --quiet -m "chore(status): refresh automated signals"

  if git push --quiet "$remote" "HEAD:$branch"; then
    echo "pushed on attempt ${attempt}"
    exit 0
  fi

  echo "push rejected (attempt ${attempt}/${max_attempts}); applying the signals again on the new ${branch} tip"
  git fetch --quiet "$remote" "$branch"
  git reset --quiet --hard FETCH_HEAD
  if [ "$sleep_max" -gt 0 ]; then
    sleep $((RANDOM % sleep_max + 1))
  fi
done

echo "push failed after ${max_attempts} attempts" >&2
exit 1
