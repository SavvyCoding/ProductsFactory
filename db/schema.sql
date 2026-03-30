-- ProductFactory — PostgreSQL Schema
-- Apply via Alembic (never hand-edit live schema)
-- Initial version: v1.0

-- ══════════════════════════════════════════════════════
-- PRODUCTS
-- ══════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS products (
    id               SERIAL PRIMARY KEY,
    working_dir      TEXT NOT NULL UNIQUE,          -- only field PM enters
    name             TEXT,                          -- discovered from README.md heading
    github_repo      TEXT,                          -- discovered from git remote
    tech_stack       TEXT[],                        -- detected from files (python, node, etc.)
    type             TEXT NOT NULL DEFAULT 'greenfield'
                         CHECK (type IN ('greenfield', 'brownfield')),
    status           TEXT NOT NULL DEFAULT 'registered'
                         CHECK (status IN ('registered', 'discovering', 'discovered', 'ready', 'paused', 'error')),
    analysis_status  TEXT NOT NULL DEFAULT 'pending'
                         CHECK (analysis_status IN ('pending', 'running', 'done')),
    config           JSONB,                         -- product_config.json content (synced from repo)
    last_run_at      TIMESTAMPTZ,                   -- used for fair round-robin queue
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ══════════════════════════════════════════════════════
-- FEATURES
-- ══════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS features (
    id           SERIAL PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'Pending'
                     CHECK (status IN (
                         'Pending', 'Approved', 'Implementing', 'Implemented',
                         'Testing', 'Committed', 'Pushed', 'Blocked', 'Rejected', 'Reverted'
                     )),
    priority     INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 1 AND 100),
    depends_on   INTEGER REFERENCES features(id),  -- optional dependency chain
    fix_attempts INTEGER NOT NULL DEFAULT 0 CHECK (fix_attempts >= 0),
    source       TEXT NOT NULL DEFAULT 'pm' CHECK (source IN ('pm', 'ai')),
    branch_name  TEXT,                             -- feature/{name}-{subfeature}
    pr_url       TEXT,
    pr_number    INTEGER,
    blocked_reason TEXT,                           -- written by Claude on Blocked
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ══════════════════════════════════════════════════════
-- SESSIONS  (audit trail — one row per Claude session)
-- ══════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sessions (
    id           SERIAL PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    session_uid  TEXT NOT NULL UNIQUE,             -- random UUID written to session.lock
    container_id TEXT,                             -- Docker container ID
    started_at   TIMESTAMPTZ DEFAULT NOW(),
    ended_at     TIMESTAMPTZ,
    exit_code    INTEGER,
    features_attempted INTEGER DEFAULT 0,
    features_pushed    INTEGER DEFAULT 0,
    notes        TEXT                              -- poller audit notes
);

-- ══════════════════════════════════════════════════════
-- ALERTS  (queue for webhook delivery)
-- ══════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS alerts (
    id           SERIAL PRIMARY KEY,
    product_id   INTEGER REFERENCES products(id) ON DELETE SET NULL,
    level        TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error', 'critical')),
    message      TEXT NOT NULL,
    delivered    BOOLEAN NOT NULL DEFAULT FALSE,
    retry_count  INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ══════════════════════════════════════════════════════
-- INDEXES
-- ══════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_features_product_status
    ON features (product_id, status);

CREATE INDEX IF NOT EXISTS idx_products_last_run
    ON products (last_run_at ASC NULLS FIRST)
    WHERE status = 'ready';

CREATE INDEX IF NOT EXISTS idx_alerts_undelivered
    ON alerts (created_at)
    WHERE delivered = FALSE;

-- ══════════════════════════════════════════════════════
-- TRIGGERS  (auto-update updated_at)
-- ══════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_products_updated
    BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_features_updated
    BEFORE UPDATE ON features
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
