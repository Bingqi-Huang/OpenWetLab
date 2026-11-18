-- Labware contents table and protocol positions_hash column (docs/spec/04 sections 5.7 & 5.16).

CREATE TABLE labware_contents (
    labware_id TEXT NOT NULL REFERENCES labware(id),
    n INTEGER NOT NULL,
    name TEXT NOT NULL,
    wells TEXT,
    volume_ul REAL,
    note TEXT,
    PRIMARY KEY (labware_id, n)
);

CREATE INDEX idx_labware_contents_name
    ON labware_contents (name);

ALTER TABLE protocols ADD COLUMN positions_hash TEXT;
