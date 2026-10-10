# smai-api-receipes

MS-4 — сервис рецептов SmartMeal: ядро домена рецептов и генерация меню через
Ollama. Сейчас готова только инфраструктура (**SMAI-58**): каркас приложения,
подключение к PostgreSQL, миграции, health, метрики, трейсы и логи, Docker/Compose
и CI. Доменный код (модели, таблицы, воркер генерации, клиент Ollama) и
бизнес-эндпоинты API-1/API-2 появятся в задачах SMAI-57 и SMAI-55.

## Эндпоинты

Интерактивная документация (Swagger UI) — `/docs`, схема OpenAPI — `/openapi.json`.

| Метод и путь | Назначение | Ответы | Статус |
| --- | --- | --- | --- |
| `GET /health` | Готовность: `SELECT 1` к PostgreSQL (Ollama не проверяется); не пишет спаны, лог запроса на уровне DEBUG | 200 `{"status":"ok"}`, 503 `INTERNAL_ERROR` | служебный |
| `GET /metrics` | Метрики Prometheus: HTTP, Python, GC и процесс (вне схемы OpenAPI) | 200 `text/plain` | служебный |
| `GET /docs` | Swagger UI | 200 `text/html` | служебный |
| `GET /openapi.json` | Схема OpenAPI | 200 `application/json` | служебный |

Все ошибки возвращаются в одном формате (без префикса `/api/v1` и без обёртки
`data`/`meta`):

```json
{"error": {"code": "NOT_FOUND", "message": "Resource not found", "details": {}}}
```

Коды: `VALIDATION_ERROR` (400, ошибка валидации запроса), `NOT_FOUND` (404),
`METHOD_NOT_ALLOWED` (405), `INTERNAL_ERROR` (500 и 503). Каждый ответ, включая
500 на необработанное исключение, содержит заголовок `X-Request-ID`: входящее
значение принимается, если это до 128 символов `A-Za-z0-9._-`, иначе
генерируется UUID4; тот же `request_id` пишется в логи. В `details.fields`
ошибки валидации приходят только `loc` (список строк с местом ошибки, например `["body", "name"]`) и
`type` (тип ошибки Pydantic), без введённых значений и текста исключений; для
неразобранного JSON `loc` равен `["body"]`. Формат совпадает с контрактом gateway. Схема OpenAPI не содержит `422` и
`HTTPValidationError` фреймворка: валидация отвечает `400`.

## Локальный запуск

Требуются Docker и Docker Compose.

```sh
cp .env.example .env   # проверьте значения, это локальные заглушки
docker compose up --build --wait
curl --fail http://127.0.0.1:8001/health
curl --fail http://127.0.0.1:8001/docs
curl --fail http://127.0.0.1:8001/metrics
docker compose down -v   # остановка; -v удаляет том PostgreSQL
```

Сервис в Compose называется `recipes` (под этим именем его ждёт gateway).
Контейнер слушает `0.0.0.0:8000`, на хосте порт публикуется как
`127.0.0.1:${RECEIPES_HTTP_PORT:-8001}`. PostgreSQL доступен только внутри
Compose-сети. При старте контейнер выполняет `alembic upgrade head`, затем
запускает uvicorn; приложение не стартует, если БД недоступна. Контейнер работает
от пользователя `receipes` (UID 10001).

## Конфигурация

