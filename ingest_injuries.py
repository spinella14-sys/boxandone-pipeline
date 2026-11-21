#!/usr/bin/env python3
"""
ingest_injuries.py — the NBA's official injury report, every game day since 2019-20.

The league publishes the report as a PDF many times a day
(ak-static.cms.nba.com/referee/injury/Injury-Report_2025-12-01_05PM.pdf, and
since early 2026 every fifteen minutes, ..._05_15PM.pdf). There is no index of
old reports, so each day's addresses are tried.

What is kept is each player's FINAL designation for each game: for every game
on a date, the last report before it tipped. A team's list is taken from the
latest report where that game appears; a player who dropped off it had been
cleared and is not listed. Reports are walked back from the day's last one
until every game on the date (from our own game index) has been seen, so a
game the league drops from late reports after it tips is still found in an
earlier one. A team that never submitted is kept as NOT YET SUBMITTED.

Rows are stored exactly as the report prints them. The reading of a reason
into category / side / body part / injury type happens at export, from the
stored text, so it can be refined and re-run without fetching anything.

    python3 ingest_injuries.py sample --date 2025-12-01   # one day, printed, nothing written
    python3 ingest_injuries.py sample --file report.pdf   # one local PDF, printed
    python3 ingest_injuries.py backfill                   # every game day since 2019-20 -> local cache
    python3 ingest_injuries.py load --sync                # cache -> database, export, push (takes the lock)
    python3 ingest_injuries.py daily                      # yesterday and today (Eastern): the nightly step
    python3 ingest_injuries.py daily --today              # today only: the hourly step
    python3 ingest_injuries.py export [--all-players]
    python3 ingest_injuries.py explain "Jayson Tatum"

The backfill needs no lock and can be stopped and restarted: each finished
day is a file in data/injury/days/, and a restart skips it.

Writes v2/injuries/{season}.parquet and v2/players/{player_id}/injuries.parquet.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)

DATA = os.path.join(HOME, "data")
CACHE = os.path.join(DATA, "injury")
DAYS = os.path.join(CACHE, "days")
PDFS = os.path.join(CACHE, "pdf")
PREFIX = "v2"
BASE = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_%s_%s.pdf"
FIRST = date(2019, 10, 1)         # nothing earlier is in the archive
CANON = {"BRK": "BKN", "PHO": "PHX", "CHO": "CHA", "NOH": "NOP", "NJN": "BKN", "SEA": "OKC", "VAN": "MEM",
         "GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}
TEAMS = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET", "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}
TEAMS_FOLDED = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in TEAMS.items()}
NYS = "NOT YET SUBMITTED"

DDL = """
CREATE TABLE IF NOT EXISTS injury_rows (
    game_date DATE, season VARCHAR, game_time VARCHAR, matchup VARCHAR,
    team VARCHAR, team_abbr VARCHAR, player_name VARCHAR, player_id VARCHAR,
    status VARCHAR, reason VARCHAR, previous VARCHAR,
    report_stamp VARCHAR, report_url VARCHAR,
    PRIMARY KEY (game_date, team_abbr, player_name));
CREATE TABLE IF NOT EXISTS injury_days (
    game_date DATE PRIMARY KEY, season VARCHAR, reports INTEGER, latest VARCHAR,
    games INTEGER, expected INTEGER, missing VARCHAR, fetched_at TIMESTAMP);
