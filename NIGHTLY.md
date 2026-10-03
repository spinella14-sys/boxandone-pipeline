# Nightly job

## What it does

1. Takes a lock in R2 so two runs cannot collide
2. Pulls the working database (~70 MB)
3. Clears the cached schedule pages for the current season — Basketball
   Reference adds box-score links as games finish, so a cached page would hide
   last night's results and the job would run cleanly forever finding nothing
4. Scrapes, bridges and parses the new games, then their play-by-play
5. Exports the season aggregates, the changed players' career files, the
   registry and the manifest
6. Parses possessions for the new games
7. Pushes the database back and touches Supabase

## Why it never downloads the archive

A nightly run needs history for only three things, and each has a cheaper path:

| needs history for | instead |
|---|---|
| season aggregates | the current season is entirely in the hot database |
| a player's career file | fetch his ~20 KB file from R2, merge, write back |
| registry and manifest | sum the thirty season files, under 2 MB total |

So a run moves about 100 MB rather than 455 MB, and finishes in minutes.

## Setting it up

Push the repo, then add these under Settings → Secrets and variables → Actions:

    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_ENDPOINT_URL
    R2_BUCKET_NAME
    SUPABASE_URL          (optional — without it the keepalive is skipped)
    SUPABASE_ANON_KEY

Seed the database into R2 once from your laptop:

    cd ~/boxandone && python3 nightly2.py --push-db

Then trigger a run by hand from the Actions tab before trusting the schedule.

## When it goes wrong

Every run writes a log to `v2/logs/nightly/` named `<timestamp>_ok.txt` or
`<timestamp>_FAIL.txt`, so a silent failure at 5am is still visible later. A
stale lock clears itself after two hours.

If the NBA feed is down but Basketball Reference is not, `--no-pbp` takes the
box scores and skips play-by-play; the next run picks up what was missed.
