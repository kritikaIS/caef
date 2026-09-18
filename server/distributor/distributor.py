"""Distributor — Task work queue + Event fan-out topic (TDD.md §2.3).

Two logical channels, modeled 1:1 on the AWS primitives the design names so the
v0.1 in-process implementation can be swapped for SQS/SNS without touching the
Listener or the Agent:

  - **Task queue** (SQS): one consumer group, the Agent. Serialized per
    `device_id` so two conflicting patches never race for the same target file.
  - **Event topic** (SNS): fan-out to observability subscribers (Frontend live
    feed). Publishing an Event never blocks on a subscriber.

`Distributor` is the interface every other component codes against; nothing
outside this module may import `LocalDistributor` directly except the process
entrypoints that construct it.
"""

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Protocol

from server.schemas import AgentTask, EventNotification

log = logging.getLogger("caef.distributor")

TaskHandler = Callable[[AgentTask], Awaitable[None]]
EventSubscriber = Callable[[EventNotification], Awaitable[None] | None]


class Distributor(Protocol):
    """Queue/topic abstraction. Swap the implementation, not the callers."""

    async def publish_task(self, task: AgentTask) -> None: ...

    async def publish_event(self, event: EventNotification) -> None: ...

    def subscribe(self, callback: EventSubscriber) -> None: ...

    async def drain(self, handler: TaskHandler) -> None: ...


class LocalDistributor:
    """In-process implementation for v0.1 (TRD.md §7: local simulations are
    correct at this stage; the interface is what has to survive)."""

    def __init__(self) -> None:
        self._tasks: asyncio.Queue[AgentTask] = asyncio.Queue()
        self._subscribers: list[EventSubscriber] = []
        # One in-flight generation per device (TDD.md §2.3). A slow device must
        # not stall the others, so the lock is per device_id, not global.
        self._device_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._inflight: set[asyncio.Task] = set()

    # --- Task queue (SQS-shaped) --------------------------------------------

    async def publish_task(self, task: AgentTask) -> None:
        log.info("task queued: %s device=%s event=%s", task.task_id, task.device_id, task.event)
        await self._tasks.put(task)

    async def drain(self, handler: TaskHandler) -> None:
        """Drain Loop (LOOPS.md §6). Runs until cancelled."""
        while True:
            task = await self._tasks.get()
            runner = asyncio.create_task(self._dispatch(handler, task))
            self._inflight.add(runner)
            runner.add_done_callback(self._inflight.discard)

    async def _dispatch(self, handler: TaskHandler, task: AgentTask) -> None:
        try:
            async with self._device_locks[task.device_id]:
                await handler(task)
        except Exception:
            # ponytail: no redelivery. The event's own retry_count (bounded by
            # MAX_RETRIES) is the retry authority per SAFETY_PROTOCOL.md §4 —
            # a second, unbounded retry mechanism here would let a poisoned task
            # loop past that cap. Add DLQ semantics when this moves to real SQS.
            log.exception("task handler failed: %s (dropped, not redelivered)", task.task_id)
        finally:
            self._tasks.task_done()

    async def join(self) -> None:
        """Block until every queued Task has been handled. Test/shutdown aid."""
        await self._tasks.join()

    # --- Event topic (SNS-shaped) -------------------------------------------

    def subscribe(self, callback: EventSubscriber) -> None:
        self._subscribers.append(callback)

    async def publish_event(self, event: EventNotification) -> None:
        for callback in self._subscribers:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Observability is not on the safety path: a broken subscriber
                # must never stop telemetry from being processed.
                log.exception("event subscriber failed for %s", event.event_id)
