"""Ручной smoke против реальной Ollama (в CI не запускается).

Собирает промпт из шаблона, который кладёт seed-миграция 0003, отправляет его в
POST /api/generate со структурной схемой format и проверяет ответ «воротами»
GeneratedMenu. Нужна работающая Ollama с загруженной моделью:

    uv run python -m scripts.ollama_smoke --people 2 --budget 5000

Адрес и модель берутся из OLLAMA_BASE_URL и OLLAMA_MODEL (или флагов).
"""

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from receipes.config import Settings
from receipes.domain import DomainError, GenerationParameters
from receipes.generated_menu import GeneratedMenu
from receipes.ollama import ResponseTruncated, parse_response
from receipes.ollama import build_request as ollama_request
from receipes.prompt import build_prompt

SEED_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0003_seed_prompt_template.py"
)
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = Settings.model_fields["ollama_model"].default


def load_seed_template(path: Path = SEED_MIGRATION) -> str:
    spec = importlib.util.spec_from_file_location("seed_prompt_template", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    template = module.TEMPLATE
    if not isinstance(template, str):
        raise RuntimeError("the seed migration has no TEMPLATE string")
    return template


def build_request(prompt: str, model: str) -> dict[str, Any]:
    fields = Settings.model_fields
    return ollama_request(
        prompt,
        model,
        num_ctx=fields["ollama_num_ctx"].default,
        num_predict=fields["ollama_num_predict"].default,
        temperature=fields["ollama_temperature"].default,
        keep_alive=fields["ollama_keep_alive"].default,
    )


def run(
    base_url: str,
    model: str,
    parameters: GenerationParameters,
    timeout: float,
    client: httpx.Client | None = None,
) -> GeneratedMenu:
    prompt = build_prompt(load_seed_template(), parameters)
    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.post(
            f"{base_url.rstrip('/')}/api/generate", json=build_request(prompt, model)
        )
        response.raise_for_status()
        return parse_response(response.json())
    finally:
        if owned:
            http.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url", default=os.getenv("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--people", type=int, default=2)
    parser.add_argument("--budget", default="5000")
    parser.add_argument(
        "--timeout",
        type=float,
        default=Settings.model_fields["ollama_timeout_seconds"].default,
    )
    args = parser.parse_args(argv)
    try:
        parameters = GenerationParameters(args.people, Decimal(args.budget))
        menu = run(args.base_url, args.model, parameters, args.timeout)
    except DomainError as error:
        print(f"Invalid input: {error}", file=sys.stderr)
        return 2
    except InvalidOperation:
        print("Invalid input: --budget must be a decimal number", file=sys.stderr)
        return 2
    except ResponseTruncated:
        print(
            "Response truncated: the answer hit the num_predict limit "
            "(done_reason=length); raise OLLAMA_NUM_PREDICT",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as error:
        print(f"Ollama request failed: {type(error).__name__}", file=sys.stderr)
        return 1
    except (ValidationError, json.JSONDecodeError, KeyError) as error:
        print(f"Response rejected by the gate: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"OK: {len(menu.recipes)} recipes")
    for recipe in menu.recipes:
        print(f"- {recipe.name} ({recipe.calories} kcal, {recipe.cooking_time} min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