"""


def canon(a):
    a = (a or "").strip().upper()
    return CANON.get(a, a)


def season_of(d):
    """The season a game date belongs to (the 2020 bubble ran into October)."""
    y = d.year
    if d.month >= 10 and not (y == 2020 and d.month < 12):
        return "%d-%s" % (y, str(y + 1)[2:])
    return "%d-%s" % (y - 1, str(y)[2:])


def today_et():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (datetime.utcnow() - timedelta(hours=5)).date()


# ---------------------------------------------------------------------------
# reading a report
# ---------------------------------------------------------------------------

STATUS = ("Out", "Doubtful", "Questionable", "Probable", "Available")
HEAD = {"Matchup": "matchup", "Team": "team", "Player": "player", "Current": "status",
        "Reason": "reason", "Previous": "previous"}
FIELDS = ("date", "time", "matchup", "team", "player", "status", "reason", "previous")


def _lines(words):
    out = {}
    for w in words:
        out.setdefault(round(w["top"], 1), []).append(w)
    return {t: sorted(ws, key=lambda w: w["x0"]) for t, ws in sorted(out.items())}


def _heading(lines):
    """Left edge of every column, and the heading's bottom, if this page has one.
    'Game' heads two columns: Game Date, then Game Time."""
    for top, ws in lines.items():
        texts = [w["text"] for w in ws]
        if "Matchup" in texts and "Reason" in texts:
            edges, games = {}, 0
            for w in ws:
                if w["text"] == "Game":
                    edges["date" if games == 0 else "time"] = w["x0"]
                    games += 1
                elif w["text"] in HEAD:
                    edges[HEAD[w["text"]]] = w["x0"]
            return edges, max(w["bottom"] for w in ws)
    return None, None


def _col(x, edges):
    best = None
    for name, x0 in sorted(edges.items(), key=lambda kv: kv[1]):
        if x >= x0 - 4:
            best = name
    return best


def _tail(reason):
    """A reason cut off mid-thought: it continues on the next line."""
    return reason.rstrip().endswith((";", "/", ",", "-", "("))


def read_pdf(path):
    """(report stamp, rows). Rows are as printed; a blank date/time/matchup/team
    cell takes the value above it, across pages too.

    The words are placed by position. Since 2023-24 the report sets its spaces
    so narrow that the default reading runs words together, so the tolerance
    is tighter than pdfplumber's default. A long reason wraps onto a line above
    and a line below its row; each line goes to the nearest row."""
    import pdfplumber
    rows, edges, carry, stamp = [], None, {}, None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1.5)
            lines = _lines(words)
            if stamp is None:
                m = re.search(r"Report:\s*(\d\d/\d\d/\d\d)\s+(\d\d:\d\d\s*[AP]M)",
                              " ".join(w["text"] for w in words[:8]))
                stamp = "%s %s" % m.groups() if m else None
            e, floor = _heading(lines)
            if e:
                edges = e
            else:
                floor = max((w["bottom"] for w in words if w["text"] == "Report:"), default=0)
            if not edges:
                continue
            body = {t: ws for t, ws in lines.items()
                    if t > floor + 1 and not (ws[0]["text"] == "Page" and len(ws) <= 4)}
            # a row is a line whose status cell holds a status, or a team
            # whose list hasn't been sent
            anchors = []
            for top, ws in body.items():
                cells = {}
                for w in ws:
                    cells.setdefault(_col(w["x0"], edges), []).append(w["text"])
                st = " ".join(cells.get("status", []))
                rs = " ".join(cells.get("reason", [])).upper()
                if st in STATUS or NYS in rs:
                    anchors.append(top)
            if not anchors:
                continue
            recs = {a: {} for a in anchors}
            for top, ws in body.items():
                if top < anchors[0] and rows and _tail(rows[-1]["reason"]) and \
                        not ws[0]["text"].startswith("Injury"):
                    # the end of the previous page's last reason
                    rows[-1]["reason"] += " " + " ".join(w["text"] for w in ws)
                    continue
                a = min(anchors, key=lambda t: abs(t - top))
                if abs(a - top) > 18:
                    continue
                for w in ws:
                    c = _col(w["x0"], edges)
                    if c:
                        recs[a].setdefault(c, []).append((top, w["x0"], w["text"]))
            for a in anchors:
                cell = {c: " ".join(t for _, _, t in sorted(v)) for c, v in recs[a].items()}
                if cell.get("date"):
                    carry = {}
                for c in ("date", "time", "matchup", "team"):
                    if cell.get(c):
                        carry[c] = cell[c]
                    else:
                        cell[c] = carry.get(c, "")
                rows.append({k: cell.get(k, "") for k in FIELDS})
    return stamp, rows


# ---------------------------------------------------------------------------
# finding a day's reports
# ---------------------------------------------------------------------------

def _label(h, minute=None):
    """'05PM' (hourly form) or '05_15PM' (quarter-hourly form) for hour h (0-23)."""
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return "%02d%s" % (h12, ap) if minute is None else "%02d_%02d%s" % (h12, minute, ap)


_session = None


def http():
    global _session
    if _session is None:
        from curl_cffi import requests as cr
        _session = cr
    return _session


from collections import Counter
from threading import Lock

STATS = Counter()          # what the report site answered, by request kind and status
_pace, _last = Lock(), [0.0]
GAP = 0.15                 # seconds between requests, across all threads


def _wait():
    """A steady pace: the report site stops answering when hit too fast."""
    with _pace:
        now = time.time()
        if now - _last[0] < GAP:
            time.sleep(GAP - (now - _last[0]))
        _last[0] = time.time()


def exists(url):
    for attempt in range(2):
        _wait()
        try:
            r = http().head(url, impersonate="chrome", timeout=20)
            STATS["HEAD %s" % r.status_code] += 1
            if r.status_code == 200:
                return True
            if r.status_code in (403, 404):
                # a missing report; also what a turned-away request looks
                # like, which the tally at the end shows
                return False
            if r.status_code in (429, 503):
                time.sleep(5)
                continue
        except Exception as e:
            STATS["HEAD %s" % type(e).__name__] += 1
        time.sleep(1)
    return False


def get_pdf(url, keep=True):
    os.makedirs(PDFS, exist_ok=True)
    p = os.path.join(PDFS, url.rsplit("/", 1)[1])
    if os.path.exists(p) and os.path.getsize(p) > 1000:
        return p
    for attempt in range(4):
        _wait()
        try:
            r = http().get(url, impersonate="chrome", timeout=40)
            STATS["GET %s" % r.status_code] += 1
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                with open(p, "wb") as f:
                    f.write(r.content)
                return p
            if r.status_code in (403, 429, 503):
                time.sleep(30 * (attempt + 1))     # turned away: wait it out
                continue
        except Exception as e:
            STATS["GET %s" % type(e).__name__] += 1
        time.sleep(2 * (attempt + 1))
    return None


HOURS = list(range(23, 10, -1))       # 11pm back to 11am, Eastern


def reports_for(d, pool):
    """The day's report addresses worth reading, latest first: one per hour,
    and the very latest one (which in the quarter-hourly era may be :45)."""
    ds = d.isoformat()
    hourly = [BASE % (ds, _label(h)) for h in HOURS]
    got = [u for u, ok in zip(hourly, pool.map(exists, hourly)) if ok]
    if got:
        return got
    quarter = [BASE % (ds, _label(h, 0)) for h in HOURS]
    hit = [(h, u) for h, u, ok in zip(HOURS, quarter, pool.map(exists, quarter)) if ok]
    if not hit:
        # a day whose reports all fall later in the hour
        quarter = [BASE % (ds, _label(h, 30)) for h in HOURS]
        hit = [(h, u) for h, u, ok in zip(HOURS, quarter, pool.map(exists, quarter)) if ok]
        if not hit:
            return []
    top = hit[0][0]
    later = [BASE % (ds, _label(top, m)) for m in (45, 30, 15)]
    extra = [u for u, ok in zip(later, pool.map(exists, later)) if ok]
    return extra[:1] + [u for _, u in hit if u not in extra[:1]]


# ---------------------------------------------------------------------------
# a day: the final list for every game
# ---------------------------------------------------------------------------

def team_abbr(name):
    return TEAMS_FOLDED.get(re.sub(r"[^a-z0-9]", "", (name or "").lower()))


def game_key(matchup):
    parts = (matchup or "").replace(" ", "").split("@")
    return frozenset(canon(p) for p in parts) if len(parts) == 2 else frozenset([matchup])


def settle_day(d, reports, expected):
    """reports: [(url, stamp, rows)] latest first. For each game, the team lists
    from the latest report it appears in. Returns (rows, games seen, missing)."""
    seen, final, pending = {}, [], {}
    for url, stamp, rows in reports:
        groups = {}
        for r in rows:
            if r["date"] and r["date"] != d.strftime("%m/%d/%Y"):
                continue          # a later day's game on the same report
            groups.setdefault(game_key(r["matchup"]), {}).setdefault(r["team"], []).append(r)
        for g, teams in groups.items():
            if g in seen:
                for t, rs in teams.items():
                    if (g, t) in pending and not any(NYS in r["reason"].upper() for r in rs):
                        del pending[(g, t)]
                        final += [dict(r, report_stamp=stamp, report_url=url) for r in rs]
                continue
            seen[g] = stamp
            for t, rs in teams.items():
                if any(NYS in r["reason"].upper() for r in rs):
                    pending[(g, t)] = [dict(r, report_stamp=stamp, report_url=url) for r in rs]
                else:
                    final += [dict(r, report_stamp=stamp, report_url=url) for r in rs]
        if expected and expected <= set(seen) and not pending:
            break
    for rs in pending.values():
        final += rs
    missing = sorted("@".join(sorted(g)) for g in (expected or set()) - set(seen))
    return final, seen, missing


def collect_day(d, expected, pool, read_all=False):
    """Read a day's reports, latest first, until every expected game is seen."""
    urls = reports_for(d, pool)
    reports, used = [], 0
    for url in urls:
        p = get_pdf(url)
        if not p:
            continue
        try:
            stamp, rows = read_pdf(p)
        except Exception as e:
            print("    %s: unreadable (%s)" % (url.rsplit("/", 1)[1], str(e)[:60]))
            continue
        reports.append((url, stamp, rows))
        used += 1
        final, seen, missing = settle_day(d, reports, expected)
        if read_all:
            continue
        if expected and not missing:
            break
        if not expected and used >= 3:
            break
    final, seen, missing = settle_day(d, reports, expected)
    return {"date": d.isoformat(), "reports": used, "available": len(urls),
            "latest": reports[0][1] if reports else None, "games": len(seen),
            "expected": len(expected or ()), "missing": missing, "rows": final}


