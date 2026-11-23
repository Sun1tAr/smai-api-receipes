"""Лайки рецептов пользователя (SMAI-121)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_recipe_like"
down_revision: str | None = "0006_recipe_replacement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recipe_like",
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column("recipe_id", sa.Uuid(), sa.ForeignKey("recipe.id"), primary_key=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_recipe_like_user_id_created_at", "recipe_like", ["user_id", "created_at"]
    )


def downgrade() -> None:
    # Пока проверяем таблицу и удаляем её, новые лайки не могут появиться.
    op.execute("LOCK TABLE recipe_like IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM recipe_like) THEN
                RAISE EXCEPTION 'Cannot downgrade while recipe likes exist'
                    USING ERRCODE = '42P16';
            END IF;
        END
        $$;
        """
    )
    op.drop_table("recipe_like")
