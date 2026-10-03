#!/usr/bin/env python3
"""
patch_divisions.py — season-aware division and conference on team_season.

Divisions matter because winning one has carried playoff implications, so a
standings view needs them. But they cannot be a flat abbreviation lookup:

  * The NBA ran FOUR divisions (Atlantic, Central, Midwest, Pacific) through
    2003-04 and SIX from 2004-05 on.
  * When the Hornets moved to New Orleans in 2002 they stayed in the EASTERN
    Conference Central Division for two seasons, then went to the Western
    Conference Southwest Division in the 2004-05 realignment. A flat map puts
    them in the West for 2002-03 and 2003-04, which is wrong.
  * Toronto, Miami and Orlando all changed division in 2004-05 without
    changing conference.

So the lookup is keyed on (season, abbreviation) with two eras.

Run:  python3 patch_divisions.py
Then: python3 export_full.py seasons
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/export_full.py")

# ---- 1996-97 .. 2003-04: four divisions -------------------------------------
OLD_ERA = {
    "Atlantic": ["BOS", "MIA", "NJN", "NYK", "ORL", "PHI", "WAS", "WSB"],
    "Central":  ["ATL", "CHH", "NOH", "CHI", "CLE", "DET", "IND", "MIL", "TOR"],
    "Midwest":  ["DAL", "DEN", "HOU", "MIN", "SAS", "UTA", "VAN", "MEM"],
    "Pacific":  ["GSW", "LAC", "LAL", "PHX", "PHO", "POR", "SAC", "SEA"],
}
OLD_CONF = {"Atlantic": "East", "Central": "East",
            "Midwest": "West", "Pacific": "West"}

# ---- 2004-05 onward: six divisions ------------------------------------------
NEW_ERA = {
    "Atlantic":  ["BOS", "NJN", "BKN", "NYK", "PHI", "TOR"],
    "Central":   ["CHI", "CLE", "DET", "IND", "MIL"],
    "Southeast": ["ATL", "CHA", "CHO", "MIA", "ORL", "WAS"],
    "Northwest": ["DEN", "MIN", "SEA", "OKC", "POR", "UTA"],
    "Pacific":   ["GSW", "LAC", "LAL", "PHX", "PHO", "SAC"],
    "Southwest": ["DAL", "HOU", "MEM", "NOH", "NOK", "NOP", "SAS"],
}
NEW_CONF = {"Atlantic": "East", "Central": "East", "Southeast": "East",
            "Northwest": "West", "Pacific": "West", "Southwest": "West"}

REALIGNMENT = "2004-05"


def build_case(field):
    """SQL CASE over (season, abbr). Seasons sort lexically, so a string
    comparison against '2004-05' splits the two eras correctly."""
    lines = ["CASE"]
    lines.append("           WHEN o.season < '%s' THEN CASE o.team_abbr" % REALIGNMENT)
    for div, abbrs in OLD_ERA.items():
        val = div if field == "division" else OLD_CONF[div]
        for a in abbrs:
            lines.append("             WHEN '%s' THEN '%s'" % (a, val))
    lines.append("             ELSE NULL END")
    lines.append("           ELSE CASE o.team_abbr")
    for div, abbrs in NEW_ERA.items():
        val = div if field == "division" else NEW_CONF[div]
        for a in abbrs:
            lines.append("             WHEN '%s' THEN '%s'" % (a, val))
    lines.append("             ELSE NULL END")
    lines.append("           END AS %s" % field)
    return "\n".join(lines)


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "AS division" in src:
        print("  already patched")
        return
    if "AS wins" not in src and "w.w AS wins" not in src:
        sys.exit("  run patch_standings.py first")

    # replace the flat conference CASE with the season-aware pair
    start = src.find("       CASE o.team_abbr\n")
    if start == -1:
        sys.exit("  could not find the conference CASE block")
    end = src.find("ELSE NULL END AS conference", start)
    if end == -1:
        sys.exit("  conference CASE block does not end as expected")
    end = src.find("\n", end) + 1

    replacement = ("       " + build_case("conference") + ",\n"
                   "       " + build_case("division") + "\n")
    src = src[:start] + replacement + src[end:]

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_div", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)

    print("  conference and division are now season-aware")
    print("    1996-97 .. 2003-04   Atlantic, Central | Midwest, Pacific")
    print("    2004-05 onward       Atlantic, Central, Southeast |")
    print("                         Northwest, Pacific, Southwest")
    print("  New Orleans sits in the East (Central) for 2002-03 and 2003-04,")
    print("  then the West (Southwest) from 2004-05.")
    print("  backup -> export_full.py.bak_div")
    print("\n  next: python3 export_full.py seasons")


if __name__ == "__main__":
    main()
