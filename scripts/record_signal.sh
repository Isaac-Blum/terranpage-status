#!/usr/bin/env bash
# Persist this run's repository_dispatch payload as its own git ref, then stop.
#
# GitHub's concurrency group keeps one waiting run: when a third dispatch arrives
# while one run is going and one is waiting, it cancels the waiting run, and a
# cancelled run never executes a step. So the payload cannot live only in the run
# that carries it. This script is the first job of every dispatch run, outside
# any concurrency group, and it writes the payload somewhere that survives the
# run: refs/status-signals/<run_id>-<attempt>, a commit holding signal.json.
# The name is unique per run, so concurrent runs never contend for a ref (no
# non-fast-forward, no retry storm), and scripts/commit_status.sh, which runs one
# at a time, applies every ref it finds and then deletes them.
#
# Environment: GITHUB_RUN_ID and DISPATCH_CLIENT_PAYLOAD (the workflow sets both);
# GITHUB_RUN_ATTEMPT; REMOTE, SIGNAL_REF_NAMESPACE, MAX_ATTEMPTS and
# RETRY_SLEEP_MAX (seconds, 0 for none) for the tests. The caller sets git
# user.name and user.email.
set -euo pipefail

remote=${REMOTE:-origin}
ns=${SIGNAL_REF_NAMESPACE:-refs/status-signals}
run_id=${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}
run_attempt=${GITHUB_RUN_ATTEMPT:-1}
max_attempts=${MAX_ATTEMPTS:-5}
sleep_step=${RETRY_SLEEP_MAX:-2}

# run_id orders the signals (GitHub numbers runs as it receives the events), so
# the envelope carries it; received_at stands in for a signal with no timestamp.
envelope=$(python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

raw = os.environ.get("DISPATCH_CLIENT_PAYLOAD", "")
payload = json.loads(raw) if raw.strip() else None
if payload is None:
    raise SystemExit(0)
print(
    json.dumps(
        {
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT") or 1),
            "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "payload": payload,
        },
        sort_keys=True,
    )
)
PY
)

if [ -z "$envelope" ]; then
  echo "dispatch carried no client_payload; nothing to record"
  exit 0
fi

ref="${ns}/${run_id}-${run_attempt}"
blob=$(printf '%s\n' "$envelope" | git hash-object -w --stdin)
tree=$(printf '100644 blob %s\tsignal.json\n' "$blob" | git mktree)
commit=$(git commit-tree "$tree" -m "status signal ${run_id}-${run_attempt}")

for attempt in $(seq 1 "$max_attempts"); do
  # --force: the name is this run's own. A retry after a push that the server
  # accepted but the runner never heard back about replaces the ref with the
  # same payload instead of failing as a non-fast-forward.
  if git push --quiet --force "$remote" "${commit}:${ref}"; then
    echo "recorded ${ref}"
    exit 0
  fi
  echo "recording ${ref} failed (attempt ${attempt}/${max_attempts})"
  if [ "$sleep_step" -gt 0 ]; then
    sleep $((sleep_step * attempt))
  fi
done

echo "could not record ${ref} after ${max_attempts} attempts" >&2
exit 1
