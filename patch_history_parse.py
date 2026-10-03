#!/usr/bin/env python3
"""
patch_history_parse.py — fix the franchise table parser.

Two things were wrong:

  The table's id is the franchise code itself ("SAS"), not something ending in
  _franchise. Matching on a header that contains wins and losses is sturdier
  than guessing ids, since it survives BBRef renaming things.

  There is no team_id column. The season abbreviation has to come out of the
  team link's href (/teams/SAS/2025.html), which is what the join onto
  team_season keys on — and it is how a Seattle row on the OKC page keeps SEA.

Run:  python3 patch_history_parse.py
Then: python3 fetch_history.py probe
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/fetch_history.py")

NEW_PARSE = '''def parse(abbr, html):
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
        m = _re.match(r"(\\d{4})-(\\d{2})", season)
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
            "playoffs": (cells.get("playoff_result") or cells.get("playoffs")
                         or cells.get("comments") or ""),
            "coaches": cells.get("coaches", ""),
            "executive": cells.get("executive", ""),
            "top_ws": cells.get("top_ws", ""),
            "srs": _float(cells.get("srs")),
        })
    if out:
        out[0]["_columns"] = ",".join(found)
    return out

'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    start = src.find("def parse(abbr, html):")
    end = src.find("def _int(v):")
    if start == -1 or end == -1:
        sys.exit("  could not locate parse()")
    src = src[:start] + NEW_PARSE + "\n" + src[end:]

    # probe should report every column BBRef actually exposes
    src = src.replace(
        '    print("  columns present:", ", ".join(k for k, v in rows[0].items() if v not in (None, "")))',
        '    print("  data-stat columns on the page:")\n'
        '    for c in (rows[0].get("_columns") or "").split(","):\n'
        '        print("    %s" % c)')

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_parse", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  parser now matches on the header, not the table id")
    print("  season abbreviation read from the team link")
    print("\n  next: python3 fetch_history.py probe")


if __name__ == "__main__":
    main()
