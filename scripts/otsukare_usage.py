#!/usr/bin/env python3
"""otsukare usage-decision + scheduling-math helper.

Reads the Claude Code statusline mirror blob (~/.claude/last-statusline-input.json)
and emits a JSON decision: continue / soft (pause-worthy) / hard (must pause).
All freshness logic is mtime-based because the blob carries no timestamp.

Modes:
  (default)           decision for the current usage
  --preflight         applicability check (subscription vs API / usage-billed)
  --wait-fresh        block until the mirror is freshly rendered, current window
  --arm               safety-net target for the 5h window (validate-or-fallback)
  --resume-check N    'clear'/'wait' check against binding reset epoch N
  --cron-for EPOCH    bounded post-target retry-window cron string for EPOCH
  --resolve-paths / --touch-heartbeat / --heartbeat / --cleanup   state files
  --state S --state-action init|pause|resume|summary              run metrics
"""
import argparse
import json
import os
import sys
import time

# 2026-11-18 사용자 지정: 5시간·주간 두 창 모두 97%에서 경고(soft), 99%에서 멈춤(hard) —
# 먼저 99%에 닿는 창에서 멈추고 그 창의 초기화 시각(+offset)에 깨어난다(주간 창이면 며칠 뒤).
DEFAULTS = dict(soft=97, hard=99, soft_7d=97, hard_7d=99,
                stale_buffer=8, stale_age=120, resume_offset_min=10)
LIMIT_KEYS = ("five_hour", "seven_day")
FIVE_HOUR_SECONDS = 5 * 3600


def _thresholds(key, cfg):
    """Per-window (soft, hard). The 7-day window gets laxer thresholds: its
    last 5% is still ~8 hours of runway, where the 5h window's last 10% is
    ~30 minutes — a shared soft=90 paused days too early."""
    if key == "seven_day":
        return cfg["soft_7d"], cfg["hard_7d"]
    return cfg["soft"], cfg["hard"]


def load_blob(path):
    """Parse JSON from path. Any failure — missing, locked, truncated, empty,
    corrupt — surfaces as OSError, so every caller's unreadable-file path also
    covers a torn mirror (empty/partial JSON raises ValueError, not OSError)."""
    try:
        with open(path) as f:
            return json.load(f)
    except ValueError as e:
        raise OSError("invalid JSON in {}: {}".format(path, e))


def expand_user_path(p):
    """Expand a leading ~ in a path arg. PowerShell does not expand ~ for
    native-exe arguments, so the helper must do it itself. No-op on None and
    on already-absolute paths."""
    return os.path.expanduser(p) if p else p


def heartbeat_path(state_dir, session_id):
    """Single owner of the heartbeat file naming — the SessionStart hook probes
    the same file resolve_paths hands the run, so liveness never diverges."""
    return os.path.join(state_dir, session_id + ".heartbeat")


def resolve_paths(session_id, home=None, task_slug=None):
    """Absolute, OS-correct paths for the otsukare state files. The agent reads
    these once at preflight so it never constructs a ~/... path (Write/Read/Cron
    tools all require absolute paths)."""
    home = home or os.path.expanduser("~")
    claude = os.path.join(home, ".claude")
    state_dir = os.path.join(claude, "otsukare")
    out = {
        "home": home,
        "state_dir": state_dir,
        "heartbeat": heartbeat_path(state_dir, session_id),
        "state": os.path.join(state_dir, session_id + ".state.json"),
        "mirror": os.path.join(claude, "last-statusline-input.json"),
    }
    if task_slug:
        out["checkpoint"] = os.path.join(state_dir,
                                         "{}-{}.md".format(session_id, task_slug))
    return out


