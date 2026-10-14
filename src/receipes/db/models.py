from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from receipes.database import Base

# Схема строго по ERD MS-4 (SMAI-A-110): PK, FK и NOT NULL по ТЗ; CHECK и UNIQUE
# сверх ERD не добавляются (решение Sun1tAr), инварианты держит код.


def utcnow() -> datetime:
    return datetime.now(UTC)


class PromptTemplate(Base):
    __tablename__ = "prompt_template"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    template: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=utcnow
    )


class GenerationParameters(Base):
    __tablename__ = "generation_parameters"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    people_count: Mapped[int] = mapped_column(Integer)
    weekly_budget: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Nutrition(Base):
    __tablename__ = "nutrition"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    calories: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    proteins: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    fats: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    carbohydrates: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class Ingredient(Base):
    __tablename__ = "ingredient"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))


class Recipe(Base):
    __tablename__ = "recipe"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))
    calories: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    cooking_time: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    # Фото в v1 не реализуется: всегда NULL, колонка заложена по ERD.
    photo: Mapped[str | None] = mapped_column(Text)
    nutrition_id: Mapped[UUID] = mapped_column(ForeignKey("nutrition.id"))

    nutrition: Mapped[Nutrition] = relationship(lazy="raise")
    ingredients: Mapped[list["RecipeIngredient"]] = relationship(lazy="raise")
    instructions: Mapped[list["Instruction"]] = relationship(
        lazy="raise", order_by="Instruction.position"
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"
    __table_args__ = (Index("ix_recipe_ingredients_recipe_id", "recipe_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    recipe_id: Mapped[UUID] = mapped_column(ForeignKey("recipe.id"))
    ingredient_id: Mapped[UUID] = mapped_column(ForeignKey("ingredient.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    unit: Mapped[str] = mapped_column(String(200))

    ingredient: Mapped[Ingredient] = relationship(lazy="raise")


class Instruction(Base):
    __tablename__ = "instruction"
    __table_args__ = (Index("ix_instruction_recipe_id", "recipe_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    recipe_id: Mapped[UUID] = mapped_column(ForeignKey("recipe.id"))
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)


class Menu(Base):
    __tablename__ = "menu"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    menu_recipes: Mapped[list["MenuRecipe"]] = relationship(lazy="raise")


class MenuRecipe(Base):
    __tablename__ = "menu_recipes"
    __table_args__ = (Index("ix_menu_recipes_menu_id", "menu_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    menu_id: Mapped[UUID] = mapped_column(ForeignKey("menu.id"))
    recipe_id: Mapped[UUID] = mapped_column(ForeignKey("recipe.id"))

    recipe: Mapped[Recipe] = relationship(lazy="raise")


class GenerationTask(Base):
    __tablename__ = "generation_task"
    __table_args__ = (
        Index("ix_generation_task_status_created_at", "status", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    status: Mapped[str] = mapped_column(String(16))
    prompt: Mapped[str | None] = mapped_column(Text)
    parameters_id: Mapped[UUID] = mapped_column(ForeignKey("generation_parameters.id"))
    menu_id: Mapped[UUID | None] = mapped_column(ForeignKey("menu.id"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), default=utcnow
    )
    error: Mapped[str | None] = mapped_column(Text)

    parameters: Mapped[GenerationParameters] = relationship(lazy="raise")
    menu: Mapped[Menu | None] = relationship(lazy="raise")
