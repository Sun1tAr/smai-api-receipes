# smai-api-receipes

MS-4 — сервис рецептов SmartMeal: ядро домена рецептов и генерация меню через
Ollama. Готовы инфраструктура (**SMAI-58**: каркас приложения, PostgreSQL,
миграции, health, метрики, трейсы и логи, Docker/Compose, CI) и слой данных и
домен (**SMAI-57**: модели, миграции, доменные правила, репозитории, «ворота»
ответа модели). Бизнес-эндпоинты API-1/API-2, воркер генерации, клиент Ollama,
шаблон промпта и его начальное наполнение появятся в задачах SMAI-55, SMAI-56 и
SMAI-54.

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
| `OLLAMA_NUM_PREDICT` | Максимум токенов ответа (JSON с 21 рецептом не влезает в 4096; вместе с промптом ≈1 тыс. токенов помещается в `OLLAMA_NUM_CTX`) | `16384` |
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
uv run mypy
uv run pytest tests/unit
```

Интеграционным тестам нужна PostgreSQL; быстрый способ без Compose:

```sh
docker run -d --name receipes-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_USER=dev \
  -e POSTGRES_DB=dev -p 127.0.0.1:55432:5432 postgres:17-alpine
TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=55432 TEST_DB_NAME=dev TEST_DB_USER=dev \
  TEST_DB_PASSWORD=pw uv run pytest
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
| `generation_parameters` | `people_count` (1–20), `weekly_budget` Numeric(12,2) (0 < x ≤ 1 000 000) |
| `generation_task` | `task_id`, `status`, `prompt`, `parameters_id` (NOT NULL, UNIQUE, 1:1), `menu_id` (NULL до `done`), `created_at`, `started_at`, `finished_at`, `error` |
| `menu` | только `id` |
| `menu_recipes` | `menu_id`, `recipe_id`, `position` |
| `recipe` | `name` (255), `calories`, `cooking_time` (минуты), `photo` (Text, NULL), `nutrition_id` |
| `ingredient` | `name` (255, отображаемое написание), `name_key` (255, UNIQUE, ключ дедупликации) |
| `recipe_ingredients` | `recipe_id`, `ingredient_id`, `quantity`, `unit` (32), `position` |
| `instruction` | `recipe_id`, `text`, `position` |
| `nutrition` | `calories`, `proteins`, `fats`, `carbohydrates` |

Числа (калории, КБЖУ, количества, время готовки) — `Numeric(10,2)`; время
`created_at`/`started_at`/`finished_at` — `timestamptz`. `generation_task.created_at`
по умолчанию `now()` (начало транзакции), а `started_at`/`finished_at` пишутся
`clock_timestamp()`: иначе при захвате задачи в той же транзакции, где она
создана, `started_at` совпал бы с `created_at`.

**Ограничения `generation_task`:** `status IN ('pending','in_progress','done','failed')`;
`(status='done') = (menu_id IS NOT NULL)`; `(status='failed') = (error IS NOT NULL)`;
`prompt` и `started_at` обязательны при `in_progress`/`done`; `finished_at` —
при `done`/`failed`. Остальное: `UNIQUE(menu_recipes.menu_id, recipe_id)` и
`(menu_id, position)`, `UNIQUE(recipe_ingredients.recipe_id, ingredient_id)` и
`(recipe_id, position)`, `UNIQUE(instruction.recipe_id, position)`, позиции ≥1,
неотрицательные числа, границы `generation_parameters` (те же, что требует домен:
строка вне них не может встать в очередь). Частичные индексы:
`generation_task(created_at) WHERE status='pending'` (очередь) и
`generation_task(started_at) WHERE status='in_progress'` (восстановление зависших).

**Каскады.** `menu` → `menu_recipes` и `recipe` → `recipe_ingredients`,
`instruction`: `ON DELETE CASCADE`. Все остальные ссылки — `RESTRICT`: нельзя
удалить рецепт, входящий в меню, ингредиент, нутриенты или параметры, на которые
есть ссылки, и меню, на которое ссылается задача (иначе задача `done` потеряла бы
результат, нарушив `menu_iff_done`).

**Ревизии Alembic.**
- `0001_base_schema` — таблицы и колонки ровно по ERD плюс ограничения целостности
  (CHECK, UNIQUE, индексы, FK с политиками удаления), не добавляющие колонок.