def touch_heartbeat(path):
    """Create the heartbeat (and its parent dir) if missing, and bump its mtime.
    os.utime is required: on Windows opening a file in append mode does NOT
    advance mtime, which would defeat the mtime-based liveness check."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a"):
        pass
    os.utime(path, None)


def heartbeat_status(path, now, stale_min=15):
    """Liveness from the heartbeat's mtime: alive if younger than stale_min.
    Missing/unreadable -> dead with age_seconds=None (JSON-safe, no inf)."""
    try:
        # Clamp at 0: now=int(time.time()) truncates below a just-written float
        # mtime, which would otherwise report a slightly negative age.
        age = max(0.0, now - os.path.getmtime(path))
    except OSError:
        return {"age_seconds": None, "alive": False, "missing": True}
    return {"age_seconds": round(age, 1), "alive": age < stale_min * 60}


def cleanup(paths):
    """Remove each path, ignoring missing/locked files (Windows raises
    FileNotFoundError / PermissionError). Never raises, so completion can't crash."""
    removed = []
    for p in paths:
        if not p:
            continue
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            pass
    return {"ok": True, "removed": removed}


def _limit(blob, key):
    rl = (blob.get("rate_limits") or {}).get(key) or {}
    return rl.get("used_percentage"), rl.get("resets_at")


def _primary_reset(blob, now=None):
    """The reset epoch freshness/arming key on: the 5h window when the plan exposes
    one, else the 7-day window (plans that show only weekly limits, e.g. the Ultra
    account on this PC, 2026-11-18). Prefers a window whose reset is still ahead of
    `now`; falls back to any window with a reset at all. Returns (key, resets_at)."""
    if now is not None:
        for key in LIMIT_KEYS:
            _used, resets = _limit(blob, key)
            if resets is not None and resets > now:
                return key, resets
    for key in LIMIT_KEYS:
        _used, resets = _limit(blob, key)
        if resets is not None:
            return key, resets
    return None, None


def _reset_in_future(resets, now):
    """True when a limit's reset epoch is known and still ahead of `now` — its
    window is current. A reset at-or-before `now` means the window already rolled
    over, so that limit's cached percentage is the prior window's and is stale.
    Single source of truth for this boundary across decide/arm_target/_is_fresh."""
    return resets is not None and resets > now


# Dispatch-gate policy: widest subagent wave for a given soft-threshold margin.
# Tighter headroom -> narrower waves, so a parallel burst can't cross 100%
# between two seam checks. (margin floor, max wave size), checked in order.
WAVE_BANDS = ((20, 4), (10, 2), (0, 1))


def _max_wave(margin):
    if margin is None:                # no live limit reading -> normal width
        return WAVE_BANDS[0][1]
    for floor, wave in WAVE_BANDS:
        if margin > floor:
            return wave
    return 0


def decide(path, now, mtime, cfg):
    return _decide_blob(load_blob(path), now, mtime, cfg)


def _decide_blob(blob, now, mtime, cfg):
    age = now - mtime
    stale = age > cfg["stale_age"]
    buf = cfg["stale_buffer"] if stale else 0
    out = {"ok": True, "now": now, "stale": stale, "age_seconds": age,
           "mtime": mtime, "limits": {}}
    over_resets = []
    margins = []
    decision = "continue"
    rolled_any = False
    for key in LIMIT_KEYS:
        soft, hard = _thresholds(key, cfg)
        used, resets = _limit(blob, key)
        if used is None:
            out["limits"][key] = None
            continue
        # A reset epoch at-or-before now means this window already rolled over:
        # the cached `used` is the PRIOR window's final value, not the current
        # one. Usage reset (dropped to ~0), so this percentage must NOT drive a
        # pause and the stale buffer must NOT be added to it (that turned a stale
        # 97% into 105% and forced a false hard-pause right after a reset). Flag
        # it instead; the caller should refresh before trusting usage again.
        if resets is not None and not _reset_in_future(resets, now):
            rolled_any = True
            out["limits"][key] = {"used": used, "effective": 0,
                                  "resets_at": resets, "rolled": True}
            continue
        eff = used + buf
        out["limits"][key] = {"used": used, "effective": eff, "resets_at": resets,
                              "rolled": False}
        margins.append(soft - eff)
        if eff >= hard:
            decision = "hard"
        elif eff >= soft and decision != "hard":
            decision = "soft"
        if eff >= soft and resets is not None:
            over_resets.append(resets)
    out["decision"] = decision
    out["stale_window"] = rolled_any
    # min headroom to any window's own soft threshold drives the dispatch gate
    out["margin"] = min(margins) if margins else None
    out["max_wave"] = _max_wave(out["margin"])
    if over_resets:
        binding = max(over_resets)  # must wait for the LATEST reset
        out["binding_resets_at"] = binding
        out["resume_target"] = binding + cfg["resume_offset_min"] * 60
    else:
        out["binding_resets_at"] = None
        out["resume_target"] = None
    return out


