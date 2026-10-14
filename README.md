# smai-api-receipes

MS-4 — сервис рецептов SmartMeal: ядро домена рецептов и генерация меню через
Ollama. Инфраструктура (**SMAI-58**): каркас приложения, подключение к PostgreSQL,
миграции, health, метрики, трейсы и логи, Docker/Compose и CI. Домен (SMAI-57),
шаблон промпта (SMAI-56), воркер генерации и клиент Ollama (SMAI-54) и
эндпоинты API-1/API-2 (SMAI-55) готовы.

## Эндпоинты

Интерактивная документация (Swagger UI) — `/docs`, схема OpenAPI — `/openapi.json`.

| Метод и путь | Назначение | Ответы | Статус |
| --- | --- | --- | --- |
| `GET /health` | Готовность: `SELECT 1` к PostgreSQL (Ollama не проверяется); не пишет спаны, лог запроса на уровне DEBUG | 200 `{"status":"ok"}`, 503 `INTERNAL_ERROR` | служебный |
| `POST /generate-menu` | API-1: создать задачу генерации меню (`peopleCount` 1–20, `weeklyBudget` > 0, ≤ 1 000 000, до 2 знаков). Поля v2 (`habits`, `diet`, `allergies`, `kitchenTools`) пока не поддерживаются и игнорируются | 202 `{"task_id"}`, 400 `VALIDATION_ERROR`, 500 `INTERNAL_ERROR` | бизнес |
| `GET /generate-menu/{task_id}` | API-2: статус задачи; всегда `status`, `menu`, `error` (`menu` только при `done`, `error` только при `failed`). Порядок рецептов и ингредиентов не гарантируется | 200, 404 `NOT_FOUND` (неизвестный или не-UUID `task_id`), 500 `INTERNAL_ERROR` | бизнес |
| `GET /metrics` | Метрики Prometheus: HTTP, Python, GC и процесс (вне схемы OpenAPI) | 200 `text/plain` | служебный |
| `GET /docs` | Swagger UI | 200 `text/html` | служебный |
| `GET /openapi.json` | Схема OpenAPI | 200 `application/json` | служебный |

Максимальный размер тела запроса задаётся на gateway, сам сервис его не ограничивает.

Все ошибки возвращаются в одном формате (без префикса `/api/v1` и без обёртки
`data`/`meta`):

```json
{"error": {"code": "NOT_FOUND", "message": "Resource not found", "details": {}}}
```

Коды: `VALIDATION_ERROR` (400, ошибка валидации запроса), `NOT_FOUND` (404; для
API-2 — `Generation task not found`),
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
| `OLLAMA_MODEL` | Модель (тег должен быть загружен в Ollama) | `qwen2.5:7b` |
| `OLLAMA_CONNECT_TIMEOUT_SECONDS` | Таймаут соединения, (0, 60] | `5` |
| `OLLAMA_TIMEOUT_SECONDS` | Таймаут ответа, (0, 3600] | `300` |
| `OLLAMA_NUM_CTX` | Размер контекста, токенов; не меньше `OLLAMA_NUM_PREDICT + 1024`, иначе сервис не стартует | `8192` |
| `OLLAMA_NUM_PREDICT` | Максимум токенов ответа (≈13 блюд; при большем числе ответ обрежется) | `4096` |
| `OLLAMA_TEMPERATURE` | Температура, [0, 2] | `0.3` |
| `OLLAMA_KEEP_ALIVE` | Сколько держать модель в памяти | `30m` |
| `GENERATION_WORKER_ENABLED` | Включить фоновый воркер генерации | `true` |
| `GENERATION_WORKER_INTERVAL_SECONDS` | Интервал опроса очереди, (0, 3600] | `2` |
| `GENERATION_BAD_RESPONSE_RETRIES` | Повторы при невалидном ответе модели (таймаут, обрезка и недоступность не повторяются), [0, 10] | `1` |
| `RECEIPES_HTTP_PORT` | Хостовой порт Compose (только Compose) | `8001` |

