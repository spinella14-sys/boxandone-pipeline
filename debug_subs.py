#!/usr/bin/env python3
"""
debug_subs.py — walk one game's substitutions and show every decision.

Prints, for each substitution: the raw description, who player_id actually is,
the two names in the text, which roster the matcher searched, and what it
resolved to. Where resolution fails it prints the candidate pool so the reason
is visible rather than inferred.

    python3 debug_subs.py                      # the first game of 2025-26
    python3 debug_subs.py --game NBA_202510210OKC
"""

import argparse
import os
import re
import sys

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")

sys.path.insert(0, HOME)
from parse_lineups import norm, match_out, SUB_RE, team_of   # noqa: E402


def main():
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("--game")
    ap.add_argument("--limit", type=int, default=14)
    a = ap.parse_args()

    con = duckdb.connect(DB_PATH, read_only=True)
    gid = a.game or con.execute(
        "SELECT game_id FROM games ORDER BY game_date DESC LIMIT 1").fetchone()[0]

    home, away = con.execute(
        "SELECT home_abbr, away_abbr FROM games WHERE game_id=?", [gid]).fetchone()
    print("game %s   home %s   away %s\n" % (gid, home, away))

    names = dict(con.execute("SELECT player_id, full_name FROM players").fetchall())

    roster = {}
    starters = {}
    for pid, team, started, secs in con.execute("""
        SELECT player_id, team_abbr, started, seconds_played
        FROM player_game_box WHERE game_id=?""", [gid]).fetchall():
        roster.setdefault(team, []).append(pid)
        if started:
            starters.setdefault(team, []).append(pid)

    print("box score teams: %s" % list(roster))
    for t in roster:
        print("  %-5s %2d players, %d starters" % (t, len(roster[t]), len(starters.get(t, []))))
        print("        starters: %s" % ", ".join(names.get(p, p) for p in starters.get(t, [])))
    print()

    subs = con.execute("""
        SELECT action_id, period, clock_raw, team_abbr, player_id, description
        FROM play_by_play WHERE game_id=? AND action_type='Substitution'
        ORDER BY action_id LIMIT ?""", [gid, a.limit]).fetchall()

    print("pbp team codes in this game: %s" % sorted(
        {r[0] for r in con.execute(
            "SELECT DISTINCT team_abbr FROM play_by_play WHERE game_id=? AND team_abbr IS NOT NULL",
            [gid]).fetchall()}))
    print()

    on = {home: set(starters.get(home, [])), away: set(starters.get(away, []))}
    print("%-4s %-6s %-34s %-22s %-22s" %
          ("per", "team", "description", "player_id is", "resolved incoming"))
    print("-" * 104)

    for action_id, period, clock, pbp_team, pid, desc in subs:
        team = team_of(pbp_team)
        m = SUB_RE.search(desc or "")
        in_txt = m.group(1) if m else "?"
        out_txt = m.group(2) if m else "?"
        pid_name = names.get(pid, "(unknown id)")

        pool = roster.get(team, [])
        got_in = match_out(in_txt, pool, names)
        on_floor = on.get(team, set())
        got_out_from_floor = match_out(out_txt, on_floor, names)

        print("%-4s %-6s %-34s %-22s %-22s" % (
            period, "%s>%s" % (pbp_team, team), (desc or "")[:34],
            pid_name[:22], (names.get(got_in) or "FAILED")[:22]))

        if not got_in:
            print("       incoming text %r did not match any of %d roster names"
                  % (in_txt, len(pool)))
            print("       roster: %s" % ", ".join(names.get(p, p) for p in pool[:14]))
        # is player_id the one named after FOR?
        if pid and norm(pid_name).endswith(norm(out_txt)):
            pass
        else:
            print("       NOTE player_id (%s) does not match the name after FOR (%r)"
                  % (pid_name, out_txt))
            if got_out_from_floor:
                print("            but %r resolves on the floor to %s"
                      % (out_txt, names.get(got_out_from_floor)))

        if pid in on.get(team, set()):
            on[team].discard(pid)
            if got_in:
                on[team].add(got_in)
        else:
            print("       player_id not currently on the floor for %s" % team)

    con.close()


if __name__ == "__main__":
    main()
