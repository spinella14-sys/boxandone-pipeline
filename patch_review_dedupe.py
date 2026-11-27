#!/usr/bin/env python3
"""
patch_review_dedupe.py — run once.

The staging table holds a row for every night an NBA.com id was staged, so the
same person came into the review queue once per row (Stephen Thompson three
times). The queue now takes one item per id, with his latest name, team and
reason, his earliest first-seen and his total sightings. A decision already
applied every row for the id, so nothing else changes.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "review_queue.py")
src = open(PATH).read()
if "GROUP BY source, source_id" in src:
    print("  review_queue.py already patched")
    sys.exit()

OLD = '''            SELECT source, source_id, raw_name, raw_birthdate, raw_team, raw_league, occurrences,
                   first_seen, last_seen, proposed_player_id, match_score, match_reasons
            FROM staging_players WHERE match_status = 'pending'
            ORDER BY last_seen DESC""").fetchall():'''
NEW = '''            SELECT source, source_id, arg_max(raw_name, last_seen), MAX(raw_birthdate),
                   arg_max(raw_team, last_seen), arg_max(raw_league, last_seen), SUM(occurrences),
                   MIN(first_seen), MAX(last_seen), arg_max(proposed_player_id, last_seen),
                   MAX(match_score), arg_max(match_reasons, last_seen)
            FROM staging_players WHERE match_status = 'pending'
            GROUP BY source, source_id
            ORDER BY MAX(last_seen) DESC""").fetchall():'''
if src.count(OLD) != 1:
    sys.exit("  review_queue.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  review_queue.py patched: one queue item per id")
