#!/usr/bin/env python3
"""
check_logos.py — does every team in every season's games have its logo? Reads only.

The site draws a team's logo from v2/logos/{code}-{season}.png, trying the
code as given, then the other source's spelling (PHX/PHO, BKN/BRK, CHA/CHO),
then the previous season's file. A coloured circle with the abbreviation is
shown only when none of those exist. This lists, season by season:

  exact     the season's own logo is there
  fallback  only the previous season's logo was found, so an older mark shows
            (fine unless the team changed its logo that season)
  missing   nothing found: the circle shows

    python3 check_logos.py
"""
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
sys.path.insert(0, HOME)
import duckdb            # noqa: E402
import nightly2 as N     # noqa: E402

ALT = {"PHX": "PHO", "BKN": "BRK", "CHA": "CHO", "PHO": "PHX", "BRK": "BKN", "CHO": "CHA"}


def prev(season):
    y = int(season[:4])
    return "%d-%s" % (y - 1, str(y)[2:])


def main():
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    have = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="v2/logos/"):
        for o in page.get("Contents", []):
            name = o["Key"].rsplit("/", 1)[-1]
            if name.endswith(".png"):
                have.add(name[:-4])
    print("  %d logo files in R2\n" % len(have))

    seasons = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="v2/seasons/"):
        for o in page.get("Contents", []):
            if o["Key"].endswith("/games.parquet"):
                seasons.add(o["Key"].split("/")[2])
    con = duckdb.connect()
    tot = {"exact": 0, "fallback": 0, "missing": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for season in sorted(seasons):
            p = os.path.join(tmp, "g.parquet")
            s3.download_file(bucket, "v2/seasons/%s/games.parquet" % season, p)
            teams = sorted({t for (t,) in con.execute(
                "SELECT home_abbr FROM read_parquet(?) UNION SELECT away_abbr FROM read_parquet(?)",
                [p, p]).fetchall() if t})
            fb, miss = [], []
            for t in teams:
                codes = [t] + ([ALT[t]] if t in ALT else [])
                if any("%s-%s" % (c, season) in have for c in codes):
                    tot["exact"] += 1
                elif any("%s-%s" % (c, prev(season)) in have for c in codes):
                    tot["fallback"] += 1
                    fb.append(t)
                else:
                    tot["missing"] += 1
                    miss.append(t)
            line = "  %s  %2d teams" % (season, len(teams))
            if fb:
                line += "   fallback: " + " ".join(fb)
            if miss:
                line += "   MISSING: " + " ".join(miss)
            print(line)
    print("\n  exact %(exact)d · previous season's logo %(fallback)d · missing %(missing)d" % tot)


if __name__ == "__main__":
    main()
