#!/usr/bin/env python3
"""
arc_bands.py — where the HELP bands land in BPM, season by season.

Mirrors src/lib/arc.js on the site: upper bands by rank among players with
500+ regular-season minutes (FR top 5, CS to 22, KS to 52, ST to 105),
replacement level at BPM -2.0 on the RE/RO line, Key Reserve the top 40% of
the players between Starter and replacement. Use it to judge the counts
against real seasons before trusting the chart.

    python3 arc_bands.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/boxandone"))
import nightly2 as N  # noqa: E402

RANKS = [("FR", 5), ("CS", 22), ("KS", 52), ("ST", 105)]
env = N.load_env()
s3 = N.s3c(env)
meta = json.loads(s3.get_object(Bucket=env["R2_BUCKET_NAME"],
                                Key="v2/config/arc_meta.json")["Body"].read())
print("window noise: BPM sd %s from %s back-to-back pairs\n"
      % (meta.get("noise_sd"), meta.get("noise_pairs")))
print("  %-8s %5s  %6s %6s %6s %6s %6s   %s" % ("season", "pool", "FR", "CS", "KS", "ST", "KR",
                                                "players above replacement (BPM -2)"))
for season, dist in sorted(meta["season_bpm"].items()):
    if len(dist) < 150:
        print("  %-8s %5d  too few qualified players — borrows the previous season" % (season, len(dist)))
        continue
    rep = sum(1 for v in dist if v >= -2.0)
    kr = round(105 + 0.4 * max(0, rep - 105))
    floors = [dist[min(n, len(dist)) - 1] for _, n in RANKS] + [dist[min(kr, len(dist)) - 1]]
    print("  %-8s %5d  %s   %d  (KR to rank %d, RE %d-%d)"
          % (season, len(dist), " ".join("%+6.1f" % f for f in floors), rep, kr, kr + 1, rep))
print("\nEach column is the lowest BPM inside that band. Below replacement the bands are"
      "\nfixed placeholders: RO to -4.0, BO to -6.0, GL to -8.0, ML below.")
