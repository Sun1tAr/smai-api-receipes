import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from seed import make_task, menu_data, parameters
from sqlalchemy import text

from receipes.domain import (
    GenerationParameters,
    GenerationTaskNotFound,
    InvalidTaskTransition,
    TaskStatus,
)
from receipes.models import PromptTemplate
from receipes.repositories import (
    INTERRUPTED_ERROR,
    QUEUE_TIMEOUT_ERROR,
    MenuRepository,
    PromptTemplateRepository,
    TaskRepository,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST is not set"),
]


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
    claimed = await repository.claim_next(prompt_for)
    assert claimed.task_id == oldest.task_id
    assert claimed.status == TaskStatus.IN_PROGRESS
    assert claimed.prompt == "2 people, 1500.50"
    assert claimed.started_at is not None
    again = await repository.claim_next(prompt_for)
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
            claimed = await TaskRepository(session).claim_next(prompt_for)
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
    assert await repository.transition(
        task.task_id, TaskStatus.PENDING, TaskStatus.IN_PROGRESS, prompt="p"
    )
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
        (TaskStatus.PENDING, TaskStatus.IN_PROGRESS),  # нет prompt
        (TaskStatus.IN_PROGRESS, TaskStatus.DONE),  # нет menu_id
        (TaskStatus.IN_PROGRESS, TaskStatus.FAILED),  # нет error
    ],
)
async def test_transition_requires_the_fields_of_the_target_status(
    session, expected, target
) -> None:
    with pytest.raises(ValueError):
        await TaskRepository(session).transition(uuid.uuid4(), expected, target)


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
    for task in (stuck, fresh_running):
        await repository.transition(
            task.task_id, TaskStatus.PENDING, TaskStatus.IN_PROGRESS, prompt="p"
        )
    await session.execute(
        text(
            "UPDATE generation_task SET started_at = now() - interval '20 minutes' "
            "WHERE task_id = :id"
        ),
        {"id": stuck.task_id},
    )
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
    assert (await repository.latest()).template == "newest"
    await session.execute(text("DELETE FROM prompt_template WHERE template = 'newest'"))
    assert (await repository.latest()).template == "old"
    await session.execute(text("DELETE FROM prompt_template WHERE template = 'old'"))
    assert (await repository.latest()).template == "tie-high"
