#!/usr/bin/env bash

set -euo pipefail

if (( $# != 1 )); then
  printf '%s\n' 'usage: morning-cron-retry-harness.sh /absolute/run-morning-cron.sh' >&2
  exit 64
fi

source "$1"

attempt_calls=0
decision_calls=0
backoff_calls=0
notification_calls=0
delivered_exit=-1
delivered_target=""
decision_exit=1
mock_notification_exit=0
mock_signal_phase=none
mock_retry_mode=recollect
mock_clock_mode=fixed
mock_context_sha256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
declare -a attempt_codes=()
declare -a observed_cutoffs=()
declare -a observed_targets=()
declare -a observed_inputs=()
declare -a observed_hashes=()
declare -a observed_attempt_times=()

run_workflow_attempt() {
  observed_attempt_times+=("$(current_utc)")
  attempt_calls=$((attempt_calls + 1))
  observed_targets+=("$2")
  observed_cutoffs+=("$3")
  observed_inputs+=("$4")
  observed_hashes+=("$5")
  if [[ "$mock_signal_phase" == "workflow" ]]; then
    kill -TERM "$$"
  fi
  return "${attempt_codes[$((attempt_calls - 1))]}"
}

retry_decision() {
  local output_variable="$1"
  local directive
  decision_calls=$((decision_calls + 1))
  if [[ "$mock_signal_phase" == "decision" ]]; then
    kill -TERM "$$"
  fi
  if [[ "$mock_retry_mode" == "frozen_context" ]]; then
    directive="retry"$'\t'"frozen_context"$'\t'"$mock_context_sha256"
  elif [[ "$mock_retry_mode" == "recollect" ]]; then
    directive="retry"$'\t'"recollect"$'\t'"-"
  else
    directive="malformed retry directive"
  fi
  printf -v "$output_variable" '%s' "$directive"
  return "$decision_exit"
}

run_retry_backoff() {
  backoff_calls=$((backoff_calls + 1))
  return 0
}

deliver_final_notification() {
  notification_calls=$((notification_calls + 1))
  delivered_exit="$1"
  delivered_target="$4"
  return "$mock_notification_exit"
}

current_utc() {
  if [[ "$mock_clock_mode" == "delayed_schedule" ]]; then
    if (( attempt_calls == 0 )); then
      printf '%s\n' '2026-08-03T00:00:30Z'
    else
      printf '%s\n' '2026-08-03T00:10:00Z'
    fi
    return
  fi
  printf '%s\n' '2026-08-03T00:30:00Z'
}

current_target_date() {
  printf '%s\n' '2026-08-03'
}

run_scenario() {
  local name="$1"
  local codes="$2"
  decision_exit="$3"
  mock_notification_exit="$4"
  local expected="$5"
  mock_signal_phase="${6:-none}"
  mock_retry_mode="${7:-recollect}"
  local expected_collections="${8:--1}"
  local expected_frozen_attempts="${9:--1}"
  local expected_cutoff="${10:-2026-08-03T00:30:00Z}"
  local expected_target="${11:-2026-08-03}"
  read -r -a attempt_codes <<<"$codes"
  attempt_calls=0
  decision_calls=0
  backoff_calls=0
  notification_calls=0
  delivered_exit=-1
  delivered_target=""
  observed_cutoffs=()
  observed_targets=()
  observed_inputs=()
  observed_hashes=()
  observed_attempt_times=()

  set +e
  if (( $# >= 10 )); then
    main --target-date "$expected_target" --frozen-cutoff "$expected_cutoff"
  else
    main
  fi
  local wrapper_exit=$?
  set -e
  local observed="$wrapper_exit $attempt_calls $decision_calls $backoff_calls $notification_calls $delivered_exit"
  if [[ "$observed" != "$expected" ]]; then
    printf 'scenario %s failed: expected [%s], observed [%s]\n' "$name" "$expected" "$observed" >&2
    exit 1
  fi
  if (( notification_calls == 1 )) && [[ "$delivered_target" != "$expected_target" ]]; then
    printf 'scenario %s delivered the wrong occurrence target: %s\n' "$name" "$delivered_target" >&2
    exit 1
  fi
  if (( expected_collections >= 0 || expected_frozen_attempts >= 0 )); then
    local collection_calls=0
    local frozen_attempts=0
    local index
    for ((index = 0; index < ${#observed_inputs[@]}; index += 1)); do
      if [[ -z "${observed_inputs[$index]}" ]]; then
        collection_calls=$((collection_calls + 1))
      else
        frozen_attempts=$((frozen_attempts + 1))
        if [[ "${observed_inputs[$index]}" != "work/morning-context.json" ]]; then
          printf 'scenario %s used unexpected frozen input: %s\n' "$name" "${observed_inputs[$index]}" >&2
          exit 1
        fi
        if [[ "${observed_hashes[$index]}" != "$mock_context_sha256" ]]; then
          printf 'scenario %s changed frozen context hash\n' "$name" >&2
          exit 1
        fi
      fi
      if [[ "${observed_targets[$index]}" != "$expected_target" ]]; then
        printf 'scenario %s changed target date: %s\n' "$name" "${observed_targets[$index]}" >&2
        exit 1
      fi
      if [[ "${observed_cutoffs[$index]}" != "$expected_cutoff" ]]; then
        printf 'scenario %s changed frozen cutoff: %s\n' "$name" "${observed_cutoffs[$index]}" >&2
        exit 1
      fi
    done
    if (( collection_calls != expected_collections || frozen_attempts != expected_frozen_attempts )); then
      printf 'scenario %s snapshot counts differ: collect=%s frozen=%s\n' \
        "$name" "$collection_calls" "$frozen_attempts" >&2
      exit 1
    fi
  fi
}

# Every source outcome retains its original exit status without sending.
ASTOCK_FINAL_DELIVERY=disabled
run_scenario success-after-two-frozen-retries '1 1 0' 0 0 '0 3 2 2 0 -1' none frozen_context 1 2
run_scenario frozen-retry-exhausted '1 1 1' 0 0 '1 3 2 2 0 -1' none frozen_context 1 2
run_scenario collect-failure-recollects '1 0 0' 0 0 '0 2 1 1 0 -1' none recollect 2 0
run_scenario publication-block '2 0 0' 0 0 '2 1 0 0 0 -1'
run_scenario timeout '124 0 0' 0 0 '124 1 0 0 0 -1'
run_scenario signal-exit '143 0 0' 0 0 '143 1 0 0 0 -1'
run_scenario wrapper-signal-during-workflow '143 0 0' 0 0 '143 1 0 0 0 -1' workflow
run_scenario wrapper-signal-during-decision '1 0 0' 1 0 '143 1 1 0 0 -1' decision
run_scenario nonretryable '1 0 0' 1 0 '1 1 1 0 0 -1'
run_scenario explicit-occurrence-identity '0 0 0' 0 0 '0 1 0 0 0 -1' none recollect 1 0 \
  '2026-08-03T00:29:59.123Z' '2026-08-03'
mock_clock_mode=delayed_schedule
run_scenario delayed-schedule-retry-keeps-0800 '1 0' 0 0 '0 2 1 1 0 -1' none recollect 2 0 \
  '2026-08-03T00:00:00.000Z' '2026-08-03'
if [[ "${observed_attempt_times[*]}" != '2026-08-03T00:00:30Z 2026-08-03T00:10:00Z' ]]; then
  printf '%s\n' 'scheduled retry did not exercise the delayed attempt clock' >&2
  exit 1
fi
mock_clock_mode=fixed
unset ASTOCK_FINAL_DELIVERY

# An unset variable must fail closed without invoking the notification
# function. The explicit disabled mode must preserve the same behavior.
run_scenario default-delivery-disabled '0 0 0' 0 0 '0 1 0 0 0 -1'

ASTOCK_FINAL_DELIVERY=disabled
run_scenario source-only-delivery-disabled '0 0 0' 0 0 '0 1 0 0 0 -1'
unset ASTOCK_FINAL_DELIVERY

ASTOCK_FINAL_DELIVERY=invalid
attempt_calls=0
notification_calls=0
set +e
main
invalid_delivery_exit=$?
set -e
unset ASTOCK_FINAL_DELIVERY
if [[ "$invalid_delivery_exit $attempt_calls $notification_calls" != "64 0 0" ]]; then
  printf 'invalid final delivery mode was not rejected before work: exit=%s attempts=%s notifications=%s\n' \
    "$invalid_delivery_exit" "$attempt_calls" "$notification_calls" >&2
  exit 1
fi

ASTOCK_FINAL_DELIVERY=telegram
attempt_calls=0
notification_calls=0
set +e
main
retired_delivery_exit=$?
set -e
unset ASTOCK_FINAL_DELIVERY
if [[ "$retired_delivery_exit $attempt_calls $notification_calls" != "64 0 0" ]]; then
  printf 'retired Telegram flag was not rejected before work: exit=%s attempts=%s notifications=%s\n' \
    "$retired_delivery_exit" "$attempt_calls" "$notification_calls" >&2
  exit 1
fi

# Force the missing-process-group branch and verify signal_workflow_group falls
# back to the direct timeout PID instead of waiting for a group that does not
# exist. Give MSYS' emulated child process time to install its signal handling;
# Unix keeps this signal pending across the fork/exec window, while MSYS can
# otherwise lose an immediate synthetic TERM and make this harness flaky.
original_group_alive="$(declare -f workflow_group_alive)"
workflow_group_alive() { return 1; }
/usr/bin/sleep 30 &
workflow_pid=$!
/usr/bin/sleep 0.1
signal_workflow_group TERM
if ! wait_for_workflow_exit 20; then
  kill -KILL -- "$workflow_pid" 2>/dev/null || true
  printf '%s\n' 'direct PID fallback did not terminate the pre-group child' >&2
  exit 1
fi
wait "$workflow_pid" 2>/dev/null || true
eval "$original_group_alive"
workflow_pid=""

printf '%s\n' 'morning cron retry integration: ok'
