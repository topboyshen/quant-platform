# Follow-on: calendar proof independent of report publication

> Current isolated-candidate supplement (2026-09-28):
> [LIGHTWEIGHT_RUNTIME.md](LIGHTWEIGHT_RUNTIME.md) adds bounded closed-interval
> evidence when today's proof is absent, validates terminal evidence at occurrence
> completion rather than before that completion, and documents the shared audited
> run decision. Final combined guarded suite: 542/542. The 480/480 and scope below
> are the earlier follow-on's evidence, not the final candidate or production state.

Base: `656a4f669115168d83204610b5cd0f321953aa4c`.
This is an isolated follow-on; no production deployment or upstream query.

## Evidence observed read-only on 2026-09-28

The existing AStock producer writes `work/morning-context.json` atomically and
binds its exact file bytes in `work/last-run.json.contextSha256` before archive
compilation, semantic analysis, report publication or sealing. A subsequent
failed last-run retains that binding. AStock's per-date orchestrator state has
the parent occurrence identity and terminal start/end times.

- The original **09-23 natural failure** last-run remains in
  `ops/acceptance/rollback/signal-gate-quality-20260923/last-run.json.pre`:
  file hash `5d2159471b585b52b9bf289aaac0ae55816b1926dae721548d7c0ac043d2db57`;
  `status=failed`, `phase=collect`, `contextFile=work/morning-context.json`,
  context byte hash `65178c14f0723814dd4a630dd74ad73f38d2e579dc48a2dedfdf25540f70456a`;
  cutoff `2026-09-23T00:00:02.699Z`.
- `work/orchestrator/states/morning-report-2026-09-23.scheduled-failed.json`
  still has hash `ca1e4b36cd4e0476d1755a9cfe482252b7f4aada4b554abb0dc320127244a1c3`;
  invocation `4d330bd6-14da-4428-bc25-8952ef32a057`; parent start equals that
  cutoff; end `00:00:10.932Z`; child exit 1; no timeout/interruption.
- The failure's context file itself has since been overwritten. The existing
  local 09-23 morning acceptance records its then-observed file hash and date,
  but is **not** a substitute for the lost bytes. No historical replay of that
  failure is claimed.
- The present 09-23 archive is a **10:48 manual recovery** with input hash
  `e76f984b14d6ea8d517b11ab1e7cdcb20c3a51f7d025794964e9f0f69884ed2d`, not the
  earlier natural failure. It must never be substituted for that occurrence.
- The **09-24 natural success** input is
  `8b97692f94d67556cdd618205063e428ab74b919233fb1560fba4ec77bad5598`.
  Its preserved `rawContextText` rehashes to
  `4835d2a193e79e6b9cda843ba684fdc589384da2fd51166d8d20bbdc0798e311`, the
  original context-file hash. `rawContextSha256=e8d86479…` and
  `compilerContextSha256=b6503fc3…` are different serialized/derived objects;
  neither replaces this file-byte hash. Calendar target 09-24, open, previous
  09-23, next 09-28; calendar and source `dataAsOf=08:00:03+08:00`, cutoff
  `08:00:03.753+08:00`. Orchestrator invocation
  `f9ac74c6-b841-4571-a30f-5fee6eff54e1` ran 08:00:03.753–08:00:33.472.
- CNINFO precollection contains announcement evidence, **not a calendar**.

## Adapter and fail-closed boundaries

When given the AStock project root, retain the existing sealed-archive path.
An existing malformed/nonsealed `published.json` remains a hard rejection;
there is no fallback around contradictory publication evidence.

If `published.json` does not exist, including an unfinished input-only archive,
read the current day's terminal orchestrator record, last-run and exact
`work/morning-context.json`. The new `validated_terminal_context` proof requires:

1. Same target day and task; parent success/exit 0 pairs only with child success,
   or parent failed/nonzero pairs with child failed/blocked. Running, missing,
   timed-out or interrupted occurrences are rejected.
