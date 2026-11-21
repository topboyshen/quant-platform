import os
import uuid

import pytest

PGDATA = os.getenv("QP_TEST_PGDATA", "/workspace/qp/pgdata")

# Runtime switches are deployment inputs.  Tests must not inherit the host's
# production values: a release tree with QP_JOB_GATES=1 / QP_SIGNALS_NOTIFY_MODE=live
# would otherwise change assertions and make the suite non-reproducible.
RUNTIME_ENV_DEFAULTS = {
    "QP_JOB_GATES": "0",
    "QP_NOTIFY_MODE": "shadow",
    "QP_SIGNALS_NOTIFY_MODE": "shadow",
    "QP_SIGNALS_COMBINED_REPORT": "0",
    "QP_RECORD_CHAIN": "0",
    "QP_CHAIN_READS": "0",
    "QP_CHAIN_READS_FALLBACK": "1",
    "QP_NOTIFY_KILL_SWITCH": "0",
    "QP_NOTIFY_DISABLED_FILE": "/nonexistent/qp-test-notify.disabled",
}
_ORIGINAL_ENV: dict[str, str | None] = {}


def pytest_addoption(parser):
    parser.addoption(
        "--run-assets",
        action="store_true",
        default=False,
        help="run tests marked 'assets' (repo docs, lockfiles, deploy units, frontend source)",
    )


def pytest_configure(config):
    # Runs before test modules import qp, so import-time env reads are covered too.
    for key, value in RUNTIME_ENV_DEFAULTS.items():
        _ORIGINAL_ENV[key] = os.environ.get(key)
        os.environ[key] = value


def pytest_unconfigure(config):
    for key, value in _ORIGINAL_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-assets"):
        return
    skip = pytest.mark.skip(reason="repo asset test; rerun with --run-assets")
    for item in items:
        if "assets" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _runtime_env_defaults(monkeypatch):
    for key, value in RUNTIME_ENV_DEFAULTS.items():
        monkeypatch.setenv(key, value)


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
