-- ============================================================================
-- BOX AND ONE — SUPABASE / POSTGRES LAYER
--
-- This holds everything humans create and mutate. Bulk immutable data
-- (box scores, computed metrics) lives in R2 as Parquet and never comes here —
-- Supabase free is 500 MB, R2 free is 10 GB with zero egress.
--
-- Two changes from the DuckDB draft, both agreed:
--   1. position and archetype are TEMPORAL. A player who changes position at
--      26 must not have his 22-year-old record overwritten. Single-column
--      position broke "one player, one unbroken record" the moment it changed.
--   2. archetype is a WEIGHT VECTOR (primary 1.0 / ancillary 0.5 /
--      potential 0.25), not a bucket. Buckets were too sparse to z-score
--      against; weights avoid sparse peer groups entirely and handle hybrids.
--
-- Paste into the Supabase SQL editor. Idempotent — safe to re-run.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ---------------------------------------------------------------------------
-- IDENTITY
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS seq_player_id START 1;

CREATE OR REPLACE FUNCTION next_player_id() RETURNS text
LANGUAGE sql VOLATILE AS $$
    SELECT 'P' || lpad(nextval('seq_player_id')::text, 8, '0');
$$;

CREATE TABLE IF NOT EXISTS players (
    player_id            text PRIMARY KEY DEFAULT next_player_id(),

    full_name            text NOT NULL,
    display_name         text,
    first_name           text,
    last_name            text,
    name_normalized      text NOT NULL,

    birthdate            date,
    birthdate_status     text NOT NULL DEFAULT 'missing'
                         CHECK (birthdate_status IN
                               ('confirmed','unconfirmed','missing','admin_verified')),

    birth_city           text,
    birth_country        text,
    nationality          text,
    college              text,
    high_school          text,
    draft_year           smallint,
    draft_round          smallint,
    draft_pick           smallint,
    draft_team           text,

    status               text NOT NULL DEFAULT 'unknown'
                         CHECK (status IN
                               ('active','inactive','retired','prospect','unknown')),

    provenance           text NOT NULL DEFAULT 'scraped'
                         CHECK (provenance IN ('scraped','human','model','derived')),
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    notes                text
);

CREATE INDEX IF NOT EXISTS idx_players_name  ON players(name_normalized);
CREATE INDEX IF NOT EXISTS idx_players_bday  ON players(birthdate);
CREATE INDEX IF NOT EXISTS idx_players_draft ON players(draft_year);


