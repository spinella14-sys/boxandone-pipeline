#!/usr/bin/env python3
"""
ingest_twoway.py — what the Two-Way Tracker is built from.

  transactions   gleague.nba.com/transactions: assignments, recalls, call-ups,
                 two-way terminations, G League signings, waivers, acquisitions,
                 injuries. One line per move, names only (no ids), so each
                 name is matched to a player by name and team where it can be,
                 and left unmatched (never guessed) where it cannot.
  list           gleague.nba.com/twowayplayers: every two-way player with his
                 NBA and G League team, kept as a dated snapshot each day.
  active         every finished NBA regular-season game's ACTIVE / INACTIVE
                 list, from NBA's box score: the count the 50-game limit runs on.
  export         the tracker file: for each current two-way player, his
                 prorated limit, active games used, games left, and the date
                 he would reach the limit if active every game from here.

All of it reads pages GitHub can reach directly — no relay.

    python3 ingest_twoway.py run              # all four, the nightly's step
    python3 ingest_twoway.py sample           # what each page parses into, nothing written
    python3 ingest_twoway.py run --sync       # on the Mac: lock, pull, write, push

Rules (2023 CBA, as Larry Coon's glossary states them):
  - limit: 50 regular-season games on the Active List, prorated for a contract
    signed after the season starts, by the share of the season left
  - deadline: no two-way signings after March 4 (2026's date; checked yearly)
The proration here follows the glossary's description, so a count can be a
game off the league's own; it is labelled as computed, not official.
"""
import argparse
import hashlib
import html as htmllib
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)

DATA = os.path.join(HOME, "data")
PREFIX = "v2"
TX_URL = "https://basketball.realgm.com/gleague/transactions/league"   # gleague.nba.com stopped after 2023-24
LIST_URL = "https://gleague.nba.com/twowayplayers"
LIMIT = 50
DEADLINE = "03-04"            # month-day of the two-way signing deadline
BBREF = {"BRK": "BKN", "PHO": "PHX", "CHO": "CHA"}   # Basketball Reference -> NBA codes

DDL = """
CREATE TABLE IF NOT EXISTS gl_transactions (
    txn_id VARCHAR PRIMARY KEY, txn_date DATE, season VARCHAR, kind VARCHAR,
    name VARCHAR, player_id VARCHAR, from_team VARCHAR, to_team VARCHAR,
    contract VARCHAR, raw_text VARCHAR, seen_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS twoway_list (
    snap_date DATE, season VARCHAR, name VARCHAR, player_id VARCHAR,
    nba_abbr VARCHAR, nba_team VARCHAR, gl_team VARCHAR, gl_team_id BIGINT,
    PRIMARY KEY (snap_date, name));
CREATE TABLE IF NOT EXISTS nba_movement (
    txn_key VARCHAR PRIMARY KEY, txn_date DATE, season VARCHAR, kind VARCHAR,
    txn_type VARCHAR, description VARCHAR, person_id VARCHAR, player_id VARCHAR,
    team_id BIGINT, team_abbr VARCHAR, seen_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS nba_active (
    nba_game_id VARCHAR, person_id VARCHAR, team_abbr VARCHAR, game_date DATE,
    season VARCHAR, active BOOLEAN, played BOOLEAN, reason VARCHAR,
    PRIMARY KEY (nba_game_id, person_id));
"""


def db():
    import duckdb
    con = duckdb.connect(os.path.join(DATA, "boxandone.duckdb"))
    for s in DDL.split(";"):
        if s.strip():
            con.execute(s)
    for col in ("realgm_id VARCHAR", "nba_team VARCHAR", "source VARCHAR"):
        con.execute("ALTER TABLE gl_transactions ADD COLUMN IF NOT EXISTS " + col)
    return con


def current_season():
    import nightly2 as N
    return N.current_season()


def fetch(url):
    from curl_cffi import requests as cr
    for attempt in range(3):
        try:
            r = cr.get(url, impersonate="chrome", timeout=40,
                       headers={"Accept": "text/html,application/xhtml+xml"})
            if r.status_code == 200:
                return r.text
            print("    %s: HTTP %s" % (url, r.status_code))
        except Exception as e:
            print("    %s: %s" % (url, str(e)[:70]))
        time.sleep(5 * (attempt + 1))
    return None


