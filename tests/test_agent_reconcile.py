"""Agent 长任务崩溃对账：卡在 queued/running 的任务被兜底处理。"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.agent.queue import new_run_id, reconcile_stale_runs
from app.models import AgentRun


async def _make_run(db, status, age_seconds):
    run = AgentRun(
        id=new_run_id(),
        owner_id="owner",
        prompt="reconcile",
        status=status,
        version=1,
        updated_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )
    async with db.sessions() as session:
        session.add(run)
        await session.commit()
        return run.id


async def test_reconcile_redelivers_stale_queued_run(client):
    db = client.app.state.db
    queue = client.app.state.agent_queue
    run_id = await _make_run(db, "queued", 9999)

    # 未投递（模拟 commit 后崩溃），对账应重新投递
    await reconcile_stale_runs(db, queue, stale_seconds=120)

    # 重新投递后手动消费一条，任务应被处理
    from app.agent.queue import run_worker
    from app.agent.service import AgentService, MockAgent

    client.app.state.agent = AgentService(MockAgent(), 5)
    task = asyncio.create_task(run_worker(client.app))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "succeeded"


async def test_reconcile_marks_stale_running_failed(client):
    db = client.app.state.db
    queue = client.app.state.agent_queue
    run_id = await _make_run(db, "running", 9999)

    await reconcile_stale_runs(db, queue, stale_seconds=120)

    async with db.sessions() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "failed"
        assert run.error_code == "agent_reconcile_timeout"


async def test_reconcile_skips_fresh_runs(client):
    db = client.app.state.db
    queue = client.app.state.agent_queue
    fresh_queued = await _make_run(db, "queued", 1)
    fresh_running = await _make_run(db, "running", 1)

    await reconcile_stale_runs(db, queue, stale_seconds=120)

    async with db.sessions() as session:
        q = await session.get(AgentRun, fresh_queued)
        r = await session.get(AgentRun, fresh_running)
        assert q.status == "queued"
        assert r.status == "running"
