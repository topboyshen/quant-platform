#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SUBJECT="$ROOT/setup_ledger.sh"

bash -n "$SUBJECT"

grep -Fq 'INTEGRATION_TEST_DB="research_ledger_integration_${RUN_ID}_test"' "$SUBJECT"
grep -Fq 'REVISION_TEST_DB="research_ledger_revision_${RUN_ID}_test"' "$SUBJECT"
grep -Fq 'for database in "${CREATED_TEST_DATABASES[@]}"' "$SUBJECT"
grep -Fq 'host=/var/run/postgresql user=researchledger' "$SUBJECT"

if grep -Fq 'LEDGER_DB_NAME="$TEST_DB"' "$SUBJECT"; then
  printf 'legacy unsafe test database binding remains\n' >&2
  exit 1
fi

if [[ $(grep -c 'LEDGER_TEST_DSN=' "$SUBJECT") -ne 2 ]]; then
  printf 'both stateful tests must receive an explicit LEDGER_TEST_DSN\n' >&2
  exit 1
fi

revision_line=$(grep -n 'test_phase2_import_revisions.py' "$SUBJECT" | cut -d: -f1)
cleanup_line=$(grep -n '^cleanup_test_databases$' "$SUBJECT" | cut -d: -f1)
production_line=$(grep -n 'production-migration-' "$SUBJECT" | cut -d: -f1)
if (( revision_line >= cleanup_line || cleanup_line >= production_line )); then
  printf 'production migration is not gated by both tests and cleanup\n' >&2
  exit 1
fi

printf 'setup_ledger_safety=passed\n'
