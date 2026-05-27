-- Full data wipe — phases→features rearchitecture preparation, 2026-05-26.
-- DB backup taken first (backups/db/productfactory_20260526_112042.dump, 1.7M).
--
-- Wipes ALL product data while preserving system_config + pm_users + the
-- alembic schema itself. The rearchitecture branch will replace
-- alembic_version's row when it stamps a fresh 001_initial migration.
--
-- Ordering: delete children before parents to satisfy non-CASCADE FKs.
-- (Most FKs cascade from products, but we want explicit DELETEs for the
-- audit trail in case any FK has the wrong ondelete in the legacy schema.)
BEGIN;

SELECT 'before' AS phase,
  (SELECT COUNT(*) FROM products)           AS products,
  (SELECT COUNT(*) FROM features)           AS features,
  (SELECT COUNT(*) FROM sessions)           AS sessions,
  (SELECT COUNT(*) FROM sprints)            AS sprints,
  (SELECT COUNT(*) FROM phases)             AS phases,
  (SELECT COUNT(*) FROM supervisor_actions) AS sup_actions,
  (SELECT COUNT(*) FROM alerts)             AS alerts,
  (SELECT COUNT(*) FROM labels)             AS labels;

-- Children of features (CASCADE handles these but be explicit)
DELETE FROM feature_changelog;
DELETE FROM feature_comments;
DELETE FROM feature_labels;
DELETE FROM feature_links;
DELETE FROM feature_reviews;

-- Children of sessions
DELETE FROM session_events;

-- Parent product tables
DELETE FROM features;
DELETE FROM sessions;
DELETE FROM sprints;
DELETE FROM phases;
DELETE FROM labels;
DELETE FROM alerts;
DELETE FROM supervisor_actions;
DELETE FROM products;

SELECT 'after' AS phase,
  (SELECT COUNT(*) FROM products)           AS products,
  (SELECT COUNT(*) FROM features)           AS features,
  (SELECT COUNT(*) FROM sessions)           AS sessions,
  (SELECT COUNT(*) FROM sprints)            AS sprints,
  (SELECT COUNT(*) FROM phases)             AS phases,
  (SELECT COUNT(*) FROM supervisor_actions) AS sup_actions,
  (SELECT COUNT(*) FROM alerts)             AS alerts,
  (SELECT COUNT(*) FROM labels)             AS labels;

-- Sanity: system_config + pm_users + alembic_version are kept
SELECT 'preserved' AS phase,
  (SELECT COUNT(*) FROM system_config)   AS system_config,
  (SELECT COUNT(*) FROM pm_users)        AS pm_users,
  (SELECT COUNT(*) FROM alembic_version) AS alembic_version;

COMMIT;