Настройки читаются только из окружения (локально — также из `.env` в рабочей
директории; переменные процесса приоритетнее). Пустое значение (`KEY=`)
считается незаданным. Некорректная конфигурация останавливает запуск.

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Подключение к PostgreSQL | Обязательны |
| `DB_PORT` | Порт PostgreSQL | `5432` |
| `DB_TIMEOUT_SECONDS` | Предельное ожидание БД (0, 60]: подключение и запросы приложения. Запросы миграций не ограничиваются | `5` |
| `MIGRATION_LOCK_TIMEOUT_SECONDS` | Ожидание чужой блокировки при миграциях, (0, 3600] | `30` |
| `LOG_LEVEL` | DEBUG / INFO / WARNING / ERROR | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Базовый адрес OTLP HTTP, например `http://collector:4318` | Экспорт отключён |
| `OLLAMA_BASE_URL` | Адрес Ollama (http/https) | Обязательна |
| `OLLAMA_MODEL` | Модель | `qwen2.5:7b-instruct-q4_K_M` |
| `OLLAMA_CONNECT_TIMEOUT_SECONDS` | Таймаут соединения, (0, 60] | `5` |
| `OLLAMA_TIMEOUT_SECONDS` | Таймаут ответа, (0, 3600] | `300` |
| `OLLAMA_NUM_CTX` | Размер контекста, токенов | `32768` |
| `OLLAMA_NUM_PREDICT` | Максимум токенов ответа | `4096` |
| `OLLAMA_TEMPERATURE` | Температура, [0, 2] | `0.3` |
| `OLLAMA_KEEP_ALIVE` | Сколько держать модель в памяти | `30m` |
| `GENERATION_WORKER_ENABLED` | Включить фоновый воркер генерации | `true` |
| `GENERATION_WORKER_INTERVAL_SECONDS` | Интервал опроса очереди, (0, 3600] | `2` |
| `GENERATION_PENDING_TTL_SECONDS` | Время жизни ожидающей задачи, [1, 86400] | `900` |
| `GENERATION_MAX_PENDING` | Лимит ожидающих задач | Не задан — без лимита |
| `GENERATION_BAD_RESPONSE_RETRIES` | Повторы при некорректном ответе модели, [0, 10] | `1` |
| `RECEIPES_HTTP_PORT` | Хостовой порт Compose (только Compose) | `8001` |

Пустые и состоящие из пробелов `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
отклоняются; `OLLAMA_BASE_URL` и `OTEL_EXPORTER_OTLP_ENDPOINT` не должны
содержать учётные данные (`user:password@host`). При неверной конфигурации
сервис пишет в лог только имена полей и типы ошибок (`invalid_configuration`)
и завершается с кодом 1.

Воркер и клиент Ollama пока не реализованы: переменные `OLLAMA_*` и
`GENERATION_*` только валидируются и пробрасываются в контейнер.
Compose требует `DB_NAME`, `DB_USER`, `DB_PASSWORD` и `OLLAMA_BASE_URL`
(без них `docker compose` не запускается); `DB_HOST` и `DB_PORT` внутри Compose
закреплены (`postgres:5432`). `.env` не попадает в Git и в образ; конфигурацию
stage/prod задаёт DevOps через окружение.

## Проверки

Python 3.12, зависимости фиксируются в `uv.lock`:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/unit
```

Полный прогон (unit и integration) с отдельной PostgreSQL на tmpfs, без обращения
к рабочей БД:

```sh
docker compose -p receipes-tests -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -p receipes-tests -f docker-compose.test.yml down
```

Интеграционные тесты (маркер `integration`) пропускаются без `TEST_DB_HOST`.
Для своей тестовой БД задайте `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_NAME`,
`TEST_DB_USER`, `TEST_DB_PASSWORD` и выполните `uv run pytest`.

## Модель данных

Схема по ERD статьи MS-4 (SMAI-A-110). Все первичные ключи — UUID, их создаёт
приложение. Модели — `src/receipes/models.py`, домен — `domain.py`,
«ворота» ответа Ollama — `generated_menu.py`, доступ к данным —
`repositories.py` (репозитории принимают `AsyncSession` и не коммитят).