def lines_of(html):
    """Page text as lines: block tags become line breaks, the rest is dropped."""
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|li|tr|h[1-6]|div|td|th|section|article)>", "\n", html)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", html))
    return [re.sub(r"\s+", " ", l).strip() for l in text.split("\n") if l.strip()]


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

def norm(s):
    import ingest_rosters as R
    return R.norm(s)


class Names:
    """Match a name (and, where known, a team) to our player id. Only an
    unambiguous match counts; anything else stays unmatched."""

    def __init__(self, con, season):
        self.by_name = {}
        rows = con.execute("""
            SELECT p.player_id, p.name_normalized, a.alias_normalized
            FROM players p LEFT JOIN player_aliases a USING (player_id)""").fetchall()
        for pid, n1, n2 in rows:
            for n in (n1, n2):
                if n:
                    self.by_name.setdefault(n, set()).add(pid)
        # who has played this season, and for whom: breaks ties between namesakes
        self.recent = {}
        for pid, team in con.execute("""
                SELECT DISTINCT player_id, team_abbr FROM gl_player_game
                WHERE season = ? AND player_id IS NOT NULL""", [season]).fetchall():
            self.recent.setdefault(pid, set()).add(norm(team))
        try:
            for pid, team in con.execute("""
                    SELECT DISTINCT player_id, team_abbr FROM player_game_box b JOIN games g USING (game_id)
                    WHERE g.season = ?""", [season]).fetchall():
                self.recent.setdefault(pid, set()).add(norm(team))
        except Exception:
            pass

    def match(self, name, *teams):
        cands = self.by_name.get(norm(name), set())
        if len(cands) == 1:
            return next(iter(cands))
        if len(cands) > 1:
            playing = [p for p in cands if p in self.recent]
            if len(playing) == 1:
                return playing[0]
        return None


# ---------------------------------------------------------------------------
# G League transactions
# ---------------------------------------------------------------------------

REALGM = "https://basketball.realgm.com"
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}

# each sentence with its links replaced by {P} (the player) and {T} (a team).
# Every {T} may be blank (RealGM names some teams without a link); the roles
# say which column each {T} slot fills, in order.
FORMS = [
    ("rights_trade", r"{P} was inactive and had his rights acquired by the {T} from the {T} in a trade", ("to", "from")),
    ("gl_trade", r"{P} was acquired by the {T} from the {T} in a trade", ("to", "from")),
    ("return_from_callup", r"{P} returned to the {T} after an NBA call up", ("to",)),
    ("returned", r"{P} returned to the {T}", ("to",)),
    ("gl_waive", r"The {T} placed the contract of {P} on waivers", ("from",)),
    ("twoway_sign", r"{P} signed a two-way contract with the {T} and {T}", ("nba", "to")),
    ("allocate", r"{P}, whose NBA Rights is held by the {T}, was allocated to the {T}", ("nba", "to")),
    ("allocate", r"{P} was allocated to the {T} from the {T}", ("to", "nba")),
    ("allocate", r"{P} was allocated to the {T}", ("to",)),
    ("deallocate", r"{P} was deallocated from the {T}", ("from",)),
    ("tryout", r"{P} tried out with the {T}", ("to",)),
    ("draft", r"The {T} selected {P} in Round (?P<rnd>\d+) with Pick (?P<pick>\d+) in the (?P<yr>\d+) (?P<dr>Annual|International) G League Draft", ("to",)),
    ("assign", r"The {T} assigned {P} to the {T}", ("nba", "to")),
    ("recall", r"The {T} recalled {P} from the {T}", ("nba", "from")),
    ("call_up", r"The {T} called up {P} from the {T}", ("nba", "from")),
    ("twoway_end", r"The {T} ended the two-way contract of {P}", ("nba",)),
    ("gl_terminate", r"The G League terminated the contract of {P} from the {T}", ("from",)),
    ("pool_claim", r"The {T} claimed {P} from the player pool", ("to",)),
    ("gl_claim", r"The {T} made a successful waiver claim for the contract of {P}", ("to",)),
    ("deactivate", r"The {T} deactivated {P}", ("from",)),
]


