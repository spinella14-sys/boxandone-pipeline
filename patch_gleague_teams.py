#!/usr/bin/env python3
"""
patch_gleague_teams.py — run once.

Adds what the G League team pages and standings need to ingest_gleague.py, as
one new command, `teams`, run nightly after the games:

  rosters    each team's official roster from NBA.com (commonteamroster,
             LeagueID 20): number, position, height, weight, birthdate,
             experience, school, how acquired. Kept as a dated snapshot.
  standings  NBA.com's G League standings (leaguestandingsv3): conference,
             division and playoff seed for each team.
  history    every franchise's every season from our own games: regular
             season, Tip-Off and playoff records, points for and against,
             regular-season finish, and how its playoffs ended.
  logos      each team's logo, fetched once into our bucket from NBA's CDN
             (several known address patterns are tried; the one that works is
             reported) so pages never depend on someone else's paths.

Files written under v2/gleague/: rosters/{season}.parquet,
standings/{season}.parquet, history.parquet, logos/{team_id}.svg|png.

    python3 patch_gleague_teams.py
    python3 ingest_gleague.py teams --season 2026-27 --sync
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_gleague.py")
NIGHTLY = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "def cmd_rosters" in src:
    print("  ingest_gleague.py already patched")
    sys.exit()

TABLES = '''DDL = """
CREATE TABLE IF NOT EXISTS gl_rosters (
    as_of DATE, season VARCHAR, team_id BIGINT, person_id VARCHAR, name VARCHAR, jersey VARCHAR,
    position VARCHAR, height_in INTEGER, weight_lb INTEGER, birthdate DATE, age DOUBLE,
    experience VARCHAR, school VARCHAR, how_acquired VARCHAR,
    PRIMARY KEY (as_of, team_id, person_id));
CREATE TABLE IF NOT EXISTS gl_standings (
    as_of DATE, season VARCHAR, team_id BIGINT, conference VARCHAR, division VARCHAR,
    playoff_rank INTEGER, w INTEGER, l INTEGER,
    PRIMARY KEY (as_of, season, team_id));
'''

FUNCS = r'''# ---------------------------------------------------------------------------
# teams: rosters, standings, history, logos
# ---------------------------------------------------------------------------

def _height(v):
    try:
        f, i = str(v).split("-")
        return int(f) * 12 + int(i)
    except Exception:
        return None


def _bdate(v):
    for fmt in ("%b %d, %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v).strip().title() if "," in str(v) else str(v)[:19], fmt).date()
        except Exception:
            pass
    return None


def cmd_rosters(con, season, relay=False):
    """Each team's official roster today, from NBA.com."""
    teams = con.execute("SELECT team_id, abbr FROM gl_teams WHERE season = ? ORDER BY abbr", [season]).fetchall()
    today = datetime.now().date()
    got, people, empty = 0, 0, []
    for tid, abbr in teams:
        j = stats_get("commonteamroster", {"LeagueID": LEAGUE, "Season": season, "TeamID": tid}, relay)
        time.sleep(DELAY)
        rows = rows_of(j, "CommonTeamRoster")
        if not rows:
            empty.append(abbr)
            continue
        con.execute("DELETE FROM gl_rosters WHERE as_of = ? AND team_id = ?", [today, tid])
        for r in rows:
            if not r.get("PLAYER_ID"):
                continue
            con.execute("INSERT INTO gl_rosters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                        [today, season, tid, str(r["PLAYER_ID"]), r.get("PLAYER"), r.get("NUM"),
                         r.get("POSITION"), _height(r.get("HEIGHT")),
                         int(r["WEIGHT"]) if str(r.get("WEIGHT") or "").isdigit() else None,
                         _bdate(r.get("BIRTH_DATE")), r.get("AGE"), str(r.get("EXP") or ""),
                         r.get("SCHOOL"), r.get("HOW_ACQUIRED")])
            people += 1
        got += 1
    con.commit()
    print("  rosters: %d players on %d of %d teams%s" % (people, got, len(teams),
          "" if not empty else " (none returned for %s)" % ", ".join(empty[:8])))


