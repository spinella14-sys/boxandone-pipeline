#!/usr/bin/env python3
"""
seed_bbref.py — populate the Box and One registry from Basketball Reference's
all-time player index.

Three phases, run separately so a parse bug never costs you a re-fetch:

    python3 seed_bbref.py fetch          # 26 requests, ~3 min, saves raw HTML
    python3 seed_bbref.py parse          # offline, reads saved HTML
    python3 seed_bbref.py load           # offline, writes to DuckDB

    python3 seed_bbref.py all            # all three

Why phases matter: BBRef rate-limits hard. Fetched HTML is cached to disk and
never re-requested. You can rerun parse and load as many times as you like.

Seeding from a single authoritative source means NO matching is required here.
Every BBRef ID is by definition a distinct player, so each gets its own
internal ID. Matching only becomes necessary when a second source arrives.

Requires: requests, beautifulsoup4
    pip3 install requests beautifulsoup4
"""

import argparse
import csv
import os
import re
import string
import sys
import time
from datetime import datetime

BASE = "https://www.basketball-reference.com"
HOME = os.path.expanduser("~/boxandone")
RAW_DIR = os.path.join(HOME, "raw", "bbref_index")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
PARSED_CSV = os.path.join(HOME, "raw", "bbref_players_parsed.csv")
HINTS_CSV = os.path.join(HOME, "raw", "bbref_position_hints.csv")

REQUEST_DELAY = 5.0          # BBRef floor is ~4s. Do not lower this.
MIN_YEAR_DEFAULT = 1980      # three-point era

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# PHASE 1 — fetch
# ---------------------------------------------------------------------------

