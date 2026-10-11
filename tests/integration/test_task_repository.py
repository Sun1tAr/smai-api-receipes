import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from receipes.domain import (
    DomainError,
    GenerationParameters,
    GenerationTaskNotFound,
    InvalidTaskTransition,
    TaskStatus,
)
from receipes.models import GenerationTask, PromptTemplate
from receipes.repositories import (
    INTERRUPTED_ERROR,
    INVALID_PARAMETERS_ERROR,
    QUEUE_TIMEOUT_ERROR,
    MenuRepository,
    PromptTemplateRepository,
    TaskRepository,
)
from tests.integration.seed import make_task, menu_data, parameters, start

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


async def claim(repository: TaskRepository) -> GenerationTask:
    claimed = await repository.claim_next(prompt_for)
    assert claimed is not None
    return claimed


async def latest_template(repository: PromptTemplateRepository) -> str:
    template = await repository.latest()
    assert template is not None
    return template.template


def prompt_for(parameters: GenerationParameters) -> str:
    return f"{parameters.people_count} people, {parameters.weekly_budget}"


async def test_create_and_get_a_pending_task(session) -> None:
    repository = TaskRepository(session)
    created = await repository.create(parameters(3, "999.99"))
    task = await repository.get(created.task_id)
    assert task.status == TaskStatus.PENDING
    assert (task.prompt, task.menu_id, task.error) == (None, None, None)
    assert (task.started_at, task.finished_at) == (None, None)
    assert task.created_at is not None
    record = await session.scalar(
        text("SELECT weekly_budget FROM generation_parameters WHERE id = :id"),
        {"id": task.parameters_id},
    )
    assert record == Decimal("999.99")


async def test_get_unknown_task_raises(session) -> None:
    missing = uuid.uuid4()
    with pytest.raises(GenerationTaskNotFound) as error:
        await TaskRepository(session).get(missing)
    assert error.value.task_id == missing


async def test_claim_next_takes_the_oldest_pending_task_once(session) -> None:
    newer = await make_task(session, timedelta(minutes=1))
    oldest = await make_task(session, timedelta(minutes=5))
    repository = TaskRepository(session)
    claimed = await claim(repository)
    assert claimed.task_id == oldest.task_id
    assert claimed.status == TaskStatus.IN_PROGRESS
    assert claimed.prompt == "2 people, 1500.50"
    assert claimed.started_at is not None
    again = await claim(repository)
    assert again.task_id == newer.task_id
    assert await repository.claim_next(prompt_for) is None


async def test_claim_next_on_an_empty_queue_returns_none(session) -> None:
    assert await TaskRepository(session).claim_next(prompt_for) is None


async def test_claim_next_ignores_tasks_that_are_not_pending(session) -> None:
    repository = TaskRepository(session)
    task = await make_task(session)
    assert await repository.transition(
        task.task_id, TaskStatus.PENDING, TaskStatus.FAILED, error="x"
    )
    assert await repository.claim_next(prompt_for) is None


async def test_concurrent_claims_skip_a_locked_task(session_factory) -> None:
    async with session_factory() as setup:
        oldest = await make_task(setup, timedelta(minutes=5))
        newer = await make_task(setup, timedelta(minutes=1))
        await setup.commit()
    first_claimed, release = asyncio.Event(), asyncio.Event()
    results: dict[str, uuid.UUID | None] = {}

    async def first_worker() -> None:
        async with session_factory() as session:
            claimed = await claim(TaskRepository(session))
            results["first"] = claimed.task_id
            first_claimed.set()
            await release.wait()  # транзакция остаётся открытой
            await session.commit()

    async def second_worker() -> None:
        await first_claimed.wait()
        async with session_factory() as session:
            claimed = await TaskRepository(session).claim_next(prompt_for)
            results["second"] = claimed.task_id if claimed else None
            third = await TaskRepository(session).claim_next(prompt_for)
            results["third"] = third.task_id if third else None
            await session.commit()
        release.set()

    await asyncio.gather(first_worker(), second_worker())
    assert results == {
        "first": oldest.task_id,
        "second": newer.task_id,
        "third": None,
    }


