#!/usr/bin/env python3
"""
gleague_probe.py — what G League data can this machine actually get?

Asks every source we might build on, once each, and reports what came back:
whether it answered, how many rows, and which fields. Nothing is stored except
the report, which goes to v2/logs/probe/gleague_{github|local}.txt when R2
credentials are present, so the Mac and GitHub answers can be compared.

    python3 gleague_probe.py            # run it here
    python3 gleague_probe.py --read     # print both machines' last reports

Sources:
  stats.nba.com with LeagueID=20   schedule, box score, play-by-play, shot chart,
                                   season stats, play types, tracking, the
                                   all-time player list, and history depth
  cdn.nba.com                      the live box score and play-by-play GitHub
                                   already reads for the NBA, for G League ids
  gleague.nba.com                  transactions and the two-way list
  basketball-reference.com         G League season pages
  official.nba.com                 the NBA's injury report (two-way G League status)
"""
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone

H = {"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
     "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
     "Accept": "application/json, text/plain, */*"}
STATS = "https://stats.nba.com/stats/"
CDN = "https://cdn.nba.com/static/json/liveData/%s/%s_%s.json"
FULL = "2025-26"          # a finished G League season, for season-level checks
NOW = "2026-27"

DASH = ("College=&Conference=&Country=&DateFrom=&DateTo=&Division=&DraftPick=&DraftYear="
        "&GameScope=&GameSegment=&Height=&LastNGames=0&LeagueID=20&Location=&Month=0"
        "&OpponentTeamID=0&Outcome=&PORound=0&PaceAdjust=N&PerMode=Totals&Period=0"
        "&PlayerExperience=&PlayerPosition=&PlusMinus=N&Rank=N&Season=%s&SeasonSegment="
        "&SeasonType=Regular%%20Season&ShotClockRange=&StarterBench=&TeamID=0&TwoWay=0"
        "&VsConference=&VsDivision=&Weight=")
PT = ("College=&Conference=&Country=&DateFrom=&DateTo=&Division=&DraftPick=&DraftYear="
      "&GameScope=&Height=&LastNGames=0&LeagueID=20&Location=&Month=0&OpponentTeamID=0"
      "&Outcome=&PORound=0&PerMode=Totals&PlayerExperience=&PlayerOrTeam=Player"
      "&PlayerPosition=&PtMeasureType=Drives&Season=%s&SeasonSegment="
      "&SeasonType=Regular%%20Season&StarterBench=&TeamID=0&VsConference=&VsDivision=&Weight=")
SHOTS = ("AheadBehind=&ClutchTime=&ContextFilter=&ContextMeasure=FGA&DateFrom=&DateTo="
         "&EndPeriod=&EndRange=&GameID=%s&GameSegment=&LastNGames=0&LeagueID=20&Location="
         "&Month=0&OpponentTeamID=0&Outcome=&Period=0&PlayerID=0&PlayerPosition=&PointDiff="
         "&Position=&RangeType=&RookieYear=&Season=&SeasonSegment=&SeasonType=&StartPeriod="
         "&StartRange=&TeamID=0&VsConference=&VsDivision=")


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def s3c():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


class Probe:
    def __init__(self, cr):
        self.cr, self.lines = cr, []

    def say(self, s=""):
        print(s)
        self.lines.append(s)

    def get(self, url, headers=H, timeout=20):
        t = time.time()
        try:
            r = self.cr.get(url, headers=headers, impersonate="chrome", timeout=timeout)
            return r, time.time() - t, None
        except Exception as e:
            return None, time.time() - t, str(e)[:80]

    def stats(self, label, endpoint, query):
        """A stats.nba.com call: status, then rows and headers of each result set."""
        r, secs, err = self.get(STATS + endpoint + "?" + query)
        if r is None:
            self.say("  %-38s %5.1fs  FAILED: %s" % (label, secs, err))
            return None
        if r.status_code != 200:
            self.say("  %-38s %5.1fs  HTTP %s" % (label, secs, r.status_code))
            return None
        try:
            j = r.json()
        except Exception:
            self.say("  %-38s %5.1fs  HTTP 200, not JSON (%d bytes)" % (label, secs, len(r.content)))
            return None
        sets = j.get("resultSets") or ([j["resultSet"]] if isinstance(j.get("resultSet"), dict) else None)
        if sets:
            desc = ", ".join("%s %d rows" % (s.get("name", "?"), len(s.get("rowSet") or [])) for s in sets[:3])
            self.say("  %-38s %5.1fs  %s" % (label, secs, desc))
        else:
            self.say("  %-38s %5.1fs  HTTP 200, keys %s" % (label, secs, list(j.keys())[:5]))
        return j


