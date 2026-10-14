# Ошибка генерации: её текст сохраняется в generation_task.error и виден клиенту через
# API-2, поэтому он короткий и безопасный (без SQL, путей и содержимого ответа модели).
class GenerationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# Шаблон промпта отсутствует или повреждён: сбой конфигурации, а не модели.
class PromptTemplateError(GenerationError):
    def __init__(self) -> None:
        super().__init__("Menu generation failed")