def arm_target(path, now, mtime, cfg):
    """Compute the safety-net resume target for the 5h window, to be armed at
    skill start as a dead-man's switch. Uses the cached 5h reset only if it is
    genuinely in the future; otherwise (missing, or a window that already rolled
    over) falls back to a worst-case 'now + 5h' and flags it provisional so the
    caller knows to re-point it at the first seam once a fresh render lands."""
    blob = load_blob(path)
    key, resets = _primary_reset(blob, now)   # 5h 창이 없으면 주간 창을 안전망 기준으로
    if _reset_in_future(resets, now):
        target = resets + cfg["resume_offset_min"] * 60
        provisional = False
    else:
        target = now + FIVE_HOUR_SECONDS + cfg["resume_offset_min"] * 60
        provisional = True
    return {
        "now": now,
        "five_hour_resets_at": _limit(blob, "five_hour")[1],
        "armed_window": key,
        "armed_resets_at": resets,
        "safety_target": target,
        "cron": cron_for(target),
        "provisional": provisional,
        "stale": (now - mtime) > cfg["stale_age"],
    }


def resume_check(path, now, mtime, reset_epoch, cfg):
    """At resume time, only report 'clear' when the file is genuinely fresh
    (mtime newer than the reset) AND the same assessment the run loop uses —
    non-rolled effective usage, stale buffer included — is back under soft.
    Waits carry a reason: 'stale_pre_reset' (no render since the reset) and
    'stale_window' (a rolled window still shows an over-soft percentage) both
    self-resolve, so the next retry tick just re-checks; 'still_over' cannot
    (usage only rises within a window), so it carries the fresh re-arm target
    for the caller to re-point the net at instead of burning ticks."""
    out = {"file_mtime": mtime, "reset_epoch": reset_epoch}
    if mtime <= reset_epoch:
        out["status"] = "wait"
        out["reason"] = "stale_pre_reset"
        return out
    d = _decide_blob(load_blob(path), now, mtime, cfg)
    suspicious = any(v and v["rolled"] and v["used"] >= _thresholds(key, cfg)[0]
                     for key, v in d["limits"].items())
    if suspicious:
        out["status"] = "wait"
        out["reason"] = "stale_window"
    elif d["decision"] == "continue":
        out["status"] = "clear"
    else:
        out["status"] = "wait"
        out["reason"] = "still_over"
        out["binding_resets_at"] = d["binding_resets_at"]
        out["resume_target"] = d["resume_target"]
    return out


RETRY_MIN_TICKS = 3


def cron_for(epoch):
    """Bounded retry-window cron (local time) whose every tick fires AT or
    AFTER epoch — never before. A pre-target tick fires into the still-exhausted
    window and re-reads the whole session context at 97%+ usage, so the safety
    net itself could push a paused run to 100%; pre-target minutes are never
    emitted. Ticks run every 5 minutes on the :02/:07/... marks (dodging the
    :00/:30 herd minutes CronCreate warns about), starting at the first mark
    at/after the target minute; when fewer than RETRY_MIN_TICKS remain in the
    target's hour, the whole NEXT hour is used instead."""
    t = time.localtime(epoch)
    first = t.tm_min + (2 - t.tm_min) % 5    # first :02-mod-5 mark >= the minute
    if len(range(first, 60, 5)) < RETRY_MIN_TICKS:
        t = time.localtime(epoch + (60 - t.tm_min) * 60)   # top of the next hour
        first = 2
    return "{} {} {} {} *".format(",".join(map(str, range(first, 60, 5))),
                                  t.tm_hour, t.tm_mday, t.tm_mon)


# --- Freshness barrier --------------------------------------------------------
# The mirror file is rewritten whenever the statusline renders, which happens
# every few seconds while a session is active (verified empirically) — but it
# reflects whichever session rendered last. A file left by a previous, already-
# expired window has a `resets_at` in the past. wait_fresh() blocks until a
# render lands after we started AND the 5h window is current, so callers never
# act on a stale reset/usage from another session.