def cmd_standings(con, season, relay=False):
    """NBA.com's standings: conference, division, playoff seed."""
    j = stats_get("leaguestandingsv3", {"LeagueID": LEAGUE, "Season": season,
                                        "SeasonType": "Regular Season"}, relay)
    rows = rows_of(j, "Standings")
    if not rows:
        print("  standings: none from NBA.com — the page computes them from the games")
        return
    today = datetime.now().date()
    con.execute("DELETE FROM gl_standings WHERE as_of = ? AND season = ?", [today, season])
    for r in rows:
        con.execute("INSERT INTO gl_standings VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [today, season, r.get("TeamID"), r.get("Conference"), r.get("Division"),
                     r.get("PlayoffRank"), r.get("WINS"), r.get("LOSSES")])
    con.commit()
    confs = sorted({str(r.get("Conference")) for r in rows if r.get("Conference")})
    print("  standings: %d teams%s" % (len(rows), " — " + ", ".join(confs) if confs else ""))


HISTORY = """
WITH g AS (
    SELECT season, season_type, label, game_date, home_id AS team_id, home_score AS pf, away_score AS pa
    FROM gl_games WHERE home_score IS NOT NULL AND away_score IS NOT NULL
    UNION ALL
    SELECT season, season_type, label, game_date, away_id, away_score, home_score
    FROM gl_games WHERE home_score IS NOT NULL AND away_score IS NOT NULL),
rec AS (
    SELECT team_id, season,
           COUNT(*) FILTER (WHERE season_type = 'regular' AND pf > pa) reg_w,
           COUNT(*) FILTER (WHERE season_type = 'regular' AND pf < pa) reg_l,
           COUNT(*) FILTER (WHERE season_type = 'tipoff' AND pf > pa) tip_w,
           COUNT(*) FILTER (WHERE season_type = 'tipoff' AND pf < pa) tip_l,
           COUNT(*) FILTER (WHERE season_type = 'showcase' AND pf > pa) show_w,
           COUNT(*) FILTER (WHERE season_type = 'showcase' AND pf < pa) show_l,
           COUNT(*) FILTER (WHERE season_type = 'playoff' AND pf > pa) po_w,
           COUNT(*) FILTER (WHERE season_type = 'playoff' AND pf < pa) po_l,
           COUNT(*) FILTER (WHERE season_type NOT IN ('preseason', 'allstar')) gp,
           SUM(pf) FILTER (WHERE season_type NOT IN ('preseason', 'allstar')) pf,
           SUM(pa) FILTER (WHERE season_type NOT IN ('preseason', 'allstar')) pa,
           MAX(game_date) FILTER (WHERE season_type = 'playoff') last_po,
           arg_max(label, game_date) FILTER (WHERE season_type = 'playoff') last_po_label,
           arg_max(pf > pa, game_date) FILTER (WHERE season_type = 'playoff') last_po_won
    FROM g GROUP BY team_id, season),
fin AS (SELECT season, MAX(game_date) d FROM gl_games WHERE season_type = 'playoff' GROUP BY season)
SELECT r.*, t.abbr, t.city, t.name,
       RANK() OVER (PARTITION BY r.season ORDER BY r.reg_w::DOUBLE / NULLIF(r.reg_w + r.reg_l, 0) DESC NULLS LAST) reg_rank,
       COUNT(*) OVER (PARTITION BY r.season) teams,
       CASE WHEN r.last_po IS NULL THEN NULL
            WHEN r.season = '{current}' THEN 'In the playoffs'
            WHEN r.last_po = f.d AND r.last_po_won THEN 'Champion'
            WHEN r.last_po = f.d THEN 'Lost in the Finals'
            ELSE 'Lost in ' || COALESCE(NULLIF(r.last_po_label, ''), 'the playoffs') END finish
FROM rec r
LEFT JOIN gl_teams t ON t.team_id = r.team_id AND t.season = r.season
LEFT JOIN fin f ON f.season = r.season
ORDER BY r.team_id, r.season"""

LOGO_URLS = [
    "https://cdn.nba.com/logos/gleague/{tid}/primary/L/logo.svg",
    "https://cdn.nba.com/logos/gleague/{tid}/global/L/logo.svg",
    "https://cdn.nba.com/logos/gleague/{tid}/primary/D/logo.svg",
    "https://cdn.nba.com/logos/nba/{tid}/primary/L/logo.svg",
    "https://cdn.nba.com/logos/nba/{tid}/global/L/logo.svg",
    "https://ak-static.cms.nba.com/wp-content/uploads/logos/gleague/{tid}/primary/L/logo.svg",
    "https://stats.gleague.nba.com/media/img/teams/logos/{abbr}_logo.svg",
]


