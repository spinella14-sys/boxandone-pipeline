#!/usr/bin/env python3
"""
ingest_pbp.py — NBA.com play-by-play, one season at a time.

Deliberately season-scoped. A 38,021-game run invites a mid-scrape block and
ties up the machine for a day. One season is ~1,320 games, ~55 minutes, and
leaves the data testable between runs.

    python3 ingest_pbp.py fetch  --season 2025-26 [--limit 50]
    python3 ingest_pbp.py parse  --season 2025-26
    python3 ingest_pbp.py fields --season 2025-26     # inspect raw JSON keys
    python3 ingest_pbp.py status

stats.nba.com fingerprints the TLS handshake — plain requests and curl both
time out silently while a browser works. curl_cffi with Chrome impersonation
is required, not optional.

personId is stored RAW as nba_person_id and resolved to our player_id only
where a bridge row exists. The player bridge has only been run for 2025-26,
so older seasons will land unresolved and can be backfilled later without
re-fetching anything.

Requires: curl_cffi
"""

import argparse
import gzip
import json
import os
import random
import re
import sys
import time

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
PBP_DIR = os.path.join(HOME, "raw", "pbp")

DELAY = 2.5
JITTER = 1.0
IMPERSONATE = "chrome"
HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Accept": "application/json, text/plain, */*",
}


def db():
    import duckdb
    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pbp_log (
            game_id        VARCHAR PRIMARY KEY,
            nba_game_id    VARCHAR,
            season         VARCHAR,
            game_date      DATE,
            fetched        BOOLEAN DEFAULT FALSE,
            parsed         BOOLEAN DEFAULT FALSE,
            events         INTEGER,
            fetch_error    VARCHAR,
            parse_error    VARCHAR,
            updated_at     TIMESTAMP DEFAULT now()
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS play_by_play (
            game_id          VARCHAR NOT NULL,
            action_id        BIGINT  NOT NULL,   -- unique within game
            event_group      INTEGER,            -- NBA actionNumber;
                                                 -- links cause+effect
                                                 -- (steal<-turnover,
                                                 --  block<-miss,
                                                 --  assist<-made shot)
            period           SMALLINT,
            clock_raw        VARCHAR,
            seconds_left     DOUBLE,       -- in the period
            elapsed_seconds  DOUBLE,       -- since tipoff

            nba_person_id    BIGINT,       -- always stored
            player_id        VARCHAR,      -- resolved where a bridge row exists
            player_name      VARCHAR,
            team_id          BIGINT,
            team_abbr        VARCHAR,

            action_type      VARCHAR,
            sub_type         VARCHAR,
            description      VARCHAR,

            shot_result      VARCHAR,
            shot_value       SMALLINT,     -- 2 or 3, straight from NBA
            location         VARCHAR,      -- 'h' / 'v'
            shot_distance    DOUBLE,
            x_legacy         INTEGER,
            y_legacy         INTEGER,
            is_field_goal    BOOLEAN,
            points_total     INTEGER,

            score_home       INTEGER,
            score_away       INTEGER,

            source           VARCHAR NOT NULL DEFAULT 'nba',
            provenance       VARCHAR NOT NULL DEFAULT 'scraped',
            PRIMARY KEY (game_id, action_id)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_pbp_game ON play_by_play(game_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pbp_group ON play_by_play(game_id, event_group)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pbp_player ON play_by_play(player_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pbp_nbaid ON play_by_play(nba_person_id)")
    return con


def path_for(nba_gid):
    return os.path.join(PBP_DIR, nba_gid[:5], "%s.json.gz" % nba_gid)


# ---------------------------------------------------------------------------

def seed_log(con, season):
    con.execute("""
        INSERT INTO pbp_log (game_id, nba_game_id, season, game_date)
        SELECT gi.game_id, gi.source_game_id, gi.season, gi.game_date
        FROM game_identifiers gi
        WHERE gi.source='nba' AND gi.season = ?
        ON CONFLICT (game_id) DO NOTHING
    """, [season])
    con.commit()


def cmd_fetch(season, limit=None):
    from curl_cffi import requests as cr

    con = db()
    seed_log(con, season)
    rows = con.execute("""
        SELECT game_id, nba_game_id FROM pbp_log
        WHERE season = ? AND NOT fetched ORDER BY game_date
    """, [season]).fetchall()
    con.close()

    if limit:
        rows = rows[:limit]
    if not rows:
        print("  %s: nothing pending" % season)
        return

    eta = len(rows) * (DELAY + JITTER / 2) / 60.0
    print("  %s: %d games pending (~%.0f min)" % (season, len(rows), eta))
    print("  Ctrl-C is safe; progress flushes every 25 games\n")

    def flush(ok, err):
        if not ok and not err:
            return
        for wait in [2, 5, 15, 30, 60]:
            try:
                c = db()
                for gid, n in ok:
                    c.execute("UPDATE pbp_log SET fetched=TRUE, events=?, "
                              "fetch_error=NULL, updated_at=now() WHERE game_id=?", [n, gid])
                for gid, msg in err:
                    c.execute("UPDATE pbp_log SET fetch_error=?, updated_at=now() "
                              "WHERE game_id=?", [msg[:150], gid])
                c.commit(); c.close()
                ok.clear(); err.clear()
                return
            except Exception:
                time.sleep(wait)
        print("  (progress write deferred)")

    ok, err = [], []
    done = fail = 0
    try:
        for i, (gid, nba_gid) in enumerate(rows, 1):
            p = path_for(nba_gid)
            if os.path.exists(p):
                try:
                    n = len(json.loads(gzip.open(p, "rt", encoding="utf-8").read())
                            .get("game", {}).get("actions", []))
                except Exception:
                    n = None
                ok.append((gid, n))
            else:
                url = ("https://stats.nba.com/stats/playbyplayv3"
                       "?GameID=%s&StartPeriod=0&EndPeriod=14" % nba_gid)
                got = None
                for attempt in range(4):
                    try:
                        r = cr.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=60)
                    except Exception as e:
                        print("    net %s: %s" % (nba_gid, str(e)[:50]))
                        time.sleep(15 * (attempt + 1))
                        continue
                    if r.status_code == 200:
                        got = r.text
                        break
                    print("    HTTP %s on %s" % (r.status_code, nba_gid))
                    time.sleep(20 * (attempt + 1))

                if got is None:
                    err.append((gid, "fetch failed"))
                    fail += 1
                else:
                    try:
                        n = len(json.loads(got).get("game", {}).get("actions", []))
                    except Exception:
                        n = None
                    if not n:
                        err.append((gid, "empty payload"))
                        fail += 1
                    else:
                        os.makedirs(os.path.dirname(p), exist_ok=True)
                        gzip.open(p, "wt", encoding="utf-8").write(got)
                        ok.append((gid, n))
                        done += 1
                time.sleep(DELAY + random.uniform(0, JITTER))

            if i % 25 == 0:
                flush(ok, err)
                left = (len(rows) - i) * (DELAY + JITTER / 2) / 60.0
                print("  %4d/%d  ok=%d fail=%d  ~%.0f min left"
                      % (i, len(rows), done, fail, left))
    except KeyboardInterrupt:
        print("\n  interrupted — rerun to resume")
    finally:
        flush(ok, err)

    print("\n  fetched %d, failed %d" % (done, fail))


# ---------------------------------------------------------------------------

def _num(v, zero_is_null=False):
    """NBA sends "" for absent and stringified numbers for present."""
    if v is None or v == "":
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    if zero_is_null and n == 0:
        return None
    return n


def _dec(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_CLOCK = re.compile(r"PT(\d+)M([\d.]+)S")


def clock_seconds(c):
    if not c:
        return None
    m = _CLOCK.match(c)
    if not m:
        return None
    return int(m.group(1)) * 60 + float(m.group(2))


def elapsed(period, left):
    if period is None or left is None:
        return None
    if period <= 4:
        return (period - 1) * 720 + (720 - left)
    return 4 * 720 + (period - 5) * 300 + (300 - left)


def cmd_fields(season):
    """Print the union of keys across one cached game — no guessing."""
    con = db()
    row = con.execute("""SELECT nba_game_id FROM pbp_log
                         WHERE season=? AND fetched LIMIT 1""", [season]).fetchone()
    con.close()
    if not row:
        print("  no fetched games for %s" % season)
        return
    acts = json.loads(gzip.open(path_for(row[0]), "rt", encoding="utf-8").read())
    acts = acts.get("game", {}).get("actions", [])
    keys = sorted({k for a in acts for k in a.keys()})
    print("  %s  game %s  %d events" % (season, row[0], len(acts)))
    print("  %d distinct keys:" % len(keys))
    for k in keys:
        vals = [a.get(k) for a in acts if a.get(k) not in (None, "")]
        ex = vals[0] if vals else ""
        print("    %-22s %5d non-empty   e.g. %s" % (k, len(vals), str(ex)[:48]))


def cmd_parse(season, limit=None):
    con = db()
    idmap = dict(con.execute("""SELECT source_id, player_id FROM player_identifiers
                                WHERE source='nba'""").fetchall())
    print("  bridged nba ids: %d" % len(idmap))

    rows = con.execute("""SELECT game_id, nba_game_id FROM pbp_log
                          WHERE season=? AND fetched AND NOT parsed
                          ORDER BY game_date""", [season]).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        print("  nothing to parse")
        con.close()
        return

    games = evts = unresolved = 0
    for gid, nba_gid in rows:
        p = path_for(nba_gid)
        if not os.path.exists(p):
            con.execute("UPDATE pbp_log SET fetched=FALSE WHERE game_id=?", [gid])
            continue
        try:
            acts = json.loads(gzip.open(p, "rt", encoding="utf-8")
                              .read()).get("game", {}).get("actions", [])
        except Exception as e:
            con.execute("UPDATE pbp_log SET parse_error=? WHERE game_id=?",
                        [str(e)[:150], gid])
            continue

        batch = []
        for a in acts:
            # personId / teamId use 0 as the "no player / no team" sentinel
            pid_nba = _num(a.get("personId"), zero_is_null=True)
            team_id = _num(a.get("teamId"), zero_is_null=True)
            # personId carries TEAM ids on team rebounds, timeouts, violations
            if pid_nba is not None and 1610612700 <= pid_nba <= 1610612800:
                if team_id is None:
                    team_id = pid_nba
                pid_nba = None
            left = clock_seconds(a.get("clock"))
            per = _num(a.get("period"))
            is_fg = a.get("isFieldGoal") in (1, True, "1")
            resolved = idmap.get(str(pid_nba)) if pid_nba else None
            if pid_nba and not resolved:
                unresolved += 1
            batch.append([
                gid, _num(a.get("actionId")), _num(a.get("actionNumber")),
                per, a.get("clock") or None, left,
                elapsed(per, left),
                pid_nba, resolved, a.get("playerName") or None,
                team_id, a.get("teamTricode") or None,
                a.get("actionType") or None, a.get("subType") or None,
                a.get("description") or None,
                a.get("shotResult") or None,
                _num(a.get("shotValue"), zero_is_null=True),
                a.get("location") or None,
                _dec(a.get("shotDistance")) if is_fg else None,
                _num(a.get("xLegacy")) if is_fg else None,
                _num(a.get("yLegacy")) if is_fg else None,
                is_fg,
                _num(a.get("pointsTotal")),
                _num(a.get("scoreHome")), _num(a.get("scoreAway")),
            ])
        con.executemany("""
            INSERT INTO play_by_play
              (game_id,action_id,event_group,period,clock_raw,seconds_left,elapsed_seconds,
               nba_person_id,player_id,player_name,team_id,team_abbr,
               action_type,sub_type,description,shot_result,shot_value,location,
               shot_distance,
               x_legacy,y_legacy,is_field_goal,points_total,score_home,score_away)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (game_id,action_id) DO NOTHING
        """, batch)
        con.execute("UPDATE pbp_log SET parsed=TRUE, parse_error=NULL, "
                    "updated_at=now() WHERE game_id=?", [gid])
        games += 1
        evts += len(batch)
        if games % 200 == 0:
            print("  parsed %d games, %d events" % (games, evts))

    con.commit()
    print("\n  games %d | events %d | events with unbridged personId %d"
          % (games, evts, unresolved))
    con.close()


def cmd_status():
    con = db()
    rows = con.execute("""
        SELECT season, COUNT(*), SUM(CASE WHEN fetched THEN 1 ELSE 0 END),
               SUM(CASE WHEN parsed THEN 1 ELSE 0 END), SUM(COALESCE(events,0))
        FROM pbp_log GROUP BY season ORDER BY season DESC
    """).fetchall()
    if not rows:
        print("  pbp_log empty — run fetch for a season first")
    else:
        print("  %-9s %7s %8s %8s %10s" % ("season", "games", "fetched", "parsed", "events"))
        for r in rows:
            print("  %-9s %7d %8d %8d %10d" % r)
    tot = con.execute("SELECT COUNT(*) FROM play_by_play").fetchone()[0]
    print("\n  play_by_play rows: %d" % tot)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "parse", "fields", "status"])
    ap.add_argument("--season", default="2025-26")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    if a.cmd == "fetch":
        cmd_fetch(a.season, a.limit)
    elif a.cmd == "parse":
        cmd_parse(a.season, a.limit)
    elif a.cmd == "fields":
        cmd_fields(a.season)
    else:
        cmd_status()
