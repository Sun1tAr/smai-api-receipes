"""Параметры генерации v2: диета и связи привычек, аллергенов, техники (SMAI-88).

Только добавление. У задач, созданных до миграции, `diet_id` пуст и связей нет,
что и означает «ничего не выбрано», поэтому данные не переносятся. Идентификаторы
справочников MS-2 хранятся числами без внешних ключей. Откат удаляет сохранённые
параметры v2.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_generation_parameters_v2"
down_revision: str | None = "0003_task_status_changed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (таблица, колонка идентификатора справочника, сокращение для имени FK)
LINKS = (
    ("generation_parameter_habits", "habit_id", "habits"),
    ("generation_parameter_allergies", "allergen_id", "allergies"),
    ("generation_parameter_kitchen_tools", "kitchen_tool_id", "kitchen_tools"),
)


def upgrade() -> None:
    op.add_column(
        "generation_parameters", sa.Column("diet_id", sa.Integer(), nullable=True)
    )
    for table, column, short in LINKS:
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("parameters_id", sa.Uuid(), nullable=False),
            sa.Column(column, sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["parameters_id"],
                ["generation_parameters.id"],
                name=f"fk_gen_param_{short}_parameters_id",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        )
        op.create_index(
            f"ix_{table}_parameters_id", table, ["parameters_id"], unique=False
        )


def downgrade() -> None:
    for table, _column, _short in reversed(LINKS):
        op.drop_index(f"ix_{table}_parameters_id", table_name=table)
        op.drop_table(table)
    op.drop_column("generation_parameters", "diet_id")