- `0002_erd_deviations` — отклонения от ERD, добавляющие колонки: `position` в
  `menu_recipes` и `recipe_ingredients` (без него порядок рецептов в меню и
  ингредиентов не определён), `started_at`/`finished_at` в `generation_task` (нужны
  для восстановления зависших задач и метрик длительности; плюс CHECK и индекс) и
  `ingredient.name_key` (нормализованный уникальный ключ: дедуплицирует ингредиенты,
  сохраняя отображаемое название). Отказ от отклонений — это не просто удаление
  файла ревизии: от этих колонок зависят `models.py` и `repositories.py`
  (`MenuRepository.save`/`load`, `TaskRepository.claim_next`/`transition`/
  `expire_stale`/`complete`), поэтому код и ревизию нужно откатывать вместе.

  Бэкфилл при `upgrade` 0002: `position` — `1..n` по `id`, `started_at`/
  `finished_at` — `created_at`, `name_key` — Python-функцией, **скопированной в
  миграцию** из приложения (`casefold` в SQL точно не повторить; копия намеренная:
  историческая миграция не должна меняться вместе с кодом); заодно в `name`
  схлопываются пробелы и убираются крайние. Если у существующих строк совпадут ключи, UNIQUE остановит миграцию,
  а не склеит ингредиенты молча.

Обе ревизии обратимы (`upgrade` / `downgrade` / `upgrade` проверяются в
интеграционных тестах, схема после `downgrade base` пуста, `alembic check` чист).
Ревизии редактировались до первого применения; локальную dev-БД созданную по
прежней версии нужно пересоздать: `docker compose down -v`.

**Названия ингредиентов.** `ingredient.name` хранит первое встреченное написание
(только `strip` и схлопывание пробелов; «Мёд» остаётся «Мёд»). Ключ `name_key`
(`strip`, пробелы, `casefold`, `ё`→`е`) считается только в Python — в
`domain.normalize_ingredient_name`, БД ключ не вычисляет. Вставка идёт через
`INSERT … ON CONFLICT (name_key) DO NOTHING`, затем `SELECT` по `name_key`; позднее
меню с другим написанием («мёд», «МЁД») название не меняет.

**Репозитории.**
- `TaskRepository`:
  - `create`; `get` (бросает `GenerationTaskNotFound`).
  - `claim_next(build_prompt)` — единственный путь в `in_progress`: `SELECT … FOR
    UPDATE SKIP LOCKED` старейшей `pending` и `UPDATE` со `started_at =
    clock_timestamp()`. Промпт задаётся при захвате, потому что CHECK требует его
    у любой задачи `in_progress`. Если параметры задачи невалидны
    (`InvalidGenerationParameters`), она условно переводится в `failed` с ошибкой
    `Invalid generation parameters`, и в том же вызове берётся следующая (не более 20
    итераций) — «ядовитая» задача не блокирует очередь. Ошибка шаблона
    (`InvalidPromptTemplate`) — дефект шаблона, а не задачи: `claim_next` её не
    ловит, задачи остаются `pending` (шаблон проверяет `builder()`). Если лимит итераций
    исчерпан, `claim_next` возвращает `None`, как при пустой очереди; оставшиеся
    `pending` воркер подберёт на следующем тике. Вызывающий коммитит
    транзакцию сразу после `claim_next`, чтобы не держать блокировку строки.
  - `complete(task_id, generated_menu)` — основной путь завершения: сначала
    `SELECT … FOR UPDATE` задачи в `in_progress` (нет такой, например её просрочил
    `expire_stale` — `False` без записи), затем в savepoint (`begin_nested`)
    сохраняет меню и условно переводит `in_progress` → `done`. При неудаче
    перехода или ошибке сохранения savepoint откатывается и возвращается `False`
    (или пробрасывается исключение): не остаётся ни меню, ни рецептов, ни
    нутриентов, ни шагов, ни связей, ни добавленных этим вызовом ингредиентов
    (ингредиенты, существовавшие раньше, не затрагиваются). Внешней транзакцией
    управляет вызывающий.
  - `transition(task_id, expected, to, …)` — условный `UPDATE … WHERE
    status=:expected RETURNING` в `done`/`failed`, `False` при чужом статусе;
    допустимость перехода проверяет `TaskStatus`. Прямое использование для `done`
    оставлено для тестов и частных случаев, в `in_progress` перехода нет.
  - `expire_stale(pending_ttl, stale_after)` (`Queue timeout`, `Generation
    interrupted`), `count_pending`, `oldest_pending_age`.
- `MenuRepository`: `save(GeneratedMenu)` (позиции с 1) и `load(menu_id)` — рецепты,
  ингредиенты и шаги отсортированы по `position`; бросает `MenuNotFound`. Связанные
  данные грузятся `selectinload`/`joinedload` (несколько запросов, без N+1).
- `PromptTemplateRepository.latest()` — `ORDER BY created_at DESC, id DESC`
  (`created_at` по умолчанию `clock_timestamp()`, поэтому шаблоны из одной транзакции
  различаются по времени; при полном равенстве решает больший `id`). Первый шаблон создаёт сид (SMAI-56):
  до этого `latest()` возвращает `None`.