def _read_freshness(path):
    """Return (mtime, five_hour_resets_at); (0, None) if missing/unreadable."""
    try:
        mtime = os.path.getmtime(path)
        blob = load_blob(path)
    except OSError:
        return 0, None
    return mtime, _primary_reset(blob)[1]   # 5h 창이 없으면 주간 창의 초기화 시각


def _is_fresh(mtime, start_mtime, resets_at, now, max_age=15):
    """Fresh = (a render landed after we began waiting, OR the file is already
    very recent) AND the 5h window is current (reset in the future). The
    future-reset guard is what rejects a stale file from an expired window."""
    rendered_since = mtime > start_mtime
    already_recent = (now - mtime) <= max_age
    valid_window = _reset_in_future(resets_at, now)
    return (rendered_since or already_recent) and valid_window


def wait_fresh(path, timeout=30, poll=0.5, max_age=15,
               clock=None, sleeper=None, reader=None):
    """Block until the mirror file is freshly rendered with a current 5h window.
    Returns {fresh, timed_out, waited_seconds, mtime, resets_at}. On timeout it
    returns fresh=False so the caller can warn and fall back conservatively."""
    clock = clock or time.time
    sleeper = sleeper or time.sleep
    reader = reader or _read_freshness
    start = clock()
    start_mtime = reader(path)[0]
    while True:
        now = clock()
        mtime, resets = reader(path)
        if _is_fresh(mtime, start_mtime, resets, now, max_age):
            return {"ok": True, "fresh": True, "timed_out": False,
                    "waited_seconds": round(now - start, 1),
                    "mtime": int(mtime), "resets_at": resets}
        if now - start >= timeout:
            return {"ok": resets is not None, "fresh": False, "timed_out": True,
                    "waited_seconds": round(now - start, 1),
                    "mtime": int(mtime), "resets_at": resets}
        sleeper(poll)


# --- Applicability preflight --------------------------------------------------
# otsukare only does something on subscription plans, which expose 5h/7d
# `rate_limits` in the statusline blob. API / usage-billed setups are metered
# per token and have no such windows (no `rate_limits` object at all), so the
# skill should detect that up front and tell the user rather than no-op.

def _read_blob(path):
    """Return (mtime, blob) or (0, None) if missing/unreadable."""
    try:
        return os.path.getmtime(path), load_blob(path)
    except OSError:
        return 0, None


def classify(blob, mtime, now, max_age=15):
    """Classify the environment from a (possibly stale) blob read:
      'applicable'      -> subscription: rate_limits.five_hour.resets_at present
      'no_rate_limits'  -> a FRESH blob with no rate limits -> API / usage-billed
      'no_mirror'       -> no blob at all (statusline mirror not wired)
      'stale'           -> old blob, can't yet decide; wait for a render
    """
    if blob is None:
        return "no_mirror"
    rl = blob.get("rate_limits") or {}
    # 5h 창이든 주간 창이든 하나라도 있으면 구독 요금제 — 주간 창만 보이는 요금제도 적용
    if any(((rl.get(k) or {}).get("resets_at") is not None) for k in LIMIT_KEYS):
        return "applicable"
    if (now - mtime) <= max_age:
        return "no_rate_limits"
    return "stale"