| Таблица | Назначение и ключевые поля |
| --- | --- |
| `prompt_template` | `template` (Text), `created_at`; актуален последний |
| `generation_parameters` | `people_count` (≥1), `weekly_budget` Numeric(12,2) (>0) |
| `generation_task` | `task_id`, `status`, `prompt`, `parameters_id` (NOT NULL, UNIQUE, 1:1), `menu_id` (NULL до `done`), `created_at`, `started_at`, `finished_at`, `error` |
| `menu` | только `id` |
| `menu_recipes` | `menu_id`, `recipe_id`, `position` |
| `recipe` | `name` (255), `calories`, `cooking_time` (минуты), `photo` (Text, NULL), `nutrition_id` |
| `ingredient` | `name` (255), хранится в нормализованном виде |
| `recipe_ingredients` | `recipe_id`, `ingredient_id`, `quantity`, `unit` (32), `position` |
| `instruction` | `recipe_id`, `text`, `position` |
| `nutrition` | `calories`, `proteins`, `fats`, `carbohydrates` |

Числа (калории, КБЖУ, количества, время готовки) — `Numeric(10,2)`; время
`created_at`/`started_at`/`finished_at` — `timestamptz` (`created_at` по умолчанию
`now()`).

**Ограничения `generation_task`:** `status IN ('pending','in_progress','done','failed')`;
`(status='done') = (menu_id IS NOT NULL)`; `(status='failed') = (error IS NOT NULL)`;
`prompt` обязателен при `in_progress`/`done`; `started_at` обязателен при
`in_progress`/`done`; `finished_at` — при `done`/`failed`.
Остальное: `UNIQUE(menu_recipes.menu_id, recipe_id)` и `(menu_id, position)`,
`UNIQUE(recipe_ingredients.recipe_id, ingredient_id)` и `(recipe_id, position)`,
`UNIQUE(instruction.recipe_id, position)`, позиции ≥1, неотрицательные числа.
Уникальность ингредиента — индекс `uq_ingredient_name_normalized` по
`lower(btrim(name))`. Частичные индексы: `generation_task(created_at) WHERE
status='pending'` (очередь) и `generation_task(started_at) WHERE
status='in_progress'` (восстановление зависших).

**Каскады.** `menu` → `menu_recipes` и `recipe` → `recipe_ingredients`,
`instruction`: `ON DELETE CASCADE`. Все остальные ссылки — `RESTRICT`: нельзя
удалить рецепт, входящий в меню, ингредиент, нутриенты или параметры, на которые
есть ссылки, и меню, на которое ссылается задача (иначе задача `done` потеряла бы
результат, нарушив `menu_iff_done`).

**Ревизии Alembic.**
- `0001_base_schema` — схема строго по ERD.
- `0002_ordering_task_times` — отличия от ERD: `position` в `menu_recipes` и
  `recipe_ingredients` (без него порядок рецептов в меню и ингредиентов не
  определён), `started_at`/`finished_at` в `generation_task` (нужны для
  восстановления зависших задач и метрик длительности), связанные CHECK и
  индекс. Вынесены отдельно: при отказе ревизию можно удалить, не трогая базовую.
  Существующие строки получают позиции `1..n` по `id`, а `started_at`/`finished_at`
  — `created_at`.

Обе ревизии обратимы (`upgrade` / `downgrade` / `upgrade` проверяются в
интеграционных тестах, схема после `downgrade base` пуста, `alembic check` чист).

**Названия ингредиентов** нормализуются в одном месте
(`domain.normalize_ingredient_name`: схлопывание пробелов, `casefold`, `ё`→`е`) и
хранятся в нормализованном виде, поэтому в ответе они строчные (`овсяные хлопья`).

**Репозитории.**
- `TaskRepository`: `create`, `get`, `claim_next(build_prompt)` (`SELECT … FOR
  UPDATE SKIP LOCKED` старейшей `pending`; промпт задаётся при захвате, потому что
  CHECK требует его у любой задачи `in_progress`), `transition(task_id, expected,
  to, …)` — условный `UPDATE … WHERE status=:expected RETURNING`, `False` при
  чужом статусе; допустимость перехода проверяет `TaskStatus`; `expire_stale(
  pending_ttl, stale_after)` (`Queue timeout`, `Generation interrupted`),
  `count_pending`, `oldest_pending_age`.
