#!/usr/bin/env python3
"""
patch_gleague_standings.py — run once.

NBA.com's G League standings came back empty because we asked only for the
regular season, which hasn't started: the league is in its Tip-Off
Tournament, filed on NBA.com under the event's old name, "Showcase". Now each
event is asked for separately — regular season, Tip-Off ("Showcase", then
"Tip-Off Tournament"), playoffs — and kept with the conference (or Tip-Off
region), division, seed and division rank NBA.com assigns. What each request
returned is printed, so it's clear which names NBA.com answers to.

Stored in gl_standings_ev; exported to v2/gleague/standings/{season}.parquet
with an `event` column the standings page groups by.

    python3 patch_gleague_standings.py
    python3 -u ingest_gleague.py teams --season 2026-27 --sync
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_gleague.py")
src = open(PATH).read()
if "gl_standings_ev" in src:
    print("  ingest_gleague.py already patched")
    sys.exit()

NEW_FN = '''STANDING_TYPES = [("regular", "Regular Season"), ("tipoff", "Showcase"),
                  ("tipoff", "Tip-Off Tournament"), ("playoff", "Playoffs")]


def cmd_standings(con, season, relay=False):
    """NBA.com's standings for each event: conference or region, division, seed."""
    today = datetime.now().date()
    got = set()
    for event, stype in STANDING_TYPES:
        if event in got:
            continue
        j = stats_get("leaguestandingsv3", {"LeagueID": LEAGUE, "Season": season, "SeasonType": stype}, relay)
        time.sleep(DELAY)
        rows = [r for r in rows_of(j, "Standings") if r.get("TeamID")]
        if not rows:
            print("  standings %-18s none" % ("'%s':" % stype))
            continue
        got.add(event)
        con.execute("DELETE FROM gl_standings_ev WHERE as_of = ? AND season = ? AND event = ?", [today, season, event])
        for r in rows:
            con.execute("INSERT INTO gl_standings_ev VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                        [today, season, event, r.get("TeamID"), r.get("Conference") or None,
                         r.get("Division") or None, r.get("PlayoffRank"), r.get("DivisionRank"),
                         r.get("WINS"), r.get("LOSSES")])
        confs = sorted({str(r["Conference"]) for r in rows if r.get("Conference")})
        divs = sorted({str(r["Division"]) for r in rows if r.get("Division")})
        print("  standings %-18s %d teams; conferences %s; divisions %s" % (
            "'%s':" % stype, len(rows), ", ".join(confs) or "-", ", ".join(divs) or "-"))
    con.commit()


HISTORY = """'''

i = src.find("def cmd_standings(")
j = src.find('HISTORY = """', i)
if i < 0 or j < 0:
    sys.exit("  ingest_gleague.py not as expected (cmd_standings) — nothing written")
src = src[:i] + NEW_FN + src[j + len('HISTORY = """'):]

PAIRS = [
    ('''CREATE TABLE IF NOT EXISTS gl_standings (''',
     '''CREATE TABLE IF NOT EXISTS gl_standings_ev (
    as_of DATE, season VARCHAR, event VARCHAR, team_id BIGINT, conference VARCHAR, division VARCHAR,
    playoff_rank INTEGER, division_rank INTEGER, w INTEGER, l INTEGER,
    PRIMARY KEY (as_of, season, event, team_id));
CREATE TABLE IF NOT EXISTS gl_standings ('''),
    ('''        put("""SELECT * FROM gl_standings WHERE season = '%s'
               QUALIFY as_of = MAX(as_of) OVER (PARTITION BY team_id)""" % season,''',
     '''        put("""SELECT * FROM gl_standings_ev WHERE season = '%s'
               QUALIFY as_of = MAX(as_of) OVER (PARTITION BY team_id, event)""" % season,'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  ingest_gleague.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  ingest_gleague.py patched: standings for each event, with conference/region and division")
