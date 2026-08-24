#!/usr/bin/env python3
from __future__ import annotations

import unittest

from integration_guard import require_test_database


class IntegrationGuardTests(unittest.TestCase):
    def test_missing_test_dsn_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "LEDGER_TEST_DSN is required"):
            require_test_database({})

    def test_production_keyword_dsn_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must end with _test"):
            require_test_database(
                {"LEDGER_TEST_DSN": "dbname=research_ledger host=/var/run/postgresql"}
            )

    def test_production_uri_dsn_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must end with _test"):
            require_test_database({"LEDGER_TEST_DSN": "postgresql:///research_ledger"})

    def test_missing_database_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must specify a database name"):
            require_test_database({"LEDGER_TEST_DSN": "host=/var/run/postgresql"})

    def test_explicit_test_database_is_exported_to_application(self) -> None:
        environ = {
            "LEDGER_TEST_DSN": "dbname=research_ledger_test host=/var/run/postgresql",
            "LEDGER_DSN": "dbname=research_ledger host=/var/run/postgresql",
        }

        parameters = require_test_database(environ)

        self.assertEqual(parameters["dbname"], "research_ledger_test")
        self.assertEqual(environ["LEDGER_DSN"], environ["LEDGER_TEST_DSN"])


if __name__ == "__main__":
    unittest.main()