# ---------------------------------------------------------------------------
# game days, from our own game index
# ---------------------------------------------------------------------------

def game_index(s3, bucket, seasons, refresh=()):
    """{date: set of games (frozenset of two teams)} for the given seasons,
    regular season and postseason, from v2/seasons/{s}/games.parquet."""
    import duckdb
    os.makedirs(CACHE, exist_ok=True)
    out = {}
    con = duckdb.connect()
    for s in seasons:
        p = os.path.join(CACHE, "games_%s.parquet" % s)
        if s in refresh or not os.path.exists(p):
            try:
                s3.download_file(bucket, "%s/seasons/%s/games.parquet" % (PREFIX, s), p)
            except Exception:
                continue
        for gd, h, a, st in con.execute("""SELECT CAST(game_date AS DATE), home_abbr, away_abbr, season_type
                                           FROM read_parquet('%s')""" % p).fetchall():
            if (st or "").lower().startswith("pre"):
                continue
            out.setdefault(gd, set()).add(frozenset((canon(h), canon(a))))
    return out


def scheduled(d):
    """Games on a date from the league schedule in the database (today's and
    tomorrow's games aren't in the game index yet)."""
    import duckdb
    import nightly2 as N
    try:
        con = duckdb.connect(N.DB_PATH, read_only=True)
        rows = con.execute("""SELECT home_abbr, away_abbr FROM scheduled_games
                              WHERE game_date = ? AND season_type NOT ILIKE 'pre%'""", [d]).fetchall()
        con.close()
        return {frozenset((canon(h), canon(a))) for h, a in rows}
    except Exception:
        return set()


