import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from receipes.domain import GenerationParameters
from receipes.generated_menu import GeneratedMenu
from tests.unit.test_generated_menu import payload

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def smoke():
    # В test-образе нет scripts/ и migrations/: проверка идёт в CI (unit-джоба).
    if not (ROOT / "scripts/ollama_smoke.py").exists():
        pytest.skip("scripts are not part of the test image")
    from scripts import ollama_smoke

    return ollama_smoke


def ollama(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_uses_the_format_schema_and_the_seed_prompt(smoke) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"response": json.dumps(payload())})

    menu = smoke.run(
        "http://ollama.test:11434/",
        "qwen2.5:7b-instruct-q4_K_M",
        GenerationParameters(3, Decimal("4200")),
        5,
        client=ollama(handler),
    )
    assert isinstance(menu, GeneratedMenu) and len(menu.recipes) == 3
    request = seen[0]
    assert str(request.url) == "http://ollama.test:11434/api/generate"
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:7b-instruct-q4_K_M"
    assert body["stream"] is False
    assert body["format"]["required"] == ["recipes"]
    assert "3 чел." in body["prompt"] and "4200.00 руб." in body["prompt"]
    assert {"temperature", "num_ctx", "num_predict"} <= set(body["options"])


def test_response_outside_the_gate_is_rejected(smoke) -> None:
    from pydantic import ValidationError

    bad = payload()
    bad["recipes"] = bad["recipes"][:2]
    with pytest.raises(ValidationError):
        smoke.parse_response({"response": json.dumps(bad)})


def test_main_reports_success_and_failures(smoke, monkeypatch, capsys) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": json.dumps(payload())})

    real_client = httpx.Client
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(ok)),
    )
    assert smoke.main(["--base-url", "http://ollama.test", "--people", "2"]) == 0
    assert "OK: 3 recipes" in capsys.readouterr().out
    assert smoke.main(["--people", "99"]) == 2
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ),
    )
    assert smoke.main(["--base-url", "http://ollama.test"]) == 1
    assert "Ollama request failed" in capsys.readouterr().err
