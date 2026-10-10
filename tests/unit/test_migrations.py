import json
import os
import subprocess
import sys
from pathlib import Path


def test_migration_connection_failure_has_safe_logs_and_nonzero_exit() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "1",
            "DB_NAME": "receipes",
            "DB_USER": "receipes",
            "DB_PASSWORD": "private-migration-password",
            "DB_TIMEOUT_SECONDS": "1",
            "OLLAMA_BASE_URL": "http://ollama.test:11434",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "private-migration-password" not in result.stdout + result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    failure = next(item for item in events if item["message"] == "migration_failed")
    assert failure["level"] == "error"
    assert failure["error_type"]