def form_regex(pat):
    """A form as a regex in which every team slot is optional (with the space
    in front of it) and a stray space before punctuation is allowed."""
    out, n = "", 0
    for i, part in enumerate(pat.split("{T}")):
        part = part.replace("{P}", r"\{P\}")
        if i:
            out = out.rstrip(" ")
            out += r"(?: (?P<t%d>\{T\}))?" % n
            n += 1
            part = " ?" + part.lstrip(" ") if part[:1] == " " else " ?" + part
        out += part
    return re.compile(out)


FORM_RES = [(kind, form_regex(pat), roles) for kind, pat, roles in FORMS]


def parse_transactions(html):
    """RealGM's log: an <h3> per date, then one <li> sentence per move."""
    out = []
    for block in re.split(r"(?i)<h3[^>]*>", html)[1:]:
        m = re.match(r"\s*([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", block)
        if not m or m.group(1) not in MONTHS:
            continue
        day = date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
        block = re.split(r"(?i)<h[123][^>]*>", block)[0]
        for li in re.findall(r"(?is)<li>(.*?)</li>", block):
            pm = re.search(r'<a href="/player/[^"]*/(\d+)"[^>]*>(.*?)</a>', li)
            if not pm:
                continue
            teams = [htmllib.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                     for t in re.findall(r"(?is)<a href=['\"](?!/player/)[^'\"]*['\"][^>]*>(.*?)</a>", li)]
            form = re.sub(r'(?is)<a href="/player/[^"]*"[^>]*>.*?</a>', "{P}", li)
            form = re.sub(r"(?is)<a [^>]*>.*?</a>", "{T}", form)
            form = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", form))).strip()
            raw = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", li))).strip()
            row = {"txn_date": day, "kind": "other", "name": htmllib.unescape(pm.group(2)).strip(),
                   "realgm_id": pm.group(1), "from_team": None, "to_team": None, "nba_team": None,
                   "detail": None, "raw_text": raw}
            for kind, rx, roles in FORM_RES:
                fm = rx.match(form)
                if not fm:
                    continue
                row["kind"] = kind
                left = list(teams)
                for n, role in enumerate(roles):
                    if fm.groupdict().get("t%d" % n) and left:
                        row[{"to": "to_team", "from": "from_team", "nba": "nba_team"}[role]] = left.pop(0)
                if kind == "draft":
                    row["detail"] = "%s draft %s, round %s, pick %s" % (fm.group("dr"), fm.group("yr"), fm.group("rnd"), fm.group("pick"))
                break
            out.append(row)
    return out


def cmd_transactions(con, season, html=None):
    html = html or fetch(TX_URL)
    if not html:
        print("  transactions: page unavailable — nothing changed")
        return False
    rows = parse_transactions(html)
    if not rows:
        print("  transactions: page fetched but no moves parsed — layout may have changed; nothing written")
        return False
    names = Names(con, season)
    new = 0
    for r in rows:
        tid = hashlib.md5(("%s|%s" % (r["txn_date"], r["raw_text"])).encode()).hexdigest()[:16]
        pid = names.match(r["name"])
        n0 = con.execute("SELECT COUNT(*) FROM gl_transactions WHERE txn_id = ?", [tid]).fetchone()[0]
        con.execute("""INSERT INTO gl_transactions
                         (txn_id, txn_date, season, kind, name, player_id, from_team, to_team, contract,
                          raw_text, seen_at, realgm_id, nba_team, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?, now(), ?,?, 'realgm')
                       ON CONFLICT (txn_id) DO UPDATE SET player_id = COALESCE(gl_transactions.player_id, excluded.player_id),
                         kind = excluded.kind, from_team = excluded.from_team, to_team = excluded.to_team,
                         nba_team = excluded.nba_team, contract = excluded.contract""",
                    [tid, r["txn_date"], season_of(r["txn_date"]), r["kind"], r["name"], pid, r["from_team"],
                     r["to_team"], r["detail"], r["raw_text"], r["realgm_id"], r["nba_team"]])
        new += 0 if n0 else 1
    con.commit()
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    matched = con.execute("SELECT COUNT(*) FILTER (WHERE player_id IS NOT NULL), COUNT(*) FROM gl_transactions "
                          "WHERE season = ?", [season]).fetchone()
    print("  transactions: %d on the page (%d new) — %s; %d of %d this season matched to a player" % (
        len(rows), new, ", ".join("%s %d" % kv for kv in sorted(kinds.items(), key=lambda x: -x[1])),
        matched[0], matched[1]))
    return True


