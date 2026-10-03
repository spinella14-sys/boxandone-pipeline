#!/usr/bin/env python3
"""
parse_possessions.py — turn the event log into possessions.

Every rate stat so far has leaned on Dean Oliver's estimate,
FGA + 0.44*FTA - ORB + TOV, which is a good approximation and still an
approximation. Counting possessions directly replaces it, and is the
foundation for lineups, on/off and RAPM.

A possession ends on:

    a made field goal          (an and-one free throw is the SAME possession,
                                which is why the free-throw sequence matters)
    a made final free throw    "1 of 1", "2 of 2", "3 of 3" — the sequence is
                                in sub_type, so this is exact rather than guessed
    a defensive rebound        determined by comparing the rebounding team to
                                the team that missed, since sub_type is
                                "Unknown" on 95% of rebounds
    a turnover
    the end of a period

An offensive rebound continues the possession. So does a missed free throw that
the offence rebounds.

    python3 parse_possessions.py check --season 2025-26   # parse, write nothing
    python3 parse_possessions.py build                    # all seasons -> duckdb
    python3 parse_possessions.py build --season 2025-26
    python3 parse_possessions.py status

Writes a `possessions` table: one row per possession with the offensive team,
start and end event, clock bounds, points scored and why it ended.
"""

import argparse
import os
import re
import sys

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")

DDL = """
CREATE TABLE IF NOT EXISTS possessions (
    game_id        VARCHAR,
    period         SMALLINT,
    poss_num       INTEGER,
    off_team       VARCHAR,
    def_team       VARCHAR,
    start_action   BIGINT,
    end_action     BIGINT,
    start_secs     DOUBLE,
    end_secs       DOUBLE,
    points         SMALLINT,
    end_reason     VARCHAR,
    PRIMARY KEY (game_id, period, poss_num)
)
"""


def clock_secs(raw):
    """'PT11M35.00S' -> seconds remaining in the period."""
    if not raw:
        return None
    m = re.match(r"PT(\d+)M([\d.]+)S", raw)
    if not m:
        return None
    return int(m.group(1)) * 60 + float(m.group(2))


def shot_made(e):
    """Same fallback for field goals, in case shot_result is thin there too."""
    res = (e.get("shot_result") or "").lower()
    if res:
        return res == "made"
    return "MISS" not in (e.get("description") or "").upper()


def ft_made(e):
    """Free throws carry no shot_result — the field is null on all 62,296 of
    them. The description is what distinguishes them: a miss reads
    "MISS Brunson Free Throw 1 of 2", a make reads
    "Holmgren Free Throw 1 of 2 (12 PTS)" with the running total."""
    res = (e.get("shot_result") or "").lower()
    if res:
        return res == "made"
    d = (e.get("description") or "").upper()
    return "MISS" not in d


def is_last_ft(sub_type):
    """True when this free throw is the last of its set — the one that can end
    a possession. Technicals and flagrants are their own sets."""
    if not sub_type:
        return False
    s = sub_type.lower()
    if "technical" in s:
        return True
    m = re.search(r"(\d+)\s+of\s+(\d+)", s)
    return bool(m) and m.group(1) == m.group(2)


def next_meaningful(events, i):
    """The next event that could continue or end a possession, skipping the
    administrative ones."""
    for j in range(i + 1, len(events)):
        at = (events[j].get("action_type") or "").lower()
        # a foul sits between a made basket and its and-one free throw, and
        # carries the FOULING team, so it must not interrupt the lookahead
        if at in ("timeout", "instant replay", "substitution", "period", "",
                  "foul", "jump ball", "ejection", "violation"):
            continue
        return events[j]
    return None


def parse_game(events):
    """events: ordered dicts for one game. Returns possession rows."""
    out = []
    period = None
    cur = None          # the possession being built
    poss_num = 0
    last_shot_team = None   # whose miss is on the glass right now

    def close(reason, action, secs):
        nonlocal cur
        if cur and cur["off_team"]:
            cur["end_action"] = action
            cur["end_secs"] = secs
            cur["end_reason"] = reason
            out.append(cur)
        cur = None

    def open_poss(team, action, secs):
        nonlocal cur, poss_num
        if not team:
            return
        poss_num += 1
        cur = {"period": period, "poss_num": poss_num, "off_team": team,
               "def_team": None, "start_action": action, "start_secs": secs,
               "points": 0}

    for i, e in enumerate(events):
        at = (e.get("action_type") or "").lower()
        st = (e.get("sub_type") or "")
        team = e.get("team_abbr")
        action = e.get("action_id")
        secs = e.get("secs")

        if e.get("period") != period:
            close("period_end", action, secs)
            period = e.get("period")
            poss_num = 0
            last_shot_team = None

        if at == "period" or at == "timeout" or at == "instant replay":
            continue

        # a team with the ball that we have not opened a possession for
        # A foul carries the fouling team, not the team with the ball, so it
        # can never open a possession. Nor can a jump ball or a violation.
        if team and cur is None and at in ("made shot", "missed shot", "turnover",
                                           "free throw", "rebound", "heave"):
            open_poss(team, action, secs)

        if at == "made shot":
            if cur and cur["off_team"] == team:
                cur["points"] += (e.get("shot_value") or 2)
                # An and-one belongs to the SAME possession, so look ahead: if
                # the next live event is a free throw by this team, the basket
                # did not end anything and the free throw will close it.
                nxt = next_meaningful(events, i)
                and_one = (nxt is not None
                           and (nxt.get("action_type") or "").lower() == "free throw"
                           and nxt.get("team_abbr") == team)
                if not and_one:
                    close("made_fg", action, secs)
            last_shot_team = team

        elif at in ("missed shot", "heave"):
            last_shot_team = team

        elif at == "rebound":
            # sub_type is "Unknown" on almost every rebound, so the type comes
            # from whether the rebounding team is the one that just missed
            if last_shot_team and team:
                if team == last_shot_team:
                    pass                      # offensive — possession continues
                else:
                    close("def_rebound", action, secs)
                    open_poss(team, action, secs)
            last_shot_team = None

        elif at == "turnover":
            if cur and cur["off_team"] == team:
                close("turnover", action, secs)
            last_shot_team = None

        elif at == "free throw":
            made = ft_made(e)
            if cur and cur["off_team"] == team and made:
                cur["points"] += 1
            if is_last_ft(st):
                if made:
                    close("made_ft", action, secs)
                    last_shot_team = None
                else:
                    last_shot_team = team     # the miss is live, rebound decides

    close("period_end", events[-1]["action_id"] if events else None,
          events[-1]["secs"] if events else None)
    return out


