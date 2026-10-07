#!/usr/bin/env python3
"""
ingest_transactions.py — every NBA transaction from 1997 on.

Basketball Reference writes transactions as prose, which usually means a
fragile parse. These pages are better than that: each entry is its own <p>
under a dated <li>, team links carry data-attr-from and data-attr-to so the
direction of a move is explicit, and player links carry Basketball Reference
ids — which are already in the registry, so there is no name matching to get
wrong.

The raw sentence is stored verbatim on every row regardless. A salary system
cannot be built on a lossy parse, and keeping the text means the parser can be
improved later without re-fetching thirty seasons.

Three tables, because a transaction is an event with participants:

  transactions        one row per event: date, type, the sentence
  transaction_teams   which franchises, and on which side
  transaction_items   one row per thing that moved — a player, a pick, cash

Picks keep their year, round and originating team as separate fields. That is
what makes a transaction tree possible later: a 2027 first from Milwaukee
traded in 2025 has to be matchable to the selection actually made in 2027, and
that only works if the pick is structured rather than left in the prose.

    python3 ingest_transactions.py fetch              # 1997 on, cached to disk
    python3 ingest_transactions.py parse --season 2024-25   # inspect, write nothing
    python3 ingest_transactions.py build
    python3 ingest_transactions.py roster --team BOS --date 2025-02-01
"""

import argparse
import gzip
import os
import re
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "raw", "transactions")
DELAY = 4.0
FIRST_YEAR = 1997          # season end year

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")}

DDL = [
"""CREATE TABLE IF NOT EXISTS transactions (
    transaction_id VARCHAR PRIMARY KEY,
    txn_date       DATE NOT NULL,
    season         VARCHAR NOT NULL,
    txn_type       VARCHAR NOT NULL,
    raw_text       VARCHAR NOT NULL,
    source         VARCHAR NOT NULL DEFAULT 'bbref',
    source_url     VARCHAR,
    ingested_at    TIMESTAMP NOT NULL DEFAULT current_timestamp
)""",
"""CREATE TABLE IF NOT EXISTS transaction_teams (
    transaction_id VARCHAR NOT NULL,
    team_abbr      VARCHAR NOT NULL,
    direction      VARCHAR NOT NULL,
    PRIMARY KEY (transaction_id, team_abbr, direction)
)""",
"""CREATE TABLE IF NOT EXISTS transaction_items (
    item_id        BIGINT PRIMARY KEY,
    transaction_id VARCHAR NOT NULL,
    item_type      VARCHAR NOT NULL,
    player_id      VARCHAR,
    bbref_id       VARCHAR,
    raw_name       VARCHAR,
    from_team      VARCHAR,
    to_team        VARCHAR,
    pick_year      SMALLINT,
    pick_round     SMALLINT,
    pick_original  VARCHAR,
    detail         VARCHAR
)""",
]

# ordered: the first pattern that matches wins, so "traded" beats "signed"
# in a sentence that happens to contain both
TYPES = [
    ("trade",       r"\btraded\b|\bin a \d-team trade\b"),
    ("draft",       r"\bdrafted\b|\bselected\b.*\bdraft\b"),
    ("claim",       r"\bclaimed\b"),
    ("waive",       r"\bwaived\b|\breleased\b"),
    ("extension",   r"\bcontract extension\b|\bextension\b"),
    ("two_way",     r"\btwo-way\b"),
    ("exhibit10",   r"\bExhibit 10\b"),
    ("option",      r"\boption\b"),
    ("sign",        r"\bsigned\b|\bre-signed\b"),
    ("convert",     r"\bconverted\b"),
    ("assign",      r"\bassigned\b|\brecalled\b"),
    ("retire",      r"\bretired\b"),
    ("suspend",     r"\bsuspended\b"),
    ("hire",        r"\bhired\b|\bnamed\b.*\bcoach\b"),
    ("fire",        r"\bfired\b|\brelieved\b"),
]


def classify(text):
    low = text.lower()
    for name, pat in TYPES:
        if re.search(pat, low):
            return name
    return "other"