Пустые и состоящие из пробелов `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
отклоняются; `OLLAMA_BASE_URL` и `OTEL_EXPORTER_OTLP_ENDPOINT` не должны
содержать учётные данные (`user:password@host`). При неверной конфигурации
сервис пишет в лог только имена полей и типы ошибок (`invalid_configuration`)
и завершается с кодом 1.

Переменные `GENERATION_PENDING_TTL_SECONDS` и `GENERATION_MAX_PENDING` удалены;
если они остались в окружении, сервис их игнорирует.
Compose требует `DB_NAME`, `DB_USER`, `DB_PASSWORD` и `OLLAMA_BASE_URL`
(без них `docker compose` не запускается); `DB_HOST` и `DB_PORT` внутри Compose
закреплены (`postgres:5432`). `.env` не попадает в Git и в образ; конфигурацию
stage/prod задаёт DevOps через окружение.

## Фоновая генерация меню

Воркер — один asyncio-таск внутри процесса сервиса
(`GENERATION_WORKER_ENABLED=true`). Он берёт `pending`-задачи по одной, самую
старую первой (`FOR UPDATE SKIP LOCKED`), собирает промпт из активного шаблона
(`prompt_template`), делает **один** запрос к Ollama «не менее 3 блюд» и в одной
транзакции сохраняет меню и ставит `done`. Любая ошибка после захвата — `failed`
с коротким текстом в `error`:

| Текст ошибки | Причина |
| --- | --- |
| `Neurocore unavailable` | Ollama недоступна: нет соединения, обрыв, ответ 503 |
| `Neurocore timeout` | ответ не пришёл за `OLLAMA_TIMEOUT_SECONDS` |
| `Neurocore model not found` | Ollama ответила 404: модель не загружена |
| `Neurocore error` | Ollama ответила 400, 500 или иным кодом |
| `Invalid neurocore response` | не JSON, не по схеме или нарушены проверки (меньше 3 блюд, дубли, пустые строки, «200 г муки» в названии ингредиента); один повтор |
| `Neurocore response truncated` | ответ оборван лимитом `OLLAMA_NUM_PREDICT` (`done_reason=length`) |
| `Service restarted` | процесс остановился во время генерации; при старте такие задачи закрываются |
| `Menu generation failed` | прочий сбой (нет шаблона, ошибка записи); подробности только в логе |

Число блюд не фиксировано: промпт просит не менее 3, `format` дословно по API-3
(`minItems: 3`, без максимума). Поле `photo` из ответа модели игнорируется
(всегда `NULL`).

**Условия эксплуатации.** Одна реплика и один процесс (образ запускается
`python -m receipes`, флаг `--workers` не добавлять); деплой stop-first. Как
страховка воркер держит `pg_try_advisory_lock` на отдельном соединении всё время
работы: пока замок занят другим экземпляром, воркер не стартует (warning
`generation_lock_busy`, повтор с интервалом `GENERATION_WORKER_INTERVAL_SECONDS`)
и не трогает чужие задачи. Только получив замок, он закрывает все `in_progress`
как `Service restarted`. Очередь не ограничена и не имеет TTL.

**Защита очереди.** Перед взятием задачи проверяется активный шаблон промпта: если
его нет или он повреждён (нет `{people_count}`/`{weekly_budget}`, лишние или
непарные скобки), задачи остаются `pending`, а в лог пишется `prompt_template_unusable`
с причиной (один раз, пока причина не изменится). После `Neurocore unavailable`
или `Neurocore timeout` воркер делает паузу 5 → 10 → 20 → 40 → 60 с и перед
следующей задачей проверяет Ollama запросом `GET /api/tags`; пока она не
отвечает, остальные задачи не берутся и не теряются (упавшая остаётся `failed`).
Сбои самого цикла (например, БД) повторяются с паузой 1 → 30 с.

**Время.** Худший случай одной задачи — `(1 + GENERATION_BAD_RESPONSE_RETRIES) ×
OLLAMA_TIMEOUT_SECONDS` (по умолчанию 600 с), всё это время очередь стоит. В норме
генерация идёт секунды (≈63 токена/с, когда модель целиком в видеопамяти). На
процессоре ответ до лимита токенов не уложится в таймаут: это сигнал проверить
GPU у Ollama, а не повод менять код. При смене `OLLAMA_TIMEOUT_SECONDS` или числа
повторов пересчитайте ожидания клиентов.

**Контекст.** Запросу нужно `токены промпта (~820 измерено) + OLLAMA_NUM_PREDICT`
(≈4,9 тыс. токенов), поэтому по умолчанию `OLLAMA_NUM_CTX=8192`: модель
занимает 6 ГБ и целиком помещается в видеопамять, при 32768 — 9,4 ГБ, не
помещается и работает в 2,6 раза медленнее. Правило старта: `OLLAMA_NUM_PREDICT + 1024
≤ OLLAMA_NUM_CTX` (1024 — запас на промпт: ~820 измерено плюс ~200 на рост
шаблона). Если фактический промпт (`prompt_eval_count`) не оставляет места под
ответ, в лог пишется предупреждение `ollama_context_tight`.

**Логи и метрики.** События `generation_task_claimed|done|failed` несут
`task_id`, длительность, число рецептов и `prompt_eval_count`/`eval_count`;
промпт, ответ модели и параметры в лог не попадают. `/metrics`:
`menu_generation_tasks_total{status}`, `menu_generation_duration_seconds`,
`ollama_request_duration_seconds`, `ollama_requests_total{outcome}`,
`menu_generation_rejected_total{reason}`.

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

## Структура репозитория

```text
src/receipes/       приложение: config, database, main (create_app), health,
                    errors, observability, migration_diagnostics, __main__ (запуск uvicorn);
                    domain/ (статусы, проверка ответа модели), db/ (модели, репозитории),
                    api/ (API-1/API-2), generation/ (промпт, клиент Ollama, воркер,
                    метрики)
migrations/         Alembic (async env.py): 0001_domain, 0002_seed_prompt_template;
                    таблица версий alembic_version_receipes
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

Workflow `.github/workflows/ci.yml` (запуск на PR в `develop`/`main` и на push в
них). Джобы:

- `lint` — `ruff check` и `ruff format --check` (проверка типов `mypy`
  отключена);
- `unit` — `pytest tests/unit`;
- `integration` — полный прогон через `docker-compose.test.yml` (PostgreSQL на
  tmpfs, Ollama замокана);
- `build-and-stage` — только при push в `develop`, после `lint`, `unit` и
  `integration`: собирает образ (цель `production`), публикует его в GHCR
  (`ghcr.io/sun1tar/smai-recipes:sha-<8 символов>`) и отправляет событие
  `stage-service` в репозиторий `Sun1tAr/smai-infra`; env сервиса передаётся
  блоком секрета `RECIPES_ENV`;
- `build-and-prod` — то же при push в `main`, окружение `prod`.

Деплой выполняет `smai-infra` (stop-first, одна реплика); значения env задаёт
DevOps.

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
