"""Operator dashboard (TDD.md §2.9).

Four things, no more: device list + status, the live event feed off the Event
topic, the per-device history table, and a force-rollback button.

The button calls `rollback.rollback` — the same function the automatic 3-strikes
path calls. There is no second implementation and there must never be one
(SAFETY_PROTOCOL.md §7); an operator-only rollback that drifted from the
automatic one is exactly the bug that protocol exists to prevent.

Rendered as plain f-string HTML with a meta-refresh rather than a template
engine or a JS app: it is an operator diagnostic view for v0.1, and a template
dependency buys nothing at this size.
"""

import html
import logging
from collections import deque

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

import config
from server.api import device_rows, history_rows
from server.db.models import aware
from server.deploy import rollback
from server.schemas import EventNotification

log = logging.getLogger("caef.frontend")

router = APIRouter()

# Live feed buffer. Bounded because this is a diagnostic tail, not a store — the
# History Table is the durable record (TRD.md §5).
FEED: deque[EventNotification] = deque(maxlen=50)


def record_event(event: EventNotification) -> None:
    """Event-topic subscriber. Registered by the entrypoint on the Distributor,
    which is the fan-out leg the Frontend was always meant to consume."""
    FEED.appendleft(event)


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<title>CAEF — operator</title>
<style>
 body {{ font: 14px/1.5 ui-monospace, monospace; margin: 2rem; max-width: 70rem; }}
 table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
 th, td {{ border: 1px solid #ccc; padding: .3rem .5rem; text-align: left; }}
 th {{ background: #f2f2f2; }}
 .bad {{ color: #b00; font-weight: bold; }}
 .ok {{ color: #060; }}
 button {{ font: inherit; }}
</style>
<h1>CAEF operator dashboard</h1>
{body}
"""


@router.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(
        PAGE.format(refresh=config.DASHBOARD_REFRESH_SECONDS, body=_devices() + _feed())
    )


@router.get("/device/{device_id}", response_class=HTMLResponse)
def device_detail(device_id: str) -> HTMLResponse:
    rows = "".join(
        f"<tr><td>{esc(aware(r.deployed_at))}</td><td>{esc(r.record_type.value)}</td>"
        f"<td>{esc(r.status.value)}</td><td>{esc(r.fw_hash)}</td>"
        f"<td>{esc(r.event_id)}</td><td>{esc(r.patch_id)}</td><td>{esc(r.poll_id)}</td></tr>"
        for r in history_rows(device_id)
    )
    body = (
        f"<p><a href='/'>&larr; all devices</a></p><h2>{esc(device_id)} — history</h2>"
        "<table><tr><th>deployed_at</th><th>type</th><th>status</th><th>fw_hash</th>"
        "<th>event_id</th><th>patch_id</th><th>poll_id</th></tr>"
        f"{rows or '<tr><td colspan=7>no history</td></tr>'}</table>"
    )
    return HTMLResponse(PAGE.format(refresh=config.DASHBOARD_REFRESH_SECONDS, body=body))


@router.post("/rollback/{device_id}")
def force_rollback(device_id: str) -> RedirectResponse:
    """The manual trigger of the *same* rollback the 3-strikes path fires (§7).

    A failed rollback still leaves generation halted — `rollback` does that first
    and unconditionally — so a device with nothing to restore is escalated to the
    operator rather than left eligible for another generated patch.
    """
    try:
        rollback.rollback(device_id, "manual rollback from operator dashboard")
    except rollback.RollbackUnavailable:
        log.exception("operator rollback unavailable for %s; escalation", device_id)
    return RedirectResponse("/", status_code=303)


def _devices() -> str:
    rows = []
    for device in device_rows():
        halted = (
            "<span class='bad'>HALTED</span>"
            if device["generation_halted"]
            else "<span class='ok'>running</span>"
        )
        sync = "" if device["in_sync"] else " <span class='bad'>drift</span>"
        strikes = f"{device['strike_count']}/{config.STRIKE_LIMIT}"
        record = device["live_record"]
        rows.append(
            f"<tr><td><a href='/device/{esc(device['id'])}'>{esc(device['id'])}</a></td>"
            f"<td>{esc(device['mcu_type'])}</td>"
            f"<td>{esc(device['active_fw_hash'])}{sync}</td>"
            f"<td>{esc(device['assigned_fw_hash'])}</td>"
            f"<td>{esc(device['inactive_fw_hash'])}</td>"
            f"<td>{esc(record.record_type.value) if record else '—'}</td>"
            f"<td>{esc(strikes)}</td><td>{halted}</td>"
            f"<td><form method='post' action='/rollback/{esc(device['id'])}'>"
            "<button type='submit'>force rollback</button></form></td></tr>"
        )
    return (
        "<h2>devices</h2><table>"
        "<tr><th>device</th><th>mcu</th><th>active</th><th>assigned</th><th>inactive (known-good)</th>"
        "<th>live record</th><th>strikes</th><th>generation</th><th></th></tr>"
        + ("".join(rows) or "<tr><td colspan=9>no devices yet</td></tr>")
        + "</table>"
    )


def _feed() -> str:
    rows = "".join(
        f"<tr><td>{esc(event.timestamp)}</td><td>{esc(event.trigger_type.value)}</td>"
        f"<td>{esc(event.event)}</td><td>{esc(event.device_id)}</td>"
        f"<td>{esc(event.current_state_hash)}</td><td>{esc(event.data)}</td></tr>"
        for event in FEED
    )
    return (
        "<h2>live event feed</h2><table>"
        "<tr><th>time</th><th>trigger</th><th>event</th><th>device</th>"
        "<th>state hash</th><th>data</th></tr>"
        + (rows or "<tr><td colspan=6>no events yet</td></tr>")
        + "</table>"
    )