async def test_transition_is_conditional_on_the_expected_status(session) -> None:
    repository = TaskRepository(session)
    task = await make_task(session)
    await start(session, task.task_id)
    # Чужой статус: задача уже не pending.
    assert not await repository.transition(
        task.task_id, TaskStatus.PENDING, TaskStatus.FAILED, error="late"
    )
    menu_id = await MenuRepository(session).save(menu_data())
    assert await repository.transition(
        task.task_id, TaskStatus.IN_PROGRESS, TaskStatus.DONE, menu_id=menu_id
    )
    done = await repository.get(task.task_id)
    assert (done.status, done.menu_id, done.error) == (TaskStatus.DONE, menu_id, None)
    assert done.started_at is not None and done.finished_at is not None
    assert not await repository.transition(
        task.task_id, TaskStatus.IN_PROGRESS, TaskStatus.DONE, menu_id=menu_id
    )


async def test_failed_transition_records_the_error(session) -> None:
    repository = TaskRepository(session)
    task = await make_task(session)
    assert await repository.transition(
        task.task_id, TaskStatus.PENDING, TaskStatus.FAILED, error="Neurocore timeout"
    )
    failed = await repository.get(task.task_id)
    assert (failed.status, failed.error) == (TaskStatus.FAILED, "Neurocore timeout")
    assert failed.finished_at is not None and failed.started_at is None


async def test_transition_to_a_missing_task_is_false(session) -> None:
    assert not await TaskRepository(session).transition(
        uuid.uuid4(), TaskStatus.PENDING, TaskStatus.FAILED, error="x"
    )


@pytest.mark.parametrize(
    ("expected", "target"),
    [
        (TaskStatus.DONE, TaskStatus.PENDING),
        (TaskStatus.FAILED, TaskStatus.IN_PROGRESS),
        (TaskStatus.PENDING, TaskStatus.DONE),
        (TaskStatus.PENDING, TaskStatus.PENDING),
    ],
)
async def test_forbidden_transition_is_rejected_before_the_database(
    session, expected, target
) -> None:
    with pytest.raises(InvalidTaskTransition):
        await TaskRepository(session).transition(uuid.uuid4(), expected, target)


@pytest.mark.parametrize(
    ("expected", "target"),
    [
        (TaskStatus.IN_PROGRESS, TaskStatus.DONE),  # нет menu_id
        (TaskStatus.IN_PROGRESS, TaskStatus.FAILED),  # нет error
    ],
)
async def test_transition_requires_the_fields_of_the_target_status(
    session, expected, target
) -> None:
    with pytest.raises(ValueError):
        await TaskRepository(session).transition(uuid.uuid4(), expected, target)


async def test_in_progress_is_reachable_only_through_claim_next(session) -> None:
    with pytest.raises(ValueError, match="claim_next"):
        await TaskRepository(session).transition(
            uuid.uuid4(), TaskStatus.PENDING, TaskStatus.IN_PROGRESS
        )


async def test_lost_done_transition_leaves_no_menu(session_factory) -> None:
    async with session_factory() as session:
        task = await make_task(session)
        assert await TaskRepository(session).transition(
            task.task_id, TaskStatus.PENDING, TaskStatus.FAILED, error="expired"
        )
        await session.commit()
    async with session_factory() as session:
        menu_id = await MenuRepository(session).save(menu_data())
        won = await TaskRepository(session).transition(
            task.task_id, TaskStatus.IN_PROGRESS, TaskStatus.DONE, menu_id=menu_id
        )
        assert not won
        await session.rollback()  # так поступает вызывающий код
    async with session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM menu")) == 0
        assert await session.scalar(text("SELECT count(*) FROM recipe")) == 0


async def test_expire_stale_fails_old_pending_and_stuck_in_progress(session) -> None:
    repository = TaskRepository(session)
    old_pending = await make_task(session, timedelta(minutes=20))
    fresh_pending = await make_task(session, timedelta(minutes=1))
    stuck = await make_task(session, timedelta(minutes=30))
    fresh_running = await make_task(session, timedelta(minutes=2))
    await start(session, stuck.task_id, timedelta(minutes=20))
    await start(session, fresh_running.task_id, timedelta(minutes=1))
    expired = await repository.expire_stale(
        timedelta(minutes=15), timedelta(minutes=10)
    )
    assert expired == 2
    states = {
        name: await repository.get(task.task_id)
        for name, task in (
            ("old_pending", old_pending),
            ("fresh_pending", fresh_pending),
            ("stuck", stuck),
            ("fresh_running", fresh_running),
        )
    }
    assert (states["old_pending"].status, states["old_pending"].error) == (
        TaskStatus.FAILED,
        QUEUE_TIMEOUT_ERROR,
    )
    assert (states["stuck"].status, states["stuck"].error) == (
        TaskStatus.FAILED,
        INTERRUPTED_ERROR,
    )
    assert states["old_pending"].finished_at is not None
    assert states["fresh_pending"].status == TaskStatus.PENDING
    assert states["fresh_running"].status == TaskStatus.IN_PROGRESS
    assert (
        await repository.expire_stale(timedelta(minutes=15), timedelta(minutes=10)) == 0
    )


