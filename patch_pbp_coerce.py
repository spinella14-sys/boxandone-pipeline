#!/usr/bin/env python3
"""
patch_pbp_coerce.py

Parse died on:  Conversion Error: Could not convert string '' to INT32

NBA.com sends empty strings rather than omitting keys. scoreHome is "" on the
~74% of events that are not scoring plays, and numbers arrive as strings ("2",
"0") on the ones that are. Every numeric field is exposed, not just score:
personId, teamId, actionId, period, shotValue, shotDistance, xLegacy, yLegacy,
pointsTotal.

Adds two coercion helpers and routes every numeric field through them:
  _num(v)   -> int or None   ("" -> None, "2" -> 2, 0 -> None for id-like)
  _dec(v)   -> float or None

Also handles the sentinel convention: personId and teamId are 0, not null, on
non-player events, so those become NULL.

Re-parse is free; raw JSON is cached.

Run:  python3 patch_pbp_coerce.py
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/ingest_pbp.py")
DB_PATH = os.path.expanduser("~/boxandone/data/boxandone.duckdb")

HELPERS = '''
def _num(v, zero_is_null=False):
    """NBA sends "" for absent and stringified numbers for present."""
    if v is None or v == "":
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    if zero_is_null and n == 0:
        return None
    return n


def _dec(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


'''

OLD_BATCH = '''        batch = []
        for a in acts:
            pid_nba = a.get("personId") or None
            left = clock_seconds(a.get("clock"))
            per = a.get("period")
            resolved = idmap.get(str(pid_nba)) if pid_nba else None
            if pid_nba and not resolved:
                unresolved += 1
            batch.append([
                gid, a.get("actionNumber"), per, a.get("clock"), left,
                elapsed(per, left),
                pid_nba, resolved, a.get("playerName"),
                a.get("teamId"), a.get("teamTricode"),
                a.get("actionType"), a.get("subType"), a.get("description"),
                a.get("shotResult"),
                (a.get("shotValue") or None),
                a.get("location") or None,
                a.get("actionId"),
                a.get("shotDistance"),
                a.get("xLegacy"), a.get("yLegacy"),
                a.get("isFieldGoal") in (1, True), a.get("pointsTotal"),
                a.get("scoreHome"), a.get("scoreAway"),
            ])'''

NEW_BATCH = '''        batch = []
        for a in acts:
            # personId / teamId use 0 as the "no player / no team" sentinel
            pid_nba = _num(a.get("personId"), zero_is_null=True)
            team_id = _num(a.get("teamId"), zero_is_null=True)
            left = clock_seconds(a.get("clock"))
            per = _num(a.get("period"))
            is_fg = a.get("isFieldGoal") in (1, True, "1")
            resolved = idmap.get(str(pid_nba)) if pid_nba else None
            if pid_nba and not resolved:
                unresolved += 1
            batch.append([
                gid, _num(a.get("actionNumber")), per, a.get("clock") or None, left,
                elapsed(per, left),
                pid_nba, resolved, a.get("playerName") or None,
                team_id, a.get("teamTricode") or None,
                a.get("actionType") or None, a.get("subType") or None,
                a.get("description") or None,
                a.get("shotResult") or None,
                _num(a.get("shotValue"), zero_is_null=True),
                a.get("location") or None,
                _num(a.get("actionId")),
                _dec(a.get("shotDistance")) if is_fg else None,
                _num(a.get("xLegacy")) if is_fg else None,
                _num(a.get("yLegacy")) if is_fg else None,
                is_fg,
                _num(a.get("pointsTotal")),
                _num(a.get("scoreHome")), _num(a.get("scoreAway")),
            ])'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("%s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "def _num(" in src:
        print("    already patched")
    else:
        if OLD_BATCH not in src:
            sys.exit("    batch block does not match expected form — nothing changed")
        src = src.replace(OLD_BATCH, NEW_BATCH)
        anchor = "_CLOCK = re.compile"
        if anchor not in src:
            sys.exit("    could not place helpers — nothing changed")
        src = src.replace(anchor, HELPERS.lstrip("\n") + anchor)

        try:
            ast.parse(src)
        except SyntaxError as e:
            sys.exit("    patch invalid (%s) — nothing changed" % e)

        open(TARGET + ".bak_coerce", "w", encoding="utf-8").write(
            open(TARGET, encoding="utf-8").read())
        open(TARGET, "w", encoding="utf-8").write(src)
        print("    added _num()/_dec(); all numeric fields coerced")
        print("    shot coords/distance now NULL on non-shot events")
        print("    backup -> ingest_pbp.py.bak_coerce")

    import duckdb
    con = duckdb.connect(DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM play_by_play").fetchone()[0]
    if n:
        con.execute("DELETE FROM play_by_play")
    con.execute("UPDATE pbp_log SET parsed=FALSE WHERE parsed")
    con.commit()
    con.close()
    print("    cleared %d rows, queued re-parse" % n)
    print("\ndone. next: python3 ingest_pbp.py parse --season 2025-26")


if __name__ == "__main__":
    main()
