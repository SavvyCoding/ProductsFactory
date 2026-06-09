"""api_flapping_features must exclude terminal-state features so rapid_flap
can't Block a feature AFTER it successfully shipped (testingcalc #1421:
Reviewed→Pushed, then Pushed→Blocked by rapid_flap one second later because the
normal pipeline progression + a rollback summed to >= min_transitions)."""
from website.models import FeatureChangelog
from tests.test_website import (  # noqa: F401
    test_engine, db, client, AUTH, make_product, make_feature,
)


def _flap(db, feature_id, n):
    for _ in range(n):
        db.add(FeatureChangelog(feature_id=feature_id, field="status",
                                old_value="A", new_value="B", changed_by="agent"))
    db.flush()


class TestFlappingExclusion:
    def test_terminal_features_excluded(self, client, db):
        prod = make_product(db, "/projects/flap")
        inflight = make_feature(db, prod.id, name="inflight", status="Reviewing")
        shipped = make_feature(db, prod.id, name="shipped", status="Pushed")
        blocked = make_feature(db, prod.id, name="blocked", status="Blocked")
        for f in (inflight, shipped, blocked):
            _flap(db, f.id, 6)   # each well over the default min_transitions=5

        r = client.get(
            f"/api/products/{prod.id}/flapping-features?window_hours=1&min_transitions=5",
            auth=AUTH,
        )
        assert r.status_code == 200
        ids = {row["feature_id"] for row in r.json()}
        assert inflight.id in ids          # genuinely in-flight + flapping → flagged
        assert shipped.id not in ids       # Pushed (success) → NOT flagged/Block-able
        assert blocked.id not in ids       # already terminal → not re-routed
