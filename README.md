# TerranPage public status

Public status page for **TerranPage**, hosted on **GitHub Pages** at
[`https://status.terranpage.com`](https://status.terranpage.com).

This repository is intentionally **outside** the product AWS stack so the status
site can stay reachable when the TerranPage origin is degraded.

## What this page shows

- **Components** — API and console, email delivery, sign-in
- **Incidents** — hand-posted updates in `status.json` (source of truth for customer messaging)
- **Automated signals** — CloudWatch alarm bridge + external `/livez` probe (no fabricated uptime % / SLA badges)

## Post a manual incident

Edit `status.json` on `main`:

```json
{
  "incidents": [
    {
      "id": "2026-07-20-example",
      "title": "Elevated API errors",
      "status": "investigating",
      "impact": "degraded",
      "components": ["api"],
      "started_at": "2026-07-20T18:00:00Z",
      "resolved_at": null,
      "summary": "We are investigating elevated 5xx responses on the API.",
      "updates": [
        {
          "at": "2026-07-20T18:10:00Z",
          "body": "Mitigation in progress; paging delivery is still attempting."
        }
      ]
    }
  ]
}
```

Set `resolved_at` when the incident ends. Keep copy factual — no invented SLAs.

## Automated signals

| Source | Path |
|---|---|
| External `/livez` probe | GitHub Action every 5 minutes |
| PRR-04 / PRR-07 / PRR-23 CloudWatch alarms | SNS → pager `status_page_hook` Lambda → `repository_dispatch` (`cloudwatch-alarm`) |

`scripts/apply_signals.py` merges those into `status.json` → `signals[]`.

### How an alarm gets from a dispatch to `status.json`

`.github/workflows/update-status.yml` has two jobs, because GitHub keeps only one
waiting run per concurrency group and cancels the older waiting one before any
of its steps run (a payload carried only by its own run is lost with it):

1. **`record`** (dispatch runs only, no lock) — `scripts/record_signal.sh` writes the
   payload to its own git ref, `refs/status-signals/<run id>-<attempt>`. Names are
   unique, so any number of runs record at once without contending for anything.
2. **`apply`** (every run, one at a time: concurrency group `status-signals`) —
   `scripts/commit_status.sh` reads *every* recorded ref, not just its own run's,
   applies them to the newest `status.json` in dispatch order together with the
   `/livez` probe, pushes, and **only after the push** deletes the refs it applied.

So a burst loses nothing: if a waiting `apply` is replaced (it shows as cancelled in
the Actions list), the job that replaced it starts after the running one and
applies the signals of all the runs before it. If an `apply` fails before its push,
the refs stay, and the next run (the 5-minute schedule at the latest, or a manual
**Run workflow**) applies them. Applying a signal twice changes nothing.

If the push is rejected because `status.json` changed in the meantime,
`commit_status.sh` takes the new tip, reads the refs again and applies them to
that file (a rebase would conflict on `updated_at` and `signals`).

Order is kept in the data, not in the queue: signals are applied by run id (the
order GitHub received the dispatches), and `status.json` holds
`signal_seq: {<alarm>: {seq, at}}` for the last signal applied per alarm. A signal
that reaches `apply` after a newer one for the same alarm (an `ALARM` whose record
job finished after the `OK` that followed it) is ignored, so it cannot bring a
cleared alarm back. Entries expire after an hour, and the key is absent when none
are left, so the file only carries it for a short time after an alarm changed.
The page does not read it.

`refs/status-signals/*` should be empty between runs; whatever is there is waiting
for the next `apply`. `git ls-remote origin 'refs/status-signals/*'` lists them.

`python3 -m unittest discover -s tests` runs both scripts against a local bare
repository: bursts where only one `apply` runs, an `apply` that fails, runs that
start behind the remote, and `ALARM`/`OK` in either arrival order.

## Local preview

Open `index.html` via any static file server from this directory (it fetches `./status.json`).
