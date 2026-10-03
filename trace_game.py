#!/usr/bin/env python3
"""
trace_game.py — walk one game's substitutions and stop at the first divergence.

Only the first failure in a game is informative; everything after it is the
consequence of a lineup that was already wrong. This prints every substitution
in order with the floor before and after, then stops where the invariant breaks
and dumps the surrounding events.

    python3 trace_game.py --game NBA_202510220ATL
"""

import argparse
import os
import sys

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")

sys.path.insert(0, HOME)
from parse_lineups import norm, match_out, match_in, SUB_RE, team_of   # noqa: E402


def short(names, pid):
    n = names.get(pid, pid or "?")
    parts = n.split()
    return parts[-1] if len(parts) < 3 else " ".join(parts[-2:])


def main():
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True)
    a = ap.parse_args()

    con = duckdb.connect(DB_PATH, read_only=True)
    gid = a.game
    home, away = con.execute(
        "SELECT home_abbr, away_abbr FROM games WHERE game_id=?", [gid]).fetchone()
    names = dict(con.execute("SELECT player_id, full_name FROM players").fetchall())

    roster, starters = {}, {}
    for pid, team, started in con.execute("""
        SELECT player_id, team_abbr, started FROM player_game_box
        WHERE game_id=?""", [gid]).fetchall():
        roster.setdefault(team, []).append(pid)
        if started:
            starters.setdefault(team, []).append(pid)

    on = {home: set(starters.get(home, [])), away: set(starters.get(away, []))}
    print("%s   home %s   away %s" % (gid, home, away))
    for t in (home, away):
        print("  %s starters: %s" % (t, ", ".join(short(names, p) for p in sorted(on[t]))))
    print()

    events = con.execute("""
        SELECT action_id, period, clock_raw, action_type, team_abbr, player_id,
               description
        FROM play_by_play WHERE game_id=? ORDER BY action_id""", [gid]).fetchall()

    subs = 0
    for idx, (aid, period, clock, at, pbp_team, pid, desc) in enumerate(events):
        if (at or "").lower() != "substitution":
            continue
        subs += 1
        team = team_of(pbp_team)
        m = SUB_RE.search(desc or "")
        in_txt = m.group(1) if m else None
        outgoing = pid
        incoming = match_in(in_txt, team, roster, names, on.get(team, set())) if m else None

        problem = None
        if not incoming or not outgoing:
            problem = "could not resolve (in=%r -> %s, out=%s)" % (
                in_txt, names.get(incoming, "FAIL"), names.get(outgoing, "FAIL"))
        elif outgoing not in on.get(team, set()):
            problem = "outgoing %s is not on the floor" % short(names, outgoing)
        elif incoming in on.get(team, set()):
            problem = "incoming %s is already on the floor" % short(names, incoming)

        if problem:
            print("DIVERGED at substitution #%d" % subs)
            print("  period %s  clock %s" % (period, clock))
            print("  description: %s" % desc)
            print("  player_id:   %s" % names.get(pid, pid))
            print("  problem:     %s" % problem)
            print("  %s floor:    %s" % (team, ", ".join(sorted(short(names, p) for p in on.get(team, set())))))
            print("  %s bench:    %s" % (team, ", ".join(sorted(
                short(names, p) for p in roster.get(team, []) if p not in on.get(team, set())))))
            print("\n  surrounding events:")
            for j in range(max(0, idx - 6), min(len(events), idx + 7)):
                e = events[j]
                mark = ">>" if j == idx else "  "
                print("   %s p%s %-11s %-14s %-6s %-20s %s" % (
                    mark, e[1], e[2] or "", (e[3] or "")[:14], e[4] or "",
                    short(names, e[5])[:20], (e[6] or "")[:40]))
            con.close()
            return

        on[team].discard(outgoing)
        on[team].add(incoming)
        print("  #%-3d p%s %-11s %-5s %-18s out, %-18s in   -> %s" % (
            subs, period, clock, team, short(names, outgoing), short(names, incoming),
            ", ".join(sorted(short(names, p) for p in on[team]))))

    print("\n  walked all %d substitutions with no divergence" % subs)
    for t in (home, away):
        print("  %s final floor: %s" % (t, ", ".join(sorted(short(names, p) for p in on[t]))))
    con.close()


if __name__ == "__main__":
    main()