def seasons_between(a, b):
    out, y = [], int(season_of(a)[:4])
    while y <= int(season_of(b)[:4]):
        out.append("%d-%s" % (y, str(y + 1)[2:]))
        y += 1
    return out


def day_path(d):
    return os.path.join(DAYS, "%s.json" % d.isoformat())


def cmd_backfill(start, end, workers):
    import nightly2 as N
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    os.makedirs(DAYS, exist_ok=True)
    seasons = seasons_between(start, end)
    idx = game_index(s3, bucket, seasons, refresh=(N.current_season(),))
    days = sorted(d for d in idx if start <= d <= end and not os.path.exists(day_path(d)))
    print("  %d game days from %s to %s; %d already done, %d to read" % (
        sum(1 for d in idx if start <= d <= end), start, end,
        sum(1 for d in idx if start <= d <= end) - len(days), len(days)))
    heads = ThreadPoolExecutor(8)
    t0, done = time.time(), [0]

    def one(d):
        try:
            res = collect_day(d, idx[d], heads)
        except Exception as e:
            print("    %s: failed (%s) — will retry next run" % (d, str(e)[:80]))
            return
        done[0] += 1
        flag = "" if not res["missing"] else "  missing %s" % ", ".join(res["missing"])
        if not res["reports"]:
            # not kept, so a later run tries the day again
            flag = "  no report found"
        else:
            with open(day_path(d), "w") as f:
                json.dump(res, f)
        el = time.time() - t0
        left = (len(days) - done[0]) * el / done[0]
        print("    %s  %2d reports read (of %2d)  %2d/%2d games  %3d rows  latest %s%s   [%d/%d, ~%d min left]" % (
            d, res["reports"], res["available"], res["games"], res["expected"], len(res["rows"]),
            res["latest"] or "-", flag, done[0], len(days), left / 60))

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, days))
    print("  backfill finished: %d days read in %d min" % (done[0], (time.time() - t0) / 60))
    print("  the report site answered: %s" % ", ".join("%s x%d" % kv for kv in sorted(STATS.items())))
    print("  days kept so far: %d" % len([f for f in os.listdir(DAYS) if f.endswith(".json")]))


# ---------------------------------------------------------------------------
# into the database
# ---------------------------------------------------------------------------

def db():
    import duckdb
    import nightly2 as N
    con = duckdb.connect(N.DB_PATH)
    for s in DDL.split(";"):
        if s.strip():
            con.execute(s)
    return con


def flip(name):
    """'Porter Jr., Kevin' -> 'Kevin Porter Jr.'"""
    if "," in name:
        last, first = name.split(",", 1)
        return ("%s %s" % (first.strip(), last.strip())).strip()
    return name.strip()


SUFFIX = re.compile(r"\s+(jr|sr|ii|iii|iv|v)$")


class Names:
    """Report name (and team, season) -> our player id. Only an unambiguous
    answer counts; a namesake is settled by who played for that team that
    season; anything else stays unmatched."""

    def __init__(self, con, s3, bucket):
        from ingest_rosters import norm
        self.norm = norm
        self.by_name = {}
        for pid, n1, n2 in con.execute("""
                SELECT p.player_id, p.name_normalized, a.alias_normalized
                FROM players p LEFT JOIN player_aliases a USING (player_id)""").fetchall():
            for n in (n1, n2):
                if n:
                    n = norm(n)
                    self.by_name.setdefault(n, set()).add(pid)
                    self.by_name.setdefault(SUFFIX.sub("", n), set()).add(pid)
        # who played for whom, by season: completed seasons from the service
        # history file, this season from the database
        self.played = set()
        p = os.path.join(CACHE, "service_history.parquet")
        try:
            os.makedirs(CACHE, exist_ok=True)
            if not os.path.exists(p):
                s3.download_file(bucket, "%s/service/history.parquet" % PREFIX, p)
            for pid, season, team in con.execute(
                    "SELECT player_id, season, team_abbr FROM read_parquet('%s')" % p).fetchall():
                self.played.add((pid, season, canon(team)))
        except Exception as e:
            print("  (service history unavailable: %s)" % str(e)[:60])
        for q in ("""SELECT DISTINCT b.player_id, g.season, b.team_abbr FROM player_game_box b
                     JOIN games g USING (game_id)""",
                  """SELECT DISTINCT player_id, season, team_abbr FROM team_rosters"""):
            try:
                for pid, season, team in con.execute(q).fetchall():
                    self.played.add((pid, season, canon(team)))
            except Exception:
                pass
        self.memo = {}

    def match(self, name, team, season):
        key = (name, team, season)
        if key in self.memo:
            return self.memo[key]
        n = self.norm(flip(name))
        cands = self.by_name.get(n) or self.by_name.get(SUFFIX.sub("", n)) or set()
        pid = None
        if len(cands) == 1:
            pid = next(iter(cands))
        elif len(cands) > 1:
            here = [c for c in cands if (c, season, team) in self.played]
            if len(here) == 1:
                pid = here[0]
        self.memo[key] = pid
        return pid


