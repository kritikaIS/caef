"""Stub Agent — echo consumer for the Task queue (TDD.md §4 step 3).

Exists so telemetry can be proven to flow end-to-end before the real Agentic
Core is wired (M6). It plans nothing, calls no model, and generates no code.

It deliberately stops at `AgentTask` -> logged echo. It does **not** emit an
`AgentOutput` toward deploy, because Guard Rail and Sandbox do not exist yet and
CLAUDE.md §3 forbids the Agent being the first thing to touch code execution.
Real generation replaces this class wholesale; the `handle` signature is the
contract the Distributor drains against.
"""

import logging

from server.db.models import Event, SessionLocal
from server.schemas import AgentTask

log = logging.getLogger("caef.agent.stub")


class StubAgent:
    """Records that a Task was received. No LLM, no code, no deploy."""

    def __init__(self) -> None:
        self.seen: list[AgentTask] = []

    async def handle(self, task: AgentTask) -> None:
        self.seen.append(task)
        with SessionLocal() as db:
            event = db.get(Event, task.event_id)
            retry_count = event.retry_count if event else 0
        log.info(
            "STUB handled task=%s device=%s trigger=%s event=%s retry=%s "
            "(no generation: Guard Rail/Sandbox not yet wired)",
            task.task_id,
            task.device_id,
            task.trigger_type,
            task.event,
            retry_count,
        )