# ---------------------------------------------------------------------------
# the two-way list
# ---------------------------------------------------------------------------

def parse_list(html):
    """Rows of the two-way table: (player, NBA team, G League team)."""
    rows = []
    for tr in re.findall(r"(?is)<tr\b.*?</tr>", html):
        cells = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"(?is)<t[dh]\b.*?>(.*?)</t[dh]>", tr)]
        if cells:
            rows.append(cells)
    if not rows:
        return []
    head = [c.lower() for c in rows[0]]
    def col(*words):
        for i, h in enumerate(head):
            if any(w in h for w in words):
                return i
        return None
    pi = col("player", "name")
    gi = col("g league", "g-league", "affiliate")
    ni = next((i for i, h in enumerate(head) if ("nba" in h or "team" in h) and i not in (pi, gi)), None)
    body = rows[1:] if pi is not None else rows
    pi = 0 if pi is None else pi
    ni = 1 if ni is None else ni
    gi = 2 if gi is None else gi
    out = []
    for r in body:
        if len(r) <= max(pi, ni, gi) or not r[pi]:
            continue
        out.append({"name": r[pi], "nba_team": r[ni], "gl_team": r[gi]})
    return out


def team_maps(con, season):
    nba = {}
    for abbr, name in con.execute("""
            SELECT DISTINCT home_abbr, home_name FROM scheduled_games WHERE season = ?""", [season]).fetchall():
        if name:
            nba[norm(name)] = abbr
            nba[norm(name.split(" ")[-1])] = abbr          # "Celtics"
            nba[norm(" ".join(name.split(" ")[-2:]))] = abbr  # "Trail Blazers"
    gl = {}
    for tid, city, name in con.execute("SELECT team_id, city, name FROM gl_teams WHERE season = ?", [season]).fetchall():
        for k in (" ".join(x for x in (city, name) if x), name or "", city or ""):
            if k.strip():
                gl.setdefault(norm(k), tid)
    return nba, gl


def lookup(m, raw):
    k = norm(raw)
    if k in m:
        return m[k]
    hits = {v for key, v in m.items() if key and (key in k or k in key) and len(key) > 3}
    return hits.pop() if len(hits) == 1 else None


def cmd_list(con, season, html=None):
    html = html or fetch(LIST_URL)
    if not html:
        print("  two-way list: page unavailable — nothing changed")
        return False
    rows = parse_list(html)
    if len(rows) < 30:
        print("  two-way list: only %d rows parsed — page layout may have changed; nothing written" % len(rows))
        return False
    today = datetime.now().date()
    names = Names(con, season)
    nba, gl = team_maps(con, season)
    con.execute("DELETE FROM twoway_list WHERE snap_date = ?", [today])
    for r in rows:
        con.execute("INSERT INTO twoway_list VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [today, season, r["name"], names.match(r["name"]), lookup(nba, r["nba_team"]),
                     r["nba_team"], r["gl_team"], lookup(gl, r["gl_team"])])
    con.commit()
    s = con.execute("""SELECT COUNT(*), COUNT(player_id), COUNT(nba_abbr), COUNT(DISTINCT nba_abbr)
                       FROM twoway_list WHERE snap_date = ?""", [today]).fetchone()
    print("  two-way list: %d players on %d NBA teams; %d matched to a player, %d to an NBA team" % (
        s[0], s[3], s[1], s[2]))
    return True


