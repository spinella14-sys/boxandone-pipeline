#!/usr/bin/env python3
"""
install_mac_jobs.py — run the nightly and hourly jobs from this Mac, on time.

GitHub fires scheduled runs hours late and cannot reach stats.nba.com. This
Mac can, so it becomes the primary runner. GitHub's schedules stay as a
backup: both take the same database lock, so they never overlap, and a late
GitHub run after the Mac has finished finds nothing new.

Installs two launchd agents (they run as you, no admin rights needed):

  com.boxandone.nightly   05:07 every day   nightly2.py
  com.boxandone.hourly    :37 every hour    hourly.py

If the Mac is asleep at the scheduled time, launchd runs the job as soon as
it wakes. Every run appends to ~/boxandone/logs/{nightly,hourly}.log, and the
nightly run posts a macOS notification with its result.

    python3 install_mac_jobs.py install
    python3 install_mac_jobs.py status      # loaded? last result?
    python3 install_mac_jobs.py run nightly # run one now, in the background
    python3 install_mac_jobs.py uninstall
"""
import os
import plistlib
import subprocess
import sys

HOME = os.path.expanduser("~/boxandone")
LOGS = os.path.join(HOME, "logs")
AGENTS = os.path.expanduser("~/Library/LaunchAgents")
WRAPPER = os.path.join(HOME, "run_job.sh")
UID = os.getuid()

JOBS = {
    "nightly": {"script": "nightly2.py", "when": [{"Hour": 5, "Minute": 7}], "notify": True},
    "hourly": {"script": "hourly.py", "when": [{"Minute": 37}], "notify": False},
}

WRAP = r'''#!/bin/bash
# run_job.sh <name> <script> <notify 0|1> <python> — written by install_mac_jobs.py
NAME="$1"; SCRIPT="$2"; NOTIFY="$3"; PY="$4"
cd "$HOME/boxandone" || exit 1
mkdir -p logs
LOG="logs/$NAME.log"
{
  echo ""
  echo "===== $NAME $(date '+%Y-%m-%d %H:%M:%S %Z') ($PY) ====="
  "$PY" "$SCRIPT" 2>&1 | grep -v -e PythonDeprecationWarning -e 'warnings.warn'
  echo "===== exit ${PIPESTATUS[0]} ====="
} >> "$LOG"
# keep the log to its last 5,000 lines
tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
if [ "$NOTIFY" = "1" ]; then
  LAST="$(grep -E 'done \(|EXCEPTION|another run started|exit [1-9]' "$LOG" | tail -n 1)"
  [ -z "$LAST" ] && LAST="finished — see logs/$NAME.log"
  /usr/bin/osascript -e "display notification \"${LAST//\"/}\" with title \"Box and One — $NAME\""
fi
'''


def label(name):
    return "com.boxandone.%s" % name


def plist_path(name):
    return os.path.join(AGENTS, label(name) + ".plist")


def launchctl(*args):
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True)


def install():
    os.makedirs(AGENTS, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    with open(WRAPPER, "w") as f:
        f.write(WRAP)
    os.chmod(WRAPPER, 0o755)
    for name, job in JOBS.items():
        if not os.path.exists(os.path.join(HOME, job["script"])):
            sys.exit("  %s is not in ~/boxandone" % job["script"])
        plist = {
            "Label": label(name),
            # the same python3 that ran this installer, which has the
            # pipeline's packages; a bare "python3" could find a different one
            "ProgramArguments": ["/bin/bash", WRAPPER, name, job["script"],
                                 "1" if job["notify"] else "0", sys.executable],
            "StartCalendarInterval": job["when"],
            "WorkingDirectory": HOME,
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "BOXANDONE_HOME": HOME,
            },
            "StandardOutPath": os.path.join(LOGS, "%s.launchd.log" % name),
            "StandardErrorPath": os.path.join(LOGS, "%s.launchd.log" % name),
            "ProcessType": "Background",
        }
        path = plist_path(name)
        launchctl("bootout", "gui/%d" % UID, path)          # replace an older copy
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        r = launchctl("bootstrap", "gui/%d" % UID, path)
        print("  %-8s %s" % (name, "installed" if r.returncode == 0
                             else "FAILED: %s" % (r.stderr or r.stdout).strip()))
    print("\n  nightly at 05:07, hourly at :37. Logs: ~/boxandone/logs/")
    status()


def status():
    for name in JOBS:
        r = launchctl("print", "gui/%d/%s" % (UID, label(name)))
        if r.returncode != 0:
            print("  %-8s not loaded" % name)
            continue
        fields = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            for key in ("state", "last exit code", "runs"):
                if line.startswith(key + " ="):
                    fields[key] = line.split("=", 1)[1].strip()
        print("  %-8s loaded · state %s · runs %s · last exit %s"
              % (name, fields.get("state", "?"), fields.get("runs", "0"),
                 fields.get("last exit code", "never run")))
        log = os.path.join(LOGS, "%s.log" % name)
        if os.path.exists(log):
            tail = [l for l in open(log).read().splitlines() if l.strip()][-3:]
            for l in tail:
                print("           %s" % l[:110])


def run(name):
    r = launchctl("kickstart", "gui/%d/%s" % (UID, label(name)))
    print("  %s started in the background — follow it with: tail -f ~/boxandone/logs/%s.log"
          % (name, name) if r.returncode == 0 else "  could not start: %s" % r.stderr.strip())


def uninstall():
    for name in JOBS:
        launchctl("bootout", "gui/%d" % UID, plist_path(name))
        if os.path.exists(plist_path(name)):
            os.remove(plist_path(name))
        print("  %-8s removed" % name)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        install()
    elif cmd == "status":
        status()
    elif cmd == "run" and len(sys.argv) > 2 and sys.argv[2] in JOBS:
        run(sys.argv[2])
    elif cmd == "uninstall":
        uninstall()
    else:
        sys.exit(__doc__)