def season_label(end_year):
    return "%d-%s" % (end_year - 1, str(end_year)[2:])


def fetch_season(end_year, force=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "NBA_%d.html.gz" % end_year)
    if os.path.exists(path) and not force:
        return gzip.open(path, "rt", encoding="utf-8").read(), True
    from curl_cffi import requests as cr
    url = ("https://www.basketball-reference.com/leagues/NBA_%d_transactions.html"
           % end_year)
    r = cr.get(url, headers=UA, impersonate="chrome", timeout=60)
    time.sleep(DELAY)
    if r.status_code != 200:
        return None, False
    gzip.open(path, "wt", encoding="utf-8").write(r.text)
    return r.text, False


LI_RE = re.compile(r"<li>\s*<span>([A-Z][a-z]+ \d{1,2}, \d{4})</span>(.*?)</li>", re.S)
P_RE = re.compile(r"<p>(.*?)</p>", re.S)
TEAM_RE = re.compile(r'data-attr-(from|to)="([A-Z]{3})"')
PLAYER_RE = re.compile(r'<a href="/players/[a-z]/([a-z0-9]+)\.html">([^<]+)</a>')
TAG_RE = re.compile(r"<[^>]+>")

# "a 2027 1st round draft pick", "the Bucks' 2026 second round pick"
PICK_RE = re.compile(
    r"(?:(\d{4})\s+)?(1st|2nd|first|second)[- ]round\s+(?:draft\s+)?pick", re.I)

# "(Melvin Ajinça was later selected)" — the player a pick eventually became.
# Not a move: nobody changed teams. It is the single most useful thing on these
# pages for tracing an asset forward, because it links a pick traded years
# earlier to the person it turned into.
CONVEY_RE = re.compile(
    r"\(([^)]*?)\s+(?:was|were)\s+later\s+(?:selected|drafted)[^)]*\)", re.I)


def strip_tags(s):
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def parse_entry(html, date, season, url, seq):
    """One <p>: the sentence, the teams and which side each is on, the players
    with their Basketball Reference ids, and any picks."""
    text = strip_tags(html)
    if not text:
        return None

    teams = []
    for direction, abbr in TEAM_RE.findall(html):
        teams.append((abbr, direction))

    players = PLAYER_RE.findall(html)

    tid = "T%s_%03d" % (date.strftime("%Y%m%d"), seq)
    txn = {
        "transaction_id": tid,
        "txn_date": date,
        "season": season,
        "txn_type": classify(text),
        "raw_text": text,
        "source_url": url,
    }

    # Direction has to be read in both directions. "The Hornets traded
    # Washington to the Mavericks" puts the sending team before the player and
    # the receiving team after, while "The Blazers signed Muoka" puts the only
    # team before. So: the sending team is the nearest `from` behind, and the
    # receiving team is the nearest `to` ahead — falling back to one behind
    # when the sentence has no team after the player at all.
    marks = [(m.start(), m.group(2), m.group(1)) for m in TEAM_RE.finditer(html)]

    # "the Knicks traded George to the Wizards FOR Jones" — everything after
    # the "for" is moving the other way. Without this, half of every two-team
    # trade is recorded travelling in the wrong direction.
    swap_at = None
    mfor = re.search(r"\bfor\b", strip_tags(html), re.I)
    if mfor and classify(text) == "trade":
        # locate the same word in the markup, which carries tags the text does not
        mh = re.search(r"\bfor\b", html, re.I)
        swap_at = mh.start() if mh else None

    # anything inside a conveyance note is not a participant in this move
    convey_spans = [(m.start(), m.end()) for m in CONVEY_RE.finditer(html)]

    def in_conveyance(pos):
        return any(a <= pos <= b for a, b in convey_spans)

    def teams_at(pos):
        before_from = [a for p0, a, d in marks if d == "from" and p0 < pos]
        before_to = [a for p0, a, d in marks if d == "to" and p0 < pos]
        after_to = [a for p0, a, d in marks if d == "to" and p0 > pos]
        after_from = [a for p0, a, d in marks if d == "from" and p0 > pos]
        frm = before_from[-1] if before_from else (after_from[0] if after_from else None)
        to = after_to[0] if after_to else (before_to[-1] if before_to else None)
        if swap_at is not None and pos > swap_at:
            frm, to = to, frm
        return frm, to

    items = []
    for m in PLAYER_RE.finditer(html):
        if in_conveyance(m.start()):
            # the person a traded pick became, recorded as the link between
            # them rather than as a transfer that never happened
            items.append({
                "item_type": "pick_became",
                "bbref_id": m.group(1),
                "raw_name": m.group(2),
                "detail": strip_tags(html[max(0, m.start() - 90):m.end() + 40]),
            })
            continue
        frm, to = teams_at(m.start())
        items.append({
            "item_type": "player",
            "bbref_id": m.group(1),
            "raw_name": m.group(2),
            "from_team": frm,
            "to_team": to,
        })

    # picks are plain text in the markup, so they are located the same way
    for m in PICK_RE.finditer(html):
        frm, to = teams_at(m.start())
        yr, rnd = m.group(1), m.group(2).lower()
        items.append({
            "item_type": "pick",
            "pick_year": int(yr) if yr else None,
            "pick_round": 1 if rnd in ("1st", "first") else 2,
            "from_team": frm,
            "to_team": to,
            "detail": strip_tags(html[max(0, m.start() - 70):m.end() + 30]),
        })

    cash = re.search(r"\bcash\b", html, re.I)
    if cash:
        frm, to = teams_at(cash.start())
        items.append({"item_type": "cash", "from_team": frm, "to_team": to,
                      "detail": "cash considerations"})

    return txn, teams, items


