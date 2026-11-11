import os
import uuid

import pytest

PGDATA = os.getenv("QP_TEST_PGDATA", "/workspace/qp/pgdata")


@pytest.fixture(scope="session")
def db_url():
    import pgserver
    srv = pgserver.get_server(PGDATA, cleanup_mode=None)
    name = "qptest_" + uuid.uuid4().hex[:8]
    srv.psql(f"CREATE DATABASE {name};")
    url = f"postgresql+psycopg://postgres@/{name}?host={PGDATA}"
    os.environ["QP_DATABASE_URL"] = url
    os.environ["QP_API_HEALTH_URL"] = "http://127.0.0.1:9/unreachable"
    os.environ["QP_BACKUP_DIR"] = f"/tmp/{name}-backups"
    from qp.db import get_engine
    get_engine.cache_clear()
    from qp.cli import main
    assert main(["db", "upgrade"]) == 0
    yield url
    get_engine.cache_clear()
    from sqlalchemy import create_engine
    srv.psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{name}';")
    srv.psql(f"DROP DATABASE IF EXISTS {name};")


@pytest.fixture()
def engine(db_url):
    from qp.db import get_engine
    return get_engine()


@pytest.fixture()
def client(engine):
    from fastapi.testclient import TestClient
    from qp.api.main import app
    return TestClient(app, base_url="https://testserver")