def to_rows(day, names):
    d = date.fromisoformat(day["date"])
    season = season_of(d)
    out = {}
    for r in day["rows"]:
        abbr = team_abbr(r["team"]) or r["team"]
        try:
            gd = datetime.strptime(r["date"], "%m/%d/%Y").date() if r["date"] else d
        except ValueError:
            gd = d
        player = r["player"].strip()
        pid = names.match(player, abbr, season) if player else None
        out[(gd, abbr, player)] = (gd, season, r["time"], r["matchup"], r["team"], abbr, player, pid,
                                   r["status"], r["reason"], r["previous"], r["report_stamp"], r["report_url"])
    return list(out.values())


def load(con, s3, bucket, day_files):
    """Put the given cached days into the database (each day replaces itself).
    Returns the set of players whose rows changed."""
    names = Names(con, s3, bucket)
    changed, unmatched, n = set(), {}, 0
    for path in day_files:
        day = json.load(open(path))
        d = date.fromisoformat(day["date"])
        rows = to_rows(day, names)
        before = set(con.execute("""SELECT player_id, team_abbr, player_name, status, reason FROM injury_rows
                                    WHERE game_date = ?""", [d]).fetchall())
        after = {(r[7], r[5], r[6], r[8], r[9]) for r in rows}
        for r in before ^ after:
            if r[0]:
                changed.add(r[0])
        con.execute("DELETE FROM injury_rows WHERE game_date = ?", [d])
        if rows:
            con.executemany("INSERT OR REPLACE INTO injury_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO injury_days VALUES (?,?,?,?,?,?,?,now())",
                    [d, season_of(d), day["reports"], day["latest"], day["games"], day["expected"],
                     ", ".join(day["missing"])])
        for r in rows:
            if r[6] and not r[7]:
                unmatched[(r[6], r[5])] = unmatched.get((r[6], r[5]), 0) + 1
        n += 1
    print("  loaded %d days; %d players' histories changed" % (n, len(changed)))
    if unmatched:
        print("  %d names not matched to a player (most listed first):" % len(unmatched))
        for (nm, t), c in sorted(unmatched.items(), key=lambda kv: -kv[1])[:15]:
            print("    %-28s %s  %d" % (nm, t, c))
    return changed


# ---------------------------------------------------------------------------
# the reading of a reason — re-runnable, from the stored text
# ---------------------------------------------------------------------------

