#!/usr/bin/env python3
"""
patch_twoway_counts.py — run once.

1. Active-game counts. A player with no two-way signing this season (in the
   second year of a two-year deal, or signed before our feeds begin) fell
   through to "the first day we saw him on the two-way list" — and we only
   began recording the list on Nov 11, so his count started there. Now:
     - a signing or conversion this season (NBA.com, then Basketball
       Reference) counts from that date, with the limit prorated;
     - "first seen on the list" is used only when an earlier day's list
       didn't have him, i.e. he truly arrived after we started watching;
     - otherwise he counts from the season's first game, against the full 50.

2. Open slots. The team list came from the schedule, which carries a blank
   code for games not yet decided; that was the blank card. Now it's the 30
   NBA teams by NBA.com id.

3. Last 10. Each player's line for his NBA team's last 10 games: games on the
   active list, games played, minutes. NBA box minutes are now stored; this
   season's active lists are read again once to fill them in (from the local
   cache on the Mac; from NBA's CDN on GitHub).

4. `python3 ingest_twoway.py explain "RayJ Dennis"` — how a player's count is
   worked out: ids, signing date and source, limit, and every game counted.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_twoway.py")
src = open(PATH).read()
if "def cmd_explain" in src:
    print("  ingest_twoway.py already patched")
    sys.exit()

PAIRS = [
    # minutes on the active-list table; refill this season's rows once
    ('''    for col in ("realgm_id VARCHAR", "nba_team VARCHAR", "source VARCHAR"):
        con.execute("ALTER TABLE gl_transactions ADD COLUMN IF NOT EXISTS " + col)
    return con''',
     '''    for col in ("realgm_id VARCHAR", "nba_team VARCHAR", "source VARCHAR"):
        con.execute("ALTER TABLE gl_transactions ADD COLUMN IF NOT EXISTS " + col)
    has = con.execute("""SELECT COUNT(*) FROM information_schema.columns
                         WHERE table_name = 'nba_active' AND column_name = 'seconds'""").fetchone()[0]
    if not has:
        con.execute("ALTER TABLE nba_active ADD COLUMN seconds INTEGER")
        con.execute("DELETE FROM nba_active")       # read again, with minutes
    return con'''),

    ('''                con.execute("INSERT INTO nba_active VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                            [gid, str(p.get("personId")), t.get("teamTricode"), gdate, season,
                             status == "ACTIVE", str(p.get("played")) == "1",
                             p.get("notPlayingReason") or p.get("notPlayingDescription")])''',
     '''                con.execute("""INSERT INTO nba_active (nba_game_id, person_id, team_abbr, game_date, season,
                                                       active, played, reason, seconds)
                               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                            [gid, str(p.get("personId")), t.get("teamTricode"), gdate, season,
                             status == "ACTIVE", str(p.get("played")) == "1",
                             p.get("notPlayingReason") or p.get("notPlayingDescription"),
                             iso_seconds((p.get("statistics") or {}).get("minutes"))])'''),

    ('''def cmd_active(con, season):''',
     '''def iso_seconds(v):
    """NBA's 'PT24M35.00S' as seconds."""
    m = re.match(r"PT(?:(\\d+)M)?(?:([\\d.]+)S)?", str(v or ""))
    if not m or not (m.group(1) or m.group(2)):
        return 0
    return int(m.group(1) or 0) * 60 + int(float(m.group(2) or 0))


