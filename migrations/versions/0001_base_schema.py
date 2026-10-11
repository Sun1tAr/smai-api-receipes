"""Base schema of MS-4: the ERD (SMAI-A-110) plus integrity constraints.

Columns and tables are exactly those of the ERD. On top of them: CHECK, UNIQUE and
partial/foreign-key indexes that add no columns. Deviations from the ERD that add
columns live in revision 0002.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_base_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID_PK = sa.Uuid()


def now() -> sa.TextClause:
    return sa.text("now()")


# Применяем изменения этой ревизии схемы.
def upgrade() -> None:
    op.create_table(
        "prompt_template",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_template")),
    )
    op.create_table(
        "generation_parameters",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("people_count", sa.Integer(), nullable=False),
        sa.Column("weekly_budget", sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint(
            "people_count BETWEEN 1 AND 20",
            name=op.f("ck_generation_parameters_people_count_range"),
        ),
        sa.CheckConstraint(
            "weekly_budget > 0 AND weekly_budget <= 1000000",
            name=op.f("ck_generation_parameters_weekly_budget_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_parameters")),
    )
    op.create_table(
        "menu",
        sa.Column("id", UUID_PK, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_menu")),
    )
    op.create_table(
        "nutrition",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("calories", sa.Numeric(10, 2), nullable=False),
        sa.Column("proteins", sa.Numeric(10, 2), nullable=False),
        sa.Column("fats", sa.Numeric(10, 2), nullable=False),
        sa.Column("carbohydrates", sa.Numeric(10, 2), nullable=False),
        sa.CheckConstraint(
            "calories >= 0 AND proteins >= 0 AND fats >= 0 AND carbohydrates >= 0",
            name=op.f("ck_nutrition_values_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nutrition")),
    )
    op.create_table(
        "ingredient",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingredient")),
    )
    op.create_table(
        "generation_task",
        sa.Column("task_id", UUID_PK, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("parameters_id", UUID_PK, nullable=False),
        sa.Column("menu_id", UUID_PK, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=now(),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'failed')",
            name=op.f("ck_generation_task_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'done') = (menu_id IS NOT NULL)",
            name=op.f("ck_generation_task_menu_iff_done"),
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)",
            name=op.f("ck_generation_task_error_iff_failed"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('in_progress', 'done') OR prompt IS NOT NULL",
            name=op.f("ck_generation_task_prompt_when_started"),
        ),
        sa.ForeignKeyConstraint(
            ["parameters_id"],
            ["generation_parameters.id"],
            name=op.f("fk_generation_task_parameters_id_generation_parameters"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["menu_id"],
            ["menu.id"],
            name=op.f("fk_generation_task_menu_id_menu"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_generation_task")),
        sa.UniqueConstraint("parameters_id", name="uq_generation_task_parameters_id"),
    )
    op.create_index(
        "ix_generation_task_created_at_pending",
        "generation_task",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_generation_task_menu_id", "generation_task", ["menu_id"])
    op.create_table(
        "recipe",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("calories", sa.Numeric(10, 2), nullable=False),
        sa.Column("cooking_time", sa.Numeric(10, 2), nullable=False),
        sa.Column("photo", sa.Text(), nullable=True),
        sa.Column("nutrition_id", UUID_PK, nullable=False),
        sa.CheckConstraint(
            "calories >= 0 AND cooking_time >= 0",
            name=op.f("ck_recipe_values_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["nutrition_id"],
            ["nutrition.id"],
            name=op.f("fk_recipe_nutrition_id_nutrition"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe")),
    )
    op.create_index("ix_recipe_nutrition_id", "recipe", ["nutrition_id"])
    op.create_table(
        "menu_recipes",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("menu_id", UUID_PK, nullable=False),
        sa.Column("recipe_id", UUID_PK, nullable=False),
        sa.ForeignKeyConstraint(
            ["menu_id"],
            ["menu.id"],
            name=op.f("fk_menu_recipes_menu_id_menu"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_menu_recipes_recipe_id_recipe"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_menu_recipes")),
        sa.UniqueConstraint(
            "menu_id", "recipe_id", name="uq_menu_recipes_menu_id_recipe_id"
        ),
    )
    op.create_index("ix_menu_recipes_recipe_id", "menu_recipes", ["recipe_id"])
    op.create_table(
        "recipe_ingredients",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("recipe_id", UUID_PK, nullable=False),
        sa.Column("ingredient_id", UUID_PK, nullable=False),
        sa.Column("quantity", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "quantity >= 0", name=op.f("ck_recipe_ingredients_quantity_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_recipe_ingredients_recipe_id_recipe"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredient.id"],
            name=op.f("fk_recipe_ingredients_ingredient_id_ingredient"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe_ingredients")),
        sa.UniqueConstraint(
            "recipe_id",
            "ingredient_id",
            name="uq_recipe_ingredients_recipe_id_ingredient_id",
        ),
    )
    op.create_index(
        "ix_recipe_ingredients_ingredient_id", "recipe_ingredients", ["ingredient_id"]
    )
    op.create_table(
        "instruction",
        sa.Column("id", UUID_PK, nullable=False),
        sa.Column("recipe_id", UUID_PK, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "position >= 1", name=op.f("ck_instruction_position_positive")
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_instruction_recipe_id_recipe"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruction")),
        sa.UniqueConstraint(
            "recipe_id", "position", name="uq_instruction_recipe_id_position"
        ),
    )


# Откатываем изменения этой ревизии схемы.
def downgrade() -> None:
    # Порядок обратный зависимостям внешних ключей.
    op.drop_table("instruction")
    op.drop_index(
        "ix_recipe_ingredients_ingredient_id", table_name="recipe_ingredients"
    )
    op.drop_table("recipe_ingredients")
    op.drop_index("ix_menu_recipes_recipe_id", table_name="menu_recipes")
    op.drop_table("menu_recipes")
    op.drop_index("ix_recipe_nutrition_id", table_name="recipe")
    op.drop_table("recipe")
    op.drop_index("ix_generation_task_menu_id", table_name="generation_task")
    op.drop_index(
        "ix_generation_task_created_at_pending",
        table_name="generation_task",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("generation_task")
    op.drop_table("ingredient")
    op.drop_table("nutrition")
    op.drop_table("menu")
    op.drop_table("generation_parameters")
    op.drop_table("prompt_template")
