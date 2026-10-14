import copy
import json
from typing import Any

# Валидный ответ модели (формат API-3) и сборка ответа Ollama вокруг него.


def sample_recipe(name: str, *ingredients: str) -> dict[str, Any]:
    return {
        "name": name,
        "ingredients": [
            {"name": item, "quantity": 200, "unit": "г"}
            for item in (ingredients or ("Соль",))
        ],
        "calories": 350,
        "nutrition": {
            "calories": 350,
            "proteins": 20.5,
            "fats": 10,
            "carbohydrates": 40,
        },
        "cookingTime": 25,
        "instructions": [f"Подготовьте продукты для блюда {name}", "Варите 20 минут"],
        "photo": None,
    }


def sample_menu() -> dict[str, Any]:
    return copy.deepcopy(
        {
            "recipes": [
                sample_recipe("Овсяная каша", "Овсяные хлопья", "Молоко"),
                sample_recipe("Куриный суп", "Курица", "Картофель", "Вода"),
                sample_recipe("Запечённый лосось", "Лосось", "Лимон"),
            ]
        }
    )


def ollama_reply(
    menu: object | str,
    *,
    done_reason: str = "stop",
    prompt_eval_count: int | None = 820,
    eval_count: int | None = 900,
) -> dict[str, Any]:
    text = menu if isinstance(menu, str) else json.dumps(menu, ensure_ascii=False)
    body: dict[str, Any] = {
        "model": "qwen2.5:7b",
        "response": text,
        "done": True,
        "done_reason": done_reason,
    }
    if prompt_eval_count is not None:
        body["prompt_eval_count"] = prompt_eval_count
    if eval_count is not None:
        body["eval_count"] = eval_count
    return body