async def test_expire_stale_does_not_touch_finished_tasks(session) -> None:
    repository = TaskRepository(session)
    task = await make_task(session, timedelta(hours=5))
    await repository.transition(
        task.task_id, TaskStatus.PENDING, TaskStatus.FAILED, error="boom"
    )
    assert (
        await repository.expire_stale(timedelta(minutes=1), timedelta(minutes=1)) == 0
    )
    assert (await repository.get(task.task_id)).error == "boom"


async def test_pending_queue_metrics(session) -> None:
    repository = TaskRepository(session)
    assert await repository.count_pending() == 0
    assert await repository.oldest_pending_age() is None
    await make_task(session, timedelta(minutes=10))
    await make_task(session, timedelta(minutes=1))
    done = await make_task(session, timedelta(hours=1))
    await repository.transition(
        done.task_id, TaskStatus.PENDING, TaskStatus.FAILED, error="x"
    )
    assert await repository.count_pending() == 2
    age = await repository.oldest_pending_age()
    assert age is not None
    assert timedelta(minutes=10) <= age < timedelta(minutes=11)


async def test_latest_prompt_template(session) -> None:
    repository = PromptTemplateRepository(session)
    assert await repository.latest() is None
    ids = sorted(uuid.uuid4() for _ in range(3))
    await session.execute(
        text(
            "INSERT INTO prompt_template VALUES (:a, 'old', now() - interval '1 day')"
        ),
        {"a": ids[2]},
    )
    # Одинаковый created_at: побеждает больший id.
    for template, template_id in (("tie-low", ids[0]), ("tie-high", ids[1])):
        await session.execute(
            text("INSERT INTO prompt_template VALUES (:i, :t, '2026-01-01+00')"),
            {"i": template_id, "t": template},
        )
    session.add(PromptTemplate(id=uuid.uuid4(), template="newest"))
    await session.flush()
    assert await latest_template(repository) == "newest"
    await session.execute(text("DELETE FROM prompt_template WHERE template = 'newest'"))
    assert await latest_template(repository) == "old"
    await session.execute(text("DELETE FROM prompt_template WHERE template = 'old'"))
    assert await latest_template(repository) == "tie-high"


async def test_complete_saves_the_menu_and_finishes_the_task(session) -> None:
    repository = TaskRepository(session)
    task = await make_task(session)
    await start(session, task.task_id)
    assert await repository.complete(task.task_id, menu_data())
    done = await repository.get(task.task_id)
    assert done.status == TaskStatus.DONE and done.menu_id is not None
    menu = await MenuRepository(session).load(done.menu_id)
    assert len(menu.menu_recipes) == 3


async def counts(session) -> dict[str, int]:
    tables = (
        "menu menu_recipes recipe nutrition instruction recipe_ingredients ingredient"
    ).split()
    return {
        table: await session.scalar(text(f"SELECT count(*) FROM {table}"))
        for table in tables
    }


async def test_lost_complete_leaves_no_menu_data_behind(session) -> None:
    repository = TaskRepository(session)
    existing = await make_task(session)
    await start(session, existing.task_id)
    # Справочник уже содержит «молоко»: оно переживает откат, новая «мука» — нет.
    await session.execute(
        text("INSERT INTO ingredient VALUES (gen_random_uuid(), 'Молоко', 'молоко')")
    )
    task = await make_task(session, timedelta(hours=1))
    await start(session, task.task_id, timedelta(hours=1))
    assert await repository.expire_stale(timedelta(days=1), timedelta(minutes=10)) == 1
    before = await counts(session)
    assert not await repository.complete(task.task_id, menu_data())
    after = await counts(session)
    assert after == before
    assert after["ingredient"] == 1
    assert (await repository.get(task.task_id)).status == TaskStatus.FAILED
    # Внешняя транзакция жива: следующая завершённая задача сохраняется.
    assert await repository.complete(existing.task_id, menu_data())
    assert (await counts(session))["menu"] == 1