def parse_season(html, end_year, url):
    season = season_label(end_year)
    out = []
    for date_str, block in LI_RE.findall(html):
        try:
            d = datetime.strptime(date_str, "%B %d, %Y").date()
        except ValueError:
            continue
        for i, p in enumerate(P_RE.findall(block)):
            parsed = parse_entry(p, d, season, url, i)
            if parsed:
                out.append(parsed)
    return out


def cmd_fetch(force):
    end = datetime.now().year + (1 if datetime.now().month >= 10 else 0)
    years = list(range(FIRST_YEAR, end + 1))
    print("  %d seasons, ~%.0f min" % (len(years), len(years) * DELAY / 60))
    got = 0
    for y in years:
        html, cached = fetch_season(y, force)
        if html:
            n = len(LI_RE.findall(html))
            got += 1
            print("  %-9s %s  %4d dated blocks"
                  % (season_label(y), "cached " if cached else "fetched", n))
        else:
            print("  %-9s unavailable" % season_label(y))
    print("\n  %d of %d seasons on disk" % (got, len(years)))


def cmd_parse(season_arg):
    end = int(season_arg.split("-")[0]) + 1 if season_arg else datetime.now().year
    html, _ = fetch_season(end)
    if not html:
        sys.exit("  no cached page for that season — run fetch first")
    rows = parse_season(html, end, "")
    print("  %s: %d transactions\n" % (season_label(end), len(rows)))

    types = {}
    for txn, _, _ in rows:
        types[txn["txn_type"]] = types.get(txn["txn_type"], 0) + 1
    for k, v in sorted(types.items(), key=lambda x: -x[1]):
        print("    %-12s %5d" % (k, v))

    print("\n  a trade, parsed:")
    for txn, teams, items in rows:
        if txn["txn_type"] == "trade" and len(items) >= 4:
            print("    %s" % txn["raw_text"][:300])
            print("    teams: %s" % teams)
            for it in items:
                if it["item_type"] == "player":
                    print("      %-22s %-4s -> %-4s"
                          % (it["raw_name"][:22], it["from_team"] or "?",
                             it["to_team"] or "?"))
                else:
                    print("      %-22s %s %s"
                          % (it["item_type"], it.get("pick_year") or "",
                             it.get("pick_round") or ""))
            break

    unmatched = sum(1 for _, _, items in rows
                    for it in items if it["item_type"] == "player"
                    and not it["from_team"] and not it["to_team"])
    print("\n  player items with no team attached: %d" % unmatched)


