#!/usr/bin/env python3
"""
patch_twoway_realgm.py — run once.

G League transactions now come from RealGM's league log
(basketball.realgm.com/gleague/transactions/league): gleague.nba.com stopped
publishing its own after 2023-24. Every move on the page is a sentence with
the player and the teams as links, and every sentence fits one of a small set
of forms, each filed as its own kind:

  assign, recall, call_up, return_from_callup  NBA <-> G League movement
  twoway_sign, twoway_end                      two-way contracts
  rights_trade      a G League team acquires a player's returning rights in a trade
  gl_trade          a G League player acquired in a trade
  allocate, deallocate, returned               G League roster rights and returns
  pool_claim, gl_waive, gl_claim, gl_terminate, deactivate, tryout
  draft             the G League Annual and International drafts (round, pick)

RealGM's player id is kept on every row, so a later review can tie it to the
player once and for all. The player is matched by name only where exactly one
person fits, as before.

The tracker also gains "where": NBA or G League, from the player's latest
assignment, recall or call-up, and the date it happened.

    python3 patch_twoway_realgm.py
    python3 ingest_twoway.py sample          # what the page parses into, nothing written
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_twoway.py")
src = open(PATH).read()
if "realgm" in src:
    print("  ingest_twoway.py already patched")
    sys.exit()
if "nba_movement" not in src:
    sys.exit("  run patch_twoway_movement.py first")

PARSER = r'''REALGM = "https://basketball.realgm.com"
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}

# each sentence with its links replaced by {P} (the player) and {T} (a team);
# the teams fill from_team / to_team in the order named below
FORMS = [
    ("rights_trade", r"\{P\} was inactive and had his rights acquired by the \{T\} from the ?\{?T?\}? ?in a trade", ("to", "from")),
    ("gl_trade", r"\{P\} was acquired by the \{T\} from the ?\{?T?\}? ?in a trade", ("to", "from")),
    ("return_from_callup", r"\{P\} returned to the \{T\} after an NBA call up", ("to",)),
    ("returned", r"\{P\} returned to the \{T\}", ("to",)),
    ("gl_waive", r"The \{T\} placed the contract of \{P\} on waivers", ("from",)),
    ("twoway_sign", r"\{P\} signed a two-way contract with the \{T\} and \{T\}", ("nba", "to")),
    ("allocate", r"\{P\}, whose NBA Rights is held by the ?\{?T?\}?,? was allocated to the \{T\}", ("to",)),
    ("allocate", r"\{P\} was allocated to the \{T\}(?: from the \{T\})?", ("to", "nba")),
    ("deallocate", r"\{P\} was deallocated from the \{T\}", ("from",)),
    ("tryout", r"\{P\} tried out with the \{T\}", ("to",)),
    ("draft", r"The ?\{?T?\}? ?selected \{P\} in Round (\d+) with Pick (\d+) in the (\d+) (Annual|International) G League Draft", ("to",)),
    ("assign", r"The \{T\} assigned \{P\} to the \{T\}", ("nba", "to")),
    ("recall", r"The \{T\} recalled \{P\} from the \{T\}", ("nba", "from")),
    ("call_up", r"The \{T\} called up \{P\} from the \{T\}", ("nba", "from")),
    ("twoway_end", r"The \{T\} ended the two-way contract of \{P\}", ("nba",)),
    ("gl_terminate", r"The G League terminated the contract of \{P\} from the ?\{?T?\}?", ("from",)),
    ("pool_claim", r"The \{T\} claimed \{P\} from the player pool", ("to",)),
    ("gl_claim", r"The \{T\} made a successful waiver claim for the contract of \{P\}", ("to",)),
    ("deactivate", r"The \{T\} deactivated \{P\}", ("from",)),
]


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
            for kind, pat, roles in FORMS:
                fm = re.match(pat, form)
                if not fm:
                    continue
                row["kind"] = kind
                for role, team in zip(roles, teams):
                    row[{"to": "to_team", "from": "from_team", "nba": "nba_team"}[role]] = team
                if kind == "draft":
                    row["detail"] = "%s draft %s, round %s, pick %s" % (fm.group(4), fm.group(3), fm.group(1), fm.group(2))
                break
            out.append(row)
    return out


'''

CMD = r'''def cmd_transactions(con, season, html=None):
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
                       ON CONFLICT (txn_id) DO UPDATE SET player_id = COALESCE(gl_transactions.player_id, excluded.player_id)""",
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


'''


def between(s, start, end, new):
    i, j = s.find(start), s.find(end, s.find(start) + 1)
    if i < 0 or j < 0 or s.count(start) != 1:
        sys.exit("  ingest_twoway.py not as expected (%r) — nothing written" % start)
    return s[:i] + new + s[j:]


# the old gleague.nba.com parser and its patterns give way to RealGM's
src = between(src, 'DATE_RE = re.compile(', "def cmd_transactions(", PARSER)
src = between(src, "def cmd_transactions(", "# ------", CMD)

PAIRS = [
    ('''TX_URL = "https://gleague.nba.com/transactions"''',
     '''TX_URL = "https://basketball.realgm.com/gleague/transactions/league"   # gleague.nba.com stopped after 2023-24'''),
    # the new columns, on databases made before this patch
    ('''def db():
    import duckdb
    con = duckdb.connect(os.path.join(DATA, "boxandone.duckdb"))
    for s in DDL.split(";"):
        if s.strip():
            con.execute(s)
    return con''',
     '''def db():
    import duckdb
    con = duckdb.connect(os.path.join(DATA, "boxandone.duckdb"))
    for s in DDL.split(";"):
        if s.strip():
            con.execute(s)
    for col in ("realgm_id VARCHAR", "nba_team VARCHAR", "source VARCHAR"):
        con.execute("ALTER TABLE gl_transactions ADD COLUMN IF NOT EXISTS " + col)
    return con'''),
    # the tracker: where each player is now
    ('''    nba_ids = dict(con.execute("SELECT player_id, source_id FROM player_identifiers WHERE source='nba'").fetchall())''',
     '''    nba_ids = dict(con.execute("SELECT player_id, source_id FROM player_identifiers WHERE source='nba'").fetchall())
    where = {pid: ("G League" if kind == "assign" else "NBA", d) for pid, kind, d in con.execute("""
        SELECT player_id, arg_max(kind, txn_date), MAX(txn_date) FROM gl_transactions
        WHERE season = ? AND player_id IS NOT NULL AND kind IN ('assign', 'recall', 'call_up')
        GROUP BY player_id""", [season]).fetchall()}'''),
    ('''            "can_reach_limit": bool(left is not None and left <= len(upcoming)),''',
     '''            "can_reach_limit": bool(left is not None and left <= len(upcoming)),
            "where": where.get(pid, (None, None))[0], "where_since": where.get(pid, (None, None))[1],'''),
    ('''    for r in rows[:12]:
        print("    %s  %-10s %-24s %s -> %s %s" % (r["txn_date"], r["kind"], r["name"], r["from_team"],
                                                  r["to_team"], r["contract"] or ""))''',
     '''    from collections import Counter
    print("  by kind:", Counter(r["kind"] for r in rows).most_common())
    seen = set()
    for r in rows:
        if r["kind"] not in seen:
            seen.add(r["kind"])
            print("    %s  %-18s %-24s %s -> %s  nba %s %s" % (r["txn_date"], r["kind"], r["name"], r["from_team"],
                                                        r["to_team"], r["nba_team"], r["detail"] or ""))'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  ingest_twoway.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  ingest_twoway.py patched: G League transactions from RealGM; tracker shows where each player is")
