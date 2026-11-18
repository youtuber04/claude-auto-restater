import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import otsukare_usage as ou

CFG = dict(soft=90, hard=97, soft_7d=95, hard_7d=98,
           stale_buffer=8, stale_age=120, resume_offset_min=10)

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otsukare_usage.py")


def run_cli(args, script=_SCRIPT):
    """Run a scripts/ CLI as a subprocess under the current interpreter. Shared
    by every CLI test class (test_session_hook imports it too) — sys.executable
    (not python3, for Windows) and encoding='utf-8' so captured non-ASCII
    output decodes on any OS."""
    return subprocess.run([sys.executable, script] + args,
                          capture_output=True, text=True, encoding="utf-8")


def write_blob(five=None, five_reset=None, seven=None, seven_reset=None):
    """Write a temp mirror-blob file and return its path."""
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five, "resets_at": five_reset}
    if seven is not None:
        rl["seven_day"] = {"used_percentage": seven, "resets_at": seven_reset}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"session_id": "s1", "rate_limits": rl}, f)
    return path


class TestDecide(unittest.TestCase):
    def test_continue_when_low(self):
        p = write_blob(five=50, five_reset=1000, seven=5, seven_reset=9000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)  # fresh (age 10s)
        self.assertEqual(out["decision"], "continue")
        self.assertIsNone(out["resume_target"])

    def test_soft_when_5h_at_91(self):
        p = write_blob(five=91, five_reset=1000, seven=5, seven_reset=9000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["decision"], "soft")
        self.assertEqual(out["binding_resets_at"], 1000)
        self.assertEqual(out["resume_target"], 1000 + 600)

    def test_hard_when_5h_at_98(self):
        p = write_blob(five=98, five_reset=1000, seven=5, seven_reset=9000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["decision"], "hard")

    def test_stale_buffer_pushes_into_soft(self):
        # used 85, fresh -> continue; stale -> effective 93 -> soft. The reset
        # stays in the future for both reads, so this exercises the stale buffer
        # on a still-valid window (a rolled-over window is covered separately).
        p = write_blob(five=85, five_reset=10_000, seven=5, seven_reset=90_000)
        fresh = ou.decide(p, now=500, mtime=490, cfg=CFG)      # age 10s
        stale = ou.decide(p, now=1000, mtime=500, cfg=CFG)     # age 500s > 120
        self.assertEqual(fresh["decision"], "continue")
        self.assertFalse(fresh["stale"])
        self.assertEqual(stale["decision"], "soft")
        self.assertTrue(stale["stale"])

    def test_binding_is_later_reset_when_both_over(self):
        p = write_blob(five=92, five_reset=1000, seven=95, seven_reset=5000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["binding_resets_at"], 5000)       # later of the two
        self.assertEqual(out["resume_target"], 5000 + 600)

    def test_missing_limit_is_ignored(self):
        p = write_blob(five=92, five_reset=1000)  # no seven_day key
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["decision"], "soft")
        self.assertIsNone(out["limits"]["seven_day"])

    def test_now_is_echoed(self):
        p = write_blob(five=50, five_reset=1000)
        out = ou.decide(p, now=777, mtime=770, cfg=CFG)
        self.assertEqual(out["now"], 777)

    def test_rolled_window_does_not_pause(self):
        # The mirror still holds the PRIOR window's blob: 97% used, but its reset
        # is now in the past -> the 5h window rolled over and usage reset to ~0.
        # decide() must NOT pause on the stale 97% (this was the false-pause bug).
        p = write_blob(five=97, five_reset=1_000_000, seven=40, seven_reset=2_000_000)
        out = ou.decide(p, now=1_000_060, mtime=999_000, cfg=CFG)  # 60s after reset
        self.assertEqual(out["decision"], "continue")
        self.assertIsNone(out["binding_resets_at"])
        self.assertIsNone(out["resume_target"])

    def test_rolled_window_is_flagged_for_refresh(self):
        p = write_blob(five=97, five_reset=1_000_000, seven=40, seven_reset=2_000_000)
        out = ou.decide(p, now=1_000_060, mtime=999_000, cfg=CFG)
        self.assertTrue(out["stale_window"])
        self.assertTrue(out["limits"]["five_hour"]["rolled"])
        self.assertEqual(out["limits"]["five_hour"]["effective"], 0)

    def test_rolled_window_skips_stale_buffer(self):
        # 95% + 8 stale buffer = 103 (hard) if treated as live; rolled -> continue.
        # The buffer must never be added to a window that already reset.
        p = write_blob(five=95, five_reset=1000)
        out = ou.decide(p, now=2000, mtime=1500, cfg=CFG)  # stale file AND past reset
        self.assertEqual(out["decision"], "continue")
        self.assertTrue(out["limits"]["five_hour"]["rolled"])

    def test_live_window_still_pauses_when_other_rolled(self):
        # 7d window rolled (stale 99%), but 5h is genuinely at 95 with a future
        # reset -> still soft on 5h. A rolled sibling must not blind the live one.
        p = write_blob(five=95, five_reset=10_000, seven=99, seven_reset=1000)
        out = ou.decide(p, now=2000, mtime=1990, cfg=CFG)
        self.assertEqual(out["decision"], "soft")
        self.assertEqual(out["binding_resets_at"], 10_000)
        self.assertTrue(out["stale_window"])
        self.assertTrue(out["limits"]["seven_day"]["rolled"])

    def test_no_rolled_window_reports_stale_window_false(self):
        p = write_blob(five=50, five_reset=10_000, seven=5, seven_reset=90_000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertFalse(out["stale_window"])

    def test_margin_and_max_wave_shrink_with_usage(self):
        # dispatch-gate policy incl. band boundaries: margin 20 -> 2, 10 -> 1, 0 -> 0
        for used, wave in ((65, 4), (70, 2), (75, 2), (80, 1), (85, 1), (90, 0), (95, 0)):
            p = write_blob(five=used, five_reset=10_000, seven=5, seven_reset=90_000)
            out = ou.decide(p, now=500, mtime=490, cfg=CFG)
            self.assertEqual(out["margin"], 90 - used, "used {}".format(used))
            self.assertEqual(out["max_wave"], wave, "used {}".format(used))

    def test_margin_is_min_headroom_across_limits(self):
        p = write_blob(five=50, five_reset=10_000, seven=85, seven_reset=90_000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["margin"], 10)          # min(90-50, 95-85): 7d binds
        self.assertEqual(out["max_wave"], 1)

    def test_stale_buffer_narrows_the_wave(self):
        p = write_blob(five=85, five_reset=10_000)
        out = ou.decide(p, now=1000, mtime=500, cfg=CFG)   # stale: effective 93
        self.assertEqual(out["margin"], -3)
        self.assertEqual(out["max_wave"], 0)

    def test_rolled_only_reading_gives_normal_wave(self):
        # no live limit -> margin unknown; the gate falls back to normal width
        # and the stale_window refresh flow covers safety
        p = write_blob(five=97, five_reset=1000)
        out = ou.decide(p, now=2000, mtime=1990, cfg=CFG)
        self.assertIsNone(out["margin"])
        self.assertEqual(out["max_wave"], 4)


class TestSevenDayThresholds(unittest.TestCase):
    """The 7-day window gets laxer thresholds (soft 95 / hard 98) — rationale
    in _thresholds."""

    def test_seven_day_92_continues(self):
        p = write_blob(five=10, five_reset=10_000, seven=92, seven_reset=90_000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["decision"], "continue")

    def test_seven_day_96_is_soft(self):
        p = write_blob(five=10, five_reset=10_000, seven=96, seven_reset=90_000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["decision"], "soft")
        self.assertEqual(out["binding_resets_at"], 90_000)

    def test_seven_day_97_is_soft_not_hard(self):
        # 97 would be hard on the 5h scale; on 7d it is merely soft (hard is 98)
        p = write_blob(five=10, five_reset=10_000, seven=97, seven_reset=90_000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["decision"], "soft")

    def test_seven_day_98_is_hard(self):
        p = write_blob(five=10, five_reset=10_000, seven=98, seven_reset=90_000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["decision"], "hard")

    def test_margin_uses_seven_day_soft(self):
        p = write_blob(five=10, five_reset=10_000, seven=92, seven_reset=90_000)
        out = ou.decide(p, now=500, mtime=490, cfg=CFG)
        self.assertEqual(out["margin"], 3)           # min(90-10, 95-92)
        self.assertEqual(out["max_wave"], 1)

    def test_resume_clears_with_seven_day_at_92(self):
        # post-reset: 5h dropped to 3, 7d sits at 92 — under its own soft, clear
        p = write_blob(five=3, five_reset=9000, seven=92, seven_reset=90_000)
        out = ou.resume_check(p, now=2000, mtime=1990, reset_epoch=1000, cfg=CFG)
        self.assertEqual(out["status"], "clear")


class TestArmTarget(unittest.TestCase):
    def test_uses_future_reset(self):
        p = write_blob(five=40, five_reset=10_000)
        out = ou.arm_target(p, now=5_000, mtime=4_990, cfg=CFG)
        self.assertFalse(out["provisional"])
        self.assertEqual(out["safety_target"], 10_000 + 600)
        self.assertEqual(out["five_hour_resets_at"], 10_000)

    def test_falls_back_when_reset_in_past(self):
        # cached reset already rolled over -> provisional now + 5h + offset
        p = write_blob(five=40, five_reset=1_000)
        out = ou.arm_target(p, now=5_000, mtime=4_990, cfg=CFG)
        self.assertTrue(out["provisional"])
        self.assertEqual(out["safety_target"], 5_000 + ou.FIVE_HOUR_SECONDS + 600)

    def test_falls_back_when_reset_missing(self):
        p = write_blob(five=40)  # no resets_at
        out = ou.arm_target(p, now=5_000, mtime=4_990, cfg=CFG)
        self.assertTrue(out["provisional"])
        self.assertIsNone(out["five_hour_resets_at"])

    def test_includes_cron_for_target(self):
        p = write_blob(five=40, five_reset=10_000)
        out = ou.arm_target(p, now=5_000, mtime=4_990, cfg=CFG)
        self.assertEqual(out["cron"], ou.cron_for(out["safety_target"]))

    def test_flags_stale_file(self):
        p = write_blob(five=40, five_reset=10_000)
        out = ou.arm_target(p, now=5_000, mtime=4_000, cfg=CFG)  # age 1000s > 120
        self.assertTrue(out["stale"])


class TestResumeCheck(unittest.TestCase):
    def test_wait_when_file_older_than_reset(self):
        # mtime <= reset means the file is pre-reset stale -> cannot trust "cleared"
        p = write_blob(five=5, five_reset=1000, seven=5, seven_reset=1000)
        out = ou.resume_check(p, now=2000, mtime=900, reset_epoch=1000, cfg=CFG)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["reason"], "stale_pre_reset")

    def test_clear_when_fresh_and_under_soft(self):
        p = write_blob(five=10, five_reset=1000, seven=4, seven_reset=1000)
        out = ou.resume_check(p, now=2000, mtime=1500, reset_epoch=1000, cfg=CFG)
        self.assertEqual(out["status"], "clear")
        self.assertNotIn("resume_target", out)

    def test_still_over_includes_rearm_target(self):
        # Usage within a window only rises, so a post-reset 'still over' cannot
        # clear until the NEXT reset — resume_check must hand the caller the
        # fresh re-arm target instead of letting it burn the remaining ticks.
        p = write_blob(five=95, five_reset=9000, seven=5, seven_reset=9999)
        out = ou.resume_check(p, now=2000, mtime=1500, reset_epoch=1000, cfg=CFG)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["reason"], "still_over")
        self.assertEqual(out["binding_resets_at"], 9000)
        self.assertEqual(out["resume_target"], 9000 + 600)

    def test_rolled_over_soft_reading_waits_for_fresh_window(self):
        # The over-soft reading is itself from an expired window — neither
        # 'clear' nor 'still_over' can be trusted; wait for a fresh render.
        p = write_blob(five=95, five_reset=1000)
        out = ou.resume_check(p, now=2000, mtime=1500, reset_epoch=1000, cfg=CFG)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["reason"], "stale_window")
        self.assertNotIn("resume_target", out)

    def test_stale_reading_near_soft_does_not_clear(self):
        # 85 on a fresh file would clear, but a stale file earns the buffer
        # (85+8=93): resume only on a reading the run loop itself would trust.
        p = write_blob(five=85, five_reset=9000)
        out = ou.resume_check(p, now=2000, mtime=1500, reset_epoch=1000, cfg=CFG)
        self.assertEqual(out["status"], "wait")
        self.assertEqual(out["reason"], "still_over")


