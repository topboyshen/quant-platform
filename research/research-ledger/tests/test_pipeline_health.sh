#!/usr/bin/env bash
set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SUBJECT="${PIPELINE_HEALTH_SUBJECT:-$PROJECT_ROOT/scripts/research-ledger-pipeline-health.sh}"
readonly FIXTURE_PATH="$TEST_DIR/fixtures"
readonly TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT

readonly MODE_FILE="$TEST_TMP/runtime-health.mode"

assert_contains() {
  local value="$1"
  local expected="$2"
  [[ "$value" == *"$expected"* ]] || {
    printf 'expected output to contain %q, got %q\n' "$expected" "$value" >&2
    exit 1
  }
}

printf 'FROZEN\n' > "$MODE_FILE"
frozen_output="$({
  PATH="$FIXTURE_PATH:/usr/bin:/bin" \
  PIPELINE_HEALTH_MODE_FILE="$MODE_FILE" \
  FAKE_TIMER_UNIT_FILE_STATE=disabled \
  FAKE_TIMER_ACTIVE_STATE=inactive \
  FAKE_SERVICE_ACTIVE_STATE=inactive \
  bash "$SUBJECT"
})"
assert_contains "$frozen_output" 'PIPELINE_HEALTH=ok'
assert_contains "$frozen_output" 'business_mode=FROZEN'

if PATH="$FIXTURE_PATH:/usr/bin:/bin" \
  PIPELINE_HEALTH_MODE_FILE="$MODE_FILE" \
  FAKE_TIMER_UNIT_FILE_STATE=enabled \
  FAKE_TIMER_ACTIVE_STATE=active \
  FAKE_SERVICE_ACTIVE_STATE=inactive \
  bash "$SUBJECT" >/dev/null 2>&1; then
  printf 'frozen mode accepted an active timer\n' >&2
  exit 1
fi

printf 'RUNNING\n' > "$MODE_FILE"
running_output="$({
  PATH="$FIXTURE_PATH:/usr/bin:/bin" \
  PIPELINE_HEALTH_MODE_FILE="$MODE_FILE" \
  FAKE_TIMER_UNIT_FILE_STATE=enabled \
  FAKE_TIMER_ACTIVE_STATE=active \
  FAKE_SERVICE_ACTIVE_STATE=inactive \
  bash "$SUBJECT"
})"
assert_contains "$running_output" 'PIPELINE_HEALTH=ok'
assert_contains "$running_output" 'business_mode=RUNNING'

printf 'PIPELINE_HEALTH_TESTS=passed\n'
