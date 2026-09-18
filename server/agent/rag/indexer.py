"""RAG indexer — SQLite FTS5 keyword index (TRD.md §8 stack resolution).

Three document types from DATA_SCHEMAS.md §8, in one table with a `doc_type`
discriminator so the retriever can weight or filter by type:

  - `docs_of_node`   per-device hardware schema + current running firmware
  - `docs_on_comp`   Sensor Driver Library snippets, pre-vetted
  - `history`        prior events and the patches that resolved them

No embeddings and no hosted vector DB: the corpus is one device schema, a
handful of driver classes and a growing history table. FTS5 keyword match over
that is both sufficient and offline-reproducible (PRD G6). The retriever's
interface is what would survive a later swap to embeddings, not this file.
"""

import ast
import inspect
import json
import sqlite3
from pathlib import Path

import config

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5(
    doc_id UNINDEXED,
    doc_type UNINDEXED,
    device_id UNINDEXED,
    title,
    body
);
"""


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(config.RAG_DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def upsert(
    connection: sqlite3.Connection,
    doc_id: str,
    doc_type: str,
    device_id: str,
    title: str,
    body: str,
) -> None:
    """FTS5 has no UPSERT, so delete-then-insert keeps re-indexing idempotent."""
    connection.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
    connection.execute(
        "INSERT INTO documents (doc_id, doc_type, device_id, title, body) VALUES (?, ?, ?, ?, ?)",
        (doc_id, doc_type, device_id, title, body),
    )


def index_hardware_schema(connection: sqlite3.Connection, device_id: str) -> None:
    """Docs of Node: the device's physical reality, verbatim.

    Indexed read-only — the RAG layer never writes back to the schema file
    (SAFETY_PROTOCOL.md §1 layer 1).
    """
    from server.schemas import load_hardware_schema

    schema = load_hardware_schema(device_id)
    connection.execute("DELETE FROM documents WHERE doc_id = ?", (f"schema:{device_id}",))
    upsert(
        connection,
        doc_id=f"schema:{device_id}",
        doc_type="docs_of_node",
        device_id=device_id,
        title=f"Hardware schema for {device_id} ({schema.mcu_type})",
        body=json.dumps(schema.model_dump(mode="json"), indent=2),
    )


def index_driver_library(connection: sqlite3.Connection) -> None:
    """Docs on comp.: one document per driver class, source included.

    Pre-vetted snippets the Agent should prefer over writing its own driver
    (DATA_SCHEMAS.md §8). Parsed from the real module, so the corpus cannot
    drift from the code the firmware actually imports.
    """
    from edge_node import drivers

    source = Path(inspect.getfile(drivers)).read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        upsert(
            connection,
            doc_id=f"driver:{node.name}",
            doc_type="docs_on_comp",
            device_id="*",  # driver docs are fleet-wide, not per device
            title=f"{node.name} driver (edge_node.drivers)",
            body=ast.get_source_segment(source, node) or "",
        )


def index_history(connection: sqlite3.Connection, device_id: str | None = None) -> None:
    """History: what worked last time for a similar failure or context.

    Only patches that actually reached a device are indexed. A rejected or
    failed attempt is not precedent — surfacing it would invite the Agent to
    repeat a known-bad fix.
    """
    from server.db.models import Event, HistoryRecord, Patch, SessionLocal
    from server.schemas import RecordStatus

    with SessionLocal() as db:
        query = (
            db.query(HistoryRecord, Patch, Event)
            .join(Patch, HistoryRecord.patch_id == Patch.id)
            .join(Event, HistoryRecord.event_id == Event.id)
            .filter(HistoryRecord.status == RecordStatus.DEPLOYED)
        )
        if device_id:
            query = query.filter(HistoryRecord.device_id == device_id)

        for record, patch, event in query.all():
            upsert(
                connection,
                doc_id=f"history:{record.id}",
                doc_type="history",
                device_id=record.device_id,
                title=f"{record.record_type} for {event.event} on {record.device_id}",
                body=(
                    f"Trigger: {event.trigger_type} {event.event}\n"
                    f"Event data: {json.dumps(event.data)}\n"
                    f"Plan: {patch.plan}\n"
                    f"Pins: {patch.pins_referenced}\n"
                    f"Deployed fw_hash: {record.fw_hash}\n"
                    f"Code:\n{patch.code}"
                ),
            )


def reindex(device_id: str | None = None) -> None:
    """Rebuild the whole corpus. Cheap at v0.1 scale; call after a deploy."""
    device_id = device_id or config.DEVICE_ID
    with connect() as connection:
        index_hardware_schema(connection, device_id)
        index_driver_library(connection)
        index_history(connection, device_id)