def _local_epoch(y, mo, d, h, mi):
    """Epoch for a LOCAL wall-clock time — keeps cron_for tests TZ-independent
    (cron_for reads local components back out via time.localtime)."""
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))


NEXT_HOUR_MINUTES = "2,7,12,17,22,27,32,37,42,47,52,57"


class TestCronFor(unittest.TestCase):
    """Every emitted tick must fire AT or AFTER the target: pre-target ticks land
    in the still-exhausted window and each one re-reads the whole session context
    at 97%+ usage — the safety net must never be able to cause the wedge it
    guards against. All ticks sit on :02-mod-5 marks, off the :00/:30 herd."""

    def test_early_target_stays_in_hour_from_next_mark(self):
        # 14:07 -> ticks 7,12,...,57 of hour 14; :02 (pre-target) never fires
        self.assertEqual(ou.cron_for(_local_epoch(2026, 7, 5, 14, 7)),
                         "7,12,17,22,27,32,37,42,47,52,57 14 5 7 *")

    def test_on_mark_target_includes_its_own_minute(self):
        # 14:47 -> exactly RETRY_MIN_TICKS in-hour ticks left -> stays in-hour
        self.assertEqual(ou.cron_for(_local_epoch(2026, 7, 5, 14, 47)),
                         "47,52,57 14 5 7 *")

    def test_late_target_rolls_to_next_hour(self):
        # 14:48 -> only :52/:57 remain (< RETRY_MIN_TICKS) -> the whole NEXT hour
        self.assertEqual(ou.cron_for(_local_epoch(2026, 7, 5, 14, 48)),
                         NEXT_HOUR_MINUTES + " 15 5 7 *")

    def test_next_hour_roll_crosses_midnight(self):
        self.assertEqual(ou.cron_for(_local_epoch(2026, 7, 5, 23, 58)),
                         NEXT_HOUR_MINUTES + " 0 6 7 *")

    def test_no_tick_ever_precedes_target_minute(self):
        for minute in range(60):
            cron = ou.cron_for(_local_epoch(2026, 7, 5, 14, minute))
            mins, hour = cron.split()[0], cron.split()[1]
            if hour == "14":                     # stayed in the target hour
                self.assertTrue(all(int(m) >= minute for m in mins.split(",")),
                                "minute {} -> {}".format(minute, cron))