def first_rows(j, name=None):
    for s in (j or {}).get("resultSets") or []:
        if name is None or s.get("name") == name:
            return [dict(zip(s["headers"], r)) for r in s.get("rowSet") or []]
    return []


def main():
    load_env()
    if "--read" in sys.argv:
        s3 = s3c()
        for name in ("github", "local"):
            try:
                print(s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"],
                                    Key="v2/logs/probe/gleague_%s.txt" % name)["Body"].read().decode() + "\n")
            except Exception:
                print("(no %s G League probe yet)\n" % name)
        return
    from curl_cffi import requests as cr
    P = Probe(cr)
    where = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else socket.gethostname()
    P.say("G League probe from %s at %s" % (where, datetime.now(timezone.utc).isoformat()))

    # ---- schedule: also finds game ids to test with
    P.say("\nstats.nba.com — schedule and game ids")
    gids = []
    for season in (NOW, FULL):
        j = P.stats("scheduleleaguev2 %s" % season, "scheduleleaguev2",
                    "LeagueID=20&Season=%s" % season)
        if j and "leagueSchedule" in j:
            games = [g for d in j["leagueSchedule"].get("gameDates", []) for g in d.get("games", [])]
            done = [g for g in games if g.get("gameStatus") == 3]
            kinds = sorted({str(g.get("gameId", ""))[:3] for g in games})
            P.say("      %d games, %d final, id prefixes %s, e.g. %s" % (
                len(games), len(done), kinds, done[0]["gameId"] if done else "-"))
            gids += [g["gameId"] for g in done[-2:]]
    # history depth: team game logs by season
    P.say("\nstats.nba.com — how far back G League game logs go")
    oldest = None
    for season in ("2001-02", "2005-06", "2009-10", "2012-13", "2015-16", "2017-18", "2019-20", "2021-22", FULL):
        j = P.stats("leaguegamelog %s" % season, "leaguegamelog",
                    "Counter=0&Direction=ASC&LeagueID=20&PlayerOrTeam=T&Season=%s"
                    "&SeasonType=Regular%%20Season&Sorter=DATE" % season)
        rows = first_rows(j)
        if rows and not oldest:
            oldest = (season, rows[0].get("GAME_ID"))

    # ---- one game, every way
    test = gids[-1] if gids else None
    P.say("\none game, every source  (game %s)" % (test or "none found — schedule unreachable"))
    if test:
        P.stats("boxscoretraditionalv3", "boxscoretraditionalv3",
                "GameID=%s&StartPeriod=0&EndPeriod=0&StartRange=0&EndRange=0&RangeType=0" % test)
        j = P.stats("playbyplayv3", "playbyplayv3", "GameID=%s&StartPeriod=0&EndPeriod=0" % test)
        acts = ((j or {}).get("game") or {}).get("actions") or []
        if acts:
            shots = [a for a in acts if a.get("isFieldGoal") == 1]
            P.say("      %d actions, %d shots, %d with x/y, %d made with assistPersonId" % (
                len(acts), len(shots), sum(1 for a in shots if a.get("xLegacy") is not None),
                sum(1 for a in shots if a.get("shotResult") == "Made" and a.get("assistPersonId"))))
        P.stats("shotchartdetail", "shotchartdetail", SHOTS % test)
        P.stats("boxscoreadvancedv3", "boxscoreadvancedv3",
                "GameID=%s&StartPeriod=0&EndPeriod=0&StartRange=0&EndRange=0&RangeType=0" % test)
        P.stats("boxscoreplayertrackv3 (tracking)", "boxscoreplayertrackv3", "GameID=%s" % test)
        P.stats("boxscorehustlev2 (hustle)", "boxscorehustlev2", "GameID=%s" % test)
        for kind in ("boxscore", "playbyplay"):
            r, secs, err = P.get(CDN % (kind, kind, test))
            if r is None:
                P.say("  %-38s %5.1fs  FAILED: %s" % ("cdn.nba.com " + kind, secs, err))
            else:
                extra = ""
                if r.status_code == 200 and kind == "boxscore":
                    g = r.json().get("game", {})
                    pl = (g.get("homeTeam") or {}).get("players") or []
                    extra = ", %d home players, status values %s" % (
                        len(pl), sorted({p.get("status") for p in pl}))
                if r.status_code == 200 and kind == "playbyplay":
                    a2 = r.json().get("game", {}).get("actions") or []
                    extra = ", %d actions" % len(a2)
                P.say("  %-38s %5.1fs  HTTP %s%s" % ("cdn.nba.com " + kind, secs, r.status_code, extra))
    if oldest:
        P.say("\noldest game found: %s %s" % oldest)
        j = P.stats("playbyplayv3 (oldest)", "playbyplayv3",
                    "GameID=%s&StartPeriod=0&EndPeriod=0" % oldest[1])
        acts = ((j or {}).get("game") or {}).get("actions") or []
        if acts:
            P.say("      %d actions, %d with x/y" % (len(acts), sum(1 for a in acts if a.get("xLegacy") is not None)))
        P.stats("boxscoretraditionalv3 (oldest)", "boxscoretraditionalv3",
                "GameID=%s&StartPeriod=0&EndPeriod=0&StartRange=0&EndRange=0&RangeType=0" % oldest[1])

    # ---- season-level
    P.say("\nstats.nba.com — season level (%s)" % FULL)
    j = P.stats("leaguedashplayerstats", "leaguedashplayerstats", DASH % FULL)
    rows = first_rows(j)
    if rows:
        P.say("      columns: %s" % ", ".join(list(rows[0].keys())[:14]) + " …")
    P.stats("synergyplaytypes (Isolation)", "synergyplaytypes",
            "LeagueID=20&PerMode=Totals&PlayType=Isolation&PlayerOrTeam=P"
            "&SeasonType=Regular%%20Season&SeasonYear=%s&TypeGrouping=offensive" % FULL)
    P.stats("leaguedashptstats (Drives)", "leaguedashptstats", PT % FULL)
    j = P.stats("commonallplayers (all-time)", "commonallplayers",
                "IsOnlyCurrentSeason=0&LeagueID=20&Season=%s" % FULL)
    rows = first_rows(j)
    if rows:
        P.say("      fields: %s" % ", ".join(rows[0].keys()))
    P.stats("commonallplayers (NBA, for id overlap)", "commonallplayers",
            "IsOnlyCurrentSeason=1&LeagueID=00&Season=%s" % NOW)
    P.stats("leaguedashplayerstats NBA two-way only", "leaguedashplayerstats",
            (DASH % NOW).replace("LeagueID=20", "LeagueID=00").replace("TwoWay=0", "TwoWay=1"))

    # ---- pages
    P.say("\npages")
    for label, url in (
            ("gleague.nba.com transactions", "https://gleague.nba.com/transactions"),
            ("gleague.nba.com two-way list", "https://gleague.nba.com/twowayplayers"),
            ("basketball-reference G League %s" % FULL, "https://www.basketball-reference.com/gleague/years/2026.html"),
            ("NBA injury report index", "https://official.nba.com/nba-injury-report-2025-26-season/")):
        r, secs, err = P.get(url, headers={"Accept": "text/html"})
        if r is None:
            P.say("  %-38s %5.1fs  FAILED: %s" % (label, secs, err))
            continue
        note = ""
        if r.status_code == 200:
            t = r.text
            if "injury" in label:
                pdfs = re.findall(r'https?://[^"\']+Injury-Report[^"\']+\.pdf', t)
                note = ", %d report PDFs linked%s" % (len(pdfs), (", latest " + pdfs[-1].split("/")[-1]) if pdfs else "")
            elif "transactions" in label:
                note = ", %d 'signed', %d 'assigned', %d 'waived' on the page" % (
                    len(re.findall(r"(?i)\bsigned\b", t)), len(re.findall(r"(?i)\bassigned\b", t)),
                    len(re.findall(r"(?i)\bwaived\b", t)))
            elif "two-way" in label:
                note = ", %d table rows" % len(re.findall(r"<tr", t))
            else:
                note = ", %d bytes" % len(t)
        P.say("  %-38s %5.1fs  HTTP %s%s" % (label, secs, r.status_code, note))

    text = "\n".join(P.lines)
    keys = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME")
    if all(os.environ.get(k) for k in keys):
        name = "github" if os.environ.get("GITHUB_ACTIONS") else "local"
        s3c().put_object(Bucket=os.environ["R2_BUCKET_NAME"], Key="v2/logs/probe/gleague_%s.txt" % name,
                         Body=text.encode(), ContentType="text/plain")
        print("\n(saved to v2/logs/probe/gleague_%s.txt)" % name)


if __name__ == "__main__":
    main()
