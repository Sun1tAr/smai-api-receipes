from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_services_restart_unless_stopped() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert compose.count("restart: unless-stopped") == 2
