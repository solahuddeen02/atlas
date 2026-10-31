"""Test-only migration that always fails. Copied in by Dockerfile.failing-migration."""

revision = "0002_injected_failure"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raise RuntimeError("injected failure for test_failed_migration_blocks_api")


def downgrade() -> None:
    pass
