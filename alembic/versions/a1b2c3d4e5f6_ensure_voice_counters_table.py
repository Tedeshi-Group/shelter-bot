"""ensure_voice_counters_table

Revision ID: a1b2c3d4e5f6
Revises: edc5e49fb5df
Create Date: 2026-08-26 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'edc5e49fb5df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create voice_counters table if it doesn't exist."""
    op.execute(
        "CREATE TABLE IF NOT EXISTS voice_counters "
        "(id INTEGER PRIMARY KEY, month INTEGER NOT NULL, year INTEGER NOT NULL, count INTEGER NOT NULL)"
    )


def downgrade() -> None:
    """Drop voice_counters table."""
    op.drop_table('voice_counters')
