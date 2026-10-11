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


def test_truncated_response_is_reported_clearly(smoke, monkeypatch, capsys) -> None:
    truncated = {"response": '{"recipes": [{"name": "Каш', "done_reason": "length"}
    with pytest.raises(smoke.ResponseTruncated):
        smoke.parse_response(truncated)
    real_client = httpx.Client
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=truncated))
        ),
    )
    assert smoke.main(["--base-url", "http://ollama.test"]) == 1
    error = capsys.readouterr().err
    assert "num_predict" in error and "JSONDecodeError" not in error


@pytest.mark.parametrize("budget", ["abc", "1,5", "", "NaN-ish"])
def test_non_numeric_budget_is_an_input_error_not_a_traceback(
    smoke, capsys, budget
) -> None:
    assert smoke.main(["--budget", budget]) == 2
    assert "--budget" in capsys.readouterr().err


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


def test_a_seed_file_without_a_template_string_is_reported(smoke, tmp_path) -> None:
    broken = tmp_path / "seed.py"
    broken.write_text("TEMPLATE = 42\n")
    with pytest.raises(RuntimeError, match="TEMPLATE"):
        smoke.load_seed_template(broken)
    not_python = tmp_path / "seed.txt"
    not_python.write_text("TEMPLATE = 'x'\n")
    with pytest.raises(RuntimeError, match="cannot load"):
        smoke.load_seed_template(not_python)


def test_a_response_rejected_by_the_gate_is_exit_code_one(
    smoke, monkeypatch, capsys
) -> None:
    short = payload()
    short["recipes"] = short["recipes"][:1]
    real_client = httpx.Client
    monkeypatch.setattr(
        smoke.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"response": json.dumps(short)})
            )
        ),
    )
    assert smoke.main(["--base-url", "http://ollama.test"]) == 1
    assert "rejected by the gate" in capsys.readouterr().err


@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_running_the_module_as_a_script_exits_with_the_main_code(
    smoke, monkeypatch
) -> None:
    import runpy

    monkeypatch.setattr("sys.argv", ["ollama_smoke", "--people", "99"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("scripts.ollama_smoke", run_name="__main__")
    assert exit_info.value.code == 2
