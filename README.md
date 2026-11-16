# Smart Meal AI — Recipes Service (MS-4)

Сервис рецептов **Smart Meal AI** — ядро домена: асинхронная генерация меню через
Ollama, хранение рецептов и меню, замена блюд, лайки, история меню и подбор
товаров ВкусВилла для корзины.

> **Ветка.** Актуальный код и полная документация — в ветке `develop`. Этот
> README описывает состояние `develop`.

## Ответственность

- приём задачи генерации меню и её фоновое выполнение;
- сборка промпта из шаблона, параметров пользователя и справочников MS-2;
- вызов Ollama и валидация ответа модели;
- хранение меню, рецептов, ингредиентов и инструкций;
- замена одного рецепта в готовом меню;
- лайки и избранные рецепты;
- история готовых меню пользователя;
- подбор товаров и ссылка на корзину ВкусВилла через MCP.

## Стек

- Python 3.12, FastAPI;
- SQLAlchemy 2 (async) + asyncpg, PostgreSQL, Alembic;
- Ollama (`qwen2.5:7b` по умолчанию);
- MCP ВкусВилла для корзины;
- httpx, Pydantic Settings;
- structlog, OpenTelemetry, Prometheus;
- uv, Ruff, pytest, Docker Compose, GitHub Actions.

## API

