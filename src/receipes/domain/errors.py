# Тексты, которые пишет сам воркер.
SERVICE_RESTARTED = "Service restarted"
GENERATION_FAILED = "Menu generation failed"


# Ошибка генерации: её текст сохраняется в generation_task.error и виден клиенту через
# API-2, поэтому он короткий и безопасный (без SQL, путей и содержимого ответа модели).
class GenerationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        # Счётчики токенов из ответа Ollama (если он дошёл): для логов отказов.
        self.prompt_eval_count: int | None = None
        self.eval_count: int | None = None


# Шаблон промпта отсутствует или повреждён: сбой конфигурации, а не модели.
class PromptTemplateError(GenerationError):
    def __init__(self) -> None:
        super().__init__(GENERATION_FAILED)


class NeurocoreUnavailable(GenerationError):
    def __init__(self) -> None:
        super().__init__("Neurocore unavailable")


class NeurocoreTimeout(GenerationError):
    def __init__(self) -> None:
        super().__init__("Neurocore timeout")


class NeurocoreModelNotFound(GenerationError):
    def __init__(self) -> None:
        super().__init__("Neurocore model not found")


class NeurocoreError(GenerationError):
    def __init__(self) -> None:
        super().__init__("Neurocore error")


# Ответ оборван лимитом токенов: повтор не поможет, меню неполное.
class NeurocoreTruncated(GenerationError):
    def __init__(self) -> None:
        super().__init__("Neurocore response truncated")


# Невалидный ответ модели; единственный вид ошибки, который повторяется.
# reason идёт в метрику и лог (без содержимого ответа).
class InvalidNeurocoreResponse(GenerationError):
    def __init__(self, reason: str) -> None:
        super().__init__("Invalid neurocore response")
        self.reason = reason


# Задачу перевели из in_progress в другое состояние, пока шла генерация (например,
# новый контейнер при старте пометил её failed). Результат записывать нельзя.
class TaskStateConflict(Exception):
    pass


# Справочники MS-2 недоступны или ответили неверно. Текст безопасный: без URL,
# тела ответа и подробностей сети.
class DictionaryError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
