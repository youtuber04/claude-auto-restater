import json
import os
import tempfile
import unittest

import session_hook as sh
from test_otsukare_usage import run_cli

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_hook.py")

SID = "11111111-2222-3333-4444-555555555555"

CHECKPOINT = """# otsukare checkpoint — demo-task

- Goal: refactor the auth module end to end
- Session ID: {sid}
- Task slug: demo-task
- Absolute cwd: /tmp/repo
- Branch: main
- WIP SHA: abc123
- Binding limit: five_hour, resets_at {epoch} (local time)
- Resume target: {target} (local time)
- Safety-net cron job ID: job-1
- Outstanding background agents: none

## Done
- step 1

## Next
- step 2
"""


def make_state(tmp, sid=SID, epoch=1_751_850_000, complete=False, heartbeat_age=None):
    """Write a checkpoint (and optionally a heartbeat aged heartbeat_age seconds,
    relative to NOW=2_000_000_000 used across these tests) into tmp."""
    text = CHECKPOINT.format(sid=sid, epoch=epoch, target=epoch + 600)
    if complete:
        text = "STATUS: complete\n" + text
    path = os.path.join(tmp, "{}-demo-task.md".format(sid))
    with open(path, "w") as f:
        f.write(text)
    if heartbeat_age is not None:
        hb = os.path.join(tmp, sid + ".heartbeat")
        open(hb, "w").close()
        old = NOW - heartbeat_age
        os.utime(hb, (old, old))
    return path


NOW = 2_000_000_000


class TestFindOrphans(unittest.TestCase):
    def test_dead_heartbeat_is_orphan(self):
        tmp = tempfile.mkdtemp()
        path = make_state(tmp, heartbeat_age=3600)
        orphans = sh.find_orphans(tmp, now=NOW)
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["checkpoint"], path)
        self.assertEqual(orphans[0]["slug"], "demo-task")
        self.assertEqual(orphans[0]["binding"], 1_751_850_000)

    def test_missing_heartbeat_is_orphan(self):
        tmp = tempfile.mkdtemp()
        make_state(tmp)                                    # no heartbeat at all
        self.assertEqual(len(sh.find_orphans(tmp, now=NOW)), 1)

    def test_live_heartbeat_is_not_orphan(self):
        # another session is actively running this checkpoint's task
        tmp = tempfile.mkdtemp()
        make_state(tmp, heartbeat_age=60)
        self.assertEqual(sh.find_orphans(tmp, now=NOW), [])

    def test_completed_checkpoint_is_skipped(self):
        tmp = tempfile.mkdtemp()
        make_state(tmp, complete=True, heartbeat_age=3600)
        self.assertEqual(sh.find_orphans(tmp, now=NOW), [])

    def test_missing_state_dir_is_empty(self):
        self.assertEqual(sh.find_orphans("/no/such/dir", now=NOW), [])

    def test_garbage_checkpoint_is_skipped(self):
        # unparseable file (no Session ID line) must be skipped, not crash
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "junk.md"), "wb") as f:
            f.write(b"\xff\xfe not a checkpoint")
        self.assertEqual(sh.find_orphans(tmp, now=NOW), [])


class TestBuildContext(unittest.TestCase):
    def test_carries_resume_prompt_and_path(self):
        tmp = tempfile.mkdtemp()
        path = make_state(tmp, heartbeat_age=3600)
        msg = sh.build_context(sh.find_orphans(tmp, now=NOW), now=NOW)
        self.assertIn(path, msg)
        self.assertIn("otsukare resume mode. Checkpoint: " + path, msg)
        self.assertIn("1751850000", msg)                   # binding epoch, verbatim
        self.assertIn("demo-task", msg)


class TestHookCli(unittest.TestCase):
    def run_hook(self, state_dir):
        return run_cli(["--state-dir", state_dir, "--now", str(NOW)], script=_SCRIPT)

    def test_emits_session_start_hook_json_for_orphan(self):
        tmp = tempfile.mkdtemp()
        path = make_state(tmp, heartbeat_age=3600)
        res = self.run_hook(tmp)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn(path, out["additionalContext"])

    def test_silent_when_nothing_to_report(self):
        res = self.run_hook(tempfile.mkdtemp())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "")

    def test_silent_zero_exit_on_missing_dir(self):
        # a session start must never be blocked or noised by this hook
        res = self.run_hook("/no/such/dir")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
