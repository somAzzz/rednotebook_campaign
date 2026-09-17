"""Forward-only schema migrations. No imported data is rewritten by v2."""

MIGRATION_2 = """
BEGIN IMMEDIATE;
CREATE TABLE briefs(
 id TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(id,version), UNIQUE(id,content_hash)
);
CREATE TABLE research_runs(
 id TEXT PRIMARY KEY, brief_id TEXT NOT NULL, brief_version INTEGER NOT NULL,
 state TEXT NOT NULL, metadata TEXT NOT NULL, report_json TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(brief_id,brief_version) REFERENCES briefs(id,version)
);
CREATE TABLE research_sources(
 run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(id), PRIMARY KEY(run_id,source_id)
);
PRAGMA user_version=2;
COMMIT;
"""

MIGRATION_3 = """
BEGIN IMMEDIATE;
CREATE TABLE bundles(
 id TEXT NOT NULL, version INTEGER NOT NULL, run_id TEXT NOT NULL REFERENCES research_runs(id),
 content_hash TEXT NOT NULL, payload TEXT, state TEXT NOT NULL,
 reviewer TEXT, approved_hash TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(id,version)
);
CREATE TABLE outcomes(
 id TEXT PRIMARY KEY, bundle_id TEXT NOT NULL, bundle_version INTEGER NOT NULL,
 source_id TEXT NOT NULL REFERENCES sources(id), payload TEXT NOT NULL,
 FOREIGN KEY(bundle_id,bundle_version) REFERENCES bundles(id,version)
);
CREATE TABLE managed_exports(
 bundle_id TEXT NOT NULL, version INTEGER NOT NULL, path TEXT NOT NULL UNIQUE,
 FOREIGN KEY(bundle_id,version) REFERENCES bundles(id,version)
);
PRAGMA user_version=3;
COMMIT;
"""

MIGRATION_4 = """
BEGIN IMMEDIATE;
ALTER TABLE managed_exports ADD COLUMN manifest_hash TEXT;
PRAGMA user_version=4;
COMMIT;
"""

MIGRATION_5 = """
BEGIN IMMEDIATE;
CREATE TABLE media_cache(
 source_id TEXT NOT NULL REFERENCES sources(id), cache_key TEXT NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(source_id,cache_key)
);
PRAGMA user_version=5;
COMMIT;
"""

MIGRATION_6 = """
BEGIN IMMEDIATE;
CREATE TABLE browser_jobs(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 state TEXT NOT NULL, request_json TEXT, result_json TEXT, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
PRAGMA user_version=6;
COMMIT;
"""

# Progress contains only bounded operational metadata. Review text is source-derived
# and is deleted with any source of its research run (including expiry sweep).
MIGRATION_7 = """
BEGIN IMMEDIATE;
ALTER TABLE browser_jobs ADD COLUMN progress_json TEXT;
CREATE TABLE finding_reviews(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(id),
 finding_id TEXT NOT NULL, decision TEXT NOT NULL,
 corrected_claim TEXT, note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX finding_reviews_run ON finding_reviews(run_id,finding_id,created_at);
PRAGMA user_version=7;
COMMIT;
"""