- `MenuRepository`: `save(GeneratedMenu)` (ингредиенты через `INSERT … ON CONFLICT
  (lower(btrim(name))) DO NOTHING`, позиции с 1) и `load(menu_id)` — рецепты,
  ингредиенты и шаги отсортированы по `position`; связанные данные грузятся
  `selectinload`/`joinedload` (несколько запросов, без N+1).
- `PromptTemplateRepository.latest()` — `ORDER BY created_at DESC, id DESC`.

**Ворота ответа модели** (`GeneratedMenu`): 3–21 рецепт, 1–50 ингредиентов и
шагов, непустые строки не длиннее колонок, конечные числа ≥0 с верхними
границами (калории ≤10000, время ≤1440 мин). Числа округляются до сотых,
дубли ингредиентов схлопываются (одна единица — количества складываются, разная —
остаётся первое и пишется предупреждение), `photo` игнорируется. Поля — camelCase
как в контракте API.

## Структура репозитория

```text
src/receipes/       приложение: config, database, main (create_app), health,
                    errors, observability, migration_diagnostics, __main__ (запуск uvicorn);
                    домен MS-4: models, domain, generated_menu, repositories
migrations/         Alembic (async env.py), ревизии в versions/ (0001, 0002)
docker/             entrypoint.sh: alembic upgrade head, затем запуск сервера
tests/unit/         unit-тесты (без БД)
tests/integration/  интеграционные тесты (нужна PostgreSQL, TEST_DB_HOST)
.github/workflows/  CI: lint, unit, integration, compose-smoke, сборка образа
```

Миграции: `uv run alembic revision --autogenerate -m "SMAI-NN описание"`,
`uv run alembic upgrade head`, `uv run alembic check` (БД должна быть доступна).

Alembic ждёт подключения не дольше `DB_TIMEOUT_SECONDS`, но длительность самих
запросов миграции не ограничивает; ожидание чужой блокировки ограничено
`lock_timeout` (`MIGRATION_LOCK_TIMEOUT_SECONDS`, по умолчанию 30 с, только для
миграций). При падении миграции в лог попадает событие
`migration_failed`: `error_type`, `sqlstate`, `driver_error_type` и
`db_message` — основное сообщение PostgreSQL, только для SQLSTATE класса 42
(синтаксис и объекты схемы); для остальных классов сообщение не пишется, так как
оно может содержать данные строк. SQL, параметры запросов и DETAIL не логируются;
`from_revision` (ревизия до запуска) и `last_applied` (последняя выполненная
в этой попытке; транзакция откатывается). Пароли и параметры запросов не логируются.

## CI

Джобы: `lint`, `unit`, `integration`, `compose-smoke` (`docker compose up --build
--wait`, затем `/health` и `/docs`), `docker-build`; у каждой джобы задан `timeout-minutes`. Права workflow —
`contents: read`; устаревшие запуски PR отменяются. Публикация образа и деплой
(stage/prod) в CI не описаны: это решение Ivan и DevOps.

## Известные ограничения

- Миграции запускает `docker/entrypoint.sh` при старте каждого контейнера без
  advisory lock: при нескольких репликах одновременный `alembic upgrade head`
  не сериализуется. Сейчас реплика одна.
- Базовые образы (`python`, `postgres`) закреплены тегом, а не digest.
- Alembic использует те же настройки, что и приложение (`Settings`), включая
  обязательные `OLLAMA_*`; отдельного набора настроек для миграций нет.
- Действия GitHub Actions закреплены тегами версий, а не SHA коммитов.
- При неверной конфигурации контейнер уходит в перезапуск (`restart: unless-stopped`),
  а `docker compose up --wait` завершается ошибкой.
