"""Reversion scheduler — the Reversion Sub-Loop (LOOPS.md §2a).

A Situational Morph is temporary by definition: the firmware is minimal for a
situation, so it must not outlive the situation. Every morph deploy schedules
exactly one reversion job here.

Trigger mode is config, resolving PRD OQ-1 the way CLAUDE.md §8 demands — the
default stated in LOOPS.md §2a with the alternative behind a flag, no third
option invented:

  - `time`      (v0.1 default) revert after `REVERSION_WINDOW_SECONDS`
  - `condition` revert once the triggering metric recovers below its threshold
  - `combined`  whichever comes first

Auto-Patching schedules nothing: a crash fix is meant to be durable (LOOPS.md
§4.5). That asymmetry is the whole distinction between the two loops.
"""

import asyncio
import logging

import config
from server.deploy import rollback

log = logging.getLogger("caef.scheduler")


class ReversionScheduler:
    """One pending reversion per device.

    A newer morph replaces the pending job rather than stacking a second one:
    two timers racing would revert the device twice, the second one to firmware
    that is no longer what the first replaced.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, asyncio.Task] = {}
        # Latest reading per device, fed by the Listener for condition mode.
        self._readings: dict[str, float] = {}
        # Which event each pending morph was generated for, so a device can be
        # told it is already morphed for that situation (see `morphing`).
        self._events: dict[str, str | None] = {}

    def observe(self, device_id: str, value: float) -> None:
        self._readings[device_id] = value

    def recovered(self, device_id: str) -> bool:
        reading = self._readings.get(device_id)
        return reading is not None and reading < config.REVERSION_RECOVERY_THRESHOLD_C

    def pending(self, device_id: str) -> bool:
        job = self._jobs.get(device_id)
        return job is not None and not job.done()

    def morphing(self, device_id: str, event: str) -> bool:
        """Is this device already running a live morph for this same situation?

        The situation outlasts the trigger: a device over its heat threshold
        re-emits `HIGH_HEAT_DETECTED` every `POST_TRIGGER_HOLD_SECONDS` for as
        long as it stays hot (LOOPS.md §1). Without this, each repeat generates
        a fresh morph *and* restarts the reversion timer via `schedule`, so the
        window never elapses and LOOPS.md §2a step 6 never runs.
        """
        return self.pending(device_id) and self._events.get(device_id) == event

    def schedule(self, device_id: str, event_id: str | None = None, event: str | None = None,
                 **deploy_kwargs) -> None:
        self.cancel(device_id)
        job = asyncio.create_task(self._run(device_id, event_id, deploy_kwargs))
        self._jobs[device_id] = job
        self._events[device_id] = event

    def cancel(self, device_id: str) -> None:
        job = self._jobs.pop(device_id, None)
        self._events.pop(device_id, None)
        if job and not job.done():
            job.cancel()

    async def _run(self, device_id: str, event_id: str | None, deploy_kwargs: dict) -> None:
        try:
            await self._wait(device_id)
            # Reversion is a deploy, so it runs off the event loop; a blocking
            # socket push must not stall the Distributor's drain loop.
            await asyncio.to_thread(rollback.revert, device_id, event_id, **deploy_kwargs)
        except asyncio.CancelledError:
            log.info("reversion for %s cancelled", device_id)
            raise
        except Exception:
            # A failed reversion is escalation-worthy but must not take down the
            # scheduler for every other device.
            log.exception("reversion for %s failed", device_id)

    async def _wait(self, device_id: str) -> None:
        mode = config.REVERSION_MODE
        window = config.REVERSION_WINDOW_SECONDS

        if mode == "time":
            await asyncio.sleep(window)
            return

        deadline = None if mode == "condition" else asyncio.get_running_loop().time() + window
        while True:
            if self.recovered(device_id):
                log.info("%s recovered below threshold; reverting early", device_id)
                return
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                log.info("%s reversion window elapsed; reverting", device_id)
                return
            await asyncio.sleep(config.REVERSION_POLL_SECONDS)
