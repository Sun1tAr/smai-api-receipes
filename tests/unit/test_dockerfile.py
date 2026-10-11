import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _stage(name: str) -> str:
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(
        rf"^FROM .* AS {name}$(.*?)(?=^FROM |\Z)", dockerfile, re.MULTILINE | re.DOTALL
    )
    assert match is not None
    return match.group(1)


def test_runtime_starts_application_as_unprivileged_user() -> None:
    runtime = _stage("runtime")
    commands = re.findall(r"^CMD (\[.*\])$", runtime, re.MULTILINE)
    assert json.loads(commands[-1]) == ["python", "-m", "receipes"]
    assert "--uid 10001" in runtime
    assert re.search(r"^USER receipes$", runtime, re.MULTILINE)
    assert "EXPOSE 8000" in runtime
    assert "HEALTHCHECK" in runtime and "/health" in runtime


def test_production_stage_exists() -> None:
    assert re.search(
        r"^FROM runtime AS production$", (ROOT / "Dockerfile").read_text(), re.MULTILINE
    )


def test_compose_service_is_named_recipes_and_publishes_port() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"^  recipes:$", compose, re.MULTILINE)
    assert '"127.0.0.1:${RECEIPES_HTTP_PORT:-8001}:8000"' in compose
    assert "condition: service_healthy" in compose
    assert "postgres:17-alpine" in compose


def test_compose_requires_secrets_and_ollama_url() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    for name in ("DB_NAME", "DB_USER", "DB_PASSWORD", "OLLAMA_BASE_URL"):
        assert f"${{{name}:?" in compose


def test_test_stage_contains_the_files_the_tests_read() -> None:
    test_stage = _stage("test")
    for path in (
        ".github",
        "scripts",
        "tests",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.test.yml",
    ):
        assert re.search(rf"^COPY .*{re.escape(path)}", test_stage, re.MULTILINE), path


def test_compose_test_run_fails_on_skips_and_low_coverage() -> None:
    compose = (ROOT / "docker-compose.test.yml").read_text()
    assert 'FAIL_ON_SKIP: "1"' in compose
    assert "--cov-fail-under=98" in compose
