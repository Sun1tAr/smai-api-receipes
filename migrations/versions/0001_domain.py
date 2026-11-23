"""Домен MS-4: таблицы по ERD (SMAI-A-110)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_domain"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Применяем изменения этой ревизии схемы.
def upgrade() -> None:
    op.create_table(
        "generation_parameters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("people_count", sa.Integer(), nullable=False),
        sa.Column("weekly_budget", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_parameters")),
    )
    op.create_table(
        "ingredient",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingredient")),
    )
    op.create_table(
        "menu",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_menu")),
    )
    op.create_table(
        "nutrition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("calories", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("proteins", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("fats", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("carbohydrates", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nutrition")),
    )
    op.create_table(
        "prompt_template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_template")),
    )
    op.create_table(
        "generation_task",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("parameters_id", sa.Uuid(), nullable=False),
        sa.Column("menu_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["menu_id"], ["menu.id"], name=op.f("fk_generation_task_menu_id_menu")
        ),
        sa.ForeignKeyConstraint(
            ["parameters_id"],
            ["generation_parameters.id"],
            name=op.f("fk_generation_task_parameters_id_generation_parameters"),
        ),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_generation_task")),
    )
    op.create_index(
        "ix_generation_task_status_created_at",
        "generation_task",
        ["status", "created_at"],
        unique=False,
    )
    op.create_table(
        "recipe",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("calories", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("cooking_time", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("photo", sa.Text(), nullable=True),
        sa.Column("nutrition_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["nutrition_id"],
            ["nutrition.id"],
            name=op.f("fk_recipe_nutrition_id_nutrition"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe")),
    )
    op.create_table(
        "instruction",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipe_id"], ["recipe.id"], name=op.f("fk_instruction_recipe_id_recipe")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruction")),
    )
    op.create_index(
        "ix_instruction_recipe_id", "instruction", ["recipe_id"], unique=False
    )
    op.create_table(
        "menu_recipes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("menu_id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["menu_id"], ["menu.id"], name=op.f("fk_menu_recipes_menu_id_menu")
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"], ["recipe.id"], name=op.f("fk_menu_recipes_recipe_id_recipe")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_menu_recipes")),
    )
    op.create_index(
        "ix_menu_recipes_menu_id", "menu_recipes", ["menu_id"], unique=False
    )
    op.create_table(
        "recipe_ingredients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipe_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("unit", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredient.id"],
            name=op.f("fk_recipe_ingredients_ingredient_id_ingredient"),
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name=op.f("fk_recipe_ingredients_recipe_id_recipe"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe_ingredients")),
    )
    op.create_index(
        "ix_recipe_ingredients_recipe_id",
        "recipe_ingredients",
        ["recipe_id"],
        unique=False,
    )


# Откатываем изменения этой ревизии схемы.
def downgrade() -> None:
    op.drop_index("ix_recipe_ingredients_recipe_id", table_name="recipe_ingredients")
    op.drop_table("recipe_ingredients")
    op.drop_index("ix_menu_recipes_menu_id", table_name="menu_recipes")
    op.drop_table("menu_recipes")
    op.drop_index("ix_instruction_recipe_id", table_name="instruction")
    op.drop_table("instruction")
    op.drop_table("recipe")
    op.drop_index("ix_generation_task_status_created_at", table_name="generation_task")
    op.drop_table("generation_task")
    op.drop_table("prompt_template")
    op.drop_table("nutrition")
    op.drop_table("menu")
    op.drop_table("ingredient")
    op.drop_table("generation_parameters")
