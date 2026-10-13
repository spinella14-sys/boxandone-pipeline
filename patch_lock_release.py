#!/usr/bin/env python3
"""
patch_lock_release.py — run once. Adds a "Release lock" step to the nightly
and hourly workflows that runs when GitHub cancels or fails a run, so a run
killed at its time limit no longer blocks the next two hours of runs.
"""
import os
import sys

WF = os.path.expanduser("~/boxandone/.github/workflows")
STEP = '''
      # A run GitHub kills at its time limit never reaches its own cleanup;
      # free the lock it holds (and only that lock) so the next run can start.
      - name: Release lock
        if: cancelled() || failure()
        env:
          BOXANDONE_HOME: /home/runner/boxandone
        run: python ~/boxandone/release_lock.py
'''
targets = {
    "nightly.yml": "        run: python ~/boxandone/nightly2.py\n",
    "transactions.yml": "        run: python ~/boxandone/hourly.py\n",
}
for name, anchor in targets.items():
    path = os.path.join(WF, name)
    src = open(path).read()
    if "release_lock.py" in src:
        print("  %-17s already patched" % name)
        continue
    if src.count(anchor) != 1:
        sys.exit("  %s: Run step not found as expected — nothing written" % name)
    open(path, "w").write(src.replace(anchor, anchor + STEP))
    print("  %-17s patched" % name)
