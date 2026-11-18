import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup as su  # noqa: E402


class TestApply(unittest.TestCase):
    def test_fresh_settings_get_statusline_and_hook(self):
        s = {}
        changed = su.apply(s, py="C:\\py\\python.exe")
        self.assertEqual(changed, ["statusLine", "hooks.SessionStart"])
        self.assertEqual(s["statusLine"]["type"], "command")
        self.assertIn("statusline.py", s["statusLine"]["command"])
        self.assertTrue(s["statusLine"]["command"].startswith('"C:\\py\\python.exe" -X utf8 "'))
        self.assertEqual(s["statusLine"]["refreshInterval"], 30)
        self.assertTrue(su.our_hook_present(s))

    def test_idempotent_and_keeps_other_settings(self):
        s = {"model": "opus", "statusLine": {"type": "command", "command": "old", "padding": 1}}
        su.apply(s, py="py")
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["statusLine"]["padding"], 1)      # 다른 키는 보존
        self.assertEqual(su.apply(s, py="py"), [])            # 두 번째는 변경 없음

    def test_no_hook_flag(self):
        s = {}
        self.assertEqual(su.apply(s, py="py", with_hook=False), ["statusLine"])
        self.assertFalse(su.our_hook_present(s))


class TestInstallAndCheck(unittest.TestCase):
    def test_install_writes_settings_and_cleans_test_mirror(self):
        with tempfile.TemporaryDirectory() as home:
            rc = su.main(["--home", home, "--quiet"])
            self.assertEqual(rc, 0)
            sp = su.settings_path(home)
            d = json.load(open(sp, encoding="utf-8"))
            self.assertIn("statusline.py", d["statusLine"]["command"])
            self.assertTrue(su.our_hook_present(d))
            # smoke test must not leave a fake mirror behind
            self.assertFalse(os.path.exists(su.mirror_path(home)))
            c = su.check(home)
            self.assertTrue(c["statusline_registered"] and c["statusline_points_to_this_skill"])
            self.assertFalse(c["mirror"]["exists"])

    def test_corrupt_settings_are_left_alone(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(su.claude_dir(home))
            with open(su.settings_path(home), "w", encoding="utf-8") as f:
                f.write("{ not json")
            rc = su.main(["--home", home, "--quiet"])
            self.assertEqual(rc, 2)
            self.assertEqual(open(su.settings_path(home), encoding="utf-8").read(), "{ not json")

    def test_check_reports_mirror_windows(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(su.claude_dir(home))
            with open(su.mirror_path(home), "w", encoding="utf-8") as f:
                json.dump({"rate_limits": {"seven_day": {"used_percentage": 3, "resets_at": 99}}}, f)
            c = su.check(home)
            self.assertTrue(c["mirror"]["exists"])
            self.assertEqual(c["mirror"]["windows"], ["seven_day"])
            self.assertFalse(c["statusline_registered"])


if __name__ == "__main__":
    unittest.main()
