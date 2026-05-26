-- One-shot reset: wipe MyTracking (product_id=14) features + all history.
-- Keeps the product row itself + system_config + pm_users untouched.
--
-- Cascade rules consulted before writing this:
--   sessions    → session_events (CASCADE)
--   features    → feature_changelog, feature_comments, feature_labels,
--                 feature_links, feature_reviews (all CASCADE)
--   labels      → feature_labels (CASCADE — but features gone first)
--   sprints     → sprint_signoffs table doesn't exist; DoD lives inline in
--                 sprints.dod_status (JSONB), so deleting the sprint row
--                 takes its DoD with it.
--
-- A backup was taken first (see scripts/backup_db.sh) so this is reversible
-- via scripts/recover_db.py.
BEGIN;

SELECT 'Before:' AS phase,
  (SELECT COUNT(*) FROM features          WHERE product_id=14) AS features,
  (SELECT COUNT(*) FROM sessions          WHERE product_id=14) AS sessions,
  (SELECT COUNT(*) FROM sprints           WHERE product_id=14) AS sprints,
  (SELECT COUNT(*) FROM phases            WHERE product_id=14) AS phases,
  (SELECT COUNT(*) FROM labels            WHERE product_id=14) AS labels,
  (SELECT COUNT(*) FROM alerts            WHERE product_id=14) AS alerts,
  (SELECT COUNT(*) FROM supervisor_actions WHERE product_id=14) AS sup_actions;

DELETE FROM alerts             WHERE product_id = 14;
DELETE FROM supervisor_actions WHERE product_id = 14;
DELETE FROM sessions           WHERE product_id = 14;
DELETE FROM features           WHERE product_id = 14;
DELETE FROM sprints            WHERE product_id = 14;
DELETE FROM phases             WHERE product_id = 14;
DELETE FROM labels             WHERE product_id = 14;

SELECT 'After:' AS phase,
  (SELECT COUNT(*) FROM features          WHERE product_id=14) AS features,
  (SELECT COUNT(*) FROM sessions          WHERE product_id=14) AS sessions,
  (SELECT COUNT(*) FROM sprints           WHERE product_id=14) AS sprints,
  (SELECT COUNT(*) FROM phases            WHERE product_id=14) AS phases,
  (SELECT COUNT(*) FROM labels            WHERE product_id=14) AS labels,
  (SELECT COUNT(*) FROM alerts            WHERE product_id=14) AS alerts,
  (SELECT COUNT(*) FROM supervisor_actions WHERE product_id=14) AS sup_actions;

-- Sanity: the product row itself + other products must be untouched.
SELECT 'Products kept:' AS phase, COUNT(*) AS total, COUNT(*) FILTER (WHERE id=14) AS mytracking_present
FROM products;

COMMIT;
