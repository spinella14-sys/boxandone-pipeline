#!/usr/bin/env python3
"""
patch_nightly_season.py

nightly.py died on:
    ValueError: invalid literal for int() with base 10: '2025-26'

ingest_games.py's --seasons takes END YEARS (2026:2026) because that is what
BBRef's URLs use. bridge_games.py and ingest_pbp.py take season LABELS
(2025-26). nightly.py was passing labels to all three.

Adds season_end_year() and uses it for the one call that needs it.

Run:  python3 patch_nightly_season.py
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/nightly.py")

HELPER = '''def season_end_year(season):
    """'2025-26' -> 2026. ingest_games.py keys on BBRef's end-year URLs."""
    return int(season.split("-")[0]) + 1


'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("%s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "season_end_year" in src:
        print("  already patched")
        return

    old = ('        ok &= run(log, ["ingest_games.py", "schedule",\n'
           '                        "--seasons", "%s:%s" % (season, season)])[0]')
    new = ('        ey = season_end_year(season)\n'
           '        ok &= run(log, ["ingest_games.py", "schedule",\n'
           '                        "--seasons", "%d:%d" % (ey, ey)])[0]')

    if old not in src:
        sys.exit("  schedule call does not match expected form — nothing changed")
    src = src.replace(old, new)

    anchor = "def current_season(today=None):"
    if anchor not in src:
        sys.exit("  could not place helper — nothing changed")
    src = src.replace(anchor, HELPER + anchor)

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_season", "w", encoding="utf-8").write(src if False else
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  ingest_games.py now receives end years (2026:2026)")
    print("  bridge_games.py and ingest_pbp.py keep season labels")
    print("  backup -> nightly.py.bak_season")


if __name__ == "__main__":
    main()
