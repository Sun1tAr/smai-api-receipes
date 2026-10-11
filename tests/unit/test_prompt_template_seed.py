import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from receipes.domain import GenerationParameters
from receipes.generated_menu import ollama_format_schema
from receipes.prompt import PLACEHOLDERS, build_prompt

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions"


def seed_module() -> ModuleType:
    path = MIGRATION / "0003_seed_prompt_template.py"
    if not path.exists():
        pytest.skip("migrations are not part of the test image")
    spec = importlib.util.spec_from_file_location("seed_0003", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seeded_template() -> str:
    template = seed_module().TEMPLATE
    assert isinstance(template, str)
    return template


def test_seed_revision_follows_the_erd_deviations() -> None:
    module = seed_module()
    assert module.revision == "0003_seed_prompt_template"
    assert module.down_revision == "0002_erd_deviations"


def test_seeded_template_has_each_placeholder_and_builds() -> None:
    template = seeded_template()
    for placeholder in PLACEHOLDERS:
        assert placeholder in template
    prompt = build_prompt(template, GenerationParameters(4, Decimal("7000")))
    assert "4 чел." in prompt and "7000.00 руб." in prompt
    assert "{people_count}" not in prompt and "{weekly_budget}" not in prompt


def test_seeded_template_asks_for_strict_json_without_photo() -> None:
    template = seeded_template()
    assert "строго один JSON-объект" in template
    assert "не менее 3" in template and "camelCase" in template
    assert "ссылки на фотографии" in template  # просим НЕ добавлять photo
    assert '"photo"' not in template


def test_template_example_matches_the_ollama_schema() -> None:
    template = seeded_template()
    example = json.loads(template[template.index('{\n  "recipes"') :])
    schema: dict[str, Any] = dict(ollama_format_schema())
    recipe_schema = schema["properties"]["recipes"]["items"]
    recipe = example["recipes"][0]
    assert set(recipe) == set(recipe_schema["properties"])
    assert set(recipe["nutrition"]) == set(
        recipe_schema["properties"]["nutrition"]["properties"]
    )
    assert set(recipe["ingredients"][0]) == set(
        recipe_schema["properties"]["ingredients"]["items"]["properties"]
    )
