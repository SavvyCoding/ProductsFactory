"""widen ck_features_type: add 'infra' feature type (system-executed stories)

Revision ID: 046
Revises: 045
Create Date: 2026-06-12

Service provisioning (Phase B): a designer that needs a live service the
agent/test container can't provide (redis, postgres, ...) files a
dependency story with feature_type='infra'. The story flows through the
normal pipeline for VISIBILITY and APPROVAL (Pending → Approved is the
human checkpoint), but it is implemented by the ORCHESTRATOR
deterministically — look up the service in orchestrator/services.py's
SERVICE_CATALOG allowlist, write product.config.services, provision +
smoke-test, mark Pushed — never by a coder/designer session (agents have
no docker access by design; canonical incident DogTinder #1582, where a
coder vendored the entire Redis source tree to satisfy a live-Redis AC).

Both orchestrator selection points (cycle/persona._decide_action and
docker_runner._fetch_assigned_features) exclude feature_type='infra' from
the coder/designer pools.

Downgrade re-narrows the constraint; any existing infra rows must be
retyped or deleted first (the downgrade will fail loudly otherwise —
deliberate, so data isn't silently mangled).
"""
from alembic import op


revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_features_type", "features", type_="check")
    op.create_check_constraint(
        "ck_features_type",
        "features",
        "feature_type IN ('feature', 'bug', 'chore', 'infra')",
    )


def downgrade():
    op.drop_constraint("ck_features_type", "features", type_="check")
    op.create_check_constraint(
        "ck_features_type",
        "features",
        "feature_type IN ('feature', 'bug', 'chore')",
    )
