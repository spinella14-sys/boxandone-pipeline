#!/usr/bin/env python3
"""
patch_realgm_mac.py — run once.

RealGM's bot check refuses GitHub's machines and Cloudflare's alike, so the
G League transaction log is read by the Mac's daily run (hourly.py in laptop
mode, once a day after 6am): the two-way step runs there in full — RealGM's
transactions, the two-way list, active lists, the tracker file. A change in
the stored G League moves now counts as a change, so the database is pushed
back and the moves aren't lost.

On GitHub nothing changes: the nightly's two-way step still runs, and its
RealGM request simply fails quickly.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "hourly.py")
src = open(PATH).read()
if "RealGM refuses" in src:
    print("  hourly.py already patched")
    sys.exit()

PAIRS = [
    ('''        """SELECT game_id FROM gl_games WHERE season = ? AND parsed ORDER BY 1""",''',
     '''        """SELECT game_id FROM gl_games WHERE season = ? AND parsed ORDER BY 1""",
        """SELECT txn_id FROM gl_transactions WHERE season = ? ORDER BY 1""",'''),
    ('''                log("G League step failed — the rest of the hour carries on")''',
     '''                log("G League step failed — the rest of the hour carries on")
        # RealGM refuses GitHub and the relay: on the Mac's daily run, read the
        # G League transaction log (and refresh the two-way tracker with it)
        if not os.environ.get("GITHUB_RUN_ID"):
            if not N.run(log, ["ingest_twoway.py", "run", "--season", season], timeout=900):
                log("two-way / RealGM step failed — the rest of the run carries on")'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  hourly.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  hourly.py patched: the Mac's daily run reads RealGM's G League transactions")