def cmd_active(con, season):'''),

    # signing date: no more counting from the day we started watching
    ('''        if not sign_date:
            fs = first_seen.get((name, abbr))
            sign_date, sign_src = (fs, "first seen on the list") if fs and start and fs > start else (None, "before the season")''',
     '''        if not sign_date:
            fs = first_seen.get((name, abbr))
            if fs and first_snap and fs > first_snap and start and fs > start:
                sign_date, sign_src = fs, "first seen on the list"
            else:
                sign_date, sign_src = None, "no signing this season — counted from the first game"'''),
    ('''    nba_ids = dict(con.execute("SELECT player_id, source_id FROM player_identifiers WHERE source='nba'").fetchall())''',
     '''    first_snap = con.execute("SELECT MIN(snap_date) FROM twoway_list WHERE season = ?", [season]).fetchone()[0]
    nba_ids = dict(con.execute("SELECT player_id, source_id FROM player_identifiers WHERE source='nba'").fetchall())'''),

    # last 10 of his NBA team's games
    ('''        left = None if used is None else max(0, limit - used)''',
     '''        left = None if used is None else max(0, limit - used)
        l10 = None
        if person and abbr:
            l10 = con.execute("""
                WITH last AS (SELECT nba_game_id FROM scheduled_games
                              WHERE season = ? AND season_type = 'regular' AND status = 'final'
                                AND (home_abbr = ? OR away_abbr = ?)
                              ORDER BY game_date DESC LIMIT 10)
                SELECT COUNT(*) FILTER (WHERE a.active), COUNT(*) FILTER (WHERE a.played),
                       COALESCE(SUM(a.seconds), 0), (SELECT COUNT(*) FROM last)
                FROM last LEFT JOIN nba_active a
                  ON a.nba_game_id = last.nba_game_id AND a.person_id = ? AND a.team_abbr = ?""",
                [season, abbr, abbr, person, abbr]).fetchone()'''),
    ('''            "can_reach_limit": bool(left is not None and left <= len(upcoming)),''',
     '''            "can_reach_limit": bool(left is not None and left <= len(upcoming)),
            "l10_games": l10[3] if l10 else None, "l10_active": l10[0] if l10 else None,
            "l10_played": l10[1] if l10 else None, "l10_min": round(l10[2] / 60, 1) if l10 else None,'''),

    # the 30 teams, not whatever the schedule's codes are
    ('''    teams = [a for (a,) in con.execute("SELECT DISTINCT home_abbr FROM scheduled_games WHERE season = ? "
                                         "AND season_type = 'regular'", [season]).fetchall()]''',
     '''    teams = list(TEAM_IDS.values())'''),

    # explain
    ('''    ap.add_argument("cmd", choices=["run", "sample", "movement"])''',
     '''    ap.add_argument("cmd", choices=["run", "sample", "movement", "explain"])
    ap.add_argument("name", nargs="?")'''),
    ('''    if a.cmd == "movement":''',
     '''    if a.cmd == "explain":
        cmd_explain(a.name, a.season or current_season())
        return
    if a.cmd == "movement":'''),
    ('''def main():''',
     '''def cmd_explain(name, season):
    """How one two-way player's count is worked out."""
    con = db()
    try:
        rows, meta = build_tracker(con, season)
        hit = [r for r in rows if norm(r["name"]) == norm(name or "")]
        if not hit:
            print("  %r is not on the latest two-way list" % name)
            return
        r = hit[0]
        person = con.execute("SELECT source_id FROM player_identifiers WHERE source = 'nba' AND player_id = ?",
                             [r["player_id"]]).fetchall()
        print("  %s — %s, player %s, NBA.com id %s" % (r["name"], r["nba_abbr"], r["player_id"],
                                                         ", ".join(p[0] for p in person) or "none"))
        print("  signed %s (%s); limit %s; active %s; left %s" % (r["signed"] or "-", r["signed_source"],
                                                                 r["limit"], r["used"], r["left"]))
        print("  last 10: active %s, played %s, %s min" % (r["l10_active"], r["l10_played"], r["l10_min"]))
        for kind, d, desc in con.execute("""SELECT kind, txn_date, description FROM nba_movement
                                            WHERE player_id = ? ORDER BY txn_date""", [r["player_id"]]).fetchall():
            print("    NBA.com  %s  %-20s %s" % (d, kind, desc[:90]))
        for p in person:
            for d, team, act, played, secs in con.execute("""
                    SELECT game_date, team_abbr, active, played, seconds FROM nba_active
                    WHERE season = ? AND person_id = ? ORDER BY game_date""", [season, p[0]]).fetchall():
                print("    %s %s  %-8s %s" % (d, team, "ACTIVE" if act else "inactive",
                                              "%d min" % round((secs or 0) / 60) if played else ""))
    finally:
        con.close()


def main():'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  ingest_twoway.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  ingest_twoway.py patched: counts from the right date, 30 teams, last-10 minutes, 'explain'")
