"""Server entrypoint: Listener + Distributor + Task consumer + HTTP, one loop.

v0.1 runs these in a single process (TRD.md §7 — local simulation is the correct
v0.1 form). The Distributor interface is what keeps them separable: swapping
LocalDistributor for SQS/SNS splits this into three processes without touching
Listener or Agent code.

The HTTP app carries both the device-facing Poll/Reconciliation endpoints and
the operator dashboard, on `API_PORT`. In-process because the dashboard's live
feed is an Event-topic subscriber: a separate process would need a real topic,
which is exactly what v0.1 defers.
"""

import asyncio
import logging

import uvicorn

import config
from frontend import app as frontend
from server.agent.agent import Agent, build_llm
from server.api import app as http_app
from server.db.models import init_db
from server.distributor.distributor import LocalDistributor
from server.listener.listener import Listener
from server.orchestrator import Orchestrator
from server.schemas import EventNotification

log = logging.getLogger("caef")


def log_event(event: EventNotification) -> None:
    log.info("EVENT %s %s device=%s", event.trigger_type, event.event, event.device_id)


async def serve() -> None:
    init_db()
    distributor = LocalDistributor()
    distributor.subscribe(log_event)
    distributor.subscribe(frontend.record_event)
    orchestrator = Orchestrator(Agent(build_llm()))
    # Condition/combined reversion polls the device's latest reading, which only
    # reaches the scheduler through the Event topic (LOOPS.md §2a).
    distributor.subscribe(lambda event: _observe(orchestrator, event))
    listener = Listener(distributor)

    await listener.serve_tcp()
    await listener.serve_udp()
    http_app.include_router(frontend.router)
    http = uvicorn.Server(
        uvicorn.Config(http_app, host=config.API_HOST, port=config.API_PORT, log_level="warning")
    )
    log.info("CAEF server up (mode=%s, reversion=%s)", config.SCENARIO, config.REVERSION_MODE)
    log.info("dashboard on http://%s:%s/", config.API_HOST, config.API_PORT)
    await asyncio.gather(http.serve(), distributor.drain(orchestrator.handle))


def _observe(orchestrator: Orchestrator, event: EventNotification) -> None:
    """Feed the triggering metric to the reversion scheduler.

    `temp_c` is the v0.1 situation metric (PRD §6 Scenario A / OQ-1's recovery
    threshold); events that carry no reading are simply not observations.
    """
    reading = event.data.get("temp_c")
    if isinstance(reading, (int, float)):
        orchestrator.scheduler.observe(event.device_id, float(reading))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        log.info("shutdown")


if __name__ == "__main__":
    main()
