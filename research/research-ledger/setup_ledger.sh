#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/scripts/research-backup-policy.sh"
research_backup_policy_load

APP=/opt/research-ledger
DATA=/var/lib/research-ledger
BACKUP=/var/backups/research-ledger
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_ID="${STAMP,,}_$$"
INTEGRATION_TEST_DB="research_ledger_integration_${RUN_ID}_test"
REVISION_TEST_DB="research_ledger_revision_${RUN_ID}_test"
declare -a CREATED_TEST_DATABASES=()

if [[ ! "$INTEGRATION_TEST_DB" =~ ^research_ledger_integration_[0-9]{8}t[0-9]{6}z_[0-9]+_test$ ]] ||
   [[ ! "$REVISION_TEST_DB" =~ ^research_ledger_revision_[0-9]{8}t[0-9]{6}z_[0-9]+_test$ ]]; then
  printf 'Refusing unsafe disposable database names\n' >&2
  exit 1
fi

if ! getent group researchledger >/dev/null; then
  groupadd --system researchledger
fi
if ! id researchledger >/dev/null 2>&1; then
  useradd --system --gid researchledger --home-dir /nonexistent --shell /usr/sbin/nologin researchledger
fi

install -d -o root -g root -m 0755 "$APP" "$APP/app" "$APP/migrations" "$APP/systemd"
install -d -o root -g researchledger -m 2750 "$DATA" "$DATA/cas" "$DATA/manifests" "$DATA/manifests/astock" "$DATA/manifests/phase2" "$DATA/manifests/models" "$DATA/import-reports"
install -d -o root -g root -m 0750 "$BACKUP"
install -d -o root -g ubuntu -m 0750 "$BACKUP/hourly"
install -d -o root -g researchledger -m 0750 /var/lib/research-ledger-backup
chmod 0755 "$APP/scripts/research-ledger-backup.sh" "$APP/scripts/research-ledger-alert.sh" "$APP/scripts/research-ledger-runtime-health.sh" "$APP/scripts/research-ledger-pipeline-health.sh"

if ! test -x "$APP/venv/bin/python"; then
  python3 -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install --disable-pip-version-check --no-input -r "$APP/requirements.txt" >/dev/null

if ! sudo -n -u postgres psql -Atqc "SELECT 1 FROM pg_roles WHERE rolname='researchledger'" | grep -qx 1; then
  sudo -n -u postgres createuser --no-superuser --no-createdb --no-createrole --no-replication --login researchledger
fi
if ! sudo -n -u postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname='research_ledger'" | grep -qx 1; then
  sudo -n -u postgres createdb --owner=researchledger --encoding=UTF8 --template=template0 research_ledger
fi

# Validate the schema and governance implementation in two isolated disposable databases
# before production migration. Each stateful test receives an explicit local DSN.
cleanup_test_databases() {
  local failed=0
  local database
  for database in "${CREATED_TEST_DATABASES[@]}"; do
    if ! sudo -n -u postgres dropdb --host=/var/run/postgresql --username=postgres --if-exists "$database" >/dev/null; then
      printf 'Failed to drop disposable database: %s\n' "$database" >&2
      failed=1
    fi
  done
  return "$failed"
}

cleanup_on_exit() {
  local status=$?
  trap - EXIT
  if ! cleanup_test_databases; then
    status=1
  fi
  exit "$status"
}

trap cleanup_on_exit EXIT
for database in "$INTEGRATION_TEST_DB" "$REVISION_TEST_DB"; do
  sudo -n -u postgres createdb --host=/var/run/postgresql --username=postgres \
    --owner=researchledger --encoding=UTF8 --template=template0 "$database"
  CREATED_TEST_DATABASES+=("$database")
  sudo -n -u postgres psql --host=/var/run/postgresql --username=postgres --dbname=postgres \
    --set=ON_ERROR_STOP=1 --quiet \
    --command="REVOKE ALL ON DATABASE \"$database\" FROM PUBLIC; GRANT CONNECT, CREATE, TEMPORARY ON DATABASE \"$database\" TO researchledger;"
  binding=$(sudo -n -u researchledger psql --host=/var/run/postgresql --username=researchledger \
    --dbname="$database" --no-psqlrc --tuples-only --no-align \
    --command='SELECT current_database(), current_user, inet_server_addr() IS NULL')
  if [[ "$binding" != "$database|researchledger|t" ]]; then
    printf 'Unsafe disposable database binding: %s\n' "$binding" >&2
    exit 1
  fi