async def test_complete_does_not_write_when_the_task_is_not_in_progress(
    session, monkeypatch
) -> None:
    async def forbidden(self, generated):
        raise AssertionError("the menu must not be saved")

    monkeypatch.setattr(MenuRepository, "save", forbidden)
    task = await make_task(session)  # pending, не in_progress
    assert not await TaskRepository(session).complete(task.task_id, menu_data())
    assert not await TaskRepository(session).complete(uuid.uuid4(), menu_data())


async def test_complete_rolls_back_when_the_transition_is_lost(
    session, monkeypatch
) -> None:
    repository = TaskRepository(session)
    task = await make_task(session)
    await start(session, task.task_id)
    before = await counts(session)

    async def lost(*args, **kwargs):
        return False

    monkeypatch.setattr(repository, "transition", lost)
    assert not await repository.complete(task.task_id, menu_data())
    assert await counts(session) == before


async def test_complete_rolls_back_when_saving_fails(session) -> None:
    repository = TaskRepository(session)
    task = await make_task(session)
    await start(session, task.task_id)
    broken = menu_data()
    broken.recipes[0].name = "я" * 300  # обходит «ворота», ловится колонкой
    before = await counts(session)
    with pytest.raises(Exception):  # noqa: B017  # DBAPIError драйвера
        await repository.complete(task.task_id, broken)
    assert await counts(session) == before
    # Savepoint откатан, внешняя транзакция по-прежнему рабочая.
    assert (await repository.get(task.task_id)).status == TaskStatus.IN_PROGRESS


async def test_poison_task_fails_and_the_next_one_is_claimed(session) -> None:
    repository = TaskRepository(session)
    poison = await make_task(session, timedelta(minutes=5))
    healthy = await make_task(session, timedelta(minutes=1))
    calls: list[int] = []

    def prompt(parameters: GenerationParameters) -> str:
        calls.append(parameters.people_count)
        if len(calls) == 1:
            raise DomainError("cannot build the prompt")
        return "prompt"

    claimed = await repository.claim_next(prompt)
    assert claimed is not None
    assert claimed.task_id == healthy.task_id
    failed = await repository.get(poison.task_id)
    assert (failed.status, failed.error) == (
        TaskStatus.FAILED,
        INVALID_PARAMETERS_ERROR,
    )
    assert failed.finished_at is not None and failed.started_at is None
    assert await repository.count_pending() == 0


async def test_only_poison_tasks_leave_the_queue_empty(session, monkeypatch) -> None:
    import receipes.repositories as module

    monkeypatch.setattr(module, "MAX_CLAIM_ATTEMPTS", 3)
    repository = TaskRepository(session)
    for age in range(5, 0, -1):
        await make_task(session, timedelta(minutes=age))

    def broken(parameters: GenerationParameters) -> str:
        raise DomainError("no")

    # Цикл ограничен: три итерации пропускают три задачи, две остаются в очереди.
    assert await repository.claim_next(broken) is None
    assert await repository.count_pending() == 2


async def test_started_and_finished_use_the_real_clock_inside_a_transaction(
    session,
) -> None:
    repository = TaskRepository(session)
    task = await make_task(session)
    # now() заморожен в начале транзакции, clock_timestamp() — нет.
    await session.execute(text("SELECT pg_sleep(0.05)"))
    claimed = await claim(repository)
    assert claimed.started_at is not None
    assert claimed.started_at > claimed.created_at
    await session.execute(text("SELECT pg_sleep(0.05)"))
    menu_id = await MenuRepository(session).save(menu_data())
    assert await repository.transition(
        task.task_id, TaskStatus.IN_PROGRESS, TaskStatus.DONE, menu_id=menu_id
    )
    done = await repository.get(task.task_id)
    assert done.finished_at and done.started_at
    assert done.finished_at > done.started_at


async def test_templates_created_in_one_transaction_are_ordered_by_time(
    session,
) -> None:
    # Без явного created_at: порядок задаёт clock_timestamp() по умолчанию, а не id.
    for template in ("first", "second", "third"):
        session.add(PromptTemplate(id=uuid.uuid4(), template=template))
        await session.flush()
    assert await latest_template(PromptTemplateRepository(session)) == "third"
