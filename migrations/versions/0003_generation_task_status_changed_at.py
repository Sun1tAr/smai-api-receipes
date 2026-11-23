"""Время последней смены статуса задачи генерации (SMAI-95)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_task_status_changed_at"
down_revision: str | None = "0002_seed_prompt_template"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_generation_task_status_status_changed_at"


# Колонка добавляется пустой, заполняется created_at и только потом становится
# обязательной; DEFAULT now() ставится в конце, чтобы он не затёр заполнение.
def upgrade() -> None:
    op.add_column(
        "generation_task",
        sa.Column(
            "status_changed_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
    )
    op.execute("UPDATE generation_task SET status_changed_at = created_at")
    op.alter_column("generation_task", "status_changed_at", nullable=False)
    op.alter_column(
        "generation_task", "status_changed_at", server_default=sa.text("now()")
    )
    op.create_index(
        INDEX, "generation_task", ["status", "status_changed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="generation_task")
    op.drop_column("generation_task", "status_changed_at")
