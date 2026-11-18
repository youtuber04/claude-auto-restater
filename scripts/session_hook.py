#!/usr/bin/env python3
"""otsukare SessionStart hook: surface interrupted runs whose scheduled resume
died with a previous session (CronCreate jobs are in-memory and session-only,
so quitting Claude Code silently kills the safety net while the checkpoint
survives on disk).

Scans the otsukare state dir for checkpoints that are not marked complete and
whose heartbeat is dead — a fresh heartbeat means another live session still
owns that run, so it is left alone. When orphans exist, prints the SessionStart
hookSpecificOutput JSON so the agent can offer the user a resume; otherwise
prints nothing. A session start must never be blocked or noised by this hook:
any unexpected error exits 0, silently."""
import argparse
import glob
import json
import os
import re
import sys
import time

from otsukare_usage import heartbeat_path, heartbeat_status, resolve_paths


def _field(text, pattern):
    m = re.search(pattern, text, re.M)
    return m.group(1) if m else None


def find_orphans(state_dir, now):
    """Checkpoints in state_dir whose run is dead and not complete."""
    orphans = []
    for path in sorted(glob.glob(os.path.join(state_dir, "*.md"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if "STATUS: complete" in text[:200]:
            continue
        sid = _field(text, r"^- Session ID:\s*(\S+)")
        if not sid:
            continue                                # not an otsukare checkpoint
        if heartbeat_status(heartbeat_path(state_dir, sid), now)["alive"]:
            continue                                # a live session owns this run
        binding = _field(text, r"resets_at\s+(\d+)")
        orphans.append({
            "checkpoint": path,
            "slug": _field(text, r"^- Task slug:\s*(\S+)") or os.path.basename(path),
            "goal": _field(text, r"^- Goal:\s*(.+)$") or "",
            "binding": int(binding) if binding else None,
        })
    return orphans


def build_context(orphans, now):
    lines = ["otsukare: found {} interrupted run(s) whose scheduled resume died "
             "with a previous session (cron jobs are in-memory and session-only). "
             "Tell the user and offer to resume:".format(len(orphans))]
    for o in orphans:
        if o["binding"]:
            when = "{} local, {}".format(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(o["binding"])),
                "already passed" if o["binding"] <= now else "still ahead")
            epoch = o["binding"]
        else:
            when = "unknown — take a fresh helper decision after loading the skill"
            epoch = "unknown"
        lines.append('- "{}" ({}) — reset {}. To resume: otsukare resume mode. '
                     'Checkpoint: {}. Binding reset epoch: {}. Invoke the otsukare '
                     'skill and follow its Resume mode section.'.format(
                         o["slug"], o["goal"], when, o["checkpoint"], epoch))
    return "\n".join(lines)


def main(argv=None):
    try:
        p = argparse.ArgumentParser(description="otsukare SessionStart hook")
        p.add_argument("--state-dir", default=resolve_paths("_")["state_dir"])
        p.add_argument("--now", type=int, default=None)
        args = p.parse_args(argv)
        now = args.now if args.now is not None else int(time.time())
        orphans = find_orphans(args.state_dir, now)
        if orphans:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": build_context(orphans, now)}}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