PREFLIGHT_MESSAGES = {
    "applicable": "Subscription rate limits detected (5h and/or 7d) — otsukare active (pauses at 99% of whichever window fills first).",
    "no_rate_limits": (
        "No 5h/7d rate-limit windows are present in the statusline data — this looks "
        "like an API / usage-billed setup, which is metered per token rather than by "
        "rolling windows. otsukare only guards subscription rate limits, so there is "
        "nothing for it to do here. Skipping. "
        "[한국어] 상태표시줄 데이터에 5시간·주간 한도 창이 없습니다. API 과금 환경이거나, "
        "세션이 아직 첫 응답을 받기 전입니다. 구독 계정이면 메시지를 하나 보낸 뒤 다시 확인하세요."),
    "no_mirror": (
        "The usage mirror file is missing — your statusline isn't exposing usage data. "
        "Run scripts/setup.py once, then restart the terminal claude session. "
        "[한국어] 사용량 미러 파일이 없습니다. 해결: scripts/setup.py 를 한 번 실행해 상태표시줄을 "
        "등록한 뒤, 열려 있는 터미널 claude 를 /exit 로 끄고 다시 켜세요(설정은 시작할 때만 읽힘). "
        "데스크톱 앱 Code 탭에서는 상태표시줄이 없어 동작하지 않습니다 — 터미널 claude 에서 쓰세요."),
    "stale": (
        "Couldn't get a fresh usage reading — the statusline mirror isn't updating. "
        "Check the README setup; otsukare needs current usage data to run. "
        "[한국어] 사용량 미러가 갱신되지 않습니다. 지금 창이 터미널 claude 가 맞는지, 상태표시줄이 "
        "맨 아래에 보이는지 확인하고, 안 보이면 /exit 후 다시 claude 를 켜세요."),
}


def preflight(path, timeout=10, poll=0.5, max_age=15, clock=None, sleeper=None, reader=None):
    """Wait briefly for a fresh render, then classify applicability so a
    not-yet-rendered subscription session isn't mistaken for an API setup."""
    clock = clock or time.time
    sleeper = sleeper or time.sleep
    reader = reader or _read_blob
    start = clock()
    while True:
        now = clock()
        mtime, blob = reader(path)
        verdict = classify(blob, mtime, now, max_age)
        if verdict in ("applicable", "no_rate_limits") or now - start >= timeout:
            return {"applicable": verdict == "applicable", "reason": verdict,
                    "message": PREFLIGHT_MESSAGES[verdict],
                    "waited_seconds": round(now - start, 1)}
        sleeper(poll)


# --- Run-metrics state (continues / time / tokens / cost) ----------------------
# All token and cost numbers are session-cumulative at the source, so the run
# summary measures the DELTA over the otsukare-wrapped span: a baseline is taken
# at start and at each resume, and per-segment deltas are accumulated. This keeps
# the math in tested code rather than in model prose.

def transcript_tokens(path):
    """Sum token usage across a Claude Code transcript .jsonl: returns
    {'in_out': input+output, 'cache': cache_creation+cache_read}. A missing or
    unreadable transcript -> zeros, so the summary degrades gracefully."""
    totals = {"in_out": 0, "cache": 0}
    try:
        f = open(path)
    except OSError:
        return totals
    with f:
        for line in f:
            try:
                usage = (json.loads(line).get("message") or {}).get("usage")
            except Exception:
                continue
            if not usage:
                continue
            totals["in_out"] += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            totals["cache"] += ((usage.get("cache_creation_input_tokens") or 0)
                                + (usage.get("cache_read_input_tokens") or 0))
    return totals


def _snapshot(blob_path):
    """Current (token totals, cost_usd, transcript_path) from the live blob."""
    try:
        blob = load_blob(blob_path)
    except OSError:
        return {"in_out": 0, "cache": 0}, 0.0, ""
    tpath = blob.get("transcript_path", "")
    cost = (blob.get("cost") or {}).get("total_cost_usd", 0.0)
    return transcript_tokens(tpath), cost, tpath


def _load_state(path):
    return load_blob(path)   # same OSError-on-corrupt contract as the mirror


def _save_state(path, state):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)


def state_init(path, task, blob_path, now):
    tok, cost, tpath = _snapshot(blob_path)
    _save_state(path, {
        "task": task, "started_at": now, "segment_start": now,
        "resume_count": 0, "pauses": [], "pause_at": None,
        "active_seconds": 0, "paused_seconds": 0,
        "tokens_accrued": {"in_out": 0, "cache": 0}, "cost_accrued_usd": 0.0,
        "token_baseline": tok, "cost_baseline_usd": cost, "transcript": tpath,
    })


def _accrue_segment(state, tok, cost):
    base = state["token_baseline"]
    state["tokens_accrued"]["in_out"] += max(0, tok["in_out"] - base["in_out"])
    state["tokens_accrued"]["cache"] += max(0, tok["cache"] - base["cache"])
    state["cost_accrued_usd"] += max(0.0, cost - state["cost_baseline_usd"])