def cmd_build(con):
    for d in DDL:
        con.execute(d)
    end = datetime.now().year + (1 if datetime.now().month >= 10 else 0)
    bbref = dict(con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='bbref'"
    ).fetchall())

    item_id = con.execute(
        "SELECT COALESCE(MAX(item_id), 0) FROM transaction_items").fetchone()[0]
    total_t = total_i = unknown = 0

    for y in range(FIRST_YEAR, end + 1):
        html, _ = fetch_season(y)
        if not html:
            continue
        url = ("https://www.basketball-reference.com/leagues/NBA_%d_transactions.html" % y)
        rows = parse_season(html, y, url)
        if not rows:
            continue

        con.executemany("""
            INSERT INTO transactions (transaction_id, txn_date, season, txn_type,
                   raw_text, source, source_url)
            VALUES (?, ?, ?, ?, ?, 'bbref', ?)
            ON CONFLICT (transaction_id) DO UPDATE SET raw_text = excluded.raw_text""",
            [(t["transaction_id"], t["txn_date"], t["season"], t["txn_type"],
              t["raw_text"], t["source_url"]) for t, _, _ in rows])

        tteams = []
        for t, teams, _ in rows:
            for abbr, direction in set(teams):
                tteams.append((t["transaction_id"], abbr, direction))
        if tteams:
            con.executemany("""
                INSERT INTO transaction_teams VALUES (?, ?, ?)
                ON CONFLICT DO NOTHING""", tteams)

        items = []
        for t, _, its in rows:
            for it in its:
                item_id += 1
                pid = bbref.get(it.get("bbref_id")) if it.get("bbref_id") else None
                if it.get("bbref_id") and not pid:
                    unknown += 1
                items.append((item_id, t["transaction_id"], it["item_type"], pid,
                              it.get("bbref_id"), it.get("raw_name"),
                              it.get("from_team"), it.get("to_team"),
                              it.get("pick_year"), it.get("pick_round"),
                              it.get("pick_original"), it.get("detail")))
        if items:
            con.executemany("""
                INSERT INTO transaction_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT DO NOTHING""", items)

        total_t += len(rows)
        total_i += len(items)
        print("  %-9s %5d transactions  %5d items" % (season_label(y), len(rows), len(items)))

    con.commit()
    print("\n  %d transactions, %d items" % (total_t, total_i))
    if unknown:
        print("  %d player references not in the registry — mostly people who" % unknown)
        print("  were signed and waived without ever playing a game.")


def cmd_roster(con, team, date):
    """A roster on a date, reconstructed from the transaction log."""
    rows = con.execute("""
        SELECT i.raw_name, t.txn_date, t.txn_type, t.raw_text
        FROM transaction_items i
        JOIN transactions t ON t.transaction_id = i.transaction_id
        WHERE i.item_type = 'player'
          AND (i.to_team = ? OR i.from_team = ?)
          AND t.txn_date <= ?
        ORDER BY t.txn_date""", [team, team, date]).fetchall()
    on = {}
    for name, d, typ, _ in rows:
        on[name] = (d, typ)
    print("  %s as of %s, from the transaction log\n" % (team, date))
    for name, (d, typ) in sorted(on.items()):
        print("    %-26s %s  %s" % (name[:26], d, typ))
    print("\n  %d names. This counts anyone whose last move involving %s was"
          % (len(on), team))
    print("  on or before that date — a first approximation, not a verified roster.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "parse", "build", "roster"])
    ap.add_argument("--season")
    ap.add_argument("--team")
    ap.add_argument("--date")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "fetch":
        cmd_fetch(a.force)
    elif a.cmd == "parse":
        cmd_parse(a.season)
    else:
        import duckdb
        con = duckdb.connect(DB_PATH, read_only=(a.cmd == "roster"))
        if a.cmd == "build":
            cmd_build(con)
        else:
            cmd_roster(con, a.team or "BOS", a.date or "2025-02-01")
        con.close()
