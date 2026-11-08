CREATE TABLE IF NOT EXISTS itu_snapshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    program text NOT NULL,
    plan_id integer NOT NULL,
    plan_title text NOT NULL,
    term_label text NOT NULL,
    term_year integer NOT NULL,
    season text NOT NULL CHECK (season IN ('Spring', 'Summer', 'Fall')),
    content_sha256 text NOT NULL UNIQUE CHECK (length(content_sha256) = 64),
    observed_at timestamptz NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_urls text[] NOT NULL CHECK (cardinality(source_urls) > 0),
    UNIQUE (id, program, plan_id, term_year, season)
);

CREATE TABLE IF NOT EXISTS itu_active_snapshots (
    program text NOT NULL,
    plan_id integer NOT NULL,
    term_year integer NOT NULL,
    season text NOT NULL,
    snapshot_id bigint NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (program, plan_id, term_year, season),
    FOREIGN KEY (snapshot_id, program, plan_id, term_year, season)
        REFERENCES itu_snapshots (id, program, plan_id, term_year, season)
);

CREATE TABLE IF NOT EXISTS itu_courses (
    snapshot_id bigint NOT NULL REFERENCES itu_snapshots (id),
    code text NOT NULL,
    title text NOT NULL,
    language text NOT NULL,
    credits numeric NOT NULL CHECK (credits >= 0),
    ects numeric NOT NULL CHECK (ects >= 0),
    PRIMARY KEY (snapshot_id, code)
);

CREATE TABLE IF NOT EXISTS itu_requirements (
    snapshot_id bigint NOT NULL REFERENCES itu_snapshots (id),
    semester smallint NOT NULL CHECK (semester BETWEEN 1 AND 8),
    position smallint NOT NULL CHECK (position > 0),
    category text NOT NULL,
    title text NOT NULL,
    elective_group text,
    unresolved_options text[] NOT NULL,
    PRIMARY KEY (snapshot_id, semester, position)
);

CREATE TABLE IF NOT EXISTS itu_requirement_courses (
    snapshot_id bigint NOT NULL,
    semester smallint NOT NULL,
    position smallint NOT NULL,
    course_code text NOT NULL,
    PRIMARY KEY (snapshot_id, semester, position, course_code),
    FOREIGN KEY (snapshot_id, semester, position)
        REFERENCES itu_requirements (snapshot_id, semester, position),
    FOREIGN KEY (snapshot_id, course_code) REFERENCES itu_courses (snapshot_id, code)
);

CREATE TABLE IF NOT EXISTS itu_prerequisites (
    snapshot_id bigint NOT NULL,
    course_code text NOT NULL,
    course_rule text NOT NULL,
    standing_rule text NOT NULL,
    source_url text NOT NULL,
    PRIMARY KEY (snapshot_id, course_code),
    FOREIGN KEY (snapshot_id, course_code) REFERENCES itu_courses (snapshot_id, code)
);

CREATE TABLE IF NOT EXISTS itu_sections (
    snapshot_id bigint NOT NULL,
    crn integer NOT NULL CHECK (crn > 0),
    course_code text NOT NULL,
    instruction text NOT NULL,
    instructor text NOT NULL,
    building text NOT NULL,
    room text NOT NULL,
    quota integer CHECK (quota >= 0),
    enrolled integer CHECK (enrolled >= 0),
    allowed_programs text[],
    source_url text NOT NULL,
    PRIMARY KEY (snapshot_id, crn),
    FOREIGN KEY (snapshot_id, course_code) REFERENCES itu_courses (snapshot_id, code)
);

CREATE TABLE IF NOT EXISTS itu_meetings (
    snapshot_id bigint NOT NULL,
    crn integer NOT NULL,
    weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_minute smallint NOT NULL CHECK (start_minute >= 0),
    end_minute smallint NOT NULL CHECK (end_minute > start_minute AND end_minute <= 1440),
    PRIMARY KEY (snapshot_id, crn, weekday, start_minute, end_minute),
    FOREIGN KEY (snapshot_id, crn) REFERENCES itu_sections (snapshot_id, crn)
);
