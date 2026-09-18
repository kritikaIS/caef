"""M9 check: the Poll/Reconciliation endpoints and the operator dashboard.

Two claims worth proving mechanically:
  - LOOPS.md §3 — a device whose OTA push was dropped can discover the drift and
    re-fetch the assigned artifact over HTTP, and the payload it gets back is
    one the watchdog's own hash check accepts.
  - SAFETY_PROTOCOL.md §7 — the dashboard's force-rollback button reaches the
    same `rollback.rollback` the automatic 3-strikes path does, not a copy.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from frontend import app as frontend  # noqa: E402
from server.api import app as http_app  # noqa: E402
from server.db import models as m  # noqa: E402
from server.deploy import deployer, rollback  # noqa: E402
from server.schemas import (  # noqa: E402
    EventNotification,
    OTAPush,
    RecordType,
    TriggerType,
    fw_hash,
)

DEVICE = "pi_node_alpha"
V1 = "print('[firmware] v1', flush=True)\n"
V2 = "print('[firmware] v2', flush=True)\n"

http_app.include_router(frontend.router)


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRMWARE_STORE_DIR", tmp_path / "store")
    # Nothing bound on OTA_PORT: pushes fail, which is exactly the missed-push
    # condition the poll loop exists to correct.
    monkeypatch.setattr(config, "TELEMETRY_TIMEOUT_SECONDS", 1)
    frontend.FEED.clear()
    yield


@pytest.fixture
def client():
    with TestClient(http_app) as c:
        yield c


def provision(code: str = V1) -> str:
    deployer.stage_soft_firmware(code)
    with m.SessionLocal() as db:
        db.add(m.Device(id=DEVICE, mcu_type="RaspberryPi_4B", active_fw_hash=fw_hash(code)))
        db.commit()
    deployer.write_history(DEVICE, fw_hash(code), RecordType.PATCH_DEPLOY)
    return fw_hash(code)


# --- /poll -------------------------------------------------------------------


def test_poll_reports_in_sync_when_the_hashes_match(client):
    provision()
    deployer.promote_to_inactive_slot(DEVICE, fw_hash(V1))

    body = client.get("/poll", params={"id": DEVICE, "current_state_hash": fw_hash(V1)}).json()

    assert body["in_sync"] is True
    assert body["assigned_fw_hash"] == fw_hash(V1)
    assert body["device_id"] == DEVICE
    assert body["poll_id"]


def test_poll_reports_drift_after_a_missed_push(client):
    """LOOPS.md §3: the assigned hash moved on, the device's did not."""
    provision()
    deployer.promote_to_inactive_slot(DEVICE, deployer.stage_soft_firmware(V2))

    body = client.get("/poll", params={"id": DEVICE, "current_state_hash": fw_hash(V1)}).json()

    assert body["in_sync"] is False
    assert body["assigned_fw_hash"] == fw_hash(V2)


def test_poll_id_lands_on_the_next_history_row(client):
    """DATA_SCHEMAS.md §7: `history.poll_id` is the poll that preceded the row."""
    provision()
    poll_id = client.get(
        "/poll", params={"id": DEVICE, "current_state_hash": fw_hash(V1)}
    ).json()["poll_id"]

    record_id = deployer.write_history(DEVICE, fw_hash(V2), RecordType.MORPH_DEPLOY)

    with m.SessionLocal() as db:
        assert db.get(m.HistoryRecord, record_id).poll_id == poll_id


def test_poll_from_an_unknown_device_is_not_an_error(client):
    body = client.get("/poll", params={"id": "ghost", "current_state_hash": "abc"}).json()
    assert body == {
        "poll_id": body["poll_id"],
        "device_id": "ghost",
        "assigned_fw_hash": None,
        "in_sync": True,
    }


# --- /firmware ---------------------------------------------------------------


def test_firmware_returns_a_payload_the_watchdog_accepts(client):
    """The re-request path must produce the same shape as an OTA push, hash
    included — the device verifies it identically either way (TRD.md §6)."""
    provision()
    deployer.promote_to_inactive_slot(DEVICE, deployer.stage_soft_firmware(V2))

    push = OTAPush.model_validate(client.get("/firmware", params={"id": DEVICE}).json())

    assert push.fw_hash == fw_hash(V2) == fw_hash(push.code)
    assert push.device_id == DEVICE


def test_firmware_404s_when_nothing_is_assigned(client):
    provision()
    assert client.get("/firmware", params={"id": DEVICE}).status_code == 404


def test_firmware_refuses_to_serve_an_artifact_missing_from_the_store(client):
    """No regeneration fallback: bytes that never passed a gate must not ship
    (SAFETY_PROTOCOL.md §1)."""
    provision()
    deployer.promote_to_inactive_slot(DEVICE, "deadbeefdeadbeef")

    assert client.get("/firmware", params={"id": DEVICE}).status_code == 410


# --- dashboard ---------------------------------------------------------------


def test_dashboard_lists_devices_and_the_live_feed(client):
    provision()
    frontend.record_event(
        EventNotification(
            event_id="e1",
            device_id=DEVICE,
            trigger_type=TriggerType.CONTEXT_TRIGGER,
            event="HIGH_HEAT_DETECTED",
            timestamp=1,
            current_state_hash=fw_hash(V1),
            data={"temp_c": 91.0},
        )
    )
    page = client.get("/").text

    assert DEVICE in page
    assert "HIGH_HEAT_DETECTED" in page


def test_device_page_shows_the_history_table(client):
    provision()
    page = client.get(f"/device/{DEVICE}").text
    assert fw_hash(V1) in page
    assert "patch_deploy" in page


def test_force_rollback_calls_the_one_rollback_implementation(client, monkeypatch):
    """SAFETY_PROTOCOL.md §7: the operator button and the 3-strikes path are the
    same function. Asserted by patching that function and watching it fire."""
    provision()
    calls = []
    monkeypatch.setattr(
        rollback, "rollback", lambda device_id, reason, *a, **k: calls.append((device_id, reason))
    )

    response = client.post(f"/rollback/{DEVICE}", follow_redirects=False)

    assert response.status_code == 303
    assert calls == [(DEVICE, "manual rollback from operator dashboard")]


def test_force_rollback_really_restores_the_known_good_artifact(client):
    """Not just wired: end to end, the button halts generation and redeploys."""
    provision()
    deployer.deploy(DEVICE, V2, RecordType.MORPH_DEPLOY)
    deployer.confirm_active(DEVICE, fw_hash(V2))  # device took the morph

    client.post(f"/rollback/{DEVICE}", follow_redirects=False)

    with m.SessionLocal() as db:
        device = db.get(m.Device, DEVICE)
        assert device.generation_halted is True
        assert device.assigned_fw_hash == fw_hash(V1)
    assert rollback.generation_halted(DEVICE)


def test_a_rollback_with_nothing_to_restore_still_halts_generation(client):
    """The escalation case (LOOPS.md §5) must not surface as a 500 that leaves
    the operator thinking nothing happened — generation stops regardless."""
    with m.SessionLocal() as db:
        db.add(m.Device(id="bare", mcu_type="unknown"))
        db.commit()

    assert client.post("/rollback/bare", follow_redirects=False).status_code == 303
    assert rollback.generation_halted("bare")
