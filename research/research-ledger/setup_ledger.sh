#!/usr/bin/env bash
set -euo pipefail

APP=/opt/research-ledger
DATA=/var/lib/research-ledger
BACKUP=/var/backups/research-ledger
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TEST_DB="research_ledger_test_${STAMP,,}"

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

# Validate the schema and governance implementation in a disposable database before production migration.
sudo -n -u postgres createdb --owner=researchledger --encoding=UTF8 --template=template0 "$TEST_DB"
cleanup() {
  sudo -n -u postgres dropdb --if-exists "$TEST_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT
sudo -n -u researchledger env LEDGER_DB_NAME="$TEST_DB" "$APP/venv/bin/python" "$APP/app/ledger_app.py" migrate > "$DATA/import-reports/test-migration-${STAMP}.json"
sudo -n -u researchledger env LEDGER_DB_NAME="$TEST_DB" "$APP/venv/bin/python" "$APP/tests/test_ledger_integration.py" > "$DATA/import-reports/test-integration-${STAMP}.txt"

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
systemctl enable --now research-ledger-backup.timer research-ledger-healthcheck.timer research-ledger-pipeline-health.timer

printf 'SETUP_STATUS=success\n'
printf 'APP=%s\nDATA=%s\nBACKUP=%s\nDB=research_ledger\nTEST_DB_DROPPED=%s\n' "$APP" "$DATA" "$BACKUP" "$TEST_DB"