# ---------------------------------------------------------------------------
# active lists, from NBA's box scores
# ---------------------------------------------------------------------------

def cmd_active(con, season):
    import cdn_feed as C
    todo = con.execute("""
        SELECT nba_game_id, game_date FROM scheduled_games
        WHERE season = ? AND season_type = 'regular' AND status = 'final'
          AND nba_game_id NOT IN (SELECT DISTINCT nba_game_id FROM nba_active)
        ORDER BY game_date""", [season]).fetchall()
    done = 0
    for gid, gdate in todo:
        try:
            box = C.load(gid, "box", fetch=True)
        except Exception as e:
            print("    %s: %s" % (gid, str(e)[:60]))
            continue
        g = (box or {}).get("game") or {}
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side) or {}
            for p in t.get("players") or []:
                status = str(p.get("status") or "").upper()
                con.execute("INSERT INTO nba_active VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                            [gid, str(p.get("personId")), t.get("teamTricode"), gdate, season,
                             status == "ACTIVE", str(p.get("played")) == "1",
                             p.get("notPlayingReason") or p.get("notPlayingDescription")])
        done += 1
    con.commit()
    print("  active lists: %d new games read (%d this season)" % (done, con.execute(
        "SELECT COUNT(DISTINCT nba_game_id) FROM nba_active WHERE season = ?", [season]).fetchone()[0]))


# ---------------------------------------------------------------------------
# the tracker
# ---------------------------------------------------------------------------

MOVEMENT_PATH = "/js/data/playermovement/NBA_Player_Movement.json"
TEAM_IDS = {37: "ATL", 38: "BOS", 39: "CLE", 40: "NOP", 41: "CHI", 42: "DAL", 43: "DEN", 44: "GSW",
            45: "HOU", 46: "LAC", 47: "LAL", 48: "MIA", 49: "MIL", 50: "MIN", 51: "BKN", 52: "NYK",
            53: "ORL", 54: "IND", 55: "PHI", 56: "PHX", 57: "POR", 58: "SAC", 59: "SAS", 60: "OKC",
            61: "TOR", 62: "UTA", 63: "MEM", 64: "WAS", 65: "DET", 66: "CHA"}


def fetch_movement():
    """NBA.com's player-movement feed: through the relay on GitHub, directly elsewhere."""
    from curl_cffi import requests as cr
    relay = os.environ.get("NBA_RELAY_URL")
    if relay and os.environ.get("GITHUB_ACTIONS"):
        url = relay.rstrip("/") + MOVEMENT_PATH
        headers = {"x-relay-token": os.environ.get("NBA_RELAY_TOKEN", "")}
    else:
        url = "https://stats.nba.com" + MOVEMENT_PATH
        headers = {"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com"}
    for attempt in range(3):
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=60)
            if r.status_code == 200:
                return r.json()["NBA_Player_Movement"]["rows"]
            print("    player movement: HTTP %s" % r.status_code)
        except Exception as e:
            print("    player movement: %s" % str(e)[:70])
        time.sleep(5 * (attempt + 1))
    return None


def season_of(d):
    """July onward belongs to the season that starts that fall."""
    y = d.year if d.month >= 7 else d.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def movement_kind(t, desc):
    two = "two-way" in desc.lower()
    if t == "Signing":
        return "twoway_sign" if two else "sign"
    if t == "ContractConverted":
        return "convert_to_twoway" if re.search(r"to a two-way", desc, re.I) else "convert_to_standard"
    return {"Waive": "waive", "AwardOnWaivers": "claim", "Trade": "trade"}.get(t, (t or "other").lower())


