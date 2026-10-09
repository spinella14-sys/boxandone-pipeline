#!/usr/bin/env python3
"""
ingest_preseason.py — preseason games and box scores.

Basketball Reference does not carry preseason, which is why the pipeline has
never seen a single preseason game. NBA.com does, through the same
leaguegamelog endpoint bridge_nba_ids.py already calls for its player bridge —
so the fetch, the headers and the rate limiting are reused rather than rebuilt.

One request returns every player-game line for a whole season. From those lines
the games themselves are reconstructed: the endpoint has no game-level feed, but
each row carries the matchup and the team score, so grouping by game id gives
both sides.

Two things worth knowing before running it:

  Starters are unknown. leaguegamelog does not say who started, so `started` is
  left NULL rather than guessed at — a false value would be indistinguishable
  from a real bench appearance.

  Rookies will not map. The player bridge only knows people already in the
  registry, and a first-year player drafted this summer is not there yet. Those
  rows are reported and skipped rather than inventing identities, because
  deciding how an amateur becomes a pro is a real decision and not one a
  scraper should make quietly.

    python3 ingest_preseason.py check --season 2026-27
    python3 ingest_preseason.py check --all
    python3 ingest_preseason.py build --season 2026-27
    python3 ingest_preseason.py build --all
"""

import argparse
import os
import re
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
sys.path.insert(0, HOME)

try:
    from bridge_nba_ids import fetch_gamelog
except ImportError:
    sys.exit("  bridge_nba_ids.py must be in %s" % HOME)

SEASON_TYPE = "Pre Season"


def nba_season(label):
    """'2026-27' -> '2026-27' as NBA.com writes it."""
    return label


def our_game_id(date, home_abbr):
    """Matches the Basketball Reference convention already in use, so a game
    ingested here and the same game scraped later resolve to one row."""
    return "NBA_%s0%s" % (date.strftime("%Y%m%d"), home_abbr)


def mins_to_secs(v):
    if v is None:
        return None
    s = str(v)
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) * 60 + int(float(parts[1]))
        except ValueError:
            return None
    try:
        return int(round(float(s) * 60))
    except ValueError:
        return None


# BBRef and the league disagree on three franchises
NBA_TO_OURS = {"BKN": "BRK", "CHA": "CHO", "PHX": "PHO"}


def team_of(abbr):
    return NBA_TO_OURS.get(abbr, abbr)


def parse_rows(raw):
    """bridge_nba_ids.fetch_gamelog already pairs headers with rows and hands
    back a list of dicts, so most of the time there is nothing left to do. The
    raw payload shape is handled too, in case the helper changes."""
    if isinstance(raw, list):
        return raw
    sets = (raw or {}).get("resultSets") or []
    if not sets:
        return []
    hdr = sets[0]["headers"]
    return [dict(zip(hdr, r)) for r in sets[0]["rowSet"]]


def build(rows):
    """Group player lines into games, then into box score rows."""
    games, box = {}, []
    for r in rows:
        gid_nba = r.get("GAME_ID")
        matchup = r.get("MATCHUP") or ""
        team = team_of(r.get("TEAM_ABBREVIATION") or "")
        if not gid_nba or not team or not matchup:
            continue
        try:
            d = datetime.strptime(r["GAME_DATE"][:10], "%Y-%m-%d").date()
        except Exception:
            continue

        # "MIA vs. TOR" means Miami is at home; "MIA @ TOR" means they are not
        m = re.match(r"^(\w+)\s+(vs\.|@)\s+(\w+)$", matchup.strip())
        if not m:
            continue
        is_home = m.group(2) == "vs."
        opp = team_of(m.group(3))
        home = team if is_home else opp
        away = opp if is_home else team

        gid = our_game_id(d, home)
        g = games.setdefault(gid, {
            "game_id": gid, "nba_game_id": gid_nba, "game_date": d,
            "home_abbr": home, "away_abbr": away,
            "home_score": None, "away_score": None,
        })
        pts = r.get("PTS")
        # the team total arrives on each player row as the team's final score
        if is_home and g["home_score"] is None:
            g["home_score"] = None
        box.append({
            "game_id": gid, "nba_person_id": str(r.get("PLAYER_ID") or ""),
            "player_name": r.get("PLAYER_NAME"),
            "team_abbr": team, "opp_abbr": opp, "is_home": is_home,
            "seconds_played": mins_to_secs(r.get("MIN")),
            "fgm": r.get("FGM"), "fga": r.get("FGA"),
            "fg3m": r.get("FG3M"), "fg3a": r.get("FG3A"),
            "ftm": r.get("FTM"), "fta": r.get("FTA"),
            "orb": r.get("OREB"), "drb": r.get("DREB"), "trb": r.get("REB"),
            "ast": r.get("AST"), "stl": r.get("STL"), "blk": r.get("BLK"),
            "tov": r.get("TOV"), "pf": r.get("PF"), "pts": pts,
            "plus_minus": r.get("PLUS_MINUS"),
        })

    # scores are summed from the players, since the endpoint gives no team line
    for b in box:
        g = games.get(b["game_id"])
        if not g:
            continue
        key = "home_score" if b["is_home"] else "away_score"
        g[key] = (g[key] or 0) + (b["pts"] or 0)
    return list(games.values()), box


def bridge(con, box):
    """NBA person ids to our player ids. Anyone unmapped is almost certainly a
    player the registry has never seen — a rookie, or a returning name signed
    this summer."""
    ids = {b["nba_person_id"] for b in box if b["nba_person_id"]}
    if not ids:
        return {}, set()
    rows = con.execute("""
        SELECT source_id, player_id FROM player_identifiers
        WHERE source = 'nba' AND source_id IN (%s)"""
        % ",".join("?" * len(ids)), list(ids)).fetchall()
    known = dict(rows)
    missing = {b["nba_person_id"] for b in box
               if b["nba_person_id"] and b["nba_person_id"] not in known}
    return known, missing