CATEGORIES = [
    ("injury", r"^injury/illness"), ("injury", r"^concussion"),
    ("reconditioning", r"return to competition|reconditioning"), ("two_way", r"^g league\s*-\s*two-way"),
    ("assignment", r"^g league\s*-\s*on assignment"), ("g_league", r"^g league"),
    ("coach", r"coach'?s decision"), ("rest", r"^rest"), ("personal", r"^personal"),
    ("suspension", r"suspension"), ("not_with_team", r"not with team"),
    ("protocols", r"health and safety|protocols"), ("not_submitted", r"not yet submitted"),
]
# body parts, most specific first; each with the region it rolls up to
PARTS = [
    ("great toe", "foot"), ("big toe", "foot"), ("plantar fascia", "foot"), ("plantar", "foot"),
    ("metatarsal", "foot"), ("navicular", "foot"), ("cuneiform", "foot"), ("toe", "foot"), ("heel", "foot"),
    ("foot", "foot"), ("high ankle", "ankle"), ("ankle", "ankle"),
    ("achilles", "lower leg"), ("soleus", "lower leg"), ("plantaris", "lower leg"), ("tibialis", "lower leg"), ("calf", "lower leg"), ("shin", "lower leg"), ("tibia", "lower leg"),
    ("fibula", "lower leg"), ("lower leg", "lower leg"), ("leg", "lower leg"),
    ("acl", "knee"), ("mcl", "knee"), ("pcl", "knee"), ("lcl", "knee"), ("meniscus", "knee"),
    ("patellar", "knee"), ("patella", "knee"), ("knee", "knee"),
    ("hamstring", "thigh"), ("quadriceps", "thigh"), ("quad", "thigh"), ("thigh", "thigh"),
    ("adductor", "groin/hip"), ("groin", "groin/hip"), ("hip flexor", "groin/hip"), ("hip", "groin/hip"),
    ("pelvis", "groin/hip"), ("pelvic", "groin/hip"), ("glute", "groin/hip"),
    ("low back", "back"), ("lower back", "back"), ("lumbar", "back"), ("back", "back"), ("disc", "back"),
    ("spine", "back"), ("sciatica", "back"), ("sacral", "back"), ("tailbone", "back"),
    ("neck", "neck"), ("cervical", "neck"),
    ("rotator cuff", "shoulder"), ("ac joint", "shoulder"), ("ac", "shoulder"), ("labrum", "shoulder"), ("shoulder", "shoulder"),
    ("clavicle", "shoulder"), ("elbow", "arm"), ("forearm", "arm"), ("bicep", "arm"), ("tricep", "arm"),
    ("arm", "arm"), ("wrist", "hand"), ("thumb", "hand"), ("finger", "hand"), ("metacarpal", "hand"),
    ("hand", "hand"),
    ("concussion", "head"), ("migraine", "head"), ("head", "head"), ("face", "head"), ("facial", "head"), ("eye", "head"),
    ("orbital", "head"), ("nasal", "head"), ("nose", "head"), ("jaw", "head"), ("tooth", "head"),
    ("mouth", "head"), ("ear", "head"),
    ("oblique", "core"), ("abdominal", "core"), ("abdomen", "core"), ("rib", "core"), ("chest", "core"),
    ("core", "core"), ("sternum", "core"),
    ("illness", "illness"), ("non-covid", "illness"), ("covid", "illness"), ("flu", "illness"),
    ("virus", "illness"), ("respiratory", "illness"), ("gastro", "illness"),
]
# a named injury outranks the phase it is in: "Strain; Injury Management" is a strain
TYPES = [
    ("surgery", "surgery"), ("repair", "surgery"), ("procedure", "surgery"), ("reconstruction", "surgery"),
    ("arthroscopy", "surgery"), ("meniscectomy", "surgery"), ("herniation", "herniation"),
    ("rupture", "tear"), ("tear", "tear"), ("torn", "tear"),
    ("fracture", "fracture"), ("break", "fracture"), ("stress reaction", "stress reaction"),
    ("dislocation", "dislocation"), ("dislocated", "dislocation"), ("displacement", "dislocation"),
    ("subluxation", "dislocation"), ("cartilage", "cartilage"),
    ("sprain", "sprain"), ("strain", "strain"), ("strained", "strain"),
    ("contusion", "contusion"), ("bruise", "contusion"),
    ("tendinitis", "tendinopathy"), ("tendinopathy", "tendinopathy"), ("tendinosis", "tendinopathy"),
    ("tendonitis", "tendinopathy"), ("bursitis", "inflammation"), ("fasciitis", "inflammation"), ("inflammation", "inflammation"),
    ("impingement", "impingement"), ("spasm", "spasms"),
    ("soreness", "soreness"), ("sore", "soreness"), ("tightness", "tightness"), ("irritation", "soreness"), ("discomfort", "soreness"),
    ("concussion", "concussion"), ("illness", "illness"), ("infection", "illness"),
    ("injury management", "management"), ("load management", "management"), ("management", "management"),
    ("rest", "management"), ("injury recovery", "recovery"), ("recovery", "recovery"), ("rehab", "recovery"),
    ("return to competition", "recovery"), ("reconditioning", "recovery"),
]


def parse_reason(reason):
    """category, side, body part, region, injury type — None where the text
    doesn't say. Matching is on words, so it reads both the older
    'Left Knee; Sprain' and the newer 'Left Knee Sprain'."""
    low = re.sub(r"\s+", " ", (reason or "").lower()).strip().strip("-").strip()
    cat = next((c for c, pat in CATEGORIES if re.search(pat, low)), "other" if low else None)
    side = part = region = kind = None
    if cat == "injury":
        body = re.sub(r"^injury/illness\s*-?\s*", "", low)
        m = re.search(r"\b(left|right|bilateral)\b", body)
        side = m.group(1) if m else None
        # the body part named first (the longest name where two start together)
        best = None
        for word, reg in PARTS:
            m = re.search(r"\b%s\b" % re.escape(word), body)
            if m and (best is None or (m.start(), -len(word)) < best[0]):
                best = ((m.start(), -len(word)), word, reg)
        if best:
            part, region = best[1], best[2]
        for word, k in TYPES:
            if re.search(r"\b%s" % re.escape(word), body):
                kind = k
                break
        if region == "illness" and not kind:
            kind = "illness"
    return cat, side, part, region, kind


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def frame(con, where, params):
    rows = con.execute("""
        SELECT r.game_date, r.season, r.game_time, r.matchup, r.team_abbr, r.player_name, r.player_id,
               r.status, r.reason, r.previous, r.report_stamp
        FROM injury_rows r WHERE %s ORDER BY r.game_date, r.matchup, r.team_abbr, r.player_name""" % where,
                       params).fetchall()
    out = []
    for r in rows:
        cat, side, part, region, kind = parse_reason(r[8])
        out.append(r + (cat, side, part, region, kind))
    return out