def parse_movement(rows):
    out = []
    for r in rows:
        try:
            d = datetime.strptime(str(r["TRANSACTION_DATE"])[:10], "%Y-%m-%d").date()
            pid = str(int(float(r.get("PLAYER_ID") or 0))) or None
            tid = int(float(r.get("TEAM_ID") or 0))
        except Exception:
            continue
        desc = (r.get("TRANSACTION_DESCRIPTION") or "").strip()
        key = hashlib.md5(("%s|%s|%s" % (d, pid, desc)).encode()).hexdigest()[:16]
        out.append({"txn_key": key, "txn_date": d, "season": season_of(d),
                    "kind": movement_kind(r.get("Transaction_Type"), desc), "txn_type": r.get("Transaction_Type"),
                    "description": desc, "person_id": None if pid == "0" else pid,
                    "team_id": tid or None, "team_abbr": TEAM_IDS.get(tid - 1610612700) if tid else None})
    return out


def cmd_movement(con, rows=None, write=True):
    rows = rows if rows is not None else fetch_movement()
    if not rows:
        print("  player movement: feed unavailable — nothing changed")
        return False
    parsed = parse_movement(rows)
    if not write:
        return parsed
    ids = dict(con.execute("SELECT source_id, player_id FROM player_identifiers WHERE source = 'nba'").fetchall())
    before = con.execute("SELECT COUNT(*) FROM nba_movement").fetchone()[0]
    for r in parsed:
        con.execute("""INSERT INTO nba_movement VALUES (?,?,?,?,?,?,?,?,?,?, now())
                       ON CONFLICT (txn_key) DO UPDATE SET player_id = COALESCE(excluded.player_id, nba_movement.player_id)""",
                    [r["txn_key"], r["txn_date"], r["season"], r["kind"], r["txn_type"], r["description"],
                     r["person_id"], ids.get(r["person_id"]), r["team_id"], r["team_abbr"]])
    con.commit()
    s = con.execute("""SELECT COUNT(*), COUNT(player_id), MIN(txn_date) FROM nba_movement""").fetchone()
    print("  player movement: %d rows (%d new) back to %s; %d matched to a player by NBA id" % (
        s[0], s[0] - before, s[2], s[1]))
    return True


def season_bounds(con, season):
    r = con.execute("""SELECT MIN(game_date), MAX(game_date) FROM scheduled_games
                       WHERE season = ? AND season_type = 'regular'""", [season]).fetchone()
    return r[0], r[1]


def limit_for(signed, start, end):
    """50 games, prorated by the share of the regular season left at signing."""
    if not signed or not start or not end or signed <= start:
        return LIMIT
    if signed > end:
        return 0
    total = (end - start).days + 1
    left = (end - signed).days + 1
    return int(round(LIMIT * left / total))


def signing_dates(con, season):
    """player_id -> (date, NBA team) of his latest two-way signing this season,
    from the NBA transaction log."""
    out = {}
    try:
        rows = con.execute("""
            SELECT i.player_id, t.txn_date, tt.team_abbr
            FROM transactions t JOIN transaction_items i USING (transaction_id)
            LEFT JOIN transaction_teams tt ON tt.transaction_id = t.transaction_id
            WHERE t.season = ? AND t.txn_type = 'two_way' AND i.player_id IS NOT NULL
              AND lower(t.raw_text) LIKE '%signed%'
            ORDER BY t.txn_date""", [season]).fetchall()
    except Exception:
        rows = []
    for pid, d, team in rows:
        out[pid] = (d, BBREF.get(team, team), "Basketball Reference")
    # NBA.com's feed, by person id, wins where it has the signing
    try:
        rows = con.execute("""
            SELECT player_id, txn_date, team_abbr FROM nba_movement
            WHERE season = ? AND kind IN ('twoway_sign', 'convert_to_twoway') AND player_id IS NOT NULL
            ORDER BY txn_date""", [season]).fetchall()
    except Exception:
        rows = []
    for pid, d, team in rows:
        out[pid] = (d, team, "NBA.com")
    return out


