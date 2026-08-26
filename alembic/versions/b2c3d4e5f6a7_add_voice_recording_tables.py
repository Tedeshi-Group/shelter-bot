"""add voice recording tables

Revision ID: b2c3d4e5f6a7
Revises: 33d567f0080c
Create Date: 2026-07-04 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = '33d567f0080c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, 'voice_recordings'):
        op.create_table('voice_recordings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('channel_id', sa.BigInteger(), nullable=False),
            sa.Column('channel_name', sa.String(length=255), nullable=False),
            sa.Column('worker_bot_id', sa.Integer(), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('ended_at', sa.DateTime(), nullable=True),
            sa.Column('duration_seconds', sa.Integer(), nullable=True),
            sa.Column('s3_key', sa.String(length=512), nullable=True),
            sa.Column('file_size_bytes', sa.BigInteger(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='recording'),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )

    if not _table_exists(conn, 'voice_recording_participants'):
        op.create_table('voice_recording_participants',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('recording_id', sa.Integer(), nullable=False),
            sa.Column('user_discord_id', sa.BigInteger(), nullable=False),
            sa.Column('username', sa.String(length=255), nullable=False),
            sa.Column('joined_at', sa.DateTime(), nullable=False),
            sa.Column('left_at', sa.DateTime(), nullable=True),
            sa.Column('duration_seconds', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['recording_id'], ['voice_recordings.id'], ),
            sa.PrimaryKeyConstraint('id')
        )

    if not _table_exists(conn, 'voice_recording_queue'):
        op.create_table('voice_recording_queue',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('channel_id', sa.BigInteger(), nullable=False),
            sa.Column('channel_name', sa.String(length=255), nullable=False),
            sa.Column('queued_at', sa.DateTime(), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='waiting'),
            sa.Column('assigned_worker_id', sa.Integer(), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )


def downgrade() -> None:
    op.drop_table('voice_recording_queue')
    op.drop_table('voice_recording_participants')
    op.drop_table('voice_recordings')
