#!/usr/bin/env python3
"""
fetch_history.py — franchise history rows: finish, playoff result, coach, exec.

Three of the columns the History tab wants are already derivable from
team_season (record, differential, the rate block). Three are not in the data
at all:

    playoff result   could be inferred from the playoff games, but BBRef states
                     it directly and unambiguously, so take it from the source
                     rather than reconstructing round names from game counts
    head coach       with their record
    executive        the front office name of record

All three sit in one table on each franchise page, so this is ~30 requests
rather than ~900.

    python3 fetch_history.py probe      # one franchise, show what parsed
    python3 fetch_history.py fetch      # all franchises -> R2
    python3 fetch_history.py status

Writes v2/history/franchise.parquet — one row per team-season, keyed on
(abbr, season) so the frontend joins it onto team_season.

Franchise pages carry the whole lineage, so the Seattle seasons appear on the
OKC page and Vancouver's on Memphis's. That is what makes a history tab
actually historical.
"""

import argparse
import gzip
import os
import re
import sys
import time

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
CACHE = os.path.join(HOME, "raw", "franchise")
KEY = "v2/history/franchise.parquet"
DELAY = 5.5

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")}

# One entry per current franchise; BBRef redirects historical codes to these.
# BBRef keys a franchise page on its ORIGINAL code: the Nets are NJN, the
# Pelicans NOH, and the Charlotte franchise CHA. BRK/CHO/NOP return a redirect
# stub, not a table.
FRANCHISES = ["ATL", "BOS", "NJN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET",
              "GSW", "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN",
              "NOH", "NYK", "OKC", "ORL", "PHI", "PHO", "POR", "SAC", "SAS",
              "TOR", "UTA", "WAS"]


def load_env():
    if not os.path.exists(ENV_PATH):
        sys.exit("  %s not found" % ENV_PATH)
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def s3c(env):
    import boto3
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def get_page(abbr, session=None):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s.html.gz" % abbr)
    if os.path.exists(path):
        return gzip.open(path, "rt", encoding="utf-8").read()
    import requests
    s = session or requests.Session()
    url = "https://www.basketball-reference.com/teams/%s/" % abbr
    r = s.get(url, headers=UA, timeout=60)
    r.encoding = "utf-8"
    time.sleep(DELAY)
    if r.status_code != 200:
        return None
    gzip.open(path, "wt", encoding="utf-8").write(r.text)
    return r.text


def parse(abbr, html):
    """Pull the franchise table. The table id is the franchise code, but match
    on the header instead — ids change, a table with wins and losses does not.
    BBRef also hides some tables in HTML comments, so strip the markers first."""
    from bs4 import BeautifulSoup
    import re as _re
    html = html.replace("<!--", "").replace("-->", "")
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for t in soup.find_all("table"):
        stats = {th.get("data-stat") for th in t.find_all("th") if th.get("data-stat")}
        if {"wins", "losses"} <= stats:
            table = t
            break
    if table is None:
        return []

    found = sorted({td.get("data-stat") for td in table.find_all("td")
                    if td.get("data-stat")})

    out = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        if tr.get("class") and "thead" in tr.get("class"):
            continue
        cells, links = {}, {}
        th = tr.find("th")
        if th:
            cells["season"] = th.get_text(strip=True)
        for td in tr.find_all("td"):
            stat = td.get("data-stat")
            if not stat:
                continue
            cells[stat] = td.get_text(strip=True)
            a = td.find("a")
            if a and a.get("href"):
                links[stat] = a["href"]

        season = cells.get("season", "")
        m = _re.match(r"(\d{4})-(\d{2})", season)
        if not m:
            continue

        # the season's own abbreviation, from /teams/XXX/2025.html
        season_abbr = abbr
        href = links.get("team_name", "")
        hm = _re.search(r"/teams/([A-Z]{3})/", href)
        if hm:
            season_abbr = hm.group(1)

        out.append({
            "franchise": abbr,
            "season": season,
            "team_abbr": season_abbr,
            "team_name": cells.get("team_name", ""),
            "lg": cells.get("lg_id", ""),
            "wins": _int(cells.get("wins")),
            "losses": _int(cells.get("losses")),
            "win_pct": _float(cells.get("win_loss_pct")),
            "finish": cells.get("rank_team") or cells.get("finish") or "",
            "playoffs": (cells.get("rank_team_playoffs") or cells.get("playoff_result")
                         or cells.get("playoffs") or ""),
            "coaches": cells.get("coaches", ""),
            "executive": cells.get("executive", ""),
            "top_ws": cells.get("top_ws", ""),
            "srs": _float(cells.get("srs")),
        })
    if out:
        out[0]["_columns"] = ",".join(found)
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cmd_probe(env):
    html = get_page("SAS")
    if not html:
        sys.exit("  could not fetch the SAS franchise page")
    rows = parse("SAS", html)
    print("  parsed %d seasons" % len(rows))
    if not rows:
        print("  no franchise table found — the page layout may have changed")
        return
    print("  data-stat columns on the page:")
    for c in (rows[0].get("_columns") or "").split(","):
        print("    %s" % c)
    print()
    for r in rows[:6]:
        print("  %-8s %-4s %3s-%-3s %-22s %-28s %s"
              % (r["season"], r["team_abbr"], r["wins"], r["losses"],
                 (r["finish"] or "")[:22], (r["playoffs"] or "")[:28],
                 (r["coaches"] or "")[:30]))
    missing = [k for k in ("playoffs", "coaches", "executive") if not rows[0].get(k)]
    if missing:
        print("\n  not on this page: %s" % ", ".join(missing))


