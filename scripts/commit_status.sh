#!/usr/bin/env bash
# Apply every recorded signal to the newest status.json and push the result.
#
# Where the signals come from: scripts/record_signal.sh leaves one ref per
# dispatch under refs/status-signals/. This script reads all of them, applies
# them to status.json in dispatch order together with the /livez probe, pushes,
# and only then deletes the refs it applied. Whichever run holds the lock
# therefore applies the signals of every run that was cancelled while it waited,
# and a run that fails before its push leaves the refs for the next run (the
# schedule runs every 5 minutes). A signal is gone only after it is on main.
#
# Why apply and push are one loop: apply_signals.py computes status.json from
# (the file as it is now) + (the recorded signals and the /livez probe). When
# the push is rejected, someone else has changed status.json since this run read
# it, and replaying our commit on top (git pull --rebase) conflicts on
# `updated_at` and `signals` every time. The commit is not worth keeping: drop
# it, take the new tip, read the refs again, and apply them to that file.
#
# Applying a signal twice is harmless (the second time changes nothing), and
# apply_signals.py ignores a signal that is older than what status.json already
# applied for the same alarm, so a ref that outlives its push is not a problem.
#
# Environment (all optional): LIVEZ_URL is read by apply_signals.py; REMOTE,
# BRANCH, SIGNAL_REF_NAMESPACE, MAX_ATTEMPTS and RETRY_SLEEP_MAX (seconds, 0 for
# none) are for the tests. The caller sets git user.name and user.email.
set -euo pipefail

remote=${REMOTE:-origin}
branch=${BRANCH:-main}
ns=${SIGNAL_REF_NAMESPACE:-refs/status-signals}
max_attempts=${MAX_ATTEMPTS:-10}
sleep_max=${RETRY_SLEEP_MAX:-3}

inbox=$(mktemp -d)
trap 'rm -rf "$inbox"' EXIT
export SIGNAL_INBOX_DIR=$inbox
consumed=()

# Copy every recorded signal into $inbox and remember its ref.
read_inbox() {
  rm -f "$inbox"/*.json
  consumed=()
  # --prune: a ref another run deleted since the last attempt is not ours any more.
  git fetch --quiet --prune "$remote" "+${ns}/*:${ns}/*"
  local ref
  while IFS= read -r ref; do
    if ! git show "${ref}:signal.json" > "$inbox/${ref##*/}.json" 2>/dev/null; then
      # Nothing in it can be applied; consuming it keeps it from blocking the rest.
      echo "::warning::${ref} has no signal.json; discarding it"
      rm -f "$inbox/${ref##*/}.json"
    fi
    consumed+=("$ref")
  done < <(git for-each-ref --format='%(refname)' "$ns")
  echo "inbox: ${#consumed[@]} recorded signal(s)"
}

# Delete the refs that were applied (all in one atomic push, skipping any that
# are already gone).
drain_inbox() {
  if [ "${#consumed[@]}" -eq 0 ]; then
    return 0
  fi
  local ref present args
  for _ in 1 2 3; do
    present=$(git ls-remote "$remote" "${ns}/*" | awk '{print $2}')
    args=()
    for ref in "${consumed[@]}"; do
      if grep -qxF "$ref" <<<"$present"; then
        args+=(":${ref}")
      fi
    done
    if [ "${#args[@]}" -eq 0 ]; then
      return 0
    fi
    if git push --quiet --atomic "$remote" "${args[@]}"; then
      echo "applied and removed ${#args[@]} recorded signal(s)"
      return 0
    fi
  done
  echo "status.json is pushed, but the applied signals could not be removed; the next run applies them again (harmless)" >&2
  return 1
}

for attempt in $(seq 1 "$max_attempts"); do
  read_inbox
  python3 scripts/apply_signals.py

  if git diff --quiet -- status.json; then
    echo "status.json unchanged"
    drain_inbox
    exit 0
  fi

  git add status.json
  git commit --quiet -m "chore(status): refresh automated signals"

  if git push --quiet "$remote" "HEAD:$branch"; then
    echo "pushed on attempt ${attempt}"
    drain_inbox
    exit 0
  fi

  echo "push rejected (attempt ${attempt}/${max_attempts}); applying the signals again on the new ${branch} tip"
  git fetch --quiet "$remote" "$branch"
  git reset --quiet --hard FETCH_HEAD
  if [ "$sleep_max" -gt 0 ]; then
    sleep $((RANDOM % sleep_max + 1))
  fi
done

echo "push failed after ${max_attempts} attempts; the recorded signals stay for the next run" >&2
exit 1
