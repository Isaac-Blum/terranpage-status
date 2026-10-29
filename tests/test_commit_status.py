"""scripts/record_signal.sh + scripts/commit_status.sh: every dispatched signal lands.

update-status.yml records each repository_dispatch payload as its own git ref
(record_signal.sh, no lock) and applies all recorded signals to status.json, one
apply at a time (commit_status.sh). GitHub's concurrency group cancels all but
one waiting run, so in a burst only some runs ever get to apply; these tests
build that with a real bare repository: any number of runs record, a few apply,
and every signal must still end up in status.json, in dispatch order, with the
refs removed only after the push.

History: run 40538905673 (2026-10-27, two dispatches 8 s apart) lost its alarm
because a queued run started from a stale checkout and its push was rejected;
and a third dispatch behind a running and a waiting one cancels the waiting run
before it can do anything (TP-31).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN0 = 40_800_000_000  # a plausible GitHub run id; later runs have larger ids
NS = "refs/status-signals"


class _Healthy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _alarm(name: str, state: str = "ALARM") -> str:
    return json.dumps({"alarms": [{"alarm_name": name, "state": state, "summary": "test"}]})


def _name(n: int, suffix: str = "http-api-5xx") -> str:
    return f"pager-prod-test-{n}-{suffix}"


class StatusSignalsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Healthy)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.livez = f"http://127.0.0.1:{cls.server.server_address[1]}/livez"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = self.tmp / "remote.git"
        _git(self.tmp, "init", "--bare", "--initial-branch=main", str(self.remote))
        seed = self._clone("seed")
        (seed / "scripts").mkdir()
        for name in ("apply_signals.py", "commit_status.sh", "record_signal.sh"):
            shutil.copy(ROOT / "scripts" / name, seed / "scripts" / name)
        shutil.copy(ROOT / "status.json", seed / "status.json")
        data = json.loads((seed / "status.json").read_text())
        data["signals"] = []
        data.pop("signal_seq", None)
        (seed / "status.json").write_text(json.dumps(data, indent=2) + "\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-m", "seed")
        _git(seed, "push", "origin", "HEAD:main")
        self.seed_keys = set(data)

    def _clone(self, name: str) -> Path:
        path = self.tmp / name
        subprocess.run(
            ["git", "clone", "--quiet", str(self.remote), str(path)],
            check=True,
            capture_output=True,
        )
        _git(path, "config", "user.name", "test")
        _git(path, "config", "user.email", "test@example.invalid")
        _git(path, "checkout", "--quiet", "-B", "main")
        return path

    def _env(self, **extra: str) -> dict[str, str]:
        return {**os.environ, "LIVEZ_URL": self.livez, "RETRY_SLEEP_MAX": "0", **extra}

    def _record(
        self, checkout: Path, payload: str, n: int, attempt: int = 1, **extra: str
    ) -> subprocess.CompletedProcess:
        """What the `record` job does for dispatch run number n."""
        return subprocess.run(
            ["bash", "scripts/record_signal.sh"],
            cwd=checkout,
            env=self._env(
                GITHUB_RUN_ID=str(RUN0 + n),
                GITHUB_RUN_ATTEMPT=str(attempt),
                DISPATCH_CLIENT_PAYLOAD=payload,
                **extra,
            ),
            capture_output=True,
            text=True,
        )

    def _apply(self, checkout: Path, **extra: str) -> subprocess.CompletedProcess:
        """What the `apply` job does (also schedule and manual runs)."""
        return subprocess.run(
            ["bash", "scripts/commit_status.sh"],
            cwd=checkout,
            env=self._env(**extra),
            capture_output=True,
            text=True,
        )

    def _ok(self, result: subprocess.CompletedProcess, label: str = "") -> None:
        self.assertEqual(result.returncode, 0, label + result.stdout + result.stderr)

    def _remote_status(self) -> dict:
        return json.loads(_git(self.tmp, "--git-dir", str(self.remote), "show", "main:status.json"))

    def _write_remote_status(self, data: dict) -> None:
        editor = self._clone("editor")
        (editor / "status.json").write_text(json.dumps(data, indent=2) + "\n")
        _git(editor, "commit", "--quiet", "-am", "edit status.json")
        _git(editor, "push", "--quiet", "origin", "HEAD:main")

    def _remote_commits(self) -> list[str]:
        return _git(
            self.tmp, "--git-dir", str(self.remote), "log", "--format=%s", "--first-parent", "main"
        ).splitlines()

    def _inbox(self) -> list[str]:
        out = _git(self.tmp, "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)", NS)
        return out.splitlines()

    def _block_pushes_to(self, ref_prefix: str) -> None:
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read old new ref; do\n"
            f'  case "$ref" in {ref_prefix}*) echo refused >&2; exit 1;; esac\n'
            "done\n"
        )
        hook.chmod(0o755)

    def _unblock_pushes(self) -> None:
        (self.remote / "hooks" / "pre-receive").unlink()

    # -- the burst: runs that never get to apply -------------------------------

    def test_burst_where_only_one_apply_runs_lands_every_signal(self):
        # GitHub cancels all but one waiting run: 12 dispatches record, and only
        # one apply job ever runs. Its payload alone would be one of the twelve.
        total = 12
        checkouts = [self._clone(f"run{n}") for n in range(total)]
        results: list[subprocess.CompletedProcess | None] = [None] * total

        def record(n: int) -> None:
            results[n] = self._record(checkouts[n], _alarm(_name(n)), n)

        threads = [threading.Thread(target=record, args=(n,)) for n in range(total)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for n, result in enumerate(results):
            self._ok(result, f"record {n}: ")
        self.assertEqual(len(self._inbox()), total)

        self._ok(self._apply(self._clone("applier")))

        ids = {s["id"] for s in self._remote_status()["signals"]}
        self.assertEqual(ids, {_name(n) for n in range(total)})
        self.assertEqual(self._inbox(), [])
        self.assertEqual(self._remote_commits().count("chore(status): refresh automated signals"), 1)

    def test_signal_recorded_while_an_apply_is_running_waits_for_the_next_one(self):
        first = self._clone("first")
        self._ok(self._record(self._clone("r1"), _alarm(_name(1)), 1))
        self._ok(self._apply(first))
        self._ok(self._record(self._clone("r2"), _alarm(_name(2)), 2))
        self.assertEqual(self._inbox(), [f"{NS}/{RUN0 + 2}-1"])
        self._ok(self._apply(self._clone("second")))
        self.assertEqual(
            sorted(s["id"] for s in self._remote_status()["signals"]), [_name(1), _name(2)]
        )

    def test_apply_that_fails_leaves_the_signals_for_the_next_run(self):
        for n in range(3):
            self._ok(self._record(self._clone(f"r{n}"), _alarm(_name(n)), n))
        self._block_pushes_to("refs/heads/")
        failed = self._apply(self._clone("blocked"), MAX_ATTEMPTS="2")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(len(self._inbox()), 3)
        self.assertEqual(self._remote_status()["signals"], [])
        self._unblock_pushes()
        # A schedule run (no payload of its own) picks them up.
        self._ok(self._apply(self._clone("schedule")))
        self.assertEqual(len(self._remote_status()["signals"]), 3)
        self.assertEqual(self._inbox(), [])

    def test_inbox_is_removed_only_after_the_push(self):
        self._ok(self._record(self._clone("r"), _alarm(_name(1)), 1))
        self._block_pushes_to("refs/heads/")
        self.assertEqual(self._apply(self._clone("blocked"), MAX_ATTEMPTS="1").returncode, 1)
        self.assertEqual(self._inbox(), [f"{NS}/{RUN0 + 1}-1"])

    # -- the push race that PR #6 fixed still holds ----------------------------

    def test_run_that_started_behind_the_remote_still_lands(self):
        first, second = self._clone("first"), self._clone("second")  # both at the same commit
        self._ok(self._record(self._clone("r1"), _alarm("pager-prod-http-api-5xx"), 1))
        self._ok(self._apply(first))
        self._ok(self._record(self._clone("r2"), _alarm("pager-prod-invoke-dlq-arrival-rate"), 2))
        late = self._apply(second)
        self._ok(late)
        self.assertIn("push rejected (attempt 1/", late.stdout)
        self.assertIn("pushed on attempt 2", late.stdout)
        ids = sorted(s["id"] for s in self._remote_status()["signals"])
        self.assertEqual(ids, ["pager-prod-http-api-5xx", "pager-prod-invoke-dlq-arrival-rate"])
        self.assertEqual(self._remote_commits().count("chore(status): refresh automated signals"), 2)

    def test_many_stale_checkouts_each_apply_their_own_signal(self):
        names = [_name(n) for n in range(8)]
        checkouts = [self._clone(f"run{n}") for n in range(len(names))]  # all at the seed commit
        for n, checkout in enumerate(checkouts):
            self._ok(self._record(self._clone(f"rec{n}"), _alarm(names[n]), n), f"record {n}: ")
            self._ok(self._apply(checkout), f"apply {n}: ")
        self.assertEqual({s["id"] for s in self._remote_status()["signals"]}, set(names))

    def test_hand_posted_incident_pushed_in_between_is_kept(self):
        runner = self._clone("runner")
        data = self._remote_status()
        data["incidents"] = [{"id": "2026-10-27-example", "title": "Example", "status": "investigating"}]
        self._write_remote_status(data)
        self._ok(self._record(self._clone("r"), _alarm("pager-prod-http-api-5xx"), 1))
        self._ok(self._apply(runner))
        status = self._remote_status()
        self.assertEqual([i["id"] for i in status["incidents"]], ["2026-10-27-example"])
        self.assertEqual([s["id"] for s in status["signals"]], ["pager-prod-http-api-5xx"])

    def test_fails_loudly_when_the_push_never_succeeds(self):
        self._ok(self._record(self._clone("r"), _alarm("pager-prod-http-api-5xx"), 1))
        self._block_pushes_to("refs/heads/")
        result = self._apply(self._clone("blocked"), MAX_ATTEMPTS="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("push failed after 3 attempts", result.stderr)
        self.assertEqual(result.stdout.count("push rejected"), 3)

    # -- ordering: an OK after an ALARM clears it, whatever finishes first -----

    def test_ok_after_alarm_clears_it_in_one_apply(self):
        name = "pager-prod-http-api-5xx"
        # recorded in the "wrong" order; applied in dispatch (run id) order
        self._ok(self._record(self._clone("r2"), _alarm(name, "OK"), 2))
        self._ok(self._record(self._clone("r1"), _alarm(name), 1))
        applied = self._apply(self._clone("applier"))
        self._ok(applied)
        self.assertEqual(self._remote_status()["signals"], [])
        self.assertIn(f"{name} ALARM (run {RUN0 + 1})", applied.stdout)
        self.assertIn(f"{name} OK (run {RUN0 + 2})", applied.stdout)

    def test_alarm_recorded_after_a_newer_ok_was_applied_does_not_come_back(self):
        name = "pager-prod-http-api-5xx"
        self._ok(self._record(self._clone("r2"), _alarm(name, "OK"), 2))
        self._ok(self._apply(self._clone("a1")))
        # run 1's record job finished late; its apply runs after run 2's
        self._ok(self._record(self._clone("r1"), _alarm(name), 1))
        late = self._apply(self._clone("a2"))
        self._ok(late)
        self.assertEqual(self._remote_status()["signals"], [])
        self.assertIn(f"{name} ALARM (run {RUN0 + 1})", late.stdout)
        self.assertEqual(self._inbox(), [])

    def test_alarm_after_ok_in_dispatch_order_is_shown(self):
        name = "pager-prod-http-api-5xx"
        self._ok(self._record(self._clone("r1"), _alarm(name, "OK"), 1))
        self._ok(self._apply(self._clone("a1")))
        self._ok(self._record(self._clone("r2"), _alarm(name), 2))
        self._ok(self._apply(self._clone("a2")))
        self.assertEqual([s["id"] for s in self._remote_status()["signals"]], [name])

    def test_ok_for_one_alarm_does_not_hold_back_another(self):
        self._ok(self._record(self._clone("r2"), _alarm(_name(1), "OK"), 2))
        self._ok(self._apply(self._clone("a1")))
        self._ok(self._record(self._clone("r1"), _alarm(_name(2)), 1))  # older run, other alarm
        self._ok(self._apply(self._clone("a2")))
        self.assertEqual([s["id"] for s in self._remote_status()["signals"]], [_name(2)])

    # -- what status.json looks like -------------------------------------------

    def test_status_json_has_no_extra_keys_without_dispatches(self):
        self._ok(self._apply(self._clone("schedule")))
        self.assertEqual(set(self._remote_status()), self.seed_keys)

    def test_order_marks_expire_so_the_file_does_not_collect_alarm_names(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._remote_status()
        data["signal_seq"] = {
            "gone": {"seq": 1, "at": old},
            "kept": {"seq": 2, "at": fresh},
            "malformed": "x",
        }
        self._write_remote_status(data)
        self._ok(self._apply(self._clone("schedule")))
        self.assertEqual(list(self._remote_status()["signal_seq"]), ["kept"])

    def test_signal_fields_are_the_ones_the_page_reads(self):
        self._ok(self._record(self._clone("r"), _alarm("pager-prod-http-api-5xx"), 1))
        self._ok(self._apply(self._clone("applier")))
        (signal,) = self._remote_status()["signals"]
        self.assertEqual(
            set(signal),
            {"id", "alarm_name", "component", "component_name", "state", "severity", "since", "summary"},
        )
        self.assertEqual((signal["component"], signal["severity"]), ("api", "major_outage"))

    def test_signal_without_a_timestamp_gets_the_time_it_was_recorded(self):
        self._ok(self._record(self._clone("r"), _alarm("pager-prod-http-api-5xx"), 1))
        recorded = json.loads(
            _git(self.tmp, "--git-dir", str(self.remote), "show", f"{NS}/{RUN0 + 1}-1:signal.json")
        )
        self._ok(self._apply(self._clone("applier")))
        (signal,) = self._remote_status()["signals"]
        self.assertEqual(signal["since"], recorded["received_at"])

    # -- record_signal.sh ------------------------------------------------------

    def test_record_without_a_payload_writes_nothing(self):
        checkout = self._clone("r")
        for payload in ("", "null"):
            result = self._record(checkout, payload, 1)
            self._ok(result)
            self.assertIn("nothing to record", result.stdout)
        self.assertEqual(self._inbox(), [])

    def test_record_twice_for_one_run_is_one_signal(self):
        checkout = self._clone("r")
        self._ok(self._record(checkout, _alarm(_name(1)), 1))
        self._ok(self._record(checkout, _alarm(_name(1)), 1))  # a retried step
        self.assertEqual(self._inbox(), [f"{NS}/{RUN0 + 1}-1"])

    def test_record_fails_loudly_when_the_ref_cannot_be_pushed(self):
        self._block_pushes_to(NS)
        result = self._record(self._clone("r"), _alarm(_name(1)), 1, MAX_ATTEMPTS="2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not record", result.stderr)

    def test_unreadable_recorded_signal_is_reported_and_does_not_block_the_rest(self):
        self._ok(self._record(self._clone("r"), _alarm(_name(1)), 1))
        junk = self._clone("junk")
        _git(junk, "push", "--quiet", "origin", f"HEAD:{NS}/1-1")  # a commit with no signal.json
        applied = self._apply(self._clone("applier"))
        self._ok(applied)
        self.assertIn("has no signal.json", applied.stdout)
        self.assertEqual([s["id"] for s in self._remote_status()["signals"]], [_name(1)])
        self.assertEqual(self._inbox(), [])


if __name__ == "__main__":
    unittest.main()