def cmd_fetch(env):
    import requests, duckdb, tempfile
    s3 = s3c(env)
    session = requests.Session()
    allrows = []
    for i, abbr in enumerate(FRANCHISES, 1):
        html = get_page(abbr, session)
        if not html:
            print("  %-4s fetch failed" % abbr)
            continue
        rows = parse(abbr, html)
        allrows.extend(rows)
        print("  %-4s %3d seasons" % (abbr, len(rows)))
    if not allrows:
        sys.exit("  nothing parsed")

    con = duckdb.connect()
    con.execute("""CREATE TABLE h (franchise VARCHAR, season VARCHAR, team_abbr VARCHAR,
        team_name VARCHAR, lg VARCHAR, wins INTEGER, losses INTEGER, win_pct DOUBLE,
        finish VARCHAR, playoffs VARCHAR, coaches VARCHAR, executive VARCHAR,
        top_ws VARCHAR, srs DOUBLE)""")
    con.executemany("INSERT INTO h VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [tuple(r[k] for k in ("franchise","season","team_abbr","team_name","lg",
                                          "wins","losses","win_pct","finish","playoffs",
                                          "coaches","executive","top_ws","srs"))
                     for r in allrows])
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        p = f.name
    con.execute("COPY h TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % p)
    s3.upload_file(p, env["R2_BUCKET_NAME"], KEY)
    size = os.path.getsize(p)
    os.unlink(p)
    con.close()
    print("\n  %d rows -> %s (%.0f KB)" % (len(allrows), KEY, size / 1024))


def cmd_status(env):
    import duckdb, tempfile
    s3 = s3c(env)
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        p = f.name
    try:
        s3.download_file(env["R2_BUCKET_NAME"], KEY, p)
    except Exception:
        print("  not uploaded yet")
        return
    con = duckdb.connect()
    print("  rows:", con.execute("SELECT COUNT(*) FROM read_parquet('%s')" % p).fetchone()[0])
    print("  franchises:", con.execute(
        "SELECT COUNT(DISTINCT franchise) FROM read_parquet('%s')" % p).fetchone()[0])
    print("  seasons:", con.execute(
        "SELECT MIN(season), MAX(season) FROM read_parquet('%s')" % p).fetchone())
    for c in ("playoffs", "coaches", "executive"):
        n = con.execute("SELECT COUNT(*) FROM read_parquet('%s') WHERE %s <> ''"
                        % (p, c)).fetchone()[0]
        print("  %-10s populated on %d rows" % (c, n))
    os.unlink(p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "fetch", "status"])
    a = ap.parse_args()
    e = load_env()
    {"probe": cmd_probe, "fetch": cmd_fetch, "status": cmd_status}[a.cmd](e)
