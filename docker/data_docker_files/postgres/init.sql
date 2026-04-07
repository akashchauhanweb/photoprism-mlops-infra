-- MLOps Data Pipeline — PostgreSQL Schema
-- Initialized automatically on first container start via /docker-entrypoint-initdb.d/

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- image_metadata
-- Populated by data-ingestor when images are ingested from HuggingFace.
-- Primary reference table linking image_id to its split and ingestion provenance.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS image_metadata (
    image_id        VARCHAR(255) PRIMARY KEY,
    split           VARCHAR(10)  NOT NULL CHECK (split IN ('train', 'val', 'test')),
    source          VARCHAR(255) NOT NULL,
    source_commit_sha VARCHAR(40),
    dataset_version VARCHAR(50)  NOT NULL,
    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- search_queries
-- One row per simulated (or real) user search event.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_queries (
    query_id    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    query_text  TEXT         NOT NULL,
    query_type  VARCHAR(20)  NOT NULL CHECK (query_type IN ('raw', 'tags', 'caption')),
    timestamp   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- search_results
-- The ranked list returned for each query — one row per (query, image) pair.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_results (
    result_id   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id    UUID         NOT NULL REFERENCES search_queries(query_id) ON DELETE CASCADE,
    image_id    VARCHAR(255) NOT NULL,
    rank        INTEGER      NOT NULL CHECK (rank >= 1),
    score       FLOAT
);

CREATE INDEX IF NOT EXISTS idx_search_results_query_id ON search_results(query_id);

-- ---------------------------------------------------------------------------
-- feedback_events
-- Explicit (click) and implicit signals captured per query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback_events (
    event_id    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id    UUID         NOT NULL REFERENCES search_queries(query_id) ON DELETE CASCADE,
    image_id    VARCHAR(255) NOT NULL,
    event_type  VARCHAR(20)  NOT NULL CHECK (event_type IN ('click')),
    rank        INTEGER      NOT NULL CHECK (rank >= 1),
    timestamp   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_events_query_id ON feedback_events(query_id);

-- ---------------------------------------------------------------------------
-- feature_jobs
-- PostgreSQL-backed async job queue for online feature computation.
-- A job is submitted when a new image arrives; the online-feature-worker
-- daemon picks it up, computes the CLIP embedding, and writes it to Qdrant.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_jobs (
    job_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    image_id     VARCHAR(255) NOT NULL,
    s3_key       TEXT         NOT NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_feature_jobs_status ON feature_jobs(status);