def build_tracker(con, season):
    snap = con.execute("SELECT MAX(snap_date) FROM twoway_list WHERE season = ?", [season]).fetchone()[0]
    if not snap:
        return [], {}
    start, end = season_bounds(con, season)
    today = datetime.now().date()
    deadline = date(int(season[:4]) + 1, *map(int, DEADLINE.split("-")))
    signed = signing_dates(con, season)
    first_seen = dict(((r[0], r[1]), r[2]) for r in con.execute("""
        SELECT name, nba_abbr, MIN(snap_date) FROM twoway_list WHERE season = ? GROUP BY ALL""", [season]).fetchall())
    nba_ids = dict(con.execute("SELECT player_id, source_id FROM player_identifiers WHERE source='nba'").fetchall())
    where = {pid: ("G League" if kind == "assign" else "NBA", d) for pid, kind, d in con.execute("""
        SELECT player_id, arg_max(kind, txn_date), MAX(txn_date) FROM gl_transactions
        WHERE season = ? AND player_id IS NOT NULL AND kind IN ('assign', 'recall', 'call_up')
        GROUP BY player_id""", [season]).fetchall()}
    rows = []
    for name, pid, abbr, nba_team, gl_team, gl_id in con.execute("""
            SELECT name, player_id, nba_abbr, nba_team, gl_team, gl_team_id
            FROM twoway_list WHERE snap_date = ? ORDER BY nba_abbr, name""", [snap]).fetchall():
        s = signed.get(pid)
        sign_date, sign_src = (s[0], s[2]) if s and (not abbr or s[1] == abbr) else (None, None)
        if not sign_date:
            fs = first_seen.get((name, abbr))
            sign_date, sign_src = (fs, "first seen on the list") if fs and start and fs > start else (None, "before the season")
        limit = limit_for(sign_date, start, end)
        person = nba_ids.get(pid)
        used = None
        if person and abbr:
            used = con.execute("""
                SELECT COUNT(*) FROM nba_active
                WHERE season = ? AND person_id = ? AND team_abbr = ? AND active
                  AND game_date >= ?""", [season, person, abbr, sign_date or start or date(1900, 1, 1)]).fetchone()[0]
        upcoming = [r[0] for r in con.execute("""
            SELECT game_date FROM scheduled_games
            WHERE season = ? AND season_type = 'regular' AND status <> 'final'
              AND (home_abbr = ? OR away_abbr = ?) ORDER BY game_date""", [season, abbr, abbr]).fetchall()] if abbr else []
        left = None if used is None else max(0, limit - used)
        hits = upcoming[left - 1] if left is not None and 0 < left <= len(upcoming) else None
        rows.append({
            "name": name, "player_id": pid, "nba_abbr": abbr, "nba_team": nba_team,
            "gl_team": gl_team, "gl_team_id": gl_id,
            "signed": sign_date, "signed_source": sign_src, "limit": limit, "used": used, "left": left,
            "team_games_left": len(upcoming),
            "limit_date": hits,                      # if active every game from here
            "can_reach_limit": bool(left is not None and left <= len(upcoming)),
            "where": where.get(pid, (None, None))[0], "where_since": where.get(pid, (None, None))[1],
        })
    meta = {"season": season, "snapshot": str(snap), "season_start": str(start), "season_end": str(end),
            "deadline": str(deadline), "days_to_deadline": (deadline - today).days, "limit": LIMIT,
            "teams_with_open_slots": open_slots(con, season, rows),
            "built_at": datetime.utcnow().isoformat() + "Z"}
    return rows, meta


def open_slots(con, season, rows):
    """NBA teams with fewer than three two-way players, and how many are open."""
    held = {}
    for r in rows:
        if r["nba_abbr"]:
            held[r["nba_abbr"]] = held.get(r["nba_abbr"], 0) + 1
    teams = [a for (a,) in con.execute("SELECT DISTINCT home_abbr FROM scheduled_games WHERE season = ? "
                                         "AND season_type = 'regular'", [season]).fetchall()]
    return {a: 3 - held.get(a, 0) for a in sorted(teams) if held.get(a, 0) < 3}


