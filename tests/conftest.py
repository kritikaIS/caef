"""Shared test isolation and the guard that keeps tests off production data.

Default backend is a scratch SQLite file: offline, fast, no credentials needed.
Set `CAEF_TEST_POSTGRES=1` to run the same suite against the real Postgres
backend instead, which is what proves the SQLite/Postgres divergence the
`aware()` helper in models.py exists to paper over.

That Postgres run is pointed at a dedicated `caef_test` schema derived from
DATABASE_URL, never `public`. The per-test fixture below truncates every table,
so pointing it at the deployment schema would wipe real history. `_guard()`
makes that a hard failure rather than a convention: it re-asserts, against the
live connection, that the search_path really did land on the test schema before
a single row is deleted.
"""

import atexit
import os
import sys
import tempfile
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_SCHEMA = "caef_test"


def _database_url() -> str:
    """Resolve the backend before any test module imports config.

    File-backed rather than `:memory:` on purpose: deploys and reversions write
    from worker threads, and the StaticPool that `:memory:` requires shares one
    connection, so a reader closing its session rolls back a writer's INSERT.
    models.py rejects `:memory:` outright for that reason.
    """
    if os.getenv("CAEF_TEST_POSTGRES") != "1":
        handle, path = tempfile.mkstemp(suffix=".db", prefix="caef_test_")
        os.close(handle)
        atexit.register(lambda: Path(path).unlink(missing_ok=True))
        return f"sqlite:///{path}"

    url = os.getenv("DATABASE_URL") or dotenv_values(ROOT / ".env").get("DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        raise RuntimeError("CAEF_TEST_POSTGRES=1 but DATABASE_URL is not a Postgres URL")
    return url


os.environ["DATABASE_URL"] = _database_url()

# Deploy refreshes the RAG corpus (it is precedent for the next generation), so
# any test that deploys writes an index. Point that at a scratch file before
# config is imported, or the suite rewrites the developer's real corpus.
_rag_handle, _rag_path = tempfile.mkstemp(suffix=".db", prefix="caef_test_rag_")
os.close(_rag_handle)
atexit.register(lambda: Path(_rag_path).unlink(missing_ok=True))
os.environ["RAG_DB_PATH"] = _rag_path

from sqlalchemy import event, text  # noqa: E402

from server.db import models as m  # noqa: E402

if m.engine.dialect.name == "postgresql":
    # Registered before anything opens a connection, so no pooled connection can
    # predate it. Set per checkout rather than via the URL's `options` parameter:
    # Supabase's connection pooler drops libpq startup options, so the URL form
    # silently leaves search_path on `public` — the one outcome that must not
    # happen before a truncating fixture runs.
    @event.listens_for(m.engine, "connect")
    def _use_test_schema(dbapi_connection, _record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"create schema if not exists {TEST_SCHEMA}")
            cursor.execute(f"set search_path to {TEST_SCHEMA}")
        dbapi_connection.commit()


def _guard() -> None:
    """Refuse to truncate anything outside the test schema.

    Asked of the live connection rather than inferred from the URL string: a
    typo'd or ignored `options` parameter silently leaves search_path on
    `public`, which is exactly the case that must not proceed to a DELETE.
    """
    if m.engine.dialect.name != "postgresql":
        return
    with m.engine.connect() as db:
        schema = db.execute(text("select current_schema()")).scalar()
    if schema != TEST_SCHEMA:
        raise RuntimeError(
            f"refusing to run destructive tests against schema {schema!r}; "
            f"expected {TEST_SCHEMA!r}"
        )


@pytest.fixture(scope="session", autouse=True)
def schema_guard():
    _guard()
    yield


@pytest.fixture(autouse=True)
def clean_db(schema_guard):
    """Reset between tests.

    The SQLite backend is one file for the whole session, so rows left by one
    module collide with another module's primary keys. Reset once here instead
    of trusting every module to remember.
    """
    m.init_db()
    with m.SessionLocal() as db:
        # Child tables first: foreign keys are enforced on both backends.
        for table in (
            m.DeploymentTransition,
            m.Deployment,
            m.HistoryRecord,
            m.Patch,
            m.Event,
            m.Device,
        ):
            db.query(table).delete()
        db.commit()
    yield