class TestCli(unittest.TestCase):
    def test_default_mode_emits_decision_json(self):
        # 2026-11-18: default soft=97 / hard=99 for both windows -> 98% is 'soft'
        p = write_blob(five=98, five_reset=1000, seven=5, seven_reset=9000)
        res = run_cli(["--file", p, "--now", "500", "--mtime", "490"])
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["decision"], "soft")

    def test_arm_mode(self):
        p = write_blob(five=40, five_reset=10_000)
        res = run_cli(["--file", p, "--now", "5000", "--mtime", "4990", "--arm"])
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["safety_target"], 10_000 + 600)
        self.assertFalse(out["provisional"])

    def test_resume_check_mode(self):
        p = write_blob(five=10, five_reset=1000, seven=4, seven_reset=1000)
        res = run_cli(["--file", p, "--now", "2000", "--mtime", "1500",
                         "--resume-check", "1000"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["status"], "clear")

    def test_cron_for_mode(self):
        # CLI parity with the unit-pinned cron_for (exact strings pinned above)
        epoch = _local_epoch(2026, 7, 5, 14, 7)
        res = run_cli(["--cron-for", str(epoch)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), ou.cron_for(epoch))

    def test_missing_file_exits_nonzero(self):
        res = run_cli(["--file", "/no/such/file.json", "--now", "1", "--mtime", "1"])
        self.assertEqual(res.returncode, 2)
        self.assertFalse(json.loads(res.stdout)["ok"])


def write_transcript(msgs):
    """msgs: list of (input, output, cache_creation, cache_read) tuples."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for (i, o, cc, cr) in msgs:
            f.write(json.dumps({"message": {"usage": {
                "input_tokens": i, "output_tokens": o,
                "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}}}) + "\n")
    return path


def write_cost_blob(cost, transcript):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"session_id": "s1", "cost": {"total_cost_usd": cost},
                   "transcript_path": transcript,
                   "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": 99999}}}, f)
    return path


class TestTranscriptTokens(unittest.TestCase):
    def test_sums_in_out_and_cache(self):
        t = write_transcript([(100, 10, 5, 50), (200, 20, 0, 1000)])
        out = ou.transcript_tokens(t)
        self.assertEqual(out["in_out"], 330)       # 110 + 220
        self.assertEqual(out["cache"], 1055)       # 55 + 1000

    def test_missing_transcript_is_zeros(self):
        out = ou.transcript_tokens("/no/such/transcript.jsonl")
        self.assertEqual(out, {"in_out": 0, "cache": 0})


class TestStateLifecycle(unittest.TestCase):
    def test_full_lifecycle_delta_math(self):
        tpath = write_transcript([(100, 10, 0, 0)])         # baseline in_out=110
        blob1 = write_cost_blob(1.00, tpath)
        fd, spath = tempfile.mkstemp(suffix=".json")
        os.close(fd)

        ou.state_init(spath, "demo", blob1, now=1000)
        st = json.load(open(spath))
        self.assertEqual(st["token_baseline"], {"in_out": 110, "cache": 0})
        self.assertEqual(st["cost_baseline_usd"], 1.00)

        # segment 1 work: transcript + cost grow
        with open(tpath, "a") as f:
            f.write(json.dumps({"message": {"usage": {
                "input_tokens": 200, "output_tokens": 20,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000}}}) + "\n")
        blob2 = write_cost_blob(3.50, tpath)

        ou.state_pause(spath, blob2, now=2000, pct=91)
        st = json.load(open(spath))
        self.assertEqual(st["active_seconds"], 1000)
        self.assertEqual(st["tokens_accrued"], {"in_out": 220, "cache": 1000})
        self.assertAlmostEqual(st["cost_accrued_usd"], 2.50)
        self.assertEqual(st["pauses"], [{"at": 2000, "pct": 91}])

        ou.state_resume(spath, blob2, now=5000)
        st = json.load(open(spath))
        self.assertEqual(st["resume_count"], 1)
        self.assertEqual(st["paused_seconds"], 3000)        # 5000 - 2000
        self.assertEqual(st["segment_start"], 5000)
        self.assertEqual(st["token_baseline"], {"in_out": 330, "cache": 1000})

        # segment 2 work
        with open(tpath, "a") as f:
            f.write(json.dumps({"message": {"usage": {
                "input_tokens": 50, "output_tokens": 5,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
        blob3 = write_cost_blob(4.00, tpath)

        s = ou.summarize(json.load(open(spath)), blob3, now=6000)
        self.assertEqual(s["resume_count"], 1)
        self.assertEqual(s["total_seconds"], 5000)          # 6000 - 1000
        self.assertEqual(s["active_seconds"], 2000)         # 1000 + (6000-5000)
        self.assertEqual(s["paused_seconds"], 3000)
        self.assertEqual(s["tokens"]["in_out"], 275)        # accrued 220 + (385-330)
        self.assertEqual(s["tokens"]["cache"], 1000)        # accrued 1000 + 0
        self.assertEqual(s["tokens"]["total"], 1275)
        self.assertAlmostEqual(s["cost_usd"], 3.00)         # accrued 2.50 + (4.00-3.50)
        self.assertEqual(s["pause_pcts"], "91%")


class TestFormatSummary(unittest.TestCase):
    def test_renders_all_fields(self):
        s = {"task": "demo", "resume_count": 2, "pause_pcts": "91%, 96%",
             "total_seconds": 4 * 3600 + 18 * 60, "active_seconds": 6660,
             "paused_seconds": 8820,
             "tokens": {"in_out": 412_000, "cache": 4_100_000, "total": 4_512_000},
             "cost_usd": 3.84}
        text = ou.format_summary(s)
        self.assertIn("otsukare summary — demo", text)
        self.assertIn("2× (91%, 96%)", text)
        self.assertIn("4h 18m total", text)
        self.assertIn("412K in/out", text)
        self.assertIn("4.5M total", text)
        self.assertIn("$3.84", text)


class TestStateCli(unittest.TestCase):
    def test_init_then_summary(self):
        tpath = write_transcript([(100, 10, 0, 0)])
        blob = write_cost_blob(1.00, tpath)
        fd, spath = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        init = run_cli(["--file", blob, "--state", spath, "--state-action", "init",
                          "--task", "demo", "--now", "1000"])
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertTrue(json.loads(init.stdout)["ok"])
        summ = run_cli(["--file", blob, "--state", spath, "--state-action", "summary",
                          "--now", "2000"])
        self.assertEqual(summ.returncode, 0, summ.stderr)
        self.assertIn("otsukare summary — demo", summ.stdout)

    def test_state_action_requires_state(self):
        res = run_cli(["--state-action", "summary"])
        self.assertEqual(res.returncode, 2)
        self.assertFalse(json.loads(res.stdout)["ok"])

    def test_pause_accepts_fractional_pct(self):
        # used_percentage arrives as a float (e.g. 91.5); --pct must not reject it
        tpath = write_transcript([(100, 10, 0, 0)])
        blob = write_cost_blob(1.00, tpath)
        fd, spath = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        run_cli(["--file", blob, "--state", spath, "--state-action", "init",
                 "--task", "demo", "--now", "1000"])
        res = run_cli(["--file", blob, "--state", spath, "--state-action", "pause",
                       "--pct", "91.5", "--now", "2000"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(json.loads(res.stdout)["ok"])
        st = json.load(open(spath))
        self.assertEqual(st["pauses"], [{"at": 2000, "pct": 91.5}])


def write_corrupt(content="{not json"):
    """A torn or empty mirror/state file (statusline killed mid-write, empty
    stdin mirrored, partial disk write)."""
    fd, p = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return p


class TestCorruptBlob(unittest.TestCase):
    """Corrupt JSON must surface exactly like a missing file — the agent reads
    the helper's stdout as JSON mid-run, so a traceback is itself a failure."""

    def test_decide_cli_reports_json_error(self):
        res = run_cli(["--file", write_corrupt(), "--now", "1", "--mtime", "1"])
        self.assertEqual(res.returncode, 2)
        self.assertFalse(json.loads(res.stdout)["ok"])

    def test_empty_mirror_reports_json_error(self):
        res = run_cli(["--file", write_corrupt(""), "--now", "1", "--mtime", "1"])
        self.assertEqual(res.returncode, 2)
        self.assertFalse(json.loads(res.stdout)["ok"])

    def test_read_freshness_treats_corrupt_as_unreadable(self):
        self.assertEqual(ou._read_freshness(write_corrupt()), (0, None))

    def test_read_blob_treats_corrupt_as_missing(self):
        self.assertEqual(ou._read_blob(write_corrupt()), (0, None))

    def test_snapshot_tolerates_corrupt_blob(self):
        tok, cost, tpath = ou._snapshot(write_corrupt())
        self.assertEqual(tok, {"in_out": 0, "cache": 0})
        self.assertEqual(cost, 0.0)

    def test_corrupt_state_file_reports_json_error(self):
        good_blob = write_blob(five=10, five_reset=99999)
        res = run_cli(["--file", good_blob, "--state", write_corrupt(),
                       "--state-action", "summary"])
        self.assertEqual(res.returncode, 2)
        self.assertFalse(json.loads(res.stdout)["ok"])


class TestClassify(unittest.TestCase):
    def test_applicable_when_rate_limits_present(self):
        blob = {"rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": 9999}}}
        self.assertEqual(ou.classify(blob, mtime=1000, now=1000), "applicable")

    def test_no_rate_limits_when_fresh_blob_lacks_them(self):
        blob = {"cost": {"total_cost_usd": 1.0}}  # API setup: no rate_limits
        self.assertEqual(ou.classify(blob, mtime=1000, now=1000), "no_rate_limits")

    def test_stale_when_old_blob_lacks_them(self):
        blob = {"cost": {"total_cost_usd": 1.0}}
        self.assertEqual(ou.classify(blob, mtime=100, now=1000), "stale")  # age 900 > 15

    def test_no_mirror_when_blob_missing(self):
        self.assertEqual(ou.classify(None, mtime=0, now=1000), "no_mirror")