def load_games(con, season=None, limit=None):
    q = """
      SELECT e.game_id, e.action_id, e.period, e.clock_raw, e.action_type,
             e.sub_type, e.player_id, e.team_abbr, e.shot_result, e.shot_value,
             e.description
      FROM play_by_play e
      JOIN games g ON g.game_id = e.game_id
    """
    args = []
    if season:
        q += " WHERE g.season = ?"
        args.append(season)
    q += " ORDER BY e.game_id, e.action_id"
    cur = con.execute(q, args)
    cols = [d[0] for d in cur.description]

    game, rows = None, []
    n = 0
    while True:
        batch = cur.fetchmany(50000)
        if not batch:
            break
        for r in batch:
            d = dict(zip(cols, r))
            d["secs"] = clock_secs(d.get("clock_raw"))
            if d["game_id"] != game:
                if rows:
                    yield game, rows
                    n += 1
                    if limit and n >= limit:
                        return
                game, rows = d["game_id"], []
            rows.append(d)
    if rows:
        yield game, rows


def cmd_check(season, limit):
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    tot = pts = 0
    reasons = {}
    games = 0
    per_game = []
    for gid, events in load_games(con, season, limit):
        rows = parse_game(events)
        games += 1
        tot += len(rows)
        pts += sum(r["points"] for r in rows)
        for r in rows:
            reasons[r["end_reason"]] = reasons.get(r["end_reason"], 0) + 1
        per_game.append(len(rows))

    print("  games parsed:       %d" % games)
    print("  possessions:        %d" % tot)
    print("  per game:           %.1f  (both teams, so ~%.1f each)"
          % (tot / games, tot / games / 2) if games else "")
    print("  points in them:     %d  (%.3f per possession)"
          % (pts, pts / tot if tot else 0))
    print("\n  how possessions ended:")
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("    %-14s %7d  %5.1f%%" % (k, v, 100 * v / tot))
    if per_game:
        per_game.sort()
        print("\n  per-game spread: min %d  median %d  max %d"
              % (per_game[0], per_game[len(per_game) // 2], per_game[-1]))
    con.close()
    print("\n  A modern NBA game is about 100 possessions per team, so ~200 here.")
    print("  Points per possession should land near 1.10 to 1.18.")


def cmd_build(season):
    import duckdb
    con = duckdb.connect(DB_PATH)
    con.execute(DDL)
    if season:
        con.execute("""DELETE FROM possessions WHERE game_id IN
                       (SELECT game_id FROM games WHERE season=?)""", [season])
    else:
        con.execute("DELETE FROM possessions")

    # DuckDB refuses a second connection to the same file with a different
    # configuration, so the write connection does the reading too.
    buf, n, games = [], 0, 0
    all_rows = []
    for gid, events in load_games(con, season):
        rows = parse_game(events)
        games += 1
        if games % 2000 == 0:
            print("  parsed %d games, %d possessions" % (games, len(all_rows)))
        for r in rows:
            all_rows.append((gid, r["period"], r["poss_num"], r["off_team"], None,
                             r["start_action"], r["end_action"], r["start_secs"],
                             r["end_secs"], r["points"], r["end_reason"]))
        # the generator is streaming off this same connection, so rows are
        # buffered and written after the read finishes rather than interleaved
    print("  inserting %d possessions..." % len(all_rows))
    for i in range(0, len(all_rows), 50000):
        con.executemany("INSERT INTO possessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        all_rows[i:i + 50000])
    n = len(all_rows)

    # the defending team is whoever else played in that game
    con.execute("""
        UPDATE possessions p SET def_team = (
          SELECT CASE WHEN g.home_abbr = p.off_team THEN g.away_abbr
                      ELSE g.home_abbr END
          FROM games g WHERE g.game_id = p.game_id)
        WHERE def_team IS NULL""")
    con.commit()
    print("\n  %d games, %d possessions stored" % (games, n))
    con.close()


def cmd_status():
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        print("  possessions: %d"
              % con.execute("SELECT COUNT(*) FROM possessions").fetchone()[0])
    except Exception:
        print("  no possessions table yet")
        return
    print("\n  by season:")
    for r in con.execute("""
        SELECT g.season, COUNT(DISTINCT p.game_id) games, COUNT(*) poss,
               ROUND(COUNT(*)::DOUBLE / COUNT(DISTINCT p.game_id), 1) per_game,
               ROUND(SUM(p.points)::DOUBLE / COUNT(*), 3) ppp
        FROM possessions p JOIN games g ON g.game_id = p.game_id
        GROUP BY 1 ORDER BY 1 DESC""").fetchall():
        print("    %-9s %5d games  %8d poss  %6.1f/g  %.3f ppp" % r)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build", "status"])
    ap.add_argument("--season")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.season, a.limit)
    elif a.cmd == "build":
        cmd_build(a.season)
    else:
        cmd_status()