CREATE TABLE IF NOT EXISTS player_identifiers (
    source        text NOT NULL,
    source_id     text NOT NULL,
    player_id     text NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    source_url    text,
    is_primary    boolean NOT NULL DEFAULT false,
    confidence    double precision NOT NULL DEFAULT 1.0,
    linked_by     text,
    linked_at     timestamptz NOT NULL DEFAULT now(),
    provenance    text NOT NULL DEFAULT 'scraped',
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_ident_player ON player_identifiers(player_id);


CREATE TABLE IF NOT EXISTS player_aliases (
    player_id        text NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    alias            text NOT NULL,
    alias_normalized text NOT NULL,
    source           text,
    first_seen       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, alias_normalized)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON player_aliases(alias_normalized);


-- Merges never delete. Every lookup resolves through here, forever.
CREATE TABLE IF NOT EXISTS player_merges (
    merged_id    text PRIMARY KEY,
    survivor_id  text NOT NULL REFERENCES players(player_id),
    merged_at    timestamptz NOT NULL DEFAULT now(),
    actor        text NOT NULL,
    reason       text
);


-- ---------------------------------------------------------------------------
-- TEMPORAL POSITION
-- One row per assignment. Current position = latest effective_from.
-- Historical z-scores use the position the player held THEN, not now.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS player_positions (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id      text NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    position       text NOT NULL CHECK (position IN ('PG','CG','W','F','C')),
    effective_from date NOT NULL,
    assigned_by    text NOT NULL,
    reason         text,
    report_id      text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (player_id, effective_from)
);
CREATE INDEX IF NOT EXISTS idx_pos_player ON player_positions(player_id, effective_from DESC);


-- ---------------------------------------------------------------------------
-- ARCHETYPE WEIGHT VECTOR
-- primary 1.0 / ancillary 0.5 / potential 0.25. A player carries several rows.
-- Also temporal: archetype migration over a career is itself signal.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS archetypes (
    position    text NOT NULL CHECK (position IN ('PG','CG','W','F','C')),
    archetype   text NOT NULL,
    PRIMARY KEY (position, archetype)
);

INSERT INTO archetypes (position, archetype) VALUES
 ('PG','Balanced'),('PG','Scorer'),('PG','Passer'),('PG','Shooter'),
 ('PG','Defender'),('PG','Engine'),
 ('CG','Scorer'),('CG','Shooter'),('CG','Movement Shooter'),('CG','Slasher'),
 ('CG','Passer'),('CG','Defender'),('CG','Engine'),
 ('W','Scorer'),('W','Shooter'),('W','Movement Shooter'),('W','Slasher'),
 ('W','Passer'),('W','Defender'),('W','Engine'),
 ('F','Finisher'),('F','Scorer'),('F','Shooter'),('F','Slasher'),
 ('F','Passer'),('F','Defender'),('F','Engine'),
 ('C','Finisher'),('C','Scorer'),('C','Shooter'),('C','Passer'),
 ('C','Anchor Defender'),('C','Mobile Defender'),('C','Engine')
ON CONFLICT DO NOTHING;


CREATE TABLE IF NOT EXISTS player_archetypes (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id      text NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    position       text NOT NULL,
    archetype      text NOT NULL,
    tier           text NOT NULL CHECK (tier IN ('primary','ancillary','potential')),
    weight         double precision NOT NULL
                   GENERATED ALWAYS AS (
                       CASE tier WHEN 'primary' THEN 1.0
                                 WHEN 'ancillary' THEN 0.5
                                 ELSE 0.25 END) STORED,
    effective_from date NOT NULL,
    assigned_by    text NOT NULL,
    report_id      text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (position, archetype) REFERENCES archetypes(position, archetype),
    UNIQUE (player_id, archetype, effective_from)
);
CREATE INDEX IF NOT EXISTS idx_arch_player ON player_archetypes(player_id, effective_from DESC);


-- ---------------------------------------------------------------------------
-- SCOUTS
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scouts (
    scout_id      text PRIMARY KEY,
    display_name  text NOT NULL,
    email         text UNIQUE,
    auth_user_id  uuid,                    -- links to Supabase auth.users
    role          text NOT NULL DEFAULT 'scout'
                  CHECK (role IN ('super_admin','admin','scout','agent')),
    help_weight   double precision NOT NULL DEFAULT 1.0,
    is_agent      boolean NOT NULL DEFAULT false,
    agent_persona text,
    specialty     text,
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- REPORTS
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS seq_report_id START 1;

CREATE OR REPLACE FUNCTION next_report_id() RETURNS text
LANGUAGE sql VOLATILE AS $$
    SELECT 'R' || lpad(nextval('seq_report_id')::text, 8, '0');
$$;

CREATE TABLE IF NOT EXISTS reports (
    report_id       text PRIMARY KEY DEFAULT next_report_id(),
    player_id       text NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    scout_id        text NOT NULL REFERENCES scouts(scout_id),

    report_type     text NOT NULL
                    CHECK (report_type IN
                          ('help','game','film','workout','interview',
                           'medical','character','quick_note','trade_target')),

    -- scout-selected: the date the judgment was FORMED. Drives age plotting.
    report_date     date NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- true = written at the time from information then available.
    -- false = reconstructed later with hindsight.
    -- Only contemporaneous reports are valid as a prediction-validation set.
    contemporaneous boolean NOT NULL DEFAULT true,

    league          text,
    season          text,
    games_viewed    smallint,
    viewing_mode    text CHECK (viewing_mode IN ('live','film','mixed','data_only')),
    team_abbr       text,
    summary         text,
    body            text,

    status          text NOT NULL DEFAULT 'published'
                    CHECK (status IN ('draft','published','archived')),
    provenance      text NOT NULL DEFAULT 'human'
                    CHECK (provenance IN ('human','model','imported'))
);

CREATE INDEX IF NOT EXISTS idx_reports_player ON reports(player_id, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_reports_type   ON reports(report_type);
CREATE INDEX IF NOT EXISTS idx_reports_scout  ON reports(scout_id);


-- ---------------------------------------------------------------------------
-- HELP
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS help_levels (
    rank   smallint PRIMARY KEY,
    code   text NOT NULL UNIQUE,
    label  text NOT NULL,
    lower_bound double precision NOT NULL,   -- inclusive
    upper_bound double precision NOT NULL    -- exclusive
);

-- .75 promotes to the next tier throughout. Franchise is the one exception:
-- it starts at 9.50 rather than 9.75 because 10 is a ceiling and there is no
-- room above it. At 9.75 a unanimous 9.5 consensus rendered as Cornerstone.
INSERT INTO help_levels (rank, code, label, lower_bound, upper_bound) VALUES
    (10,'FR','Franchise',    9.50, 10.01),
    ( 9,'CS','Cornerstone',  8.75,  9.50),
    ( 8,'KS','Key Starter',  7.75,  8.75),
    ( 7,'ST','Starter',      6.75,  7.75),
    ( 6,'KR','Key Reserve',  5.75,  6.75),
    ( 5,'RE','Reserve',      4.75,  5.75),
    ( 4,'RO','Roster',       3.75,  4.75),
    ( 3,'BO','Border',       2.75,  3.75),
    ( 2,'GL','G-League',     1.75,  2.75),
    ( 1,'ML','Minor League', 0.00,  1.75)
ON CONFLICT (rank) DO UPDATE
    SET code=EXCLUDED.code, label=EXCLUDED.label,
        lower_bound=EXCLUDED.lower_bound, upper_bound=EXCLUDED.upper_bound;


CREATE TABLE IF NOT EXISTS help_assessments (
    report_id       text PRIMARY KEY REFERENCES reports(report_id) ON DELETE CASCADE,
    player_id       text NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    scout_id        text NOT NULL REFERENCES scouts(scout_id),
    report_date     date NOT NULL,

    high_val        double precision NOT NULL CHECK (high_val     BETWEEN 0 AND 10),
    expected_val    double precision NOT NULL CHECK (expected_val BETWEEN 0 AND 10),
    low_val         double precision NOT NULL CHECK (low_val      BETWEEN 0 AND 10),

    high_note       text,
    expected_note   text,
    low_note        text,

    horizon_years   double precision DEFAULT 3.0,
    conviction      double precision CHECK (conviction BETWEEN 0 AND 1),

    position_at_report text CHECK (position_at_report IN ('PG','CG','W','F','C')),

    CHECK (low_val <= expected_val AND expected_val <= high_val)
);
CREATE INDEX IF NOT EXISTS idx_help_player ON help_assessments(player_id, report_date DESC);


-- Versioned metric -> HELP mapping. Early rows are judgment; later rows can be
-- fitted by regressing metric values against HELP labels on completed careers.
CREATE TABLE IF NOT EXISTS metric_help_scale (
    metric       text NOT NULL,
    version      text NOT NULL,
    help_rank    smallint NOT NULL REFERENCES help_levels(rank),
    metric_value double precision NOT NULL,
    method       text NOT NULL DEFAULT 'judgment'
                 CHECK (method IN ('judgment','fitted','anchored')),
    fitted_on    text,
    active       boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric, version, help_rank)
);


-- ---------------------------------------------------------------------------
-- FUNCTIONS
-- ---------------------------------------------------------------------------

-- Score -> code. Band lookup, not rounding: a 0.4 returns ML rather than blank.
CREATE OR REPLACE FUNCTION help_code(val double precision) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT code FROM help_levels
    WHERE val >= lower_bound AND val < upper_bound
    LIMIT 1;
$$;

-- Resolve any player_id through the merge chain to its survivor.
CREATE OR REPLACE FUNCTION resolve_player_id(pid text) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE cur text := pid; nxt text; i int := 0;
BEGIN
    LOOP
        SELECT survivor_id INTO nxt FROM player_merges WHERE merged_id = cur;
        EXIT WHEN nxt IS NULL OR i > 10;
        cur := nxt; nxt := NULL; i := i + 1;
    END LOOP;
    RETURN cur;
END;
$$;


-- ---------------------------------------------------------------------------
-- VIEWS
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW current_position AS
SELECT DISTINCT ON (player_id)
       player_id, position, effective_from, assigned_by
FROM player_positions
ORDER BY player_id, effective_from DESC;


-- Current archetype weight vector, one row per archetype the player carries.
CREATE OR REPLACE VIEW current_archetypes AS
SELECT a.player_id, a.position, a.archetype, a.tier, a.weight, a.effective_from
FROM player_archetypes a
JOIN (
    SELECT player_id, max(effective_from) AS ef
    FROM player_archetypes GROUP BY player_id
) latest ON latest.player_id = a.player_id AND latest.ef = a.effective_from;


-- Every HELP judgment with the player's exact age on the date it was formed.
-- x = age_years, y = the three scores. This is the scatter behind the chart.
CREATE OR REPLACE VIEW help_points AS
SELECT
    h.player_id,
    p.full_name,
    h.report_id,
    h.scout_id,
    s.display_name AS scout_name,
    s.help_weight,
    s.is_agent,
    h.report_date,
    r.contemporaneous,
    (h.report_date - p.birthdate) / 365.25          AS age_years,
    p.birthdate_status,
    h.high_val, h.expected_val, h.low_val,
    help_code(h.high_val)     AS high_code,
    help_code(h.expected_val) AS expected_code,
    help_code(h.low_val)      AS low_code,
    h.high_val - h.low_val    AS band_width,
    h.conviction
FROM help_assessments h
JOIN reports r ON r.report_id = h.report_id
JOIN players p ON p.player_id = h.player_id
JOIN scouts  s ON s.scout_id  = h.scout_id
WHERE r.status = 'published';


CREATE OR REPLACE VIEW help_latest_per_scout AS
SELECT DISTINCT ON (player_id, scout_id) *
FROM help_points
WHERE report_date >= (CURRENT_DATE - INTERVAL '5 years')
ORDER BY player_id, scout_id, report_date DESC;


-- Weighted consensus WITH dispersion. A 6/9/9 high set averages to 8.25 with
-- Adam at 2x, but the spread of 3.0 is itself information about the player.
CREATE OR REPLACE VIEW help_consensus AS
SELECT
    player_id,
    full_name,
    count(*)                                                    AS n_scouts,
    sum(help_weight)                                            AS total_weight,
    round((sum(high_val     * help_weight) / sum(help_weight))::numeric, 2) AS high_val,
    round((sum(expected_val * help_weight) / sum(help_weight))::numeric, 2) AS expected_val,
    round((sum(low_val      * help_weight) / sum(help_weight))::numeric, 2) AS low_val,
    help_code(sum(high_val     * help_weight) / sum(help_weight)) AS high_code,
    help_code(sum(expected_val * help_weight) / sum(help_weight)) AS expected_code,
    help_code(sum(low_val      * help_weight) / sum(help_weight)) AS low_code,
    round((max(high_val)     - min(high_val))::numeric, 2)      AS high_spread,
    round((max(expected_val) - min(expected_val))::numeric, 2)  AS expected_spread,
    round((max(low_val)      - min(low_val))::numeric, 2)       AS low_spread,
    round(coalesce(stddev_pop(expected_val), 0)::numeric, 2)    AS expected_sd,
    min(report_date) AS oldest_report,
    max(report_date) AS newest_report,
    count(*) FILTER (WHERE is_agent) AS agent_reports
FROM help_latest_per_scout
GROUP BY player_id, full_name;


-- Scout disagreement is a study trigger, not noise to average away.
CREATE OR REPLACE VIEW help_disagreement AS
SELECT player_id, full_name, n_scouts, expected_val, expected_code,
       expected_spread, high_spread, low_spread
FROM help_consensus
WHERE n_scouts >= 2 AND (expected_spread >= 1.5 OR high_spread >= 2.0)
ORDER BY expected_spread DESC;


-- Players needing a manual position call.
CREATE OR REPLACE VIEW position_unset AS
SELECT p.player_id, p.full_name, p.status, p.birthdate_status
FROM players p
LEFT JOIN current_position cp ON cp.player_id = p.player_id
WHERE cp.player_id IS NULL;


-- ---------------------------------------------------------------------------
-- ROW LEVEL SECURITY (Supabase)
-- Default closed. Any table added later without a policy is unreadable,
-- which is the correct failure direction.
-- ---------------------------------------------------------------------------

ALTER TABLE players            ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_identifiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_aliases     ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_merges      ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_positions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_archetypes  ENABLE ROW LEVEL SECURITY;
ALTER TABLE archetypes         ENABLE ROW LEVEL SECURITY;
ALTER TABLE scouts             ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports            ENABLE ROW LEVEL SECURITY;
ALTER TABLE help_assessments   ENABLE ROW LEVEL SECURITY;
ALTER TABLE help_levels        ENABLE ROW LEVEL SECURITY;
ALTER TABLE metric_help_scale  ENABLE ROW LEVEL SECURITY;

-- helper: is the caller an admin?
CREATE OR REPLACE FUNCTION is_admin() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT EXISTS (
        SELECT 1 FROM scouts
        WHERE auth_user_id = auth.uid()
          AND role IN ('super_admin','admin')
          AND active
    );
$$;

CREATE OR REPLACE FUNCTION my_scout_id() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT scout_id FROM scouts WHERE auth_user_id = auth.uid() AND active LIMIT 1;
$$;

DO $$
DECLARE t text;
BEGIN
    -- any signed-in scout can read everything
    FOREACH t IN ARRAY ARRAY['players','player_identifiers','player_aliases',
                             'player_merges','player_positions','player_archetypes',
                             'archetypes','scouts','reports','help_assessments',
                             'help_levels','metric_help_scale']
    LOOP
        EXECUTE format(
            'DROP POLICY IF EXISTS read_all ON %I; '
            'CREATE POLICY read_all ON %I FOR SELECT TO authenticated USING (true);',
            t, t);
    END LOOP;

    -- admin-only writes on identity and reference tables
    FOREACH t IN ARRAY ARRAY['players','player_identifiers','player_aliases',
                             'player_merges','player_positions','player_archetypes',
                             'archetypes','scouts','help_levels','metric_help_scale']
    LOOP
        EXECUTE format(
            'DROP POLICY IF EXISTS admin_write ON %I; '
            'CREATE POLICY admin_write ON %I FOR ALL TO authenticated '
            'USING (is_admin()) WITH CHECK (is_admin());', t, t);
    END LOOP;
END $$;

-- scouts write their own reports; admins write any
DROP POLICY IF EXISTS own_reports ON reports;
CREATE POLICY own_reports ON reports FOR ALL TO authenticated
    USING (scout_id = my_scout_id() OR is_admin())
    WITH CHECK (scout_id = my_scout_id() OR is_admin());

DROP POLICY IF EXISTS own_help ON help_assessments;
CREATE POLICY own_help ON help_assessments FOR ALL TO authenticated
    USING (scout_id = my_scout_id() OR is_admin())
    WITH CHECK (scout_id = my_scout_id() OR is_admin());
