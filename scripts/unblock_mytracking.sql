-- One-shot: unblock the 47 MyTracking features whose sessions died on Ollama 429.
-- Two dimensions to reset:
--   (1) `status` from Blocked → Designed (coder kills) / Approved (designer + flap)
--   (2) `sprint_id` out of the per-product Blocked sprint (kind='blocked') so the
--       PM UI no longer shows them as parked there. Cleared to NULL so the next
--       /api/products/14/plan-sprints call re-buckets them into real sprints.
BEGIN;

UPDATE features
SET status = 'Designed',
    fix_attempts = 0,
    blocked_reason = NULL,
    review_outcome = NULL,
    review_notes = NULL,
    last_changes_signature = NULL,
    repeated_changes_count = 0
WHERE product_id = 14
  AND status = 'Blocked'
  AND blocked_reason ILIKE '%coder session%killed%'
RETURNING id;

UPDATE features
SET status = 'Approved',
    fix_attempts = 0,
    blocked_reason = NULL,
    review_outcome = NULL,
    review_notes = NULL,
    last_changes_signature = NULL,
    repeated_changes_count = 0
WHERE product_id = 14
  AND status = 'Blocked'
  AND (blocked_reason ILIKE '%designer session%killed%'
       OR blocked_reason ILIKE '%rapid status flap%')
RETURNING id;

-- Sprint-membership reset: move every feature currently parked in the
-- per-product Blocked sprint (kind='blocked') OUT to sprint_id=NULL.
UPDATE features
SET sprint_id = NULL
WHERE product_id = 14
  AND sprint_id IN (SELECT id FROM sprints
                    WHERE product_id = 14 AND kind = 'blocked')
RETURNING id;

SELECT status, COUNT(*) FROM features WHERE product_id=14 GROUP BY status ORDER BY status;
SELECT s.id AS sprint_id, s.kind, s.name, COUNT(f.id) AS feat_count
FROM sprints s LEFT JOIN features f ON f.sprint_id=s.id
WHERE s.product_id=14 AND s.kind='blocked'
GROUP BY s.id, s.kind, s.name;

COMMIT;
