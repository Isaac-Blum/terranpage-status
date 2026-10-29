"""scripts/commit_status.sh: signals from runs that overlap all end up in status.json.

update-status.yml runs one update at a time, but a queued run used to start
from the commit that was main when its event fired. If the run before it had
pushed in between, its push was rejected and `git pull --rebase` stopped on a
conflict in status.json, so its alarm never reached the page (run 40538905673,
2026-10-27, two dispatches 8 s apart). These tests build that situation with
a real bare repository: a checkout that is behind the remote when the script
runs, the same state a queued run starts in.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


class CommitStatusTest(unittest.TestCase):
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
        for name in ("apply_signals.py", "commit_status.sh"):
            shutil.copy(ROOT / "scripts" / name, seed / "scripts" / name)
        shutil.copy(ROOT / "status.json", seed / "status.json")
        data = json.loads((seed / "status.json").read_text())
        data["signals"] = []
        (seed / "status.json").write_text(json.dumps(data, indent=2) + "\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-m", "seed")
        _git(seed, "push", "origin", "HEAD:main")

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

    def _run(self, checkout: Path, payload: str, **extra: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "LIVEZ_URL": self.livez,
            "DISPATCH_CLIENT_PAYLOAD": payload,
            "RETRY_SLEEP_MAX": "0",
            **extra,
        }
        return subprocess.run(
            ["bash", "scripts/commit_status.sh"],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
        )

    def _remote_status(self) -> dict:
        return json.loads(_git(self.tmp, "--git-dir", str(self.remote), "show", "main:status.json"))

    def _remote_commits(self) -> list[str]:
        return _git(
            self.tmp, "--git-dir", str(self.remote), "log", "--format=%s", "--first-parent", "main"
        ).splitlines()

    def test_run_that_started_behind_the_remote_still_lands(self):
        first, second = self._clone("first"), self._clone("second")  # both at the same commit
        done = self._run(first, _alarm("pager-prod-http-api-5xx"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        late = self._run(second, _alarm("pager-prod-invoke-dlq-arrival-rate"))
        self.assertEqual(late.returncode, 0, late.stdout + late.stderr)
        self.assertIn("push rejected (attempt 1/", late.stdout)
        self.assertIn("pushed on attempt 2", late.stdout)
        ids = sorted(s["id"] for s in self._remote_status()["signals"])
        self.assertEqual(ids, ["pager-prod-http-api-5xx", "pager-prod-invoke-dlq-arrival-rate"])
        self.assertEqual(self._remote_commits().count("chore(status): refresh automated signals"), 2)

    def test_many_runs_started_from_one_commit_all_land(self):
        names = [f"pager-prod-http-api-5xx-{n}" for n in range(8)]
        checkouts = [self._clone(f"run{n}") for n in range(len(names))]  # all at the seed commit
        for checkout, name in zip(checkouts, names):
            result = self._run(checkout, _alarm(name))
            self.assertEqual(result.returncode, 0, name + result.stdout + result.stderr)
        self.assertEqual({s["id"] for s in self._remote_status()["signals"]}, set(names))

    def test_ok_after_alarm_clears_it_when_applied_in_order(self):
        name = "pager-prod-http-api-5xx"
        first, second = self._clone("first"), self._clone("second")
        self.assertEqual(self._run(first, _alarm(name)).returncode, 0)
        self.assertEqual(self._run(second, _alarm(name, "OK")).returncode, 0)
        self.assertEqual(self._remote_status()["signals"], [])

    def test_hand_posted_incident_pushed_in_between_is_kept(self):
        runner, editor = self._clone("runner"), self._clone("editor")
        path = editor / "status.json"
        data = json.loads(path.read_text())
        data["incidents"] = [{"id": "2026-10-27-example", "title": "Example", "status": "investigating"}]
        path.write_text(json.dumps(data, indent=2) + "\n")
        _git(editor, "commit", "--quiet", "-am", "post incident")
        _git(editor, "push", "--quiet", "origin", "HEAD:main")
        result = self._run(runner, _alarm("pager-prod-http-api-5xx"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self._remote_status()
        self.assertEqual([i["id"] for i in status["incidents"]], ["2026-10-27-example"])
        self.assertEqual([s["id"] for s in status["signals"]], ["pager-prod-http-api-5xx"])

    def test_fails_loudly_when_the_push_never_succeeds(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
        hook.chmod(0o755)
        checkout = self._clone("blocked")
        result = self._run(checkout, _alarm("pager-prod-http-api-5xx"), MAX_ATTEMPTS="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("push failed after 3 attempts", result.stderr)
        self.assertEqual(result.stdout.count("push rejected"), 3)


if __name__ == "__main__":
    unittest.main()