**Ворота ответа модели** (`GeneratedMenu`): 3–21 рецепт (минимум 3 заложен
контрактом API-2 и форматом API-3), 1–50 ингредиентов и шагов, непустые строки
не длиннее колонок (шаг — до 10 000 символов), без `\x00` и суррогатов, конечные
числа ≥0 с верхними границами (калории ≤10000, время ≤1440 мин, количество
≤100000). Числа округляются до сотых, дубли ингредиентов в рецепте схлопываются
(одна единица — количества складываются, если сумма не превышает лимит; иначе, как
и при разных единицах, остаётся первое вхождение и пишется предупреждение), `photo`
игнорируется (не хранится, поэтому его содержимое не проверяется). Поля — camelCase
как в контракте API.

## Шаблон промпта

Шаблон хранится в `prompt_template` (актуален последний по `created_at`). Первый
шаблон кладёт seed-ревизия `0003_seed_prompt_template` (фиксированный `id`, откат
удаляет только эту строку). Текст — на русском: роль диетолога/повара, задача,
параметры, требования (3–21 рецепт, без ссылок на фото, без повторов ингредиентов
в рецепте) и формат ответа — строго один JSON в camelCase по схеме. Плейсхолдеры:
`{people_count}` и `{weekly_budget}`. Новую версию шаблона добавляют новой ревизией
с новой строкой (старую не правят).

Подстановка — `receipes.prompt.build_prompt(template, parameters)`: явная замена двух
плейсхолдеров, а не `str.format`, поэтому фигурные скобки JSON в тексте шаблона
безопасны. Число людей — целое, бюджет — всегда с двумя знаками без научной
нотации (`1000` → `1000.00`). Шаблон без одного из плейсхолдеров, с неизвестным
(`{слово}`: латинские буквы, цифры и `_`, например `{People}` или `{x1}`) или с
экранированием в стиле `str.format` (`{{people_count}}`: не поддерживается и
отвергается, а не превращается молча в `{2}`) даёт `InvalidPromptTemplate`
(`DomainError`). Это дефект шаблона, а не задачи, поэтому шаблон проверяется один
раз в `PromptTemplateRepository.builder()`: сломанный шаблон не переводит задачи в
`failed`, они остаются `pending`, а воркер (SMAI-54) логирует ошибку и ждёт
исправленной версии шаблона. `builder()` возвращает готовый `build_prompt` для
`claim_next(build_prompt)` на основе актуального шаблона (`PromptTemplateNotFound`,
если шаблонов нет); результат сохраняется в `generation_task.prompt`.

Для запроса к Ollama `generated_menu.ollama_format_schema()` даёт JSON-схему
параметра `format` (структурированный вывод; `photo` не запрашивается). Верхние
границы длин и числа элементов (`maxLength`, `maxItems`) в схему не входят:
llama.cpp разворачивает их в грамматику, что замедляет компиляцию и приводит к
отказам; их проверяют «ворота». Нижние границы и диапазоны чисел совпадают с
«воротами». Ответ, оборванный по `OLLAMA_NUM_PREDICT` (`done_reason=length`),
невалиден как JSON: воркер должен распознавать этот случай и помечать задачу
ошибкой понятным текстом — это работа SMAI-54; в smoke-скрипте он уже обработан.

**Ручной smoke против реальной Ollama** (в CI не запускается, нужна работающая
Ollama с моделью `qwen2.5:7b-instruct-q4_K_M`):

```sh
OLLAMA_BASE_URL=http://localhost:11434 uv run python -m scripts.ollama_smoke \
  --people 2 --budget 5000
```

Скрипт собирает промпт из шаблона seed-ревизии, отправляет `POST /api/generate` с
`format` и опциями по умолчанию (`temperature`, `num_ctx`, `num_predict`) и
проверяет ответ «воротами» `GeneratedMenu`. Код возврата: 0 — меню принято, 1 —
ошибка запроса, ответ обрезан по `num_predict` (`done_reason=length`) или не прошёл
«ворота», 2 — неверные параметры (в том числе нечисловой бюджет).

## Структура репозитория

```text
src/receipes/       приложение: config, database, main (create_app), health,
                    errors, observability, migration_diagnostics, __main__ (запуск uvicorn);
                    домен MS-4: models, domain, generated_menu, prompt, repositories
migrations/         Alembic (async env.py), ревизии в versions/ (0001–0003)
scripts/            ручные инструменты (ollama_smoke.py)
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
- Политики очистки данных нет: удаление меню оставляет рецепты, нутриенты и
  ингредиенты, удаление задачи оставляет `generation_parameters`.
- При неверной конфигурации контейнер уходит в перезапуск (`restart: unless-stopped`),
  а `docker compose up --wait` завершается ошибкой.