def fetch(force=False):
    import requests

    os.makedirs(RAW_DIR, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    fetched = skipped = 0
    for letter in string.ascii_lowercase:
        out = os.path.join(RAW_DIR, f"players_{letter}.html")

        if os.path.exists(out) and not force:
            print(f"  [{letter}] cached, skipping")
            skipped += 1
            continue

        url = f"{BASE}/players/{letter}/"
        for attempt in range(4):
            try:
                r = session.get(url, timeout=30)
            except Exception as e:
                print(f"  [{letter}] network error: {e}")
                time.sleep(10 * (attempt + 1))
                continue

            if r.status_code == 200:
                r.encoding = "utf-8"
                with open(out, "w", encoding="utf-8") as f:
                    f.write(r.text)
                print(f"  [{letter}] ok  ({len(r.text):,} bytes)")
                fetched += 1
                break
            elif r.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  [{letter}] 429 rate limited, waiting {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                print(f"  [{letter}] 404 (no players this letter)")
                break
            else:
                print(f"  [{letter}] HTTP {r.status_code}, retrying")
                time.sleep(15)
        else:
            print(f"  [{letter}] FAILED after retries — rerun 'fetch' later")

        time.sleep(REQUEST_DELAY)

    print(f"\nfetch complete: {fetched} downloaded, {skipped} already cached")


# ---------------------------------------------------------------------------
# PHASE 2 — parse
# ---------------------------------------------------------------------------

def _cell(row, stat):
    el = row.find(attrs={"data-stat": stat})
    return el.get_text(strip=True) if el else ""


def _height_to_inches(h):
    m = re.match(r"^(\d+)-(\d+)$", (h or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2))


def _parse_birthdate(row):
    """BBRef puts a csk='1995-02-19' attribute on the birth date cell."""
    el = row.find(attrs={"data-stat": "birth_date"})
    if not el:
        return None
    csk = el.get("csk")
    if csk and re.match(r"^\d{4}-\d{2}-\d{2}$", csk):
        return csk
    txt = el.get_text(strip=True)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse(min_year=MIN_YEAR_DEFAULT):
    from bs4 import BeautifulSoup

    rows_out = []
    files = sorted(f for f in os.listdir(RAW_DIR) if f.endswith(".html")) \
        if os.path.isdir(RAW_DIR) else []

    if not files:
        sys.exit(f"No cached HTML in {RAW_DIR}. Run 'fetch' first.")

    for fn in files:
        with open(os.path.join(RAW_DIR, fn), encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        table = soup.find("table", id="players")
        if not table:
            print(f"  {fn}: no players table found")
            continue

        body = table.find("tbody") or table
        count = 0
        for row in body.find_all("tr"):
            if row.get("class") and "thead" in row.get("class"):
                continue

            name_cell = row.find(attrs={"data-stat": "player"})
            if not name_cell:
                continue
            link = name_cell.find("a")
            if not link or not link.get("href"):
                continue

            m = re.search(r"/players/[a-z]/([^.]+)\.html", link["href"])
            if not m:
                continue
            bbref_id = m.group(1)
            name = link.get_text(strip=True)

            # BBRef marks Hall of Famers with an asterisk
            name = name.replace("*", "").strip()

            try:
                year_min = int(_cell(row, "year_min") or 0)
                year_max = int(_cell(row, "year_max") or 0)
            except ValueError:
                year_min = year_max = 0

            # BBRef "year_max" is the season END year: 1980 == 1979-80.
            if year_max < min_year:
                continue

            rows_out.append({
                "bbref_id":   bbref_id,
                "full_name":  name,
                "year_min":   year_min,
                "year_max":   year_max,
                "bbref_pos":  _cell(row, "pos"),
                "height_in":  _height_to_inches(_cell(row, "height")),
                "weight_lb":  _cell(row, "weight") or None,
                "birthdate":  _parse_birthdate(row),
                "college":    _cell(row, "colleges"),
                "source_url": BASE + link["href"],
            })
            count += 1
        print(f"  {fn}: {count} players kept")

    os.makedirs(os.path.dirname(PARSED_CSV), exist_ok=True)
    fields = ["bbref_id", "full_name", "year_min", "year_max", "bbref_pos",
              "height_in", "weight_lb", "birthdate", "college", "source_url"]
    with open(PARSED_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    # BBRef positions are a HINT ONLY. They never touch players.position.
    with open(HINTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bbref_id", "full_name", "bbref_pos", "height_in", "year_max"])
        for r in rows_out:
            w.writerow([r["bbref_id"], r["full_name"], r["bbref_pos"],
                        r["height_in"], r["year_max"]])

    no_bday = sum(1 for r in rows_out if not r["birthdate"])
    print(f"\nparsed {len(rows_out):,} players (active {min_year}+)")
    print(f"  missing birthdate: {no_bday:,}")
    print(f"  -> {PARSED_CSV}")
    print(f"  -> {HINTS_CSV}  (position hints, NOT imported)")


# ---------------------------------------------------------------------------
# PHASE 3 — load
# ---------------------------------------------------------------------------

def load():
    import duckdb
    sys.path.insert(0, HOME)
    from identity import normalize_name

    if not os.path.exists(PARSED_CSV):
        sys.exit(f"{PARSED_CSV} not found. Run 'parse' first.")

    with open(PARSED_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    con = duckdb.connect(DB_PATH)

    existing = {r[0] for r in con.execute(
        "SELECT source_id FROM player_identifiers WHERE source='bbref'"
    ).fetchall()}

    seq = con.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(player_id,2) AS INTEGER)),0) FROM players"
    ).fetchone()[0]

    inserted = 0
    name_index = {}

    for r in rows:
        if r["bbref_id"] in existing:
            continue

        seq += 1
        pid = f"P{seq:08d}"
        n = normalize_name(r["full_name"])

        bd = r["birthdate"] or None
        status = "unconfirmed" if bd else "missing"

        con.execute("""
            INSERT INTO players
              (player_id, full_name, display_name, first_name, last_name,
               name_normalized, birthdate, birthdate_status, height_in, weight_lb,
               college, status, provenance, created_by, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'scraped','seed_bbref',?)
        """, [
            pid, r["full_name"], r["full_name"], n["first"], n["last"],
            n["name_key"], bd, status,
            float(r["height_in"]) if r["height_in"] else None,
            float(r["weight_lb"]) if r["weight_lb"] else None,
            r["college"] or None,
            "active" if int(r["year_max"] or 0) >= 2025 else "retired",
            f"bbref {r['year_min']}-{r['year_max']}",
        ])

        con.execute("""
            INSERT INTO player_identifiers
              (source, source_id, player_id, source_url, is_primary, linked_by, provenance)
            VALUES ('bbref',?,?,?,TRUE,'seed_bbref','scraped')
        """, [r["bbref_id"], pid, r["source_url"]])

        con.execute("""
            INSERT INTO player_aliases (player_id, alias, alias_normalized, source)
            VALUES (?,?,?,'bbref')
        """, [pid, r["full_name"], n["name_key"]])

        name_index.setdefault(n["name_key"], []).append((pid, r["full_name"]))
        inserted += 1

    con.commit()

    collisions = {k: v for k, v in name_index.items() if len(v) > 1}
    total = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]

    print(f"\ninserted {inserted:,} players  (registry now {total:,})")
    if collisions:
        print(f"\n{len(collisions)} identical-name groups — NOT merged, review these:")
        for k, v in sorted(collisions.items())[:25]:
            print(f"  {k}: {', '.join(p for p, _ in v)}")
        if len(collisions) > 25:
            print(f"  ... and {len(collisions)-25} more")

    print("\nnext: assign positions. Query:  SELECT * FROM position_unset LIMIT 50")
    con.close()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["fetch", "parse", "load", "all"])
    ap.add_argument("--min-year", type=int, default=MIN_YEAR_DEFAULT)
    ap.add_argument("--force", action="store_true", help="re-fetch cached HTML")
    a = ap.parse_args()

    if a.phase in ("fetch", "all"):
        print("PHASE 1 — fetch")
        fetch(force=a.force)
    if a.phase in ("parse", "all"):
        print("\nPHASE 2 — parse")
        parse(min_year=a.min_year)
    if a.phase in ("load", "all"):
        print("\nPHASE 3 — load")
        load()
