"""api_flapping_features detects OSCILLATION (re-entering the same status), not
raw transition count, and excludes terminal-state features. Together these stop
rapid_flap from Blocking (a) features that legitimately PROGRESSED through the
pipeline and (b) features that already shipped — the testingcalc #1421 bug
(Reviewed→Pushed, then Pushed→Blocked one second later)."""
from website.models import FeatureChangelog
from tests.test_website import (  # noqa: F401
    test_engine, db, client, AUTH, make_product, make_feature,
)


def _changes(db, feature_id, statuses):
    """Record a status-change row entering each status in `statuses`."""
    prev = "Approved"
    for s in statuses:
        db.add(FeatureChangelog(feature_id=feature_id, field="status",
                                old_value=prev, new_value=s, changed_by="agent"))
        prev = s
    db.flush()


def _flap(db, feature_id, n, status="Implementing"):
    _changes(db, feature_id, [status] * n)   # re-enters the same status n times


def _q(client, pid):
    return client.get(
        f"/api/products/{pid}/flapping-features?window_hours=1&min_transitions=5&min_revisits=3",
        auth=AUTH,
    ).json()


class TestFlappingDetection:
    def test_oscillation_is_flagged(self, client, db):
        prod = make_product(db, "/projects/flap-osc")
        f = make_feature(db, prod.id, name="loop", status="Implementing")
        _flap(db, f.id, 6)                       # entered Implementing 6×
        ids = {r["feature_id"] for r in _q(client, prod.id)}
        assert f.id in ids

    def test_forward_progression_not_flagged(self, client, db):
        """7 DISTINCT statuses (a clean pipeline run) — each entered once, so
        max_revisits=1 < 3 — must NOT be flagged despite > min_transitions."""
        prod = make_product(db, "/projects/flap-fwd")
        f = make_feature(db, prod.id, name="progress", status="Reviewing")
        _changes(db, f.id, ["Designing", "Designed", "Implementing",
                            "Implemented", "Reviewing", "Reviewed", "Designed"])
        ids = {r["feature_id"] for r in _q(client, prod.id)}
        assert f.id not in ids                   # one re-entry ("Designed"×2) < 3

    def test_terminal_features_excluded(self, client, db):
        prod = make_product(db, "/projects/flap-term")
        inflight = make_feature(db, prod.id, name="inflight", status="Reviewing")
        shipped = make_feature(db, prod.id, name="shipped", status="Pushed")
        blocked = make_feature(db, prod.id, name="blocked", status="Blocked")
        for f in (inflight, shipped, blocked):
            _flap(db, f.id, 6)                   # all oscillating
        ids = {r["feature_id"] for r in _q(client, prod.id)}
        assert inflight.id in ids                # non-terminal oscillation → flagged
        assert shipped.id not in ids             # Pushed → never flagged (#1421)
        assert blocked.id not in ids             # already terminal
