#!/usr/bin/env python3
"""claude auto restater — 한 번에 설치·점검하는 스크립트 (idempotent).

  python -X utf8 setup.py            설치: ~/.claude/settings.json 에 상태표시줄 미러 + SessionStart 훅 등록(백업 남김)
  python -X utf8 setup.py --check    점검만(아무것도 바꾸지 않음)
  python -X utf8 setup.py --no-hook  훅 없이 상태표시줄만 등록

출력: 첫 줄에 JSON 보고, 그 아래에 한국어 요약과 다음 할 일.
종료 코드: 0 정상 / 2 settings.json 이 깨져 있어 손대지 않음.

왜 필요한가: 이 스킬은 Claude Code 상태표시줄이 매번 그려질 때 사용량(5시간·주간 %)을
~/.claude/last-statusline-input.json 에 비춰 주는 것을 읽는다. 상태표시줄 설정은 터미널
`claude` 세션을 **시작할 때만** 읽히므로, 설정을 넣은 뒤에는 세션을 껐다 켜야 한다.
데스크톱 앱 Code 탭은 상태표시줄을 그리지 않아 이 스킬이 동작하지 않는다.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATUSLINE = os.path.join(HERE, "statusline.py")
HOOK = os.path.join(HERE, "session_hook.py")
MARKER = "claude-auto-restater-setup-test"


def claude_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), ".claude")


def settings_path(home=None):
    return os.path.join(claude_dir(home), "settings.json")


def mirror_path(home=None):
    return os.path.join(claude_dir(home), "last-statusline-input.json")


def _q(p):
    return '"' + p + '"'


def statusline_command(py=None):
    return "{} -X utf8 {}".format(_q(py or sys.executable), _q(STATUSLINE))


def hook_command(py=None):
    return "{} -X utf8 {}".format(_q(py or sys.executable), _q(HOOK))


def load_settings(path):
    """(settings dict, existed). A corrupt file raises ValueError — never clobber it."""
    if not os.path.exists(path):
        return {}, False
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    if not text.strip():
        return {}, True
    return json.loads(text), True


def our_hook_present(settings):
    for entry in (settings.get("hooks") or {}).get("SessionStart") or []:
        for h in (entry or {}).get("hooks") or []:
            if "session_hook.py" in str((h or {}).get("command", "")):
                return True
    return False


def apply(settings, py=None, with_hook=True):
    """Merge our statusLine (and optionally the SessionStart hook) into settings in place.
    Returns the list of keys changed; [] means already set up."""
    changed = []
    want = statusline_command(py)
    sl = settings.get("statusLine")
    if not isinstance(sl, dict) or sl.get("type") != "command" or sl.get("command") != want:
        new = dict(sl) if isinstance(sl, dict) else {}
        new.update({"type": "command", "command": want})
        new.setdefault("refreshInterval", 30)
        settings["statusLine"] = new
        changed.append("statusLine")
    if with_hook and not our_hook_present(settings):
        hooks = settings.setdefault("hooks", {})
        lst = hooks.setdefault("SessionStart", [])
        lst.append({"hooks": [{"type": "command", "command": hook_command(py)}]})
        changed.append("hooks.SessionStart")
    return changed


def smoke_test(py=None, home=None):
    """Run statusline.py once with a marker blob inside a throw-away HOME and confirm it
    writes a mirror there. The real ~/.claude/last-statusline-input.json is never touched,
    so a live session (or a running guardrail) can't see fake usage data even for a moment."""
    import tempfile
    blob = json.dumps({"model": {"display_name": MARKER}})
    with tempfile.TemporaryDirectory() as tmp_home:
        env = dict(os.environ)
        env["HOME"] = tmp_home
        env["USERPROFILE"] = tmp_home
        try:
            r = subprocess.run([py or sys.executable, "-X", "utf8", STATUSLINE], input=blob,
                               capture_output=True, text=True, env=env, timeout=30)
            ok = r.returncode == 0 and MARKER in (r.stdout or "")
            out = (r.stdout or r.stderr or "").strip()[:120]
        except (OSError, subprocess.SubprocessError) as e:
            ok, out = False, str(e)[:120]
        wrote = False
        try:
            with open(mirror_path(tmp_home), encoding="utf-8") as f:
                wrote = (json.load(f).get("model") or {}).get("display_name") == MARKER
        except (OSError, ValueError):
            pass
    return {"ok": ok and wrote, "mirror_written": wrote, "output": out}


