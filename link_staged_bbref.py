#!/usr/bin/env python3
"""
link_staged_bbref.py — give players we already hold their Basketball Reference id.

Rookies reach the registry from NBA.com rosters before their first game, with
an NBA id but no Basketball Reference id. Box scores come from Basketball
Reference, so on debut night their lines are set aside in staging_players as
"unknown" and never land. resolve_staging.py would create a second copy of
each of them; this links the staged id to the player we already have.

A staged id is linked only when exactly one player without a Basketball
Reference id has that name (accents, punctuation and Jr./III ignored) and,
when we have him on a roster, he is on the team the box score shows. Anything
else is listed and left alone.

    python3 link_staged_bbref.py            # by hand: lock, pull, link, re-parse,
                                            # export what changed, push
    python3 link_staged_bbref.py --dry      # show what it would link, change nothing
    python3 link_staged_bbref.py --in-run   # inside the nightly, before parsing:
                                            # links only, on the database it holds
"""
import argparse
import os
import sys
import unicodedata
from collections import defaultdict

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
BASE = "https://www.basketball-reference.com"


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() or c == " " else (" " if c in "-–" else "") for c in s)
    return " ".join(w for w in s.split() if w not in SUFFIX)


def team(a):
    import bridge_games as BG
    return BG.norm_team(a)


def proposals(con):
    staged = con.execute("""SELECT source_id, MAX(raw_name), MAX(raw_team), SUM(COALESCE(occurrences, 1))
                            FROM staging_players
                            WHERE source='bbref' AND match_status='pending'
                            GROUP BY source_id ORDER BY 1""").fetchall()
    if not staged:
        return [], []
    has_bbref = {r[0] for r in con.execute(
        "SELECT DISTINCT player_id FROM player_identifiers WHERE source='bbref'").fetchall()}
    names = defaultdict(set)
    for pid, nm in con.execute("SELECT player_id, full_name FROM players").fetchall():
        if pid not in has_bbref:
            names[fold(nm)].add(pid)
    for pid, nm in con.execute("SELECT player_id, alias FROM player_aliases").fetchall():
        if pid not in has_bbref:
            names[fold(nm)].add(pid)
    on_team = {}
    try:
        on_team = dict(con.execute("""SELECT player_id, team_abbr FROM team_rosters
                                      WHERE as_of = (SELECT MAX(as_of) FROM team_rosters)""").fetchall())
    except Exception:
        pass
    shown = dict(con.execute("SELECT player_id, full_name FROM players").fetchall())
    link, review = [], []
    for bid, name, raw_team, occ in staged:
        exists = con.execute("SELECT player_id FROM player_identifiers WHERE source='bbref' "
                             "AND source_id=?", [bid]).fetchone()
        if exists:
            link.append((bid, exists[0], name, "already linked"))
            continue
        cands = sorted(names.get(fold(name), ()))
        if len(cands) == 1:
            pid = cands[0]
            t = on_team.get(pid)
            if t and raw_team and team(t) != team(raw_team):
                review.append((bid, name, "%s is on %s's roster, box score says %s"
                               % (shown.get(pid), t, raw_team)))
            else:
                link.append((bid, pid, name, "on %s roster" % t if t else "no roster entry"))
        elif not cands:
            review.append((bid, name, "no player of that name without a Basketball Reference id"))
        else:
            review.append((bid, name, "%d players share the name: %s"
                           % (len(cands), ", ".join(cands))))
    return link, review


def write_links(con, link):
    for bid, pid, name, why in link:
        con.execute("""INSERT INTO player_identifiers
                         (source, source_id, player_id, source_url, is_primary, linked_by, provenance)
                       VALUES ('bbref', ?, ?, ?, TRUE, 'link_staged_bbref', 'scraped')
                       ON CONFLICT DO NOTHING""",
                    [bid, pid, "%s/players/%s/%s.html" % (BASE, bid[0], bid)])
        con.execute("""UPDATE staging_players SET match_status='auto_linked', proposed_player_id=?,
                       resolved_at=now() WHERE source='bbref' AND source_id=? AND match_status='pending'""",
                    [pid, bid])


def show(link, review):
    for bid, pid, name, why in link:
        print("    link    %-12s -> %s  %-24s (%s)" % (bid, pid, name, why))
    for bid, name, why in review:
        print("    REVIEW  %-12s    %-24s %s" % (bid, name, why))
    if not link and not review:
        print("    nothing staged")


def in_run():
    import duckdb
    con = duckdb.connect(DB_PATH)
    link, review = proposals(con)
    write_links(con, link)
    fresh = [l for l in link if l[3] != "already linked"]
    if fresh:
        # tonight's games set these players aside: parse the season again so
        # their lines land (rows already held are kept)
        import nightly2 as N
        con.execute("UPDATE ingest_log SET parsed=FALSE WHERE season=? AND parsed",
                    [N.current_season()])
    con.commit()
    con.close()
    show(link, review)


def by_hand(dry):
    import duckdb
    import nightly2 as N
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    log = N.Log()
    if not N.acquire_lock(s3, bucket, log):
        sys.exit("  another run holds the database — try again when it finishes")
    try:
        if not N.pull_db(s3, bucket, log):
            return
        season = N.current_season()
        con = duckdb.connect(DB_PATH)
        link, review = proposals(con)
        show(link, review)
        if dry or not link:
            con.close()
            return
        write_links(con, link)
        pids = sorted({pid for _, pid, _, _ in link})
        before = con.execute("SELECT COUNT(*) FROM player_game_box WHERE player_id IN (%s)"
                             % ",".join("?" * len(pids)), pids).fetchone()[0]
        # games already parsed this season lost these players' lines: parse again
        # (rows already held are kept — every insert is ON CONFLICT DO NOTHING)
        con.execute("UPDATE ingest_log SET parsed=FALSE WHERE season=? AND parsed", [season])
        con.commit()
        con.close()
        N.run(log, ["ingest_games.py", "parse"])
        con = duckdb.connect(DB_PATH, read_only=True)
        rows = con.execute("""SELECT p.full_name, b.game_id, b.played, b.seconds_played, b.pts
                              FROM player_game_box b JOIN players p USING (player_id)
                              WHERE b.player_id IN (%s) ORDER BY 1, 2""" % ",".join("?" * len(pids)),
                           pids).fetchall()
        con.close()
        log("%d box lines recovered for linked players" % (len(rows) - before))
        for name, gid, played, secs, pts in rows:
            log("    %-24s %s  %s" % (name, gid, "%d min, %s pts" % ((secs or 0) // 60, pts)
                                       if played else "did not play"))
        if len(rows) > before:
            N.export_season(env, log, season)
            N.run(log, ["compute_bpm.py", "--season", season])
            N.export_changed_players(env, log, season, pids)
        N.rebuild_registry_and_manifest(env, log)
        N.push_db(s3, bucket, log)
        log("done")
    finally:
        N.release_lock(s3, bucket)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--in-run", action="store_true")
    a = ap.parse_args()
    in_run() if a.in_run else by_hand(a.dry)
