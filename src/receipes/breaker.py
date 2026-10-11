import time
from collections.abc import Callable


# Circuit breaker воркера: после сбоя Ollama (недоступность, таймаут, HTTP-ошибка)
# новые задачи не берутся, пока не пройдёт пауза и проба доступности. Пауза растёт
# вдвое от минимума до максимума и сбрасывается только после успешной генерации, а не
# после удачной пробы: так «мигающая» Ollama не получает по задаче каждые min секунд.
class CircuitBreaker:
    def __init__(
        self,
        min_pause: float,
        max_pause: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_pause = min_pause
        self.max_pause = max_pause
        self.clock = clock
        self.is_open = False
        self.streak = 0
        self.pause = 0.0
        self.next_probe_at = 0.0

    # Сбой Ollama. True — breaker только что открылся.
    def record_failure(self) -> bool:
        opened = not self.is_open
        self.is_open = True
        self.streak += 1
        self.pause = min(self.min_pause * 2 ** (self.streak - 1), self.max_pause)
        self.next_probe_at = self.clock() + self.pause
        return opened

    # Проба прошла: breaker закрывается, серия сбоев сохраняется до успешной генерации.
    def record_probe_success(self) -> bool:
        closed = self.is_open
        self.is_open = False
        return closed

    # Задача успешно сгенерирована: серия сбоев и пауза сбрасываются.
    def record_success(self) -> None:
        self.is_open = False
        self.streak = 0
        self.pause = 0.0

    def probe_due(self) -> bool:
        return self.is_open and self.clock() >= self.next_probe_at
