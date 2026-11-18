-- Island metadata table for db_id, island_mode, and closed_skills (docs/spec/04 section 5.13).

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