def cmd_export(con, season, s3, bucket):
    import tempfile
    rows, meta = build_tracker(con, season)
    with tempfile.TemporaryDirectory() as tmp:
        def put_sql(sql, key):
            p = os.path.join(tmp, key.replace("/", "_"))
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (sql, p))
            s3.upload_file(p, bucket, "%s/%s" % (PREFIX, key))
        put_sql("SELECT * EXCLUDE (seen_at) FROM gl_transactions WHERE season = '%s' ORDER BY txn_date DESC, raw_text" % season,
                "gleague/transactions/%s.parquet" % season)
        put_sql("SELECT * EXCLUDE (seen_at) FROM gl_transactions ORDER BY txn_date DESC, raw_text",
                "gleague/transactions/all.parquet")
        if rows:
            p = os.path.join(tmp, "tracker.json")
            with open(p, "w") as f:
                json.dump({"meta": meta, "players": rows}, f, default=str)
            s3.upload_file(p, bucket, "%s/twoway/%s.json" % (PREFIX, season),
                           ExtraArgs={"ContentType": "application/json"})
    near = [r for r in rows if r["left"] is not None and r["left"] <= 5 and r["can_reach_limit"]]
    print("  tracker: %d two-way players, %d teams with an open slot, %d within 5 games of the limit, "
          "%d with no active count (unmatched); deadline in %s days" % (
              len(rows), len(meta.get("teams_with_open_slots", [])), len(near),
              sum(1 for r in rows if r["used"] is None), meta.get("days_to_deadline")))


# ---------------------------------------------------------------------------

def cmd_sample():
    h = fetch(TX_URL)
    rows = parse_transactions(h or "")
    print("  transactions page: %d moves parsed" % len(rows))
    from collections import Counter
    print("  by kind:", Counter(r["kind"] for r in rows).most_common())
    seen = set()
    for r in rows:
        if r["kind"] not in seen:
            seen.add(r["kind"])
            print("    %s  %-18s %-24s %s -> %s  nba %s %s" % (r["txn_date"], r["kind"], r["name"], r["from_team"],
                                                        r["to_team"], r["nba_team"], r["detail"] or ""))
    other = [r["raw_text"] for r in rows if r["kind"] == "other"][:6]
    if other:
        print("  not recognised:", *other, sep="\n    ")
    h = fetch(LIST_URL)
    rows = parse_list(h or "")
    print("\n  two-way list: %d rows parsed" % len(rows))
    for r in rows[:8]:
        print("    %-26s %-26s %s" % (r["name"], r["nba_team"], r["gl_team"]))


def run(season):
    import nightly2 as N
    con = db()
    try:
        cmd_transactions(con, season)
        cmd_movement(con)
        cmd_list(con, season)
        cmd_active(con, season)
        env = N.load_env()
        cmd_export(con, season, N.s3c(env), env["R2_BUCKET_NAME"])
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "sample", "movement"])
    ap.add_argument("--season")
    ap.add_argument("--sync", action="store_true")
    a = ap.parse_args()
    if a.cmd == "sample":
        cmd_sample()
        return
    if a.cmd == "movement":
        from collections import Counter
        rows = cmd_movement(None, write=False) or []
        season = a.season or current_season()
        this = [r for r in rows if r["season"] == season]
        print("  %d rows, %s to %s" % (len(rows), min(r["txn_date"] for r in rows), max(r["txn_date"] for r in rows)))
        print("  rows by season:", ", ".join("%s %d" % kv for kv in sorted(Counter(r["season"] for r in rows).items())))
        print("  %s by kind: %s" % (season, Counter(r["kind"] for r in this).most_common()))
        for r in [r for r in this if r["kind"] in ("twoway_sign", "convert_to_twoway", "convert_to_standard")][:8]:
            print("    %s %-20s %-4s %s" % (r["txn_date"], r["kind"], r["team_abbr"], r["description"][:90]))
        return
    season = a.season or current_season()
    if not a.sync:
        run(season)
        return
    import nightly2 as N
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    log = N.Log()
    if not N.acquire_lock(s3, bucket, log):
        sys.exit("  another run holds the lock — try again when it finishes")
    try:
        if not N.pull_db(s3, bucket, log):
            sys.exit("  could not pull the database")
        run(season)
        N.push_db(s3, bucket, log)
    finally:
        N.release_lock(s3, bucket)


if __name__ == "__main__":
    main()
