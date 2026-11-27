#!/usr/bin/env python3
"""
patch_nightly_backup.py — run once.

On Saturday, Nov 14, GitHub never started the 5am nightly — no log was
written at all — so the night's data waited until someone ran it by hand.
GitHub doesn't promise to run a scheduled job, or to run it on time.

Two backup start times are added, about 7am and 8am Eastern. A backup run
looks in R2 for a successful nightly from this morning and, finding one,
exits at once without touching anything. Only when the 5am run never
happened (or failed) does the backup do the night's work. Manual runs are
unaffected.

    python3 patch_nightly_backup.py
    git add nightly2.py .github/workflows/nightly.yml && git commit -m "Nightly backup runs" && git push
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s not as expected (%r) — nothing written" % (name, old[:60]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %s patched" % name)


patch(".github/workflows/nightly.yml", [
    ("""    - cron: '7 10 * * *'     # 5:07am EST (November-March)
""",
     """    - cron: '7 10 * * *'     # 5:07am EST (November-March)
    # Backups, in case GitHub skips or delays the run above: about 7am and
    # 8am Eastern. Each exits at once if this morning's run already succeeded.
    - cron: '7 12 * * *'
    - cron: '7 13 * * *'
"""),
    ("""    outputs:
      skip: ${{ steps.slot.outputs.skip }}""",
     """    outputs:
      skip: ${{ steps.slot.outputs.skip }}
      backup: ${{ steps.slot.outputs.backup }}"""),
    ("""          echo "Eastern zone $zone, trigger '$slot', skip=$skip"
          echo "skip=$skip" >> "$GITHUB_OUTPUT\"""",
     """          backup=false
          if [ "$slot" = "7 12 * * *" ] || [ "$slot" = "7 13 * * *" ]; then backup=true; fi
          echo "Eastern zone $zone, trigger '$slot', skip=$skip, backup=$backup"
          echo "skip=$skip" >> "$GITHUB_OUTPUT"
          echo "backup=$backup" >> "$GITHUB_OUTPUT\""""),
    ("""          BOXANDONE_HOME: /home/runner/boxandone
        run: python ~/boxandone/nightly2.py""",
     """          BOXANDONE_HOME: /home/runner/boxandone
          NIGHTLY_BACKUP: ${{ needs.gate.outputs.backup }}
        run: python ~/boxandone/nightly2.py"""),
], "NIGHTLY_BACKUP")

patch("nightly2.py", [
    ("""def write_log(s3, bucket, log, ok):""",
     """def ran_this_morning(s3, bucket):
    \"\"\"Did a nightly finish successfully today (UTC), after 3am Eastern?\"\"\"
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        r = s3.list_objects_v2(Bucket=bucket, Prefix="%s/%s_" % (LOG_PREFIX, day))
    except Exception:
        return False
    for o in r.get("Contents", []):
        name = os.path.basename(o["Key"])      # 20261114_101532_ok.txt
        if name.endswith("_ok.txt") and name[9:11] >= "08":
            return True
    return False


def write_log(s3, bucket, log, ok):"""),
    ("""    if not acquire_lock(s3, bucket, log):
        # say so in R2 rather than exiting without a trace""",
     """    # a backup start (7am / 8am Eastern) does nothing if the 5am run succeeded
    if os.environ.get("NIGHTLY_BACKUP") == "true" and ran_this_morning(s3, bucket):
        log("backup start: this morning's nightly already succeeded — nothing to do")
        return

    if not acquire_lock(s3, bucket, log):
        # say so in R2 rather than exiting without a trace"""),
], "ran_this_morning")