def state_pause(path, blob_path, now, pct):
    state = _load_state(path)
    tok, cost, _ = _snapshot(blob_path)
    state["active_seconds"] += max(0, now - state["segment_start"])
    _accrue_segment(state, tok, cost)
    state["pauses"].append({"at": now, "pct": pct})
    state["pause_at"] = now
    _save_state(path, state)


def state_resume(path, blob_path, now):
    state = _load_state(path)
    tok, cost, tpath = _snapshot(blob_path)
    state["resume_count"] += 1
    if state.get("pause_at"):
        state["paused_seconds"] += max(0, now - state["pause_at"])
    state["pause_at"] = None
    state["segment_start"] = now
    state["token_baseline"] = tok
    state["cost_baseline_usd"] = cost
    state["transcript"] = tpath
    _save_state(path, state)


def summarize(state, blob_path, now):
    tok, cost, _ = _snapshot(blob_path)
    in_out = state["tokens_accrued"]["in_out"] + max(0, tok["in_out"] - state["token_baseline"]["in_out"])
    cache = state["tokens_accrued"]["cache"] + max(0, tok["cache"] - state["token_baseline"]["cache"])
    cost_total = state["cost_accrued_usd"] + max(0.0, cost - state["cost_baseline_usd"])
    return {
        "task": state.get("task", ""),
        "resume_count": state.get("resume_count", 0),
        "pause_pcts": ", ".join("{}%".format(p["pct"]) for p in state.get("pauses", [])
                                if p.get("pct") is not None),
        "total_seconds": max(0, now - state["started_at"]),
        "active_seconds": state["active_seconds"] + max(0, now - state["segment_start"]),
        "paused_seconds": state["paused_seconds"],
        "tokens": {"in_out": in_out, "cache": cache, "total": in_out + cache},
        "cost_usd": round(cost_total, 2),
    }


