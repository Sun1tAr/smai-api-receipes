import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from receipes.database import Base
from receipes.domain import NAME_MAX_LENGTH, UNIT_MAX_LENGTH

STATUSES = ("pending", "in_progress", "done", "failed")
# Всё, что хранится как число: калории, КБЖУ, количества, время, бюджет.
Amount = Numeric(10, 2)


def uuid_pk() -> Mapped[uuid.UUID]:
    # Идентификаторы создаёт приложение, а не БД.
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


# Все отношения «lazy=raise»: в async-коде связанные данные загружаются явно.
class PromptTemplate(Base):
    __tablename__ = "prompt_template"

    id: Mapped[uuid.UUID] = uuid_pk()
    template: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GenerationParametersRecord(Base):
    __tablename__ = "generation_parameters"
    __table_args__ = (
        CheckConstraint("people_count >= 1", name="people_count_positive"),
        CheckConstraint("weekly_budget > 0", name="weekly_budget_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    people_count: Mapped[int] = mapped_column(Integer)
    weekly_budget: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Menu(Base):
    __tablename__ = "menu"

    id: Mapped[uuid.UUID] = uuid_pk()

    # Удаление меню каскадно удаляет строки menu_recipes (на уровне БД).
    menu_recipes: Mapped[list["MenuRecipe"]] = relationship(
        order_by="MenuRecipe.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class GenerationTask(Base):
    __tablename__ = "generation_task"
    __table_args__ = (
        UniqueConstraint("parameters_id", name="uq_generation_task_parameters_id"),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'done') = (menu_id IS NOT NULL)", name="menu_iff_done"
        ),
        CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)", name="error_iff_failed"
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'done') OR prompt IS NOT NULL",
            name="prompt_when_started",
        ),
        CheckConstraint(
            "status NOT IN ('in_progress', 'done') OR started_at IS NOT NULL",
            name="started_at_when_started",
        ),
        CheckConstraint(
            "status NOT IN ('done', 'failed') OR finished_at IS NOT NULL",
            name="finished_at_when_finished",
        ),
        Index(
            "ix_generation_task_created_at_pending",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_generation_task_started_at_in_progress",
            "started_at",
            postgresql_where=text("status = 'in_progress'"),
        ),
        Index("ix_generation_task_menu_id", "menu_id"),
    )

    task_id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[str] = mapped_column(String(16))
    prompt: Mapped[str | None] = mapped_column(Text)
    parameters_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_parameters.id", ondelete="RESTRICT")
    )
    menu_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("menu.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    parameters: Mapped[GenerationParametersRecord] = relationship(lazy="raise")


class Nutrition(Base):
    __tablename__ = "nutrition"
    __table_args__ = (
        CheckConstraint(
            "calories >= 0 AND proteins >= 0 AND fats >= 0 AND carbohydrates >= 0",
            name="values_non_negative",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    calories: Mapped[Decimal] = mapped_column(Amount)
    proteins: Mapped[Decimal] = mapped_column(Amount)
    fats: Mapped[Decimal] = mapped_column(Amount)
    carbohydrates: Mapped[Decimal] = mapped_column(Amount)


class Recipe(Base):
    __tablename__ = "recipe"
    __table_args__ = (
        CheckConstraint(
            "calories >= 0 AND cooking_time >= 0", name="values_non_negative"
        ),
        Index("ix_recipe_nutrition_id", "nutrition_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH))
    calories: Mapped[Decimal] = mapped_column(Amount)
    # Минуты, как number в контракте API.
    cooking_time: Mapped[Decimal] = mapped_column(Amount)
    photo: Mapped[str | None] = mapped_column(Text)
    nutrition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nutrition.id", ondelete="RESTRICT")
    )

    nutrition: Mapped[Nutrition] = relationship(lazy="raise")
    # Ингредиенты и шаги принадлежат рецепту и удаляются каскадом (в БД).
    recipe_ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        order_by="RecipeIngredient.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    instructions: Mapped[list["Instruction"]] = relationship(
        order_by="Instruction.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class Ingredient(Base):
    __tablename__ = "ingredient"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Хранится в нормализованном виде (см. domain.normalize_ingredient_name).
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH))


# Уникальность по нормализованному имени; вставка использует то же выражение.
INGREDIENT_NAME_KEY = func.lower(func.btrim(Ingredient.name))
Index("uq_ingredient_name_normalized", INGREDIENT_NAME_KEY, unique=True)


class MenuRecipe(Base):
    __tablename__ = "menu_recipes"
    __table_args__ = (
        UniqueConstraint(
            "menu_id", "recipe_id", name="uq_menu_recipes_menu_id_recipe_id"
        ),
        UniqueConstraint(
            "menu_id", "position", name="uq_menu_recipes_menu_id_position"
        ),
        CheckConstraint("position >= 1", name="position_positive"),
        Index("ix_menu_recipes_recipe_id", "recipe_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    menu_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("menu.id", ondelete="CASCADE")
    )
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipe.id", ondelete="RESTRICT")
    )
    position: Mapped[int] = mapped_column(Integer)

    recipe: Mapped[Recipe] = relationship(lazy="raise")


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"
    __table_args__ = (
        UniqueConstraint(
            "recipe_id",
            "ingredient_id",
            name="uq_recipe_ingredients_recipe_id_ingredient_id",
        ),
        UniqueConstraint(
            "recipe_id", "position", name="uq_recipe_ingredients_recipe_id_position"
        ),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("position >= 1", name="position_positive"),
        Index("ix_recipe_ingredients_ingredient_id", "ingredient_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE")
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingredient.id", ondelete="RESTRICT")
    )
    quantity: Mapped[Decimal] = mapped_column(Amount)
    unit: Mapped[str] = mapped_column(String(UNIT_MAX_LENGTH))
    position: Mapped[int] = mapped_column(Integer)

    ingredient: Mapped[Ingredient] = relationship(lazy="raise")


class Instruction(Base):
    __tablename__ = "instruction"
    __table_args__ = (
        UniqueConstraint(
            "recipe_id", "position", name="uq_instruction_recipe_id_position"
        ),
        CheckConstraint("position >= 1", name="position_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE")
    )
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)