Интерактивная документация — `/docs`, схема OpenAPI — `/openapi.json`.

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/generate-menu` | API-1: создать задачу генерации меню (202 `{"task_id"}`) |
| `GET` | `/generate-menu/{task_id}` | API-2: статус, меню и ошибка задачи |
| `POST` | `/generate-menu/{task_id}/replace-recipe` | API-18: заменить рецепт в меню |
| `GET` | `/profile/parameters` | API-13: параметры последней готовой генерации |
| `GET` | `/profile/menus` | API-22: история готовых меню пользователя |
| `POST` | `/recipes/{recipe_id}/like` | API-19: поставить лайк |
| `DELETE` | `/recipes/{recipe_id}/like` | API-20: убрать лайк |
| `GET` | `/recipes/liked` | API-21: избранные рецепты |
| `POST` | `/recipes/cart` | API-14: подобрать товары для рецепта |
| `POST` | `/recipes/cart/link` | API-16: ссылка на корзину ВкусВилла |
| `GET` | `/health` | Готовность БД (Ollama не проверяется) |
| `GET` | `/metrics` | Метрики Prometheus |

Пользователь определяется доверенным заголовком `X-User-Id`, который ставит
gateway по JWT. Без него гостевые сценарии (генерация, просмотр статуса и
замена) работают, а профильные и лайки отвечают 401.

### Генерация меню

`POST /generate-menu` принимает:

```json
{
  "peopleCount": 4,
  "weeklyBudget": 5000,
  "habits": [1, 2],
  "diet": 3,
  "allergies": [1],
  "kitchenTools": [1, 4]
}
```

- `peopleCount` — 1–20, `weeklyBudget` — больше 0 (до 2 знаков после запятой);
- `habits` и `kitchenTools` обязательны (можно `[]`), `diet` — число или `null`,
  `allergies` — можно не передавать;
- ID проверяются по справочникам MS-2; неизвестный ID даёт 400 `unknown_id`;
- задача сохраняется со снимком промпта и выполняется фоновым воркером.

`GET /generate-menu/{task_id}` всегда возвращает `status`, `menu`, `error`:
`menu` — только при `done`, `error` — только при `failed`. Каждый рецепт содержит
`recipeId` — UUID сохранённого рецепта.

### Фоновая генерация

Воркер — один asyncio-таск внутри процесса сервиса. Он берёт самую старую
`pending`-задачу (`FOR UPDATE SKIP LOCKED`), делает один запрос к Ollama и в одной
транзакции сохраняет меню и ставит `done`. Ошибка после захвата переводит задачу
в `failed`.

- Промпт собирается из активного шаблона `prompt_template` и сохраняется в
  задаче при создании, поэтому смена шаблона не влияет на очередь.
- В промпт попадают ограничения пользователя (привычки, диета, аллергены,
  техника) и последние лайки (`LIKES_IN_PROMPT_LIMIT`).
- Валидный ответ модели — не меньше 3 блюд, без дублей и пустых строк.
- Невалидный ответ повторяется `GENERATION_BAD_RESPONSE_RETRIES` раз; таймаут,
  обрезка ответа и недоступность Ollama не повторяются.
- При сбоях Ollama воркер уходит в паузу 5 → 10 → 20 → 40 → 60 секунд.
- Одна реплика и один процесс; дополнительно воркер держит
  `pg_try_advisory_lock`, чтобы второй экземпляр не взял очередь.

Тексты ошибок задачи: `Dictionaries unavailable`, `Neurocore unavailable`,
`Neurocore timeout`, `Neurocore model not found`, `Neurocore error`,
`Invalid neurocore response`, `Neurocore response truncated`,
`Service restarted`, `Menu generation failed`.

### Замена рецепта

API-18 ставит задачу `replace` в общую очередь и ждёт результат в пределах
`REPLACE_WAIT_SECONDS`. Воркер использует активный шаблон `replace`, вставляет
параметры исходного меню и ограничения, запрашивает один рецепт и заменяет
выбранную связь. Старый рецепт сохраняется в БД. Поздний ответ модели меню не
изменяет.

### Корзина

`POST /recipes/cart` принимает UUID сохранённого негостевого рецепта и возвращает
`matches`, `total`, `allMatched`, `unmatchedCount`. Товары подбираются по
категориям и названиям каталога ВкусВилла через MCP; при частичном подборе
позиции без совпадения остаются с `error` и `q: null`. Бюджет запроса —
`CART_DEADLINE_SECONDS`.

`POST /recipes/cart/link` принимает 1–20 позиций `{id, q}` из API-14 и возвращает
одну HTTPS-ссылку на корзину. Без настроенного `VKUSVILL_MCP_URL` готовый
негостевой рецепт получает 503.

## Конфигурация

Настройки читаются из окружения (локально — из `.env`). Пустое значение считается
незаданным; некорректная конфигурация останавливает запуск.

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | PostgreSQL | обязательны |
| `DB_PORT` | Порт PostgreSQL | `5432` |
| `DB_TIMEOUT_SECONDS` | Предельное ожидание БД | `5` |
| `OLLAMA_BASE_URL` | Адрес Ollama | обязательна |
| `OLLAMA_MODEL` | Модель (тег должен быть загружен) | `qwen2.5:7b` |
| `OLLAMA_TIMEOUT_SECONDS` | Таймаут ответа | `300` |
| `OLLAMA_NUM_CTX` | Размер контекста; ≥ `OLLAMA_NUM_PREDICT + 1536` | `8192` |
| `OLLAMA_NUM_PREDICT` | Максимум токенов ответа | `4096` |
| `OLLAMA_TEMPERATURE` | Температура | `0.3` |
| `OLLAMA_KEEP_ALIVE` | Время удержания модели | `30m` |
| `MS2_BASE_URL` | Адрес сервиса справочников | обязательна |
| `MS2_TIMEOUT_SECONDS` | Таймаут запроса к MS-2 | `5` |
| `VKUSVILL_MCP_URL` | URL MCP ВкусВилла | нет; корзина отвечает 503 |
| `MCP_CALL_TIMEOUT_SECONDS` | Бюджет одной попытки MCP | `5` |
| `MCP_CALL_RETRIES` | Повторы временных ошибок MCP | `1` |
| `MCP_CONCURRENCY` | Параллельных обращений к MCP | `4` |
| `MCP_CACHE_TTL_SECONDS` | Время кэша успешного ответа | `900` |
| `CART_DEADLINE_SECONDS` | Общий бюджет API-14 | `35` |
| `GENERATION_WORKER_ENABLED` | Включить воркер | `true` |
| `GENERATION_WORKER_INTERVAL_SECONDS` | Интервал опроса очереди | `2` |
| `GENERATION_BAD_RESPONSE_RETRIES` | Повторы невалидного ответа | `1` |
| `GENERATION_METRICS_REFRESH_SECONDS` | Обновление gauge очереди | `15` |
| `REPLACE_WAIT_SECONDS` | Бюджет замены рецепта | `120` |
| `LIKES_IN_PROMPT_LIMIT` | Лайков в промпте, 0–10 | `10` |
| `RECEIPES_HTTP_PORT` | Хостовой порт Compose | `8001` |

## Данные

Основные таблицы: `prompt_template`, `generation_parameters`,
`generation_parameter_habits`, `generation_parameter_allergies`,
`generation_parameter_kitchen_tools`, `generation_task`, `menu`, `menu_recipes`,
`recipe`, `recipe_ingredients`, `recipe_like`, `ingredient`, `instruction`,
`nutrition`.

Миграции: `0001_domain`, `0002_seed_prompt_template`,
`0003_generation_task_status_changed_at`, `0004_generation_parameters_v2`,
`0005_parameters_user_id`, `0006_recipe_replacement`, `0007_recipe_like`.
Таблица версий Alembic — `alembic_version_receipes`.

## Метрики очереди

`/metrics` публикует `recipes_generation_tasks`,
`recipes_generation_task_oldest_age_seconds`,
`recipes_generation_task_duration_seconds`,
`recipes_generation_task_processing_seconds` и
`recipes_generation_tasks_refresh_timestamp_seconds` с метками `kind`
(`menu`/`replace`) и `status`. Gauge обновляются фоновой задачей раз в
`GENERATION_METRICS_REFRESH_SECONDS`.

## Локальный запуск

```sh
cp .env.example .env
docker compose up --build --wait
curl --fail http://127.0.0.1:8001/health
curl --fail http://127.0.0.1:8001/docs
docker compose down -v
```

Нужны Docker, Docker Compose и доступная Ollama. Сервис в Compose называется
`recipes`, контейнер слушает `8000`, на хосте публикуется
`127.0.0.1:${RECEIPES_HTTP_PORT:-8001}`. При старте выполняется
`alembic upgrade head`, затем запускается uvicorn. Контейнер работает от
пользователя `receipes` (UID 10001).

## Разработка и тесты

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit

docker compose -p receipes-tests -f docker-compose.test.yml up --build \
  --abort-on-container-exit --exit-code-from tests
docker compose -p receipes-tests -f docker-compose.test.yml down
```

