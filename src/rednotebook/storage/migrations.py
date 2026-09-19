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


# Rebuild only the parent table with FK enforcement temporarily disabled by the caller.
# Existing payload bytes, hashes, approvals, exports and outcome FK targets are unchanged.
# New per-version dependencies also cover author-led drafts that include research context.
MIGRATION_8 = """
BEGIN IMMEDIATE;
CREATE TABLE bundles_v8(
 id TEXT NOT NULL, version INTEGER NOT NULL, run_id TEXT REFERENCES research_runs(id),
 content_hash TEXT NOT NULL, payload TEXT, state TEXT NOT NULL,
 reviewer TEXT, approved_hash TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(id,version)
);
INSERT INTO bundles_v8 SELECT * FROM bundles;
DROP TABLE bundles;
ALTER TABLE bundles_v8 RENAME TO bundles;
CREATE TABLE bundle_sources(
 bundle_id TEXT NOT NULL, version INTEGER NOT NULL,
 source_id TEXT NOT NULL REFERENCES sources(id),
 PRIMARY KEY(bundle_id,version,source_id),
 FOREIGN KEY(bundle_id,version) REFERENCES bundles(id,version)
);
INSERT INTO bundle_sources
 SELECT b.id,b.version,s.source_id FROM bundles b
 JOIN research_sources s ON s.run_id=b.run_id;
CREATE TABLE bundle_runs(
 bundle_id TEXT NOT NULL, version INTEGER NOT NULL,
 run_id TEXT NOT NULL REFERENCES research_runs(id),
 PRIMARY KEY(bundle_id,version,run_id),
 FOREIGN KEY(bundle_id,version) REFERENCES bundles(id,version)
);
INSERT INTO bundle_runs SELECT id,version,run_id FROM bundles WHERE run_id IS NOT NULL;
CREATE TABLE diagnostic_events(
 id TEXT PRIMARY KEY, error_stage TEXT NOT NULL, error_code TEXT NOT NULL, created_at TEXT NOT NULL
);
PRAGMA user_version=8;
"""


# Forward-only activation of the persisted intent-plan/job JSON contract.
# Legacy jobs remain byte-identical; new fields have defaults on read. Older binaries
# must reject v9 instead of attempting to parse unknown plan_search/search_plan jobs.
MIGRATION_9 = """
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO settings VALUES ('search_plan_contract_version','1');
PRAGMA user_version=9;
COMMIT;
"""


# Topic jobs and campaign depth references live in existing source-purgeable JSON.
# Old payloads/hashes stay byte-identical; older binaries must reject the new contract.
MIGRATION_10 = """
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO settings VALUES ('topic_research_contract_version','1');
PRAGMA user_version=10;
COMMIT;
"""


# Capture checkpoints, text-origin and comment reply metrics activate a new contract.
# Source-bound job JSON/files are purged by existing revocation; old payload bytes stay intact.
MIGRATION_11 = """
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO settings VALUES ('capture_review_contract_version','1');
PRAGMA user_version=11;
COMMIT;
"""


# Explicit assistant submissions use existing source-bound reports/jobs and managed
# capture sidecars. Original grants/payloads/hashes are unchanged. Existing source
# expiry/revocation removes the sidecars, reports and dependent campaign bundles.
MIGRATION_12 = """
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO settings VALUES ('assistant_review_contract_version','1');
PRAGMA user_version=12;
COMMIT;
"""


# Caller-mode jobs and generic processor provenance remain in source-bound JSON.
# No grant, evidence or historical payload rewrite; existing expiry/revocation purge applies.
MIGRATION_13 = """
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO settings VALUES ('caller_analysis_contract_version','1');
PRAGMA user_version=13;
COMMIT;
"""