COLS = ["game_date", "season", "game_time", "matchup", "team_abbr", "player_name", "player_id", "status",
        "reason", "previous", "report_stamp", "category", "side", "body_part", "region", "injury_type"]


GAME_IDS = {}


def game_ids(season, s3=None, bucket=None):
    """{(date, game key): game_id} for a season, from its game index — the id
    the box score is filed under. Abbreviations are folded to NBA codes."""
    if season in GAME_IDS:
        return GAME_IDS[season]
    import duckdb
    p = os.path.join(CACHE, "games_%s.parquet" % season)
    out = {}
    if s3 is not None:
        try:
            os.makedirs(CACHE, exist_ok=True)
            s3.download_file(bucket, "%s/seasons/%s/games.parquet" % (PREFIX, season), p)
        except Exception:
            pass
    if os.path.exists(p):
        c = duckdb.connect()
        for gid, gd, h, a in c.execute("""SELECT game_id, CAST(game_date AS DATE), home_abbr, away_abbr
                                          FROM read_parquet('%s')""" % p).fetchall():
            out[(gd, frozenset((canon(h), canon(a))))] = gid
        c.close()
    GAME_IDS[season] = out
    return out


def write_parquet(rows, path):
    """rows: frame() rows; each gets the game id of its game, where we hold it."""
    import duckdb
    c = duckdb.connect()
    c.execute("CREATE TABLE t (game_date DATE, season VARCHAR, game_time VARCHAR, matchup VARCHAR, "
              "team_abbr VARCHAR, player_name VARCHAR, player_id VARCHAR, status VARCHAR, reason VARCHAR, "
              "previous VARCHAR, report_stamp VARCHAR, category VARCHAR, side VARCHAR, body_part VARCHAR, "
              "region VARCHAR, injury_type VARCHAR, game_id VARCHAR)")
    out = []
    for r in rows:
        ids = game_ids(r[1])
        out.append(r + (ids.get((r[0], game_key(r[3]))),))
    if out:
        c.executemany("INSERT INTO t VALUES (%s)" % ",".join("?" * (len(COLS) + 1)), out)
    c.execute("COPY (SELECT * FROM t ORDER BY game_date, matchup, team_abbr, player_name) "
              "TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % path)
    c.close()


def export(con, s3, bucket, seasons=None, players=None):
    """Season files for the given seasons (default: all held), and a career
    file for each given player (None: none; 'all': everyone listed)."""
    held = [s for (s,) in con.execute("SELECT DISTINCT season FROM injury_rows ORDER BY 1").fetchall()]
    seasons = held if seasons is None else [s for s in seasons if s in held]
    with tempfile.TemporaryDirectory() as tmp:
        for s in held:
            game_ids(s, s3, bucket)
        for s in seasons:
            rows = frame(con, "season = ?", [s])
            p = os.path.join(tmp, "%s.parquet" % s)
            write_parquet(rows, p)
            s3.upload_file(p, bucket, "%s/injuries/%s.parquet" % (PREFIX, s))
            print("  v2/injuries/%s.parquet: %d rows" % (s, len(rows)))
        if players == "all":
            players = [p for (p,) in con.execute(
                "SELECT DISTINCT player_id FROM injury_rows WHERE player_id IS NOT NULL").fetchall()]
        players = sorted(players or [])
        for i, pid in enumerate(players):
            rows = frame(con, "player_id = ?", [pid])
            p = os.path.join(tmp, "p.parquet")
            write_parquet(rows, p)
            s3.upload_file(p, bucket, "%s/players/%s/injuries.parquet" % (PREFIX, pid))
            if (i + 1) % 100 == 0:
                print("  player files: %d/%d" % (i + 1, len(players)), end="\r")
        if players:
            print("  player files: %d written                " % len(players))


# ---------------------------------------------------------------------------

def cmd_sample(d=None, path=None):
    if path:
        stamp, rows = read_pdf(path)
        print("  %s — report of %s, %d rows" % (os.path.basename(path), stamp, len(rows)))
        for r in rows:
            print("    %-10s %-7s %-22s %-24s %-12s %s" % (r["date"], r["matchup"], r["team"][:22],
                                                         r["player"][:24], r["status"], r["reason"]))
        return
    import nightly2 as N
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    idx = game_index(s3, bucket, [season_of(d)], refresh=(season_of(d),))
    expected = idx.get(d) or scheduled(d)
    heads = ThreadPoolExecutor(8)
    urls = reports_for(d, heads)
    print("  %s: %d games expected; reports found (latest first):" % (d, len(expected)))
    for u in urls:
        print("    " + u.rsplit("/", 1)[1])
    res = collect_day(d, expected, heads)
    print("  the report site answered: %s" % ", ".join("%s x%d" % kv for kv in sorted(STATS.items())))
    print("  read %d report(s): %d games seen, missing: %s" % (res["reports"], res["games"],
                                                             ", ".join(res["missing"]) or "none"))
    # does the day's last report still list games that have already tipped?
    if urls:
        stamp, rows = read_pdf(get_pdf(urls[0]))
        times = sorted({r["time"] for r in rows if r["time"]})
        print("  the last report (%s) lists games at: %s" % (stamp, ", ".join(times)))
    for r in res["rows"]:
        print("    %-7s %-4s %-24s %-12s %-50s [%s]" % (r["matchup"], team_abbr(r["team"]) or r["team"],
                                                     r["player"][:24], r["status"], r["reason"][:50],
                                                     r["report_stamp"]))


def cmd_daily(con, s3, bucket, dates):
    """Collect the given dates now, load them, export what they touched."""
    os.makedirs(DAYS, exist_ok=True)
    heads = ThreadPoolExecutor(8)
    seasons = sorted({season_of(d) for d in dates})
    idx = game_index(s3, bucket, seasons, refresh=seasons)
    files = []
    for d in dates:
        expected = idx.get(d) or scheduled(d)
        if not expected:
            print("  %s: no games" % d)
            continue
        res = collect_day(d, expected, heads)
        if not res["reports"]:
            print("  %s: no report published yet" % d)
            continue
        with open(day_path(d), "w") as f:
            json.dump(res, f)
        files.append(day_path(d))
        print("  %s: %d reports read, %d/%d games, %d rows, latest %s" % (
            d, res["reports"], res["games"], res["expected"], len(res["rows"]), res["latest"]))
    if not files:
        return
    changed = load(con, s3, bucket, files)
    export(con, s3, bucket, seasons=seasons, players=changed)


def cmd_explain(con, name):
    from ingest_rosters import norm
    rows = con.execute("""SELECT r.player_id, r.game_date, r.matchup, r.team_abbr, r.status, r.reason
                          FROM injury_rows r LEFT JOIN players p USING (player_id)
                          ORDER BY r.game_date""").fetchall()
    want = norm(name)
    pids = [p for (p, n) in con.execute("SELECT player_id, name FROM players").fetchall() if norm(n) == want]
    if not pids:
        print("  no player named %r" % name)
        return
    from collections import Counter
    for pid in pids:
        mine = [r for r in rows if r[0] == pid]
        print("  %s (%s): listed for %d games" % (name, pid, len(mine)))
        print("   by status:", ", ".join("%s %d" % kv for kv in Counter(r[4] for r in mine).most_common()))
        regions = Counter(parse_reason(r[5])[3] or parse_reason(r[5])[0] for r in mine)
        print("   by reason:", ", ".join("%s %d" % kv for kv in regions.most_common()))
        prev = None
        for r in mine:
            key = (r[4], r[5])
            if key != prev:
                print("    %s %-8s %-4s %-12s %s" % (r[1], r[2], r[3], r[4], r[5]))
            prev = key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sample", "backfill", "load", "daily", "export", "explain"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--date")
    ap.add_argument("--file")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--all-players", action="store_true")
    ap.add_argument("--today", action="store_true", help="daily: today only (the hourly step)")
    a = ap.parse_args()

    if a.cmd == "sample":
        cmd_sample(date.fromisoformat(a.date) if a.date else today_et() - timedelta(days=1), a.file)
        return
    if a.cmd == "backfill":
        start = date.fromisoformat(a.start) if a.start else FIRST
        end = date.fromisoformat(a.end) if a.end else today_et() - timedelta(days=1)
        cmd_backfill(start, end, a.workers)
        return

    import nightly2 as N
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    log = N.Log()
    if a.sync:
        if not N.acquire_lock(s3, bucket, log):
            sys.exit("  another run holds the lock — try again when it finishes")
    try:
        if a.sync and not N.pull_db(s3, bucket, log):
            sys.exit("  could not pull the database")
        con = db()
        try:
            if a.cmd == "load":
                files = sorted(os.path.join(DAYS, f) for f in os.listdir(DAYS) if f.endswith(".json"))
                load(con, s3, bucket, files)
                export(con, s3, bucket, players="all")
            elif a.cmd == "daily":
                t = today_et()
                dates = [date.fromisoformat(a.date)] if a.date else [t] if a.today else [t - timedelta(days=1), t]
                cmd_daily(con, s3, bucket, dates)
            elif a.cmd == "export":
                export(con, s3, bucket, players="all" if a.all_players else None)
            elif a.cmd == "explain":
                cmd_explain(con, a.name or "")
        finally:
            con.close()
        if a.sync:
            N.push_db(s3, bucket, log)
    finally:
        if a.sync:
            N.release_lock(s3, bucket)


if __name__ == "__main__":
    main()