class TestPreflight(unittest.TestCase):
    def test_applicable_subscription(self):
        clock = _Clock(1000)
        blob = {"rate_limits": {"five_hour": {"resets_at": 9999}}}
        out = ou.preflight("x", clock=clock, sleeper=clock.advance, reader=lambda p: (1000, blob))
        self.assertTrue(out["applicable"])
        self.assertEqual(out["reason"], "applicable")

    def test_api_setup_reported_not_applicable(self):
        clock = _Clock(1000)
        blob = {"cost": {"total_cost_usd": 5.0}}  # fresh, no rate_limits
        out = ou.preflight("x", clock=clock, sleeper=clock.advance, reader=lambda p: (1000, blob))
        self.assertFalse(out["applicable"])
        self.assertEqual(out["reason"], "no_rate_limits")
        self.assertIn("API", out["message"])

    def test_missing_mirror_times_out_to_no_mirror(self):
        clock = _Clock(1000)
        out = ou.preflight("x", timeout=2, clock=clock, sleeper=clock.advance,
                           reader=lambda p: (0, None))
        self.assertFalse(out["applicable"])
        self.assertEqual(out["reason"], "no_mirror")


class _Clock:
    def __init__(self, start=0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestIsFresh(unittest.TestCase):
    def test_recent_and_valid_is_fresh(self):
        # file just written (age 0), reset in the future
        self.assertTrue(ou._is_fresh(mtime=1000, start_mtime=1000, resets_at=9999, now=1000))

    def test_reset_in_past_is_not_fresh(self):
        # the reported bug: recent file but an expired window
        self.assertFalse(ou._is_fresh(mtime=1000, start_mtime=1000, resets_at=500, now=1000))

    def test_render_since_start_overrides_old_age(self):
        # mtime advanced past the baseline -> a new render landed
        self.assertTrue(ou._is_fresh(mtime=200, start_mtime=100, resets_at=9999, now=1000))

    def test_stale_and_no_new_render_is_not_fresh(self):
        self.assertFalse(ou._is_fresh(mtime=100, start_mtime=100, resets_at=9999, now=1000))


class TestWaitFresh(unittest.TestCase):
    def test_returns_fresh_immediately_when_recent_valid(self):
        clock = _Clock(1000)
        out = ou.wait_fresh("x", timeout=30, clock=clock, sleeper=clock.advance,
                            reader=lambda p: (1000, 9999))
        self.assertTrue(out["fresh"])
        self.assertFalse(out["timed_out"])

    def test_waits_then_succeeds_when_window_refreshes(self):
        clock = _Clock(1000)
        calls = {"n": 0}

        def reader(_p):
            calls["n"] += 1
            # baseline read + first loop read are an expired window; then valid
            return (1000, 500) if calls["n"] <= 2 else (1000, 9999)

        out = ou.wait_fresh("x", timeout=30, poll=0.5, clock=clock,
                            sleeper=clock.advance, reader=reader)
        self.assertTrue(out["fresh"])

    def test_times_out_on_persistently_stale(self):
        clock = _Clock(1000)
        out = ou.wait_fresh("x", timeout=2, poll=0.5, clock=clock,
                            sleeper=clock.advance, reader=lambda p: (100, 500))
        self.assertTrue(out["timed_out"])
        self.assertFalse(out["fresh"])


class TestWaitFreshCli(unittest.TestCase):
    def test_fresh_file_returns_immediately(self):
        p = write_blob(five=40, five_reset=9_999_999_999)  # reset far in the future
        res = run_cli(["--file", p, "--wait-fresh", "--wait-timeout", "3"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(json.loads(res.stdout)["fresh"])

    def test_expired_window_times_out(self):
        p = write_blob(five=40, five_reset=1000)  # reset in the past
        res = run_cli(["--file", p, "--wait-fresh", "--wait-timeout", "1"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(json.loads(res.stdout)["timed_out"])


class TestExpandUserPath(unittest.TestCase):
    def test_expands_leading_tilde(self):
        out = ou.expand_user_path("~/x/y.json")
        self.assertFalse(out.startswith("~"))
        # expanduser preserves the input's forward slashes in the tail, so assert
        # with a literal "/" — os.path.join would emit "\" on Windows and fail.
        self.assertTrue(out.endswith("x/y.json"))

    def test_absolute_path_unchanged(self):
        p = os.path.join(tempfile.gettempdir(), "abs.json")
        self.assertEqual(ou.expand_user_path(p), p)

    def test_none_passthrough(self):
        self.assertIsNone(ou.expand_user_path(None))


class TestResolvePaths(unittest.TestCase):
    def test_emits_absolute_paths_under_home(self):
        home = os.path.join(tempfile.gettempdir(), "fakehome")
        out = ou.resolve_paths("sess9", home=home)
        sd = os.path.join(home, ".claude", "otsukare")
        self.assertEqual(out["state_dir"], sd)
        self.assertEqual(out["heartbeat"], os.path.join(sd, "sess9.heartbeat"))
        self.assertEqual(out["state"], os.path.join(sd, "sess9.state.json"))
        self.assertEqual(out["mirror"],
                         os.path.join(home, ".claude", "last-statusline-input.json"))
        self.assertNotIn("checkpoint", out)  # no slug given

    def test_includes_checkpoint_when_slug_given(self):
        home = os.path.join(tempfile.gettempdir(), "fakehome")
        out = ou.resolve_paths("sess9", home=home, task_slug="refactor-auth")
        self.assertEqual(out["checkpoint"],
                         os.path.join(home, ".claude", "otsukare",
                                      "sess9-refactor-auth.md"))


class TestTouchHeartbeat(unittest.TestCase):
    def test_creates_file_and_parent(self):
        d = tempfile.mkdtemp()
        hb = os.path.join(d, "otsukare", "s1.heartbeat")
        ou.touch_heartbeat(hb)
        self.assertTrue(os.path.exists(hb))

    def test_bumps_mtime_on_existing_file(self):
        fd, hb = tempfile.mkstemp(suffix=".heartbeat")
        os.close(fd)
        old = os.path.getmtime(hb) - 10_000      # force a known-old mtime
        os.utime(hb, (old, old))
        ou.touch_heartbeat(hb)
        self.assertGreater(os.path.getmtime(hb), old)


class TestHeartbeatStatus(unittest.TestCase):
    def test_alive_when_recent(self):
        fd, hb = tempfile.mkstemp(); os.close(fd)
        os.utime(hb, (1000, 1000))
        out = ou.heartbeat_status(hb, now=1100, stale_min=15)   # 100s old
        self.assertTrue(out["alive"])
        self.assertEqual(out["age_seconds"], 100)

    def test_dead_when_old(self):
        fd, hb = tempfile.mkstemp(); os.close(fd)
        os.utime(hb, (1000, 1000))
        out = ou.heartbeat_status(hb, now=1000 + 16 * 60, stale_min=15)
        self.assertFalse(out["alive"])

    def test_missing_is_dead(self):
        out = ou.heartbeat_status("/no/such.heartbeat", now=1000, stale_min=15)
        self.assertFalse(out["alive"])
        self.assertIsNone(out["age_seconds"])

    def test_age_clamped_to_zero_when_now_before_mtime(self):
        # now=int(time.time()) truncation can land just below the float mtime of
        # a freshly-touched file; age_seconds must never report negative.
        fd, hb = tempfile.mkstemp(); os.close(fd)
        os.utime(hb, (1000, 1000))
        out = ou.heartbeat_status(hb, now=995, stale_min=15)  # now < mtime
        self.assertEqual(out["age_seconds"], 0.0)
        self.assertTrue(out["alive"])


class TestCleanup(unittest.TestCase):
    def test_removes_existing_and_is_idempotent(self):
        fd1, a = tempfile.mkstemp(); os.close(fd1)
        fd2, b = tempfile.mkstemp(); os.close(fd2)
        out = ou.cleanup([a, b])
        self.assertFalse(os.path.exists(a))
        self.assertFalse(os.path.exists(b))
        self.assertEqual(sorted(out["removed"]), sorted([a, b]))
        # second call must not raise
        out2 = ou.cleanup([a, b])
        self.assertEqual(out2["removed"], [])

    def test_ignores_none_entries(self):
        out = ou.cleanup([None, ""])
        self.assertTrue(out["ok"])


class TestPathsCli(unittest.TestCase):
    """Subprocess tests for the path/heartbeat subcommands. The file's convention
    is that every CLI mode gets one; these also pin the load-bearing
    --cleanup-before---heartbeat dispatch ordering."""

    def test_resolve_paths_emits_absolute_paths(self):
        res = run_cli(["--resolve-paths", "--session-id", "s1"])
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        for key in ("heartbeat", "state", "mirror"):
            self.assertTrue(os.path.isabs(out[key]), out[key])

    def test_resolve_paths_without_session_id_errors(self):
        res = run_cli(["--resolve-paths"])
        self.assertEqual(res.returncode, 2)
        self.assertFalse(json.loads(res.stdout)["ok"])

    def test_touch_heartbeat_creates_file(self):
        hb = os.path.join(tempfile.mkdtemp(), "s1.heartbeat")
        res = run_cli(["--touch-heartbeat", hb])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(json.loads(res.stdout)["ok"])
        self.assertTrue(os.path.exists(hb))

    def test_heartbeat_reports_alive_for_fresh_file(self):
        hb = os.path.join(tempfile.mkdtemp(), "s1.heartbeat")
        run_cli(["--touch-heartbeat", hb])               # create it first
        res = run_cli(["--heartbeat", hb])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(json.loads(res.stdout)["alive"])

    def test_cleanup_takes_precedence_over_heartbeat_status(self):
        # --cleanup and --heartbeat share the heartbeat path arg; dispatch order
        # must route to cleanup (remove files), not heartbeat-status (read mtime).
        fd1, hb = tempfile.mkstemp(suffix=".heartbeat"); os.close(fd1)
        fd2, st = tempfile.mkstemp(suffix=".state.json"); os.close(fd2)
        res = run_cli(["--cleanup", "--heartbeat", hb, "--state", st])
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(sorted(out["removed"]), sorted([hb, st]))
        self.assertFalse(os.path.exists(hb))
        self.assertFalse(os.path.exists(st))


if __name__ == "__main__":
    unittest.main()


class TestWeeklyOnlyPlans(unittest.TestCase):
    """2026-11-18: plans that expose only the 7-day window (no 5h window) must still
    be applicable, key freshness/arming on the weekly reset, and pause at 99%."""
    def test_classify_applicable_with_seven_day_only(self):
        blob = {"rate_limits": {"seven_day": {"used_percentage": 23, "resets_at": 9000}}}
        self.assertEqual(ou.classify(blob, mtime=490, now=500), "applicable")

    def test_primary_reset_prefers_five_hour_else_seven_day(self):
        both = {"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": 1000},
                                "seven_day": {"used_percentage": 1, "resets_at": 9000}}}
        self.assertEqual(ou._primary_reset(both, now=500), ("five_hour", 1000))
        self.assertEqual(ou._primary_reset(both, now=2000), ("seven_day", 9000))  # 5h rolled -> weekly
        weekly = {"rate_limits": {"seven_day": {"used_percentage": 1, "resets_at": 9000}}}
        self.assertEqual(ou._primary_reset(weekly, now=500), ("seven_day", 9000))

    def test_arm_uses_weekly_reset_when_no_five_hour(self):
        p = write_blob(seven=23, seven_reset=9000)
        out = ou.arm_target(p, now=500, mtime=490, cfg=dict(ou.DEFAULTS))
        self.assertEqual(out["armed_window"], "seven_day")
        self.assertEqual(out["safety_target"], 9000 + 10 * 60)
        self.assertFalse(out["provisional"])

    def test_default_thresholds_pause_at_99_on_either_window(self):
        cfg = dict(ou.DEFAULTS)
        p = write_blob(five=10, five_reset=1000, seven=99, seven_reset=9000)
        self.assertEqual(ou.decide(p, now=500, mtime=490, cfg=cfg)["decision"], "hard")
        p = write_blob(five=99, five_reset=1000, seven=10, seven_reset=9000)
        out = ou.decide(p, now=500, mtime=490, cfg=cfg)
        self.assertEqual(out["decision"], "hard")
        self.assertEqual(out["binding_resets_at"], 1000)   # wakes at the window that filled
        p = write_blob(five=96, five_reset=1000, seven=96, seven_reset=9000)
        self.assertEqual(ou.decide(p, now=500, mtime=490, cfg=cfg)["decision"], "continue")
