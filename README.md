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

## Local preview

Open `index.html` via any static file server from this directory (it fetches `./status.json`).