def _fmt_dur(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "{}h {}m".format(h, m)
    if m:
        return "{}m {}s".format(m, s)
    return "{}s".format(s)


def _fmt_tokens(n):
    if n >= 1_000_000:
        return "{:.1f}M".format(n / 1_000_000)
    if n >= 1_000:
        return "{:.0f}K".format(n / 1_000)
    return str(int(n))


def format_summary(s):
    pcts = " ({})".format(s["pause_pcts"]) if s["pause_pcts"] else ""
    title = "お疲れ! otsukare summary"
    if s["task"]:
        title += " — " + s["task"]
    t = s["tokens"]
    return "\n".join([
        title,
        "  Continued:  {}×{}".format(s["resume_count"], pcts),
        "  Run time:   {} total · {} active · {} waiting".format(
            _fmt_dur(s["total_seconds"]), _fmt_dur(s["active_seconds"]),
            _fmt_dur(s["paused_seconds"])),
        "  Tokens:     {} in/out · {} cache ({} total)".format(
            _fmt_tokens(t["in_out"]), _fmt_tokens(t["cache"]), _fmt_tokens(t["total"])),
        "  Cost:       ${:.2f}".format(s["cost_usd"]),
    ])


def _build_parser():
    p = argparse.ArgumentParser(description="otsukare usage-decision helper")
    # Path args use type=expand_user_path so a leading ~ (which PowerShell does not
    # expand for a native exe) is resolved at parse time, co-located with each arg.
    p.add_argument("--file", type=expand_user_path,
                   default=os.path.expanduser("~/.claude/last-statusline-input.json"))
    p.add_argument("--now", type=int, default=None, help="override current epoch (testing)")
    p.add_argument("--mtime", type=int, default=None, help="override file mtime (testing)")
    p.add_argument("--arm", action="store_true",
                   help="print the safety-net arm target for the 5h window")
    p.add_argument("--resume-check", type=int, default=None, metavar="RESET_EPOCH",
                   help="resume-clear check against this binding reset epoch")
    p.add_argument("--cron-for", type=int, default=None, metavar="EPOCH",
                   help="print the bounded retry-window cron string for this epoch")
    p.add_argument("--preflight", action="store_true",
                   help="classify applicability (subscription vs API / usage-billed)")
    p.add_argument("--wait-fresh", action="store_true",
                   help="block until the mirror file is freshly rendered with a current 5h reset")
    p.add_argument("--wait-timeout", type=int, default=30,
                   help="max seconds to wait for --wait-fresh")
    p.add_argument("--resolve-paths", action="store_true",
                   help="print absolute otsukare state paths for --session-id")
    p.add_argument("--session-id", default=None, help="session id (for --resolve-paths)")
    p.add_argument("--task-slug", default=None, help="task slug (for --resolve-paths)")
    p.add_argument("--touch-heartbeat", type=expand_user_path, default=None, metavar="PATH",
                   help="create/bump the heartbeat file at PATH")
    p.add_argument("--heartbeat", type=expand_user_path, default=None, metavar="PATH",
                   help="report alive/age for the heartbeat at PATH")
    p.add_argument("--heartbeat-stale-min", type=int, default=15,
                   help="minutes before a heartbeat is considered dead")
    p.add_argument("--cleanup", action="store_true",
                   help="remove the heartbeat (--heartbeat) and state (--state) files")
    p.add_argument("--state", type=expand_user_path, default=None,
                   help="path to the otsukare run-state JSON")
    p.add_argument("--state-action", choices=["init", "pause", "resume", "summary"],
                   default=None, help="run-metrics lifecycle action on the state file")
    p.add_argument("--task", default="", help="task name (for --state-action init)")
    p.add_argument("--pct", type=float, default=None,
                   help="usage %% at pause (for --state-action pause; floats ok)")
    for k, v in DEFAULTS.items():
        env = os.environ.get("OTSUKARE_" + k.upper())
        p.add_argument("--" + k.replace("_", "-"), type=int,
                       default=int(env) if env else v)
    return p


def main(argv=None):
    # CLI output may include non-ASCII (the お疲れ summary header). On Windows a
    # redirected/piped stdout defaults to the locale codec (e.g. cp1252) and would
    # raise UnicodeEncodeError; force UTF-8 so captured output never crashes.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = _build_parser().parse_args(argv)
    now = args.now if args.now is not None else int(time.time())
    if args.resolve_paths:
        if not args.session_id:
            print(json.dumps({"ok": False, "error": "--resolve-paths requires --session-id"}))
            return 2
        print(json.dumps(resolve_paths(args.session_id, task_slug=args.task_slug)))
        return 0
    if args.touch_heartbeat:
        touch_heartbeat(args.touch_heartbeat)
        print(json.dumps({"ok": True, "heartbeat": args.touch_heartbeat}))
        return 0
    if args.cron_for is not None:
        print(cron_for(args.cron_for))
        return 0
    if args.preflight:
        print(json.dumps(preflight(args.file)))
        return 0
    if args.wait_fresh:
        print(json.dumps(wait_fresh(args.file, timeout=args.wait_timeout)))
        return 0
    if args.cleanup:
        print(json.dumps(cleanup([args.heartbeat, args.state])))
        return 0
    if args.heartbeat is not None:
        print(json.dumps(heartbeat_status(args.heartbeat, now, args.heartbeat_stale_min)))
        return 0
    if args.state_action is not None:
        if not args.state:
            print(json.dumps({"ok": False, "error": "--state-action requires --state"}))
            return 2
        try:
            if args.state_action == "init":
                state_init(args.state, args.task, args.file, now)
                print(json.dumps({"ok": True, "state": args.state}))
            elif args.state_action == "pause":
                state_pause(args.state, args.file, now, args.pct)
                print(json.dumps({"ok": True}))
            elif args.state_action == "resume":
                state_resume(args.state, args.file, now)
                print(json.dumps({"ok": True}))
            else:  # summary
                print(format_summary(summarize(_load_state(args.state), args.file, now)))
        except OSError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        return 0
    cfg = {k: getattr(args, k) for k in DEFAULTS}
    try:
        mtime = args.mtime if args.mtime is not None else int(os.path.getmtime(args.file))
        if args.arm:
            print(json.dumps(arm_target(args.file, now, mtime, cfg)))
        elif args.resume_check is not None:
            print(json.dumps(resume_check(args.file, now, mtime, args.resume_check, cfg)))
        else:
            print(json.dumps(decide(args.file, now, mtime, cfg)))
    except OSError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
