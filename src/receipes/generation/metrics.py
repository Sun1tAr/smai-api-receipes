from prometheus_client import CollectorRegistry, Counter, Histogram

# Генерация идёт от секунд до десятков минут в худшем случае: свои границы.
DURATION_BUCKETS = (5, 10, 20, 30, 60, 120, 300, 600)
TASK_STATUSES = ("done", "failed")
OLLAMA_OUTCOMES = (
    "success",
    "timeout",
    "unavailable",
    "invalid_response",
    "truncated",
    "error",
)
REJECT_REASONS = (
    "schema",
    "count",
    "duplicate_name",
    "duplicate_ingredient",
    "ingredient_name",
    "bounds",
    "text",
)


# Метрики генерации живут в реестре приложения (отдаются через /metrics).
class GenerationMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.tasks = Counter(
            "menu_generation_tasks",
            "Finished menu generation tasks by final status.",
            ["status"],
            registry=registry,
        )
        self.duration = Histogram(
            "menu_generation_duration_seconds",
            "Time from task claim to its final status.",
            buckets=DURATION_BUCKETS,
            registry=registry,
        )
        self.ollama_duration = Histogram(
            "ollama_request_duration_seconds",
            "Duration of the Ollama generate call.",
            buckets=DURATION_BUCKETS,
            registry=registry,
        )
        self.ollama_requests = Counter(
            "ollama_requests",
            "Ollama generate calls by outcome.",
            ["outcome"],
            registry=registry,
        )
        self.rejected = Counter(
            "menu_generation_rejected",
            "Model responses rejected by validation, by reason.",
            ["reason"],
            registry=registry,
        )
        for status in TASK_STATUSES:
            self.tasks.labels(status)
        for outcome in OLLAMA_OUTCOMES:
            self.ollama_requests.labels(outcome)
        for reason in REJECT_REASONS:
            self.rejected.labels(reason)
