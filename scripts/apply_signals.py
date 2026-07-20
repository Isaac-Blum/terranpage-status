#!/usr/bin/env python3
"""Apply livez + CloudWatch-alarm signals into status.json (MKT-36).

Runs on GitHub Actions (off the TerranPage AWS origin). Reads the current
status.json, merges automated signals, writes the file back for commit.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "status.json"

# Alarm name fragments → (component_id, severity_when_ALARM)
ALARM_MAP: list[tuple[str, str, str]] = [
    ("invocation-heartbeat", "api", "major_outage"),
    ("sweep-invocation-heartbeat", "api", "major_outage"),
    ("sweep-heartbeat", "api", "degraded"),
    ("http-api-5xx", "api", "major_outage"),
    ("http-api-latency", "api", "degraded"),
    ("system-errors", "api", "major_outage"),
    ("throttled-requests", "api", "degraded"),
    ("invoke-dlq", "api", "degraded"),
    ("duration-near-timeout", "api", "degraded"),
    ("ses-reputation-bounce", "email_delivery", "degraded"),
    ("ses-reputation-complaint", "email_delivery", "degraded"),
    ("receipt-undeliverable", "email_delivery", "degraded"),
]

COMPONENT_NAMES = {
    "api": "API and console",
    "email_delivery": "Email delivery",
    "sign_in": "Sign-in",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _probe_livez(url: str) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return None
            return {
                "id": "livez-probe",
                "alarm_name": "external-livez-probe",
                "component": "api",
                "component_name": COMPONENT_NAMES["api"],
                "state": "ALARM",
                "severity": "major_outage",
                "since": _now(),
                "summary": f"External /livez probe returned HTTP {resp.status}.",
            }
    except Exception as exc:  # noqa: BLE001 — surface any probe failure as signal
        return {
            "id": "livez-probe",
            "alarm_name": "external-livez-probe",
            "component": "api",
            "component_name": COMPONENT_NAMES["api"],
            "state": "ALARM",
            "severity": "major_outage",
            "since": _now(),
            "summary": f"External /livez probe failed: {type(exc).__name__}.",
        }


def _map_alarm(name: str) -> tuple[str, str] | None:
    lowered = name.lower()
    for fragment, component, severity in ALARM_MAP:
        if fragment in lowered:
            return component, severity
    return None


def _signals_from_dispatch(raw: str) -> list[dict[str, Any]]:
    if not raw or raw == "null":
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict) and "alarms" in payload:
        items = payload["alarms"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("alarm_name") or item.get("AlarmName") or "")
        state = str(item.get("state") or item.get("NewStateValue") or "").upper()
        mapped = _map_alarm(name)
        if not mapped:
            continue
        component, severity = mapped
        if state not in {"ALARM", "OK"}:
            continue
        out.append(
            {
                "id": name,
                "alarm_name": name,
                "component": component,
                "component_name": COMPONENT_NAMES.get(component, component),
                "state": state,
                "severity": severity,
                "since": str(item.get("since") or item.get("StateChangeTime") or _now()),
                "summary": str(
                    item.get("summary")
                    or item.get("NewStateReason")
                    or f"CloudWatch alarm {name} is {state}."
                )[:500],
            }
        )
    return out


def _merge_signals(
    existing: list[dict[str, Any]], updates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(s.get("id") or s.get("alarm_name")): dict(s) for s in existing}
    for update in updates:
        key = str(update.get("id") or update.get("alarm_name"))
        if update.get("state") == "OK":
            by_id.pop(key, None)
            continue
        prev = by_id.get(key)
        if prev and prev.get("since") and not update.get("since"):
            update = {**update, "since": prev["since"]}
        by_id[key] = update
    # Drop non-ALARM leftovers.
    return [s for s in by_id.values() if s.get("state") == "ALARM"]


def main() -> int:
    livez_url = os.environ.get("LIVEZ_URL", "https://terranpage.com/livez")
    dispatch_raw = os.environ.get("DISPATCH_CLIENT_PAYLOAD", "")

    data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    existing = list(data.get("signals") or [])
    updates = _signals_from_dispatch(dispatch_raw)

    livez_signal = _probe_livez(livez_url)
    if livez_signal is None:
        updates.append(
            {
                "id": "livez-probe",
                "alarm_name": "external-livez-probe",
                "state": "OK",
            }
        )
    else:
        updates.append(livez_signal)

    data["signals"] = _merge_signals(existing, updates)
    data["updated_at"] = _now()
    STATUS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "updated_at": data["updated_at"],
                "signal_count": len(data["signals"]),
                "signal_ids": [s.get("id") for s in data["signals"]],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
