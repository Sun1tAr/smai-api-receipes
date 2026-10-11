import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# В test-образе нет .github (Dockerfile копирует только нужное): проверки идут в CI.
def ci() -> str:
    if not (ROOT / ".github/workflows/ci.yml").exists():
        pytest.skip("workflow is not part of the test image")
    return (ROOT / ".github/workflows/ci.yml").read_text()


def test_workflow_has_least_privilege_and_cancels_superseded_pr_runs() -> None:
    text = ci()
    assert re.search(r"^permissions:\n  contents: read$", text, re.MULTILINE)
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text


def test_compose_smoke_job_checks_health_and_docs_and_always_cleans_up() -> None:
    text = ci()
    smoke = text.split("  compose-smoke:")[1].split("\n  docker-build:")[0]
    assert "cp .env.example .env" in smoke
    assert "docker compose up --build --wait" in smoke
    assert "curl --fail --silent --show-error http://127.0.0.1:8001/health" in smoke
    assert "http://127.0.0.1:8001/docs" in smoke
    assert re.search(r"- if: always\(\)\n\s+run: docker compose down -v", smoke)
    assert "compose-smoke" in text.split("  docker-build:")[1]


def test_integration_job_cleans_up_even_when_tests_fail() -> None:
    integration = ci().split("  integration:")[1].split("\n  compose-smoke:")[0]
    assert re.search(r"- if: always\(\)\n\s+run: .*down -v", integration)


def test_workflow_has_no_deploy_jobs() -> None:
    text = "\n".join(line for line in ci().splitlines() if not line.startswith("#"))
    for forbidden in ("ghcr.io", "repository-dispatch", "packages: write", "secrets."):
        assert forbidden not in text


def test_services_restart_unless_stopped() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert compose.count("restart: unless-stopped") == 2


def test_every_job_has_a_timeout() -> None:
    jobs = re.split(r"^  (?=[a-z-]+:$)", ci().split("\njobs:\n")[1], flags=re.M)
    jobs = [job for job in jobs if job.strip()]
    assert len(jobs) == 5
    for job in jobs:
        assert re.search(r"^    timeout-minutes: \d+$", job, re.MULTILINE), job
