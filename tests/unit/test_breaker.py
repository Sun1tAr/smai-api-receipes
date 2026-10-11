import pytest

from receipes.breaker import CircuitBreaker


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(min_pause: float = 5, max_pause: float = 60):
    clock = Clock()
    return CircuitBreaker(min_pause, max_pause, clock), clock


def test_a_new_breaker_is_closed() -> None:
    breaker, _ = make()
    assert not breaker.is_open and not breaker.probe_due()


def test_the_first_failure_opens_it_with_the_minimum_pause() -> None:
    breaker, clock = make()
    assert breaker.record_failure() is True
    assert breaker.is_open and breaker.pause == 5
    clock.now += 4.9
    assert not breaker.probe_due()
    clock.now += 0.1
    assert breaker.probe_due()


def test_the_pause_doubles_up_to_the_maximum() -> None:
    breaker, _ = make()
    pauses = []
    for _ in range(7):
        breaker.record_failure()
        pauses.append(breaker.pause)
    assert pauses == [5, 10, 20, 40, 60, 60, 60]
    assert breaker.record_failure() is False  # уже открыт


def test_a_successful_probe_closes_but_keeps_the_streak() -> None:
    breaker, clock = make()
    breaker.record_failure()
    breaker.record_failure()
    clock.now += 10
    assert breaker.record_probe_success() is True
    assert not breaker.is_open and not breaker.probe_due()
    # Мигающая Ollama: следующий сбой сразу даёт большую паузу, не минимальную.
    assert breaker.record_failure() is True
    assert breaker.pause == 20


def test_a_successful_generation_resets_the_pause() -> None:
    breaker, _ = make()
    for _ in range(4):
        breaker.record_failure()
    assert breaker.pause == 40
    breaker.record_success()
    assert not breaker.is_open and breaker.streak == 0
    breaker.record_failure()
    assert breaker.pause == 5


def test_probe_success_when_already_closed_is_a_noop() -> None:
    breaker, _ = make()
    assert breaker.record_probe_success() is False


@pytest.mark.parametrize(("low", "high"), [(1, 1), (0.5, 8)])
def test_pause_never_exceeds_the_maximum(low, high) -> None:
    breaker, _ = make(low, high)
    for _ in range(10):
        breaker.record_failure()
    assert breaker.pause == high
