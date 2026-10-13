#!/usr/bin/env python3
"""
nba_probe.py — can this machine reach the sites the pipeline depends on?

One request to each, 20-second limit, same headers and Chrome impersonation
the real scripts use. Prints the result and, when R2 credentials are
present, saves it to v2/logs/probe/{host}.txt so it can be read from the Mac.

    python3 nba_probe.py
"""
import os
import socket
import sys
import time
from datetime import datetime, timezone

NBA_HEADERS = {
    "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
    "Accept": "application/json, text/plain, */*",
}
TARGETS = [
    ("stats.nba.com  rosters (commonteamroster)",
     "https://stats.nba.com/stats/commonteamroster?LeagueID=00&Season=2026-27&TeamID=1610612738",
     NBA_HEADERS),
    ("stats.nba.com  play-by-play (playbyplayv3)",
     "https://stats.nba.com/stats/playbyplayv3?GameID=0022500001&StartPeriod=0&EndPeriod=0",
     NBA_HEADERS),
    ("cdn.nba.com    schedule",
     "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json", NBA_HEADERS),
    ("basketball-reference.com",
     "https://www.basketball-reference.com/leagues/NBA_2027_transactions.html", {}),
]


def load_env():
    # the Mac keeps credentials in ~/boxandone/.env; GitHub passes them in
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def read_back():
    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    for name in ("github", "local"):
        try:
            body = s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"],
                                 Key="v2/logs/probe/%s.txt" % name)["Body"].read().decode()
            print(body + "\n")
        except Exception:
            print("(no %s probe result yet)\n" % name)


CDN = "https://cdn.nba.com/static/json/liveData/%s/%s_%s.json"
FEED_GAMES = ["0022500001", "0022400001", "0022000001", "0021500001", "0021000001"]
WANT = ["actionNumber", "period", "clock", "teamTricode", "personId", "playerName",
        "actionType", "subType", "shotResult", "shotDistance", "x", "y",
        "xLegacy", "yLegacy", "assistPersonId", "description", "scoreHome", "scoreAway"]


def feed_check(cr):
    """Does NBA's CDN play-by-play carry what stats.nba.com's does, and how far back?"""
    out = ["", "cdn.nba.com liveData feed"]
    first = None
    for gid in FEED_GAMES:
        try:
            r = cr.get(CDN % ("playbyplay", "playbyplay", gid), headers=NBA_HEADERS,
                       impersonate="chrome", timeout=20)
            if r.status_code != 200:
                out.append("  pbp %s  HTTP %s" % (gid, r.status_code)); continue
            acts = r.json()["game"]["actions"]
            out.append("  pbp %s  HTTP 200, %d actions" % (gid, len(acts)))
            first = first or (gid, acts)
        except Exception as e:
            out.append("  pbp %s  FAILED: %s" % (gid, str(e)[:70]))
        try:
            r = cr.get(CDN % ("boxscore", "boxscore", gid), headers=NBA_HEADERS,
                       impersonate="chrome", timeout=20)
            out.append("  box %s  HTTP %s" % (gid, r.status_code))
        except Exception as e:
            out.append("  box %s  FAILED: %s" % (gid, str(e)[:70]))
    if not first:
        return out
    gid, acts = first
    keys = set().union(*(a.keys() for a in acts))
    out.append("  fields in %s: missing %s" % (gid, [k for k in WANT if k not in keys] or "none"))
    made = [a for a in acts if a.get("isFieldGoal") == 1 and a.get("shotResult") == "Made"]
    withast = [a for a in made if a.get("assistPersonId")]
    out.append("  made FG %d, with assistPersonId %d" % (len(made), len(withast)))
    # where stats.nba.com is reachable, check the coordinates are the same numbers
    try:
        r = cr.get("https://stats.nba.com/stats/playbyplayv3?GameID=%s&StartPeriod=0&EndPeriod=0" % gid,
                   headers=NBA_HEADERS, impersonate="chrome", timeout=10)
        v3 = {a["actionNumber"]: a for a in r.json()["game"]["actions"]}
        shots = [a for a in acts if a.get("isFieldGoal") == 1 and a["actionNumber"] in v3]
        same = sum(1 for a in shots if a.get("xLegacy") == v3[a["actionNumber"]].get("xLegacy")
                   and a.get("yLegacy") == v3[a["actionNumber"]].get("yLegacy"))
        out.append("  vs playbyplayv3: %d shots matched by actionNumber, %d identical x/y"
                   % (len(shots), same))
    except Exception:
        out.append("  vs playbyplayv3: not reachable here")
    return out


def main():
    load_env()
    if "--read" in sys.argv:
        return read_back()
    from curl_cffi import requests as cr
    where = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else socket.gethostname()
    lines = ["probe from %s at %s" % (where, datetime.now(timezone.utc).isoformat())]
    for label, url, headers in TARGETS:
        t = time.time()
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=20)
            res = "HTTP %s, %d bytes" % (r.status_code, len(r.content))
        except Exception as e:
            res = "FAILED: %s" % str(e)[:90]
        lines.append("  %-44s %5.1fs  %s" % (label, time.time() - t, res))
    lines += feed_check(cr)
    # the translated CDN feed, end to end: possessions must add up to the score
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    lines += ["", "cdn_feed selftest"]
    for gid in ("0022500001", "0042500101", "0012600001"):
        p = subprocess.run([sys.executable, os.path.join(here, "cdn_feed.py"),
                            "selftest", "--game", gid],
                           capture_output=True, text=True, cwd=here)
        lines += [l for l in (p.stdout + p.stderr).splitlines() if l.strip()][-4:]
    text = "\n".join(lines)
    print(text)

    keys = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME")
    if all(os.environ.get(k) for k in keys):
        import boto3
        s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                          region_name="auto")
        name = "github" if os.environ.get("GITHUB_ACTIONS") else "local"
        s3.put_object(Bucket=os.environ["R2_BUCKET_NAME"], Key="v2/logs/probe/%s.txt" % name,
                      Body=text.encode(), ContentType="text/plain")


if __name__ == "__main__":
    main()
