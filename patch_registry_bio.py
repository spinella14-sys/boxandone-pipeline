#!/usr/bin/env python3
"""
patch_registry_bio.py — let the registry export see the enrichment table.

enrich_players.py writes what NBA.com knows about a player into player_bio
rather than into players, because DuckDB will not update a row that other
tables reference by foreign key — and because keeping a source's claims
separate from the registry is the better shape anyway.

The consequence is that none of it reaches the app: the registry export reads
players alone, so 3,304 birthdates and 2,875 heights sit in the database
invisible. This joins the two, preferring whatever the registry already holds
and falling back to the enrichment.

That order matters. A value in players came from Basketball Reference or from
an admin; a value in player_bio came from a scrape. Where they disagree the
first should win, and this never overwrites, it only fills.

Run:  python3 patch_registry_bio.py
Then: python3 export_full.py registry
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/export_full.py")

OLD = '''SELECT p.player_id, p.full_name, p.display_name, p.name_normalized,
       p.birthdate, p.birthdate_status, p.position,
       p.height_in, p.weight_lb, p.college, p.draft_year, p.draft_round,
       p.draft_pick, p.status, p.nationality,'''

NEW = '''SELECT p.player_id, p.full_name, p.display_name, p.name_normalized,
       -- the registry first, the scrape second: a value already here was put
       -- there by a better-trusted source, so enrichment fills blanks only
       COALESCE(p.birthdate, bio.birthdate) AS birthdate,
       CASE WHEN p.birthdate IS NULL AND bio.birthdate IS NOT NULL
            THEN 'confirmed' ELSE p.birthdate_status END AS birthdate_status,
       p.position,
       COALESCE(p.height_in, bio.height_in) AS height_in,
       COALESCE(p.weight_lb, bio.weight_lb) AS weight_lb,
       COALESCE(p.college, bio.college) AS college,
       COALESCE(p.draft_year, bio.draft_year) AS draft_year,
       COALESCE(p.draft_round, bio.draft_round) AS draft_round,
       COALESCE(p.draft_pick, bio.draft_pick) AS draft_pick,
       p.status,
       COALESCE(p.nationality, bio.country) AS nationality,'''

JOIN_AFTER = ("LEFT JOIN hot.player_identifiers n "
              "ON n.player_id=p.player_id AND n.source='nba'")
JOIN_NEW = JOIN_AFTER + """
LEFT JOIN hot.player_bio bio ON bio.player_id=p.player_id AND bio.source='nba'"""


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "player_bio" in src:
        print("  already patched")
        return
    if OLD not in src:
        sys.exit("  REGISTRY_SQL does not match the expected form — not touching it")
    if JOIN_AFTER not in src:
        sys.exit("  could not find the nba identifier join")

    src = src.replace(OLD, NEW).replace(JOIN_AFTER, JOIN_NEW)

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_bio", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  registry export now falls back to player_bio")
    print("  backup -> export_full.py.bak_bio")
    print("\n  next: python3 export_full.py registry")


if __name__ == "__main__":
    main()