def check(home=None, py=None):
    sp, mp = settings_path(home), mirror_path(home)
    err = None
    try:
        settings, exists = load_settings(sp)
    except ValueError as e:
        settings, exists, err = {}, True, str(e)
    sl = settings.get("statusLine") or {}
    registered = isinstance(sl, dict) and sl.get("type") == "command" and "statusline.py" in str(sl.get("command", ""))
    points_here = bool(registered and os.path.normcase(STATUSLINE) in os.path.normcase(str(sl.get("command"))))
    mirror = {"path": mp, "exists": os.path.exists(mp)}
    if mirror["exists"]:
        mirror["age_seconds"] = int(time.time() - os.path.getmtime(mp))
        try:
            with open(mp, encoding="utf-8") as f:
                blob = json.load(f)
            rl = blob.get("rate_limits") or {}
            mirror["windows"] = sorted(k for k in rl if (rl.get(k) or {}).get("resets_at") is not None)
        except (OSError, ValueError):
            mirror["windows"] = None
    return {"settings_path": sp, "settings_exists": exists, "settings_error": err,
            "statusline_registered": registered, "statusline_points_to_this_skill": points_here,
            "hook_registered": our_hook_present(settings), "mirror": mirror,
            "python": py or sys.executable, "skill_dir": os.path.dirname(HERE)}


def _summary_ko(report):
    c, lines = report["check"], []
    if report.get("error"):
        lines.append("❌ settings.json 을 읽을 수 없어 손대지 않았습니다: " + report["error"])
        lines.append("   파일을 열어 JSON 오류를 고친 뒤 다시 실행해 주세요: " + c["settings_path"])
        return "\n".join(lines)
    if report["mode"] == "install":
        if report["changed"]:
            lines.append("✅ settings.json 에 넣었습니다: " + ", ".join(report["changed"]) + " (백업: " + str(report.get("backup") or "없음") + ")")
        else:
            lines.append("✅ 이미 설정돼 있어 바꾼 것이 없습니다.")
        st = report.get("smoke") or {}
        lines.append(("✅" if st.get("ok") else "⚠️") + " 상태표시줄 스크립트 시험 " + ("통과" if st.get("ok") else "실패: " + str(st.get("output"))))
    m = c["mirror"]
    if m["exists"] and m.get("age_seconds", 9999) <= 120 and m.get("windows"):
        lines.append("✅ 사용량 미러가 살아 있습니다(" + str(m["age_seconds"]) + "초 전, 창: " + ", ".join(m["windows"]) + ") → 바로 쓸 수 있습니다.")
    elif report["mode"] == "install" and report["changed"]:
        lines.append("➡️ 다음 할 일: 상태표시줄 설정은 세션을 시작할 때만 읽힙니다. 지금 열린 터미널 claude 를 /exit 로 끄고 다시 `claude` 를 켠 뒤, 같은 명령을 다시 입력하세요.")
        lines.append("   맨 아래에 '모델 · 5h N%' 줄이 보이면 준비된 것입니다.")
    elif c["statusline_registered"] and not m["exists"]:
        lines.append("⚠️ 설정은 돼 있는데 미러 파일이 아직 없습니다. 원인은 둘 중 하나: ① 지금 창이 데스크톱 앱(Code 탭)이라 상태표시줄이 없음 → 터미널에서 `claude` 로 쓰세요. ② 설정 뒤 터미널 claude 를 다시 켜지 않음 → /exit 후 다시 `claude`.")
    elif not c["statusline_registered"]:
        lines.append("⚠️ 상태표시줄이 등록돼 있지 않습니다. `python -X utf8 setup.py` 를 실행하세요.")
    lines.append("ℹ️ 데스크톱 앱 Code 탭에서는 상태표시줄이 없어 이 스킬이 동작하지 않습니다. 터미널 `claude` 세션에서 쓰세요.")
    return "\n".join(lines)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="claude auto restater installer / checker")
    p.add_argument("--check", action="store_true", help="report only, change nothing")
    p.add_argument("--no-hook", action="store_true", help="do not register the SessionStart hook")
    p.add_argument("--home", default=None, help=argparse.SUPPRESS)
    p.add_argument("--python", default=None, help=argparse.SUPPRESS)
    p.add_argument("--quiet", action="store_true", help="JSON only")
    a = p.parse_args(argv)
    report = {"mode": "check" if a.check else "install"}
    if a.check:
        report["check"] = check(a.home, a.python)
    else:
        sp = settings_path(a.home)
        try:
            settings, existed = load_settings(sp)
        except ValueError as e:
            report["error"] = str(e)
            report["check"] = check(a.home, a.python)
            print(json.dumps(report, ensure_ascii=False))
            if not a.quiet:
                print(_summary_ko(report))
            return 2
        backup = None
        if existed:
            backup = sp + ".bak-restater"
            shutil.copy(sp, backup)
        changed = apply(settings, a.python, with_hook=not a.no_hook)
        if changed:
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        report.update({"changed": changed, "backup": backup if changed else None,
                       "smoke": smoke_test(a.python, a.home), "check": check(a.home, a.python)})
    print(json.dumps(report, ensure_ascii=False))
    if not a.quiet:
        print(_summary_ko(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
