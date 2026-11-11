"""Fail-closed database selection for stateful Ledger integration tests."""

from __future__ import annotations

import os
from collections.abc import MutableMapping

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict


def require_test_database(
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Require an explicit DSN whose database name ends in ``_test``.

    The application normally defaults to the production ``research_ledger``
    database. Stateful tests must never inherit that default implicitly.
    """

    target = os.environ if environ is None else environ
    dsn = target.get("LEDGER_TEST_DSN", "").strip()
    if not dsn:
        raise RuntimeError("LEDGER_TEST_DSN is required for stateful integration tests")

    try:
        parameters = conninfo_to_dict(dsn)
    except ProgrammingError as exc:
        raise RuntimeError("LEDGER_TEST_DSN is not a valid PostgreSQL DSN") from exc

    database = parameters.get("dbname", "")
    if not database:
        raise RuntimeError("LEDGER_TEST_DSN must specify a database name")
    if database == "research_ledger" or not database.endswith("_test"):
        raise RuntimeError("LEDGER_TEST_DSN database name must end with _test")

    target["LEDGER_DSN"] = dsn
    return parameters
