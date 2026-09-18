"""Task handler wiring the Agentic Core to Deploy and the Safety Rollback Protocol.

This is the component the arrows in ARCHITECTURE.md §1 point through: it is what
turns a `Task` off the Distributor into either a deployed artifact, a scheduled
reversion, or a rollback. It replaces `StubAgent` as the Distributor's handler —
the `handle` signature is the contract, unchanged.

The two loops it drives differ in exactly one respect (LOOPS.md §2 vs §4):

    CONTEXT_TRIGGER  -> morph_deploy, **schedules a reversion** (temporary)
    CRITICAL_FAILURE -> patch_deploy, **schedules nothing** (durable)

Order matters and is not negotiable. Generation is checked against the halt flag
*before* the model is reached, and a crash is attributed *before* a patch is
generated for it — a device already past its strike limit must not get one more
generated patch on the way to being rolled back (SAFETY_PROTOCOL.md §5.1).

Nothing here re-implements a safety decision: strike counting, attribution and
the restore itself all live in `rollback.py`, so the manual and automatic paths
stay one implementation (§7).
"""

import asyncio
import logging

import config
from server.agent.agent import Agent, AgentResult
from server.db.models import Device, Event, SessionLocal
from server.deploy import deployer, rollback
from server.deploy.scheduler import ReversionScheduler
from server.schemas import AgentTask, RecordType, TriggerType

log = logging.getLogger("caef.orchestrator")


def current_firmware(device_id: str) -> str:
    """What the device is running now — the Agent's starting point and the
    Sandbox's baseline for `delta_firmware`.

    Falls back to the on-disk baseline for a device that has never been patched,
    so the first generation still has real source to reason about.
    """
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        active = device.active_fw_hash if device else None
    if active:
        code = deployer.load_firmware(active)
        if code is not None:
            return code
    return config.FIRMWARE_PATH.read_text()


class Orchestrator:
    def __init__(self, agent: Agent, scheduler: ReversionScheduler | None = None) -> None:
        self.agent = agent
        self.scheduler = scheduler or ReversionScheduler()

    async def handle(self, task: AgentTask) -> None:
        if rollback.generation_halted(task.device_id):
            # §5.1: halted means halted until an operator clears it. Returning
            # before the model is touched is the whole point of the flag.
            log.warning(
                "generation halted for %s; dropping task %s (operator must clear strikes)",
                task.device_id,
                task.task_id,
            )
            return

        if task.trigger_type is TriggerType.CONTEXT_TRIGGER and self.scheduler.morphing(
            task.device_id, task.event
        ):
            # The device is already running a morph for this exact situation and
            # its reversion is still pending. Re-generating would spend a model
            # call to rebuild firmware the device already has, and — worse —
            # `schedule` would restart the reversion window, so a situation that
            # outlasts one window would never revert at all (LOOPS.md §2a).
            log.info(
                "%s already morphed for %s; dropping duplicate task %s",
                task.device_id,
                task.event,
                task.task_id,
            )
            return

        if task.trigger_type is TriggerType.CRITICAL_FAILURE and self._attribute_crash(task):
            if await self._roll_back(task, "crash-loop: strike limit reached"):
                return

        result = await asyncio.to_thread(
            self.agent.run, task, current_firmware(task.device_id)
        )
        self._persist(result)

        if result.escalated:
            self._mark_escalated(task.event_id)
            rollback.record_strike(task.device_id, f"retry budget exhausted for {task.event_id}")
            await self._roll_back(task, "retry budget exhausted")
            return

        await self._deploy(task, result)

    # --- strikes -------------------------------------------------------------

    def _attribute_crash(self, task: AgentTask) -> bool:
        """Count a device-reported crash against the deploy that preceded it.

        Attribution is `rollback`'s call, not ours: a crash unrelated to a recent
        deploy is a new problem to patch, not evidence the last patch was bad.
        """
        with SessionLocal() as db:
            event = db.get(Event, task.event_id)
        if event is None or not rollback.crash_is_attributable(task.device_id, event):
            return False
        rollback.record_strike(task.device_id, f"on-device crash after deploy ({event.event})")
        return True

    async def _roll_back(self, task: AgentTask, reason: str) -> bool:
        """Roll back if the device is at its strike limit. Returns whether it did."""
        if not rollback.should_roll_back(task.device_id):
            return False
        # The morph this device was running is about to be replaced by the
        # known-good artifact; a reversion timer still pointing at it would
        # later restore firmware that is no longer what it replaced.
        self.scheduler.cancel(task.device_id)
        try:
            await asyncio.to_thread(rollback.rollback, task.device_id, reason, task.event_id)
        except rollback.RollbackUnavailable:
            # Generation is already halted by `rollback` before it raises. This
            # is the operator-escalation case (LOOPS.md §5 failure mode), not a
            # pipeline crash.
            log.exception("rollback unavailable for %s; operator escalation", task.device_id)
        return True

    # --- deploy --------------------------------------------------------------

    async def _deploy(self, task: AgentTask, result: AgentResult) -> None:
        output = result.output
        record_type = (
            RecordType.MORPH_DEPLOY
            if task.trigger_type is TriggerType.CONTEXT_TRIGGER
            else RecordType.PATCH_DEPLOY
        )
        record_id, ack = await asyncio.to_thread(
            deployer.deploy,
            task.device_id,
            output.code,
            record_type,
            task.event_id,
            output.patch_id,
            output.target_file,
        )
        log.info(
            "%s deployed for %s: record=%s patch=%s",
            record_type,
            task.device_id,
            record_id,
            output.patch_id,
        )

        if record_type is RecordType.MORPH_DEPLOY:
            # LOOPS.md §2a. A morph is minimal *for a situation* and must not
            # outlive it; an Auto-Patch is durable and schedules nothing.
            self.scheduler.schedule(task.device_id, task.event_id, event=task.event)

    # --- persistence ---------------------------------------------------------

    def _persist(self, result: AgentResult) -> None:
        """Store every attempt, rejected ones included (NFR-5): a rejection is
        evidence the safety layers worked, and Guard Rail needs the tool-call
        trace kept alongside the code it vetted."""
        for attempt in result.attempts:
            deployer.record_patch(
                attempt.output, attempt.number, attempt.guardrail, attempt.sandbox
            )

    def _mark_escalated(self, event_id: str) -> None:
        with SessionLocal() as db:
            event = db.get(Event, event_id)
            if event:
                event.escalated = True
                event.retry_count = config.MAX_RETRIES
                db.commit()
