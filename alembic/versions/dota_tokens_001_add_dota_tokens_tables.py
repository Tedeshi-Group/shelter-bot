"""add dota tokens tables and friendship_points

Revision ID: dota_tokens_001
Revises: 666c30da3ff2
Create Date: 2026-08-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "dota_tokens_001"
down_revision: Union[str, None] = "33d567f0080c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add friendship_points to users
    op.add_column("users", sa.Column("friendship_points", sa.Integer(), nullable=False, server_default="0"))

    # Create dota_tokens table
    op.create_table(
        "dota_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("emoji", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # Create token_requests table
    op.create_table(
        "token_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requester_id", sa.BigInteger(), sa.ForeignKey("users.discord_id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )

    # Create token_request_items table
    op.create_table(
        "token_request_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("token_requests.id"), nullable=False),
        sa.Column("token_id", sa.Integer(), sa.ForeignKey("dota_tokens.id"), nullable=False),
    )

    # Create token_fulfillments table
    op.create_table(
        "token_fulfillments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("token_requests.id"), unique=True, nullable=False),
        sa.Column("fulfiller_id", sa.BigInteger(), sa.ForeignKey("users.discord_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("token_fulfillments")
    op.drop_table("token_request_items")
    op.drop_table("token_requests")
    op.drop_table("dota_tokens")
    op.drop_column("users", "friendship_points")
