"""Виды задач и шаблонов, ссылки для замены рецепта (SMAI-120)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_recipe_replacement"
down_revision: str | None = "0005_parameters_user_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATE_ID = "b115d84a-e70e-4514-96bb-fca6ed4553b8"
TEMPLATE = """\
Ты шеф-повар и диетолог. Предложи ровно один новый рецепт для {people_count} человек.
Общий недельный бюджет на продукты: {weekly_budget} рублей.
Блюдо на замену: {replaced_recipe}.
Блюда в меню: {existing_recipes}.
Выбери рецепт с похожим назначением. Его название должно отличаться от всех
названий в меню, включая название блюда на замену.

Укажи количество каждого ингредиента на всех людей, калории и пищевую
ценность на одну порцию, время приготовления в минутах. Количество каждого
ингредиента больше нуля. Перечисли каждый ингредиент один раз, без количества
в названии. Дай конкретные шаги приготовления без нумерации внутри текста.
Названия, ингредиенты и шаги напиши по-русски. Поле photo всегда null.

Верни один JSON-объект строго по схеме: массив recipes содержит ровно один
рецепт, числовые поля содержат числа. Не добавляй текст вне JSON."""


def upgrade() -> None:
    for table in ("generation_task", "prompt_template"):
        op.add_column(
            table,
            sa.Column("kind", sa.String(16), nullable=False, server_default="menu"),
        )
    for column, target in (
        ("source_task_id", "generation_task.task_id"),
        ("replaced_recipe_id", "recipe.id"),
        ("result_recipe_id", "recipe.id"),
    ):
        op.add_column(
            "generation_task", sa.Column(column, sa.Uuid(), sa.ForeignKey(target))
        )
    op.execute(
        sa.text(
            "INSERT INTO prompt_template (id, kind, template, created_at) "
            "VALUES (CAST(:id AS uuid), 'replace', :template, now())"
        ).bindparams(id=TEMPLATE_ID, template=TEMPLATE)
    )


def downgrade() -> None:
    # Проверка и удаление колонок должны видеть один и тот же набор записей.
    op.execute("LOCK TABLE generation_task, prompt_template IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM generation_task WHERE kind <> 'menu'
                    OR source_task_id IS NOT NULL OR replaced_recipe_id IS NOT NULL
                    OR result_recipe_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM prompt_template
                WHERE kind <> 'menu' AND id <> '{TEMPLATE_ID}'
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while replacement data exists'
                    USING ERRCODE = '42P16';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        sa.text("DELETE FROM prompt_template WHERE id = CAST(:id AS uuid)").bindparams(
            id=TEMPLATE_ID
        )
    )
    for column in ("result_recipe_id", "replaced_recipe_id", "source_task_id", "kind"):
        op.drop_column("generation_task", column)
    op.drop_column("prompt_template", "kind")
