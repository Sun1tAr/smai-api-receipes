"""Владелец параметров генерации: `user_id` (SMAI-102).

Только добавление. Колонка допускает NULL: у задач, созданных до миграции, и у
гостевых запросов владельца нет. Внешнего ключа нет, пользователи живут в другой
БД (MS-3). Откат удаляет индекс и колонку вместе с сохранёнными владельцами.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_parameters_user_id"
down_revision: str | None = "0004_generation_parameters_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_parameters", sa.Column("user_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_generation_parameters_user_id",
        "generation_parameters",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generation_parameters_user_id", table_name="generation_parameters"
    )
    op.drop_column("generation_parameters", "user_id")
