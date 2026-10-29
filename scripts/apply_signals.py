#!/usr/bin/env python3
"""Apply livez + CloudWatch-alarm signals into status.json (MKT-36).

Runs on GitHub Actions (off the TerranPage AWS origin). Reads the current
status.json, merges automated signals, writes the file back for commit.

The CloudWatch alarm signals come from SIGNAL_INBOX_DIR: one JSON file per
dispatch, written by scripts/record_signal.sh and handed over by
scripts/commit_status.sh. They are applied in dispatch order (run id), and
status.json remembers, per alarm, the order of the last signal it applied
(`signal_seq`), so a signal that reaches this script after a newer one for the
same alarm has already been applied is ignored instead of undoing it.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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

# How long status.json remembers the order of the last signal applied per alarm.
# It only has to outlast a dispatch that is recorded late (the record job has a
# few minutes at most); after that the entry is dropped so the file does not
# collect an entry for every alarm name it has ever seen.
SEQ_TTL = timedelta(hours=1)
SEQ_KEY = "signal_seq"

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
            body = resp.read(200).decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                return None
            # Some edge paths return a non-2xx with an explicit healthy body.
            if '"status":"ok"' in body.replace(" ", ""):
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
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(200).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        if '"status":"ok"' in body.replace(" ", ""):
            return None
        return {
            "id": "livez-probe",
            "alarm_name": "external-livez-probe",
            "component": "api",
            "component_name": COMPONENT_NAMES["api"],
            "state": "ALARM",
            "severity": "major_outage",
            "since": _now(),
            "summary": f"External /livez probe returned HTTP {exc.code}.",
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


def _signals_from_dispatch(
    payload: Any, seq: int, received_at: str
) -> list[dict[str, Any]]:
    """Signals of one recorded dispatch; each carries `seq`, its dispatch order."""
    if isinstance(payload, dict) and "alarms" in payload:
        items = payload["alarms"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        return []
    if not isinstance(items, list):
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
                "since": str(
                    item.get("since") or item.get("StateChangeTime") or received_at
                ),
                "summary": str(
                    item.get("summary")
                    or item.get("NewStateReason")
                    or f"CloudWatch alarm {name} is {state}."
                )[:500],
                "seq": seq,
            }
        )
    return out


def _load_inbox(directory: str) -> list[dict[str, Any]]:
    """Recorded dispatches in dispatch order (run id, then run attempt)."""
    entries: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            entries.append(
                {
                    "run_id": int(envelope["run_id"]),
                    "run_attempt": int(envelope.get("run_attempt") or 1),
                    "received_at": str(envelope.get("received_at") or _now()),
                    "payload": envelope.get("payload"),
                }
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            print(f"::warning::skipped unreadable signal {path.name}: {type(exc).__name__}")
    entries.sort(key=lambda e: (e["run_id"], e["run_attempt"]))
    return entries


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _live_marks(raw: Any, now: datetime) -> dict[str, dict[str, Any]]:
    """The `signal_seq` entries that are still inside SEQ_TTL and well formed."""
    if not isinstance(raw, dict):
        return {}
    marks: dict[str, dict[str, Any]] = {}
    for key, mark in raw.items():
        if not isinstance(mark, dict) or not isinstance(mark.get("seq"), int):
            continue
        at = _parse_time(mark.get("at"))
        if at is not None and now - at <= SEQ_TTL:
            marks[str(key)] = {"seq": mark["seq"], "at": mark["at"]}
    return marks


def _merge_signals(
    existing: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    marks: dict[str, dict[str, Any]],
    now: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply updates in order; return (signals, updates ignored as out of order).

    An update with a `seq` older than the last one applied for the same alarm is
    ignored; `marks` is updated in place with the order of what was applied.
    """
    by_id = {str(s.get("id") or s.get("alarm_name")): dict(s) for s in existing}
    ignored: list[dict[str, Any]] = []
    for update in updates:
        key = str(update.get("id") or update.get("alarm_name"))
        seq = update.get("seq")
        if seq is not None:
            mark = marks.get(key)
            if mark is not None and seq < mark["seq"]:
                ignored.append(update)
                continue
            marks[key] = {"seq": seq, "at": now}
        update = {k: v for k, v in update.items() if k != "seq"}
        if update.get("state") == "OK":
            by_id.pop(key, None)
            continue
        prev = by_id.get(key)
        if prev and prev.get("since") and not update.get("since"):
            update = {**update, "since": prev["since"]}
        by_id[key] = update
    # Drop non-ALARM leftovers.
    return [s for s in by_id.values() if s.get("state") == "ALARM"], ignored


def main() -> int:
    livez_url = os.environ.get("LIVEZ_URL", "https://terranpage.com/livez")
    inbox_dir = os.environ.get("SIGNAL_INBOX_DIR", "")

    data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    existing = list(data.get("signals") or [])
    now = _now()
    marks = _live_marks(data.get(SEQ_KEY), _parse_time(now))

    updates: list[dict[str, Any]] = []
    if inbox_dir:
        for entry in _load_inbox(inbox_dir):
            updates.extend(
                _signals_from_dispatch(
                    entry["payload"], entry["run_id"], entry["received_at"]
                )
            )
    dispatched = list(updates)

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

    data["signals"], ignored = _merge_signals(existing, updates, marks, now)
    if marks:
        data[SEQ_KEY] = marks
    else:
        data.pop(SEQ_KEY, None)
    data["updated_at"] = now
    STATUS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    ignored_ids = {id(u) for u in ignored}
    print(
        json.dumps(
            {
                "updated_at": data["updated_at"],
                "signal_count": len(data["signals"]),
                "signal_ids": [s.get("id") for s in data["signals"]],
                "applied": [
                    f"{u['alarm_name']} {u['state']} (run {u['seq']})"
                    for u in dispatched
                    if id(u) not in ignored_ids
                ],
                "ignored_as_older": [
                    f"{u['alarm_name']} {u['state']} (run {u['seq']})" for u in ignored
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