def season_list(con):
    """Every season, not only the two the hot database holds. The rest live in
    the archive cache, so --all would otherwise survey a fiftieth of history
    and look like it had finished."""
    cache = os.path.join(HOME, "data", "archive")
    if os.path.isdir(cache) and os.listdir(cache):
        try:
            rows = con.execute("""
                SELECT DISTINCT season FROM (
                  SELECT season FROM read_parquet('%s/*/games.parquet',
                                                  union_by_name=true)
                  UNION ALL SELECT season FROM games
                ) ORDER BY season DESC""" % cache).fetchall()
            return [r[0] for r in rows]
        except Exception:
            pass
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM games ORDER BY season DESC").fetchall()]


def cmd_check(seasons, con):
    print("  %-9s %6s %7s %8s %9s" % ("season", "games", "rows", "mapped", "unmapped"))
    for s in seasons:
        try:
            raw = fetch_gamelog(nba_season(s), SEASON_TYPE)
        except Exception as e:
            print("  %-9s  fetch failed: %s" % (s, str(e)[:48]))
            continue
        rows = parse_rows(raw)
        if not rows:
            print("  %-9s %6s" % (s, "none"))
            continue
        games, box = build(rows)
        known, missing = bridge(con, box)
        mapped = sum(1 for b in box if known.get(b["nba_person_id"]))
        print("  %-9s %6d %7d %8d %9d"
              % (s, len(games), len(box), mapped, len(missing)))
        if missing and s == seasons[0]:
            names = sorted({b["player_name"] for b in box
                            if b["nba_person_id"] in missing})[:8]
            print("      not in the registry: %s" % ", ".join(names))


def current_season():
    d = datetime.now()
    y = d.year if d.month >= 10 else d.year - 1
    return "%d-%s" % (y, str(y + 1)[2:])


def cmd_build(seasons, con):
    total_g = total_b = total_skip = 0
    for s in seasons:
        try:
            # the cache would serve the first night's games forever; a season
            # still being played is always fetched fresh
            raw = fetch_gamelog(nba_season(s), SEASON_TYPE,
                                force=(s == current_season()))
        except Exception as e:
            print("  %-9s fetch failed: %s" % (s, str(e)[:48]))
            continue
        rows = parse_rows(raw)
        if not rows:
            print("  %-9s no preseason data" % s)
            continue
        games, box = build(rows)
        known, missing = bridge(con, box)

        con.executemany("""
            INSERT INTO games (game_id, league, season, game_date, season_type,
                               home_abbr, away_abbr, home_score, away_score,
                               source, source_game_id, ingested_at, box_complete)
            VALUES (?, 'NBA', ?, ?, 'preseason', ?, ?, ?, ?, 'nba', ?,
                    current_timestamp, TRUE)
            ON CONFLICT (game_id) DO UPDATE SET
              home_score = excluded.home_score,
              away_score = excluded.away_score,
              box_complete = TRUE""",
            [(g["game_id"], s, g["game_date"], g["home_abbr"], g["away_abbr"],
              g["home_score"], g["away_score"], g["nba_game_id"]) for g in games])

        con.executemany("""
            INSERT INTO game_identifiers
              (source, source_game_id, game_id, season, season_type, game_date,
               linked_by, linked_at)
            VALUES ('nba', ?, ?, ?, 'Pre Season', ?, 'ingest_preseason',
                    current_timestamp)
            ON CONFLICT DO NOTHING""",
            [(g["nba_game_id"], g["game_id"], s, g["game_date"]) for g in games])

        usable = [b for b in box if known.get(b["nba_person_id"])]
        con.executemany("""
            INSERT INTO player_game_box
              (player_id, game_id, team_abbr, opp_abbr, is_home, started, played,
               seconds_played, fgm, fga, fg3m, fg3a, ftm, fta, orb, drb, trb,
               ast, stl, blk, tov, pf, pts, plus_minus, provenance, source,
               ingested_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, 'scraped', 'nba', current_timestamp)
            ON CONFLICT (player_id, game_id) DO NOTHING""",
            [(known[b["nba_person_id"]], b["game_id"], b["team_abbr"],
              b["opp_abbr"], b["is_home"], (b["seconds_played"] or 0) > 0,
              b["seconds_played"], b["fgm"], b["fga"], b["fg3m"], b["fg3a"],
              b["ftm"], b["fta"], b["orb"], b["drb"], b["trb"], b["ast"],
              b["stl"], b["blk"], b["tov"], b["pf"], b["pts"], b["plus_minus"])
             for b in usable])

        con.commit()
        total_g += len(games)
        total_b += len(usable)
        total_skip += len(box) - len(usable)
        print("  %-9s %4d games  %5d box rows  %4d skipped (unknown players)"
              % (s, len(games), len(usable), len(box) - len(usable)))

    print("\n  %d games, %d box rows, %d rows skipped"
          % (total_g, total_b, total_skip))
    if total_skip:
        print("  Skipped rows are players the registry does not have — mostly")
        print("  rookies. They need adding before those lines can land.")


if __name__ == "__main__":
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    con = duckdb.connect(DB_PATH, read_only=(a.cmd == "check"))
    if a.all:
        seasons = season_list(con)
    elif a.season:
        seasons = [a.season]
    else:
        seasons = ["2026-27"]
    (cmd_check if a.cmd == "check" else cmd_build)(seasons, con)
    con.close()
