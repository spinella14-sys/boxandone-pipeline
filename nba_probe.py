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


def main():
    import sys
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