2. `frozenCutoffInstant == occurrence.startedAt <= run.startedAt <= run terminal
   time <= occurrence.completedAt <= check time`, all on the same Shanghai day.
   Failed children use `failedAt`; success/blocked children use `completedAt`.
3. Exact byte hash equals `last-run.contextSha256`. The context meta day matches,
   and its second-resolution creation timestamp is within the run. Compiler
   context hashes are unrelated and are not compared to raw bytes.
4. An open calendar for that day, source ID
   `akshare:sina:trade-calendar` uniquely present in the source registry with the
   existing Sina calendar URL and matching `dataAsOf`; previous/next dates in
   order, zoned declared time no later than frozen cutoff, same-day expiry and
   at most 24 hours old.
5. Re-read the three shared files and require unchanged bytes, so an overwritten
   context or later conflicting occurrence cannot be silently mixed in.
6. The live fallback receives the exact unpublished archive path explicitly.
   Check that `published.json` is truly absent both before and after reading the
   context. Any intervening publication, even malformed/nonsealed or a dangling
   symlink, blocks the fallback. Parent directories are rechecked without
   following symlinks. Project root and `work/` must still exist; only the
   optional archive parents/file may be absent. A stable input-only archive
   remains valid.

These checks detect changes observed during this read; they are not a global
lock or a filesystem transaction with the concurrent AStock publisher. No
claim is made that the shared files cannot change after the proof is returned.

The proof preserves three file hashes, occurrence identity/status/times and
the calendar document inside the core artifact. The pure core validator also
checks its internal hash, terminal status and time relationships when auditing
that stored artifact. These hashes attest the read objects, not a remote
exchange signature. `dataAsOf` is the producer's declared timestamp, not an
independently observed network completion time.

The closed-skip path and sealed-archive path remain supported. Core target-date,
Qlib generation/coverage and immutable insert-only checks remain unchanged.

**Remaining dependency:** the AStock calendar collector and its existing
orchestration must still have run far enough to save a valid bound context.
Early collection failure, missing/stale/overwritten evidence or an unknown
calendar still defers. This removes dependence on successful publication; it
does not create an independent calendar provider or guarantee two wholly
independent availability domains. Persisting separate immutable per-occurrence
calendar records is a possible later change, not silently implemented here.

Tests use synthetic fixed fixtures following observed field shapes, no market
collection or production database. The 09-23 failure is not re-executed.

## Verification

The follow-on adds 16 test methods with positive failed/success/blocked terminal
cases and missing/tampered/stale/wrong-day/source/time/overwritten/unfinished
and mid-read-change counterexamples, including publication/parent-link races
and exact absent-path binding and required-directory disappearance after the
final file read. A fixed `failed` AStock occurrence feeds
the real core builder/store and pure health row validator successfully without
`published.json`; Qlib itself is a fixed stub and the socket connector is denied.

- Full guarded fixed-fixture suite: **480/480 OK**, no failures or skips.
- Path-relocated wrapper integration: **7/7 OK**.
- Real health-alert shell plus fake Hermes integration: **1/1 OK**.
- `daily_snapshot.py`, update/coverage/timeouts, policy, wrapper, alert and
  systemd paths are not changed by this follow-on.

Tests ran only on macOS in disposable local fixtures. The production offline
runner's network/process/production-file/database/external-write guards were
kept. Reused installed bundled Python plus cached dependencies; no installation.
Host harness maps Linux descriptor resolution to macOS `F_GETPATH` and mocks
`Path.home` to the fixture root; no HOME/CODEX_HOME mutation. An initial run
without the existing cache path failed import resolution (requests/bs4), not
functional tests; the final full run used the same cache as Batch 3.

No natural production result, Linux execution or successful publication is
claimed. Deployment is separate; this commit must follow `656a4f6`, not be
applied directly to the old production baseline.
