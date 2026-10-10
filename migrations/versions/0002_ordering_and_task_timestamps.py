"""Deviations from the ERD: ordering of menu recipes and ingredients, task timestamps.

Separate revision on purpose: if the lead rejects these deviations, this file is
removed without touching the base schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_ordering_task_times"
down_revision: str | None = "0001_base_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORDERED = (
    ("menu_recipes", "menu_id", "uq_menu_recipes_menu_id_position"),
    ("recipe_ingredients", "recipe_id", "uq_recipe_ingredients_recipe_id_position"),
)


# Применяем изменения этой ревизии схемы.
def upgrade() -> None:
    for table, parent, unique_name in ORDERED:
        # Существующие строки получают позиции 1..n по id, затем колонка обязательна.
        op.add_column(table, sa.Column("position", sa.Integer(), nullable=True))
        op.execute(
            f"UPDATE {table} AS t SET position = numbered.rn FROM ("
            f"SELECT id, row_number() OVER (PARTITION BY {parent} ORDER BY id) AS rn "
            f"FROM {table}) AS numbered WHERE t.id = numbered.id"
        )
        op.alter_column(table, "position", nullable=False)
        op.create_check_constraint(
            op.f(f"ck_{table}_position_positive"), table, "position >= 1"
        )
        op.create_unique_constraint(unique_name, table, [parent, "position"])

    op.add_column(
        "generation_task",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "generation_task",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Задачи, созданные до ревизии, получают created_at как приблизительные границы.
    op.execute(
        "UPDATE generation_task SET started_at = created_at "
        "WHERE status IN ('in_progress', 'done')"
    )
    op.execute(
        "UPDATE generation_task SET finished_at = created_at "
        "WHERE status IN ('done', 'failed')"
    )
    op.create_check_constraint(
        op.f("ck_generation_task_started_at_when_started"),
        "generation_task",
        "status NOT IN ('in_progress', 'done') OR started_at IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_generation_task_finished_at_when_finished"),
        "generation_task",
        "status NOT IN ('done', 'failed') OR finished_at IS NOT NULL",
    )
    op.create_index(
        "ix_generation_task_started_at_in_progress",
        "generation_task",
        ["started_at"],
        postgresql_where=sa.text("status = 'in_progress'"),
    )


# Откатываем изменения этой ревизии схемы.
def downgrade() -> None:
    op.drop_index(
        "ix_generation_task_started_at_in_progress",
        table_name="generation_task",
        postgresql_where=sa.text("status = 'in_progress'"),
    )
    op.drop_constraint(
        op.f("ck_generation_task_finished_at_when_finished"),
        "generation_task",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_generation_task_started_at_when_started"),
        "generation_task",
        type_="check",
    )
    op.drop_column("generation_task", "finished_at")
    op.drop_column("generation_task", "started_at")
    for table, _parent, unique_name in reversed(ORDERED):
        op.drop_constraint(unique_name, table, type_="unique")
        op.drop_constraint(op.f(f"ck_{table}_position_positive"), table, type_="check")
        op.drop_column(table, "position")