Интеграционные тесты (маркер `integration`) требуют отдельную PostgreSQL через
`TEST_DB_*`; тестовый Compose поднимает её на tmpfs и мокает Ollama. CI выполняет
lint, unit, integration, compose-smoke и сборку образа; при push в `develop` и
`main` публикует образ в GHCR и запускает stage/prod-деплой через `smai-infra`.

## Известные ограничения

- Одна реплика и один процесс; воркер не масштабируется горизонтально.
- Миграции в `docker/entrypoint.sh` запускаются без advisory lock: при
  нескольких репликах `alembic upgrade head` не сериализуется.
- Alembic использует те же обязательные `OLLAMA_*` настройки, что и приложение.
- Базовые образы закреплены тегами, а не digest.
- Худший случай генерации — `(1 + повторы) × OLLAMA_TIMEOUT_SECONDS`, всё это
  время очередь стоит.

## Связанные репозитории

- [SmartMeal-AI](https://github.com/Sun1tAr/SmartMeal-AI);
- [smai-api-gateway](https://github.com/Sun1tAr/smai-api-gateway) — публикует API наружу;
- [smai-api-dictionary](https://github.com/Sun1tAr/smai-api-dictionary) — справочники;
- [smai-infra](https://github.com/Sun1tAr/smai-infra) — деплой и наблюдаемость.
