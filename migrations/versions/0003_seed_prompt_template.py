"""Seed the first prompt template for menu generation (SMAI-56)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_seed_prompt_template"
down_revision: str | None = "0002_erd_deviations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Фиксированный id: ревизия откатывается удалением именно этой строки. Новые версии
# шаблона добавляются новыми ревизиями (latest() берёт самую свежую), эту не меняют.
TEMPLATE_ID = "6f3b1c0e-5d2a-4f7e-9a41-0c8b7d2e1a10"

# Плейсхолдеры {people_count} и {weekly_budget} подставляет
# receipes.prompt.build_prompt; фигурные скобки примера JSON остаются как есть.
TEMPLATE = """\
Ты — профессиональный диетолог и повар. Составь разнообразное и вкусное меню на \
неделю для семьи из {people_count} чел. Недельный бюджет на продукты: \
{weekly_budget} руб.

Требования:
- Предложи не менее 3 и не более 21 рецепта (завтраки, обеды, ужины).
- Рецепты должны быть разными, из доступных продуктов, и вместе укладываться в \
бюджет.
- Для каждого рецепта укажи: название, ингредиенты с количеством и единицей \
измерения (г, мл, шт, ст. л. и т. п.), калорийность одной порции в ккал, \
пищевую ценность одной порции (калории, белки, жиры, углеводы в граммах), время \
приготовления в минутах и пошаговую инструкцию.
- Не повторяй один и тот же ингредиент внутри одного рецепта.
- Не добавляй ссылки на фотографии и любые другие поля, кроме перечисленных.
- Все числа — неотрицательные десятичные числа.

Ответ — строго один JSON-объект без пояснений, комментариев и markdown-разметки. \
Ключи — на английском в camelCase, значения — на русском. Формат:
{
  "recipes": [
    {
      "name": "строка",
      "ingredients": [{"name": "строка", "quantity": 0.0, "unit": "строка"}],
      "calories": 0.0,
      "nutrition": {
        "calories": 0.0, "proteins": 0.0, "fats": 0.0, "carbohydrates": 0.0
      },
      "cookingTime": 0.0,
      "instructions": ["строка"]
    }
  ]
}
"""


# Применяем изменения этой ревизии схемы.
def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "INSERT INTO prompt_template (id, template) VALUES (:id, :template) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": TEMPLATE_ID, "template": TEMPLATE},
    )


# Откатываем изменения этой ревизии схемы.
def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM prompt_template WHERE id = :id"), {"id": TEMPLATE_ID}
    )
