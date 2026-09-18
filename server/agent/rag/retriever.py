"""RAG retriever — assembles the Agent's grounding context (TDD.md §2.4).

Pulls the four things FR-11 requires, per event:
  1. the device hardware schema (read-only),
  2. the firmware currently running on that device,
  3. relevant driver snippets from the Sensor Driver Library,
  4. similar historical failures and the patches that resolved them.

Items 1 and 2 are fetched directly, never by keyword search — the Agent must
always see the exact physical reality of the device it is patching, not the
schema that happened to rank highest. Only 3 and 4 are retrieved.
"""

import re
import sqlite3
from dataclasses import dataclass

import config
from server.agent.rag import indexer
from server.schemas import AgentTask, HardwareSchema, load_hardware_schema

# FTS5 treats punctuation as syntax; queries are built from bare word tokens.
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass
class RetrievedContext:
    schema: HardwareSchema
    current_firmware: str
    driver_docs: list[str]
    history_docs: list[str]


def _query_terms(task: AgentTask) -> str:
    """Build an FTS5 OR-query from the event and its payload.

    Deliberately lossy: an over-broad query returns extra context, which the
    Agent can ignore; an over-narrow one returns nothing and silently drops the
    grounding that FR-11 requires.
    """
    text = f"{task.event} {task.trigger_type} {task.raw_payload.get('data', '')}"
    terms = {word.lower() for word in _WORD.findall(text)}
    return " OR ".join(sorted(terms)) if terms else task.event.lower()


def _search(
    connection: sqlite3.Connection, doc_type: str, device_id: str, query: str, limit: int
) -> list[str]:
    try:
        rows = connection.execute(
            "SELECT title, body FROM documents "
            "WHERE documents MATCH ? AND doc_type = ? AND device_id IN (?, '*') "
            "ORDER BY rank LIMIT ?",
            (query, doc_type, device_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # A malformed FTS query must not take down generation; the Agent still
        # gets the schema and current firmware, which are fetched directly.
        return []
    return [f"### {row['title']}\n{row['body']}" for row in rows]


def _all(connection: sqlite3.Connection, doc_type: str, device_id: str, limit: int) -> list[str]:
    rows = connection.execute(
        "SELECT title, body FROM documents WHERE doc_type = ? AND device_id IN (?, '*') LIMIT ?",
        (doc_type, device_id, limit),
    ).fetchall()
    return [f"### {row['title']}\n{row['body']}" for row in rows]


def retrieve(task: AgentTask, current_firmware: str) -> RetrievedContext:
    schema = load_hardware_schema(task.device_id)
    query = _query_terms(task)
    with indexer.connect() as connection:
        # The Sensor Driver Library is small and every entry is a driver for
        # hardware actually wired to this device, so it is fetched whole rather
        # than keyword-ranked. Ranking it only risks withholding the driver the
        # Agent needed, which makes it hand-roll one instead (FR-11).
        drivers = _all(connection, "docs_on_comp", task.device_id,
                       config.RAG_DRIVER_DOC_LIMIT)
        if not drivers:
            # An empty corpus is a cold start, not a valid retrieval: the driver
            # library is built from source at index time, so "no drivers" means
            # nothing has indexed yet, never that the device has none. Without
            # this the Agent invents plausible driver APIs that do not exist,
            # which Guard Rail passes and the Sandbox fails — three times, until
            # the retry budget is gone (FR-11).
            indexer.index_driver_library(connection)
            drivers = _all(connection, "docs_on_comp", task.device_id,
                           config.RAG_DRIVER_DOC_LIMIT)
        # History does grow unbounded, so it stays query-ranked.
        history = _search(
            connection, "history", task.device_id, query, config.RAG_HISTORY_DOC_LIMIT
        )
    return RetrievedContext(
        schema=schema,
        current_firmware=current_firmware,
        driver_docs=drivers,
        history_docs=history,
    )
