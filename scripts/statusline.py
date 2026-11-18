#!/usr/bin/env python3
"""otsukare statusline: mirror Claude Code's rate-limit blob to a file the local
agent can read, and print a minimal status line. Cross-platform (no shell)."""
import json
import os
import sys


def mirror_path(home=None):
    return os.path.join(home or os.path.expanduser("~"),
                        ".claude", "last-statusline-input.json")


def write_mirror(raw, dest):
    """Atomically write the blob: tmp in the same dir, then os.replace (atomic on
    Windows and Unix). Swallow OSError so a transient lock never breaks rendering."""
    tmp = dest + ".tmp"
    try:
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, dest)
    except OSError:
        pass


def mirror_if_valid(raw, dest):
    """Mirror only a parseable OBJECT blob — a torn/empty/non-object stdin must
    never clobber a good mirror the helper is reading as JSON mid-run. Returns
    the parsed dict (for reuse), or None when nothing was written."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    write_mirror(raw, dest)
    return d


def format_status(d):
    """Minimal status line from a parsed blob dict (None -> bare fallback)."""
    if d is None:
        return "Claude"
    model = (d.get("model") or {}).get("display_name", "Claude")
    five = ((d.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage", "?")
    return "{} · 5h {}%".format(model, five)


def main():
    # The status line includes non-ASCII (the · separator). On a cp932/Shift-JIS
    # console stdout defaults to the locale codec and would raise UnicodeEncodeError
    # every render; force UTF-8 so rendering never crashes.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    raw = sys.stdin.read()
    sys.stdout.write(format_status(mirror_if_valid(raw, mirror_path())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