def cmd_logos(con, s3, bucket, season):
    """Each current team's logo into our bucket, once."""
    from curl_cffi import requests as cr
    have = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="%s/gleague/logos/" % PREFIX):
        for o in page.get("Contents", []):
            have.add(os.path.basename(o["Key"]).split(".")[0])
    teams = con.execute("SELECT team_id, abbr FROM gl_teams WHERE season = ?", [season]).fetchall()
    todo = [(t, a) for t, a in teams if str(t) not in have]
    if not todo:
        print("  logos: all %d on file" % len(teams))
        return
    worked, missing = {}, []
    for tid, abbr in todo:
        for pat in LOGO_URLS:
            url = pat.format(tid=tid, abbr=abbr)
            try:
                r = cr.get(url, impersonate="chrome", timeout=20)
            except Exception:
                continue
            body = r.content or b""
            head = body[:200].lstrip()
            kind = "svg" if (head.startswith(b"<svg") or head.startswith(b"<?xml")) and b"<svg" in body[:2000] \
                else "png" if body[:8] == b"\x89PNG\r\n\x1a\n" else None
            if r.status_code == 200 and kind and len(body) > 200:
                s3.put_object(Bucket=bucket, Key="%s/gleague/logos/%s.%s" % (PREFIX, tid, kind), Body=body,
                              ContentType="image/svg+xml" if kind == "svg" else "image/png")
                worked[pat] = worked.get(pat, 0) + 1
                break
        else:
            missing.append(abbr)
    for pat, n in worked.items():
        print("  logos: %d from %s" % (n, pat))
    if missing:
        print("  logos: none found for %s — those teams keep the lettered badge" % ", ".join(missing))


def cmd_team_files(con, season, s3, bucket):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        def put(sql, key):
            p = os.path.join(tmp, key.replace("/", "_"))
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (sql, p))
            s3.upload_file(p, bucket, "%s/gleague/%s" % (PREFIX, key))
        put("""SELECT r.*, i.player_id FROM gl_rosters r
               LEFT JOIN player_identifiers i ON i.source = 'nba' AND i.source_id = r.person_id
               WHERE r.season = '%s'
               QUALIFY r.as_of = MAX(r.as_of) OVER (PARTITION BY r.team_id)
               ORDER BY r.team_id, r.name""" % season, "rosters/%s.parquet" % season)
        put("""SELECT * FROM gl_standings WHERE season = '%s'
               QUALIFY as_of = MAX(as_of) OVER (PARTITION BY team_id)""" % season,
            "standings/%s.parquet" % season)
        put(HISTORY.format(current=season), "history.parquet")
    print("  team files: rosters, standings, history")


def cmd_teams(season, relay=False):
    con = db()
    try:
        cmd_rosters(con, season, relay)
        cmd_standings(con, season, relay)
        s3, bucket = s3_client()
        cmd_team_files(con, season, s3, bucket)
        cmd_logos(con, s3, bucket, season)
    finally:
        con.close()


def run_season('''

PAIRS = [
    ('DDL = """\n', TABLES),
    ("def run_season(", FUNCS),
    ('''    ap.add_argument("cmd", choices=["players", "run", "backfill", "download", "check", "sample", "export",
                                    "repair"])''',
     '''    ap.add_argument("cmd", choices=["players", "run", "backfill", "download", "check", "sample", "export",
                                    "repair", "teams"])'''),
    ('''    writes = a.cmd in ("players", "run", "backfill", "repair")''',
     '''    writes = a.cmd in ("players", "run", "backfill", "repair", "teams")'''),
    ('''def dispatch(a):
    if a.cmd == "repair":''',
     '''def dispatch(a):
    if a.cmd == "teams":
        cmd_teams(a.season, a.relay)
    elif a.cmd == "repair":'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  ingest_gleague.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)

nsrc = open(NIGHTLY).read()
NOLD = '''        ok &= run(log, ["ingest_gleague.py", "run", "--season", season])
'''
NNEW = '''        ok &= run(log, ["ingest_gleague.py", "run", "--season", season])
        # G League rosters, standings, franchise history, logos
        if not run(log, ["ingest_gleague.py", "teams", "--season", season]):
            log("G League teams step failed — the rest of the night carries on")
'''
if "ingest_gleague.py\", \"teams\"" not in nsrc:
    if nsrc.count(NOLD) != 1:
        sys.exit("  nightly2.py not as expected — nothing written")
    nsrc = nsrc.replace(NOLD, NNEW)

open(PATH, "w").write(src)
open(NIGHTLY, "w").write(nsrc)
print("  ingest_gleague.py patched: 'teams' (rosters, standings, history, logos); nightly runs it")