done

INTEGRATION_TEST_DSN="dbname=$INTEGRATION_TEST_DB host=/var/run/postgresql user=researchledger"
REVISION_TEST_DSN="dbname=$REVISION_TEST_DB host=/var/run/postgresql user=researchledger"
sudo -n -u researchledger env PYTHONDONTWRITEBYTECODE=1 LEDGER_DSN="$INTEGRATION_TEST_DSN" \
  "$APP/venv/bin/python" "$APP/app/ledger_app.py" migrate > "$DATA/import-reports/test-migration-integration-${STAMP}.json"
sudo -n -u researchledger env PYTHONDONTWRITEBYTECODE=1 LEDGER_TEST_DSN="$INTEGRATION_TEST_DSN" \
  "$APP/venv/bin/python" "$APP/tests/test_ledger_integration.py" > "$DATA/import-reports/test-integration-${STAMP}.txt"
sudo -n -u researchledger env PYTHONDONTWRITEBYTECODE=1 LEDGER_DSN="$REVISION_TEST_DSN" \
  "$APP/venv/bin/python" "$APP/app/ledger_app.py" migrate > "$DATA/import-reports/test-migration-revision-${STAMP}.json"
sudo -n -u researchledger env PYTHONDONTWRITEBYTECODE=1 LEDGER_TEST_DSN="$REVISION_TEST_DSN" \
  "$APP/venv/bin/python" "$APP/tests/test_phase2_import_revisions.py" > "$DATA/import-reports/test-revisions-${STAMP}.txt"

# Production migration starts only after both tests pass and their databases are gone.
cleanup_test_databases
trap - EXIT

# Apply the exact same verified migration to the production Ledger database.
sudo -n -u researchledger env LEDGER_DB_NAME=research_ledger "$APP/venv/bin/python" "$APP/app/ledger_app.py" migrate > "$DATA/import-reports/production-migration-${STAMP}.json"

# Install new units only; no existing unit is modified.
install -o root -g root -m 0644 "$APP/systemd/research-ledger.service" /etc/systemd/system/research-ledger.service
install -o root -g root -m 0644 "$APP/systemd/research-ledger-worker.service" /etc/systemd/system/research-ledger-worker.service
install -o root -g root -m 0644 "$APP/systemd/research-ledger-worker.timer" /etc/systemd/system/research-ledger-worker.timer
install -o root -g root -m 0644 "$APP/systemd/research-ledger-healthcheck.service" /etc/systemd/system/research-ledger-healthcheck.service
install -o root -g root -m 0644 "$APP/systemd/research-ledger-healthcheck.timer" /etc/systemd/system/research-ledger-healthcheck.timer
install -o root -g root -m 0644 "$APP/systemd/research-ledger-backup.service" /etc/systemd/system/research-ledger-backup.service
install -o root -g root -m 0644 "$APP/systemd/research-ledger-backup.timer" /etc/systemd/system/research-ledger-backup.timer
install -o root -g root -m 0644 "$APP/systemd/research-ledger-alert@.service" /etc/systemd/system/research-ledger-alert@.service
install -o root -g root -m 0644 "$APP/systemd/research-ledger-pipeline-health.service" /etc/systemd/system/research-ledger-pipeline-health.service
install -o root -g root -m 0644 "$APP/systemd/research-ledger-pipeline-health.timer" /etc/systemd/system/research-ledger-pipeline-health.timer
for unit in research-system-phase2.service research-system-phase2-producer.service research-system-phase2-settler.service research-ledger-phase2-import.service; do
  install -d -o root -g root -m 0755 "/etc/systemd/system/$unit.d"
  install -o root -g root -m 0644 "$APP/systemd/research-ledger-on-failure.conf" "/etc/systemd/system/$unit.d/alert.conf"
done
systemctl daemon-reload
systemctl enable --now research-ledger-healthcheck.timer research-ledger-pipeline-health.timer
if [[ "$RESEARCH_BACKUP_POLICY" == "REQUIRED" ]]; then
  systemctl enable --now research-ledger-backup.timer
else
  printf 'BACKUP_SCHEDULE=unchanged policy=NO_BACKUP\n'
fi

printf 'SETUP_STATUS=success\n'
printf 'APP=%s\nDATA=%s\nBACKUP=%s\nDB=research_ledger\nTEST_DATABASES_DROPPED=%s,%s\n' \
  "$APP" "$DATA" "$BACKUP" "$INTEGRATION_TEST_DB" "$REVISION_TEST_DB"
