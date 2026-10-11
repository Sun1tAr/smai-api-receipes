import os

import pytest


# В прогоне с FAIL_ON_SKIP=1 (docker-compose.test.yml) пропущенный тест — это ошибка:
# в test-образе есть всё, что нужно тестам, поэтому skip означает тихую потерю проверки.
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not os.getenv("FAIL_ON_SKIP"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    if skipped:
        names = ", ".join(report.nodeid for report in skipped[:10])
        print(f"\nFAIL_ON_SKIP: {len(skipped)} skipped test(s): {names}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
