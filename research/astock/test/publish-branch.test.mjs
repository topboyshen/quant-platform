/**
 * A validated trading-calendar skip closes the source-to-seal daily branch;
 * anything else leaves it open (previous behaviour, seal gates still apply).
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile, symlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { decidePublishBranch, evaluatePublishBranch, markerPath, shanghaiDate } from "../src/publish-branch.mjs";
import { parseGateArgs } from "../scripts/publish-branch-gate.mjs";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOLIDAY = "2026-09-25";
const NOW = new Date("2026-09-25T00:00:12.000Z");

async function fixture({ state = {}, lastRun = {}, calendar = {}, contextTamper = false } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-publish-branch-"));
  await mkdir(path.join(root, "work", "orchestrator", "states"), { recursive: true });
  const context = { schemaVersion: "1.0", data: { tradingCalendar: {
    targetDate: HOLIDAY, status: "non_trading_day", previousTradingDate: "2026-09-24",
    nextTradingDate: "2026-09-28", dataAsOf: "2026-09-25T08:00:03+08:00",
    sourceIds: ["akshare:sina:trade-calendar"], ...calendar } } };
  const contextBytes = Buffer.from(JSON.stringify(context));
  await writeFile(path.join(root, "work", "morning-context.json"), contextBytes);
  const digest = createHash("sha256").update(contextBytes).digest("hex");
  if (contextTamper) await writeFile(path.join(root, "work", "morning-context.json"), Buffer.concat([contextBytes, Buffer.from(" ")]));
  await writeFile(path.join(root, "work", "orchestrator", "states", `morning-report-${HOLIDAY}.json`), JSON.stringify({
    schemaVersion: "1.0", task: "morning-report", targetDate: HOLIDAY, status: "skipped",
    invocationId: "2f454f14-e89f-477c-b0f3-5c0b66f55932", childExitCode: 0, childSignal: null,
    timedOut: false, interrupted: false, ...state }));
  await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
    schemaVersion: "1.0", targetDate: HOLIDAY, contextFile: "work/morning-context.json",
    contextSha256: digest, tradingStatus: "non_trading_day", publicationGate: "not_applicable",
    status: "skipped", phase: "collect", ...lastRun }));
  return root;
}

async function exists(target) {
  try { await stat(target); return true; } catch { return false; }
}

test("validated holiday skip closes the branch and writes only the marker", async () => {
  const root = await fixture();
  try {
    const result = await decidePublishBranch({ projectRoot: root, now: NOW });
    assert.equal(result.decision, "closed");
    assert.equal(result.reason, "validated_non_trading_day");
    assert.equal(result.previousTradingDate, "2026-09-24");
    assert.equal(result.nextTradingDate, "2026-09-28");
    assert.equal(result.markerWritten, true);
    const marker = JSON.parse(await readFile(markerPath(root), "utf8"));
    assert.equal(marker.targetDate, HOLIDAY);
    assert.equal(marker.decision, "closed");
    assert.equal((await stat(markerPath(root))).mode & 0o777, 0o600);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("dry run never writes a marker", async () => {
  const root = await fixture();
  try {
    const result = await decidePublishBranch({ projectRoot: root, now: NOW, dryRun: true });
    assert.equal(result.decision, "closed");
    assert.equal(await exists(markerPath(root)), false);
  } finally { await rm(root, { recursive: true, force: true }); }
});

const OPEN_CASES = [
  ["trading day success", { state: { status: "success" }, lastRun: { status: "success", phase: "publish", tradingStatus: "trading_day", publicationGate: "passed" } }, "orchestrator_status_success"],
  ["blocked evidence", { state: { status: "blocked", childExitCode: 2 } }, "orchestrator_status_blocked"],
  ["generation failed", { state: { status: "failed", childExitCode: 1 } }, "orchestrator_status_failed"],
  ["skip not clean", { state: { childExitCode: 1 } }, "orchestrator_skip_not_clean"],
  ["last-run other date", { lastRun: { targetDate: "2026-09-24" } }, "last_run_date_mismatch"],
  ["last-run not calendar skip", { lastRun: { tradingStatus: "unknown" } }, "last_run_not_calendar_skip"],
  ["context hash mismatch", { contextTamper: true }, "context_hash_mismatch"],
  ["calendar says trading day", { calendar: { status: "trading_day" } }, "calendar_not_non_trading_day"],
  ["calendar other date", { calendar: { targetDate: "2026-09-24" } }, "calendar_not_non_trading_day"],
  ["calendar unknown source", { calendar: { sourceIds: ["weekday-guess"] } }, "calendar_source_unrecognized"],
  ["context path escape", { lastRun: { contextFile: "work/../../etc/passwd.json" } }, "context_file_invalid"]
];

for (const [name, options, reason] of OPEN_CASES) {
  test(`branch stays open: ${name}`, async () => {
    const root = await fixture(options);
    try {
      const result = await decidePublishBranch({ projectRoot: root, now: NOW });
      assert.equal(result.decision, "open");
      assert.equal(result.reason, reason);
      assert.equal(await exists(markerPath(root)), false);
    } finally { await rm(root, { recursive: true, force: true }); }
  });
}

test("a different Shanghai day never reuses the holiday records", async () => {
  const root = await fixture();
  try {
    const monday = new Date("2026-09-28T00:00:05.000Z");
    assert.equal(shanghaiDate(monday), "2026-09-28");
    const result = await evaluatePublishBranch({ projectRoot: root, now: monday });
    assert.equal(result.decision, "open");
    assert.equal(result.reason, "orchestrator_state_unavailable");
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("symlinked state or marker path is refused", async () => {
  const root = await fixture();
  try {
    const statePath = path.join(root, "work", "orchestrator", "states", `morning-report-${HOLIDAY}.json`);
    const real = `${statePath}.real`;
    await writeFile(real, await readFile(statePath));
    await rm(statePath);
    await symlink(real, statePath);
    assert.equal((await evaluatePublishBranch({ projectRoot: root, now: NOW })).reason, "orchestrator_state_unavailable");
    await rm(statePath);
    await writeFile(statePath, await readFile(real));
    await symlink("/tmp/elsewhere", markerPath(root));
    await assert.rejects(decidePublishBranch({ projectRoot: root, now: NOW }), /marker_path_not_regular_file/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("gate CLI only allows a historical date in dry-run mode", () => {
  assert.deepEqual(parseGateArgs(["decide"]), { dryRun: false, targetDate: null });
  assert.deepEqual(parseGateArgs(["decide", "--dry-run", "--date", HOLIDAY]), { dryRun: true, targetDate: HOLIDAY });
  assert.throws(() => parseGateArgs(["decide", "--date", HOLIDAY]), /only allowed with --dry-run/);
  assert.throws(() => parseGateArgs(["send"]), /usage/);
});

test("wrapper clears the marker before every run and decides only after a zero exit", async () => {
  const wrapper = await readFile(path.join(repositoryRoot, "scripts", "run-orchestrated-morning-cron.sh"), "utf8");
  const clear = wrapper.indexOf('rm -f -- "$PUBLISH_CLOSED_MARKER"');
  const run = wrapper.indexOf('task-orchestrator.mjs" \\\n  run morning-report');
  const decide = wrapper.indexOf("publish-branch-gate.mjs\" decide");
  assert.ok(clear > 0 && run > clear && decide > run, "clear -> run -> decide order");
  assert.match(wrapper, /if \(\( orchestrator_exit == 0 \)\); then/);
  assert.match(wrapper, /exit "\$orchestrator_exit"/);
  assert.ok(wrapper.indexOf("flock --exclusive --nonblock 9") < clear, "marker is only touched under the lock");
  assert.doesNotMatch(wrapper, /hermes-notify|deliver-morning-report|systemctl/);
});

test("every publish-branch unit carries the same marker condition", async () => {
  const expected = "ConditionPathExists=!/home/ubuntu/AStock_AI_System/work/orchestrator/publish-branch.closed";
  for (const unit of ["astock-research-seal", "astock-xhs-draft", "astock-morning-notify"]) {
    const dropIn = await readFile(path.join(repositoryRoot, "systemd", `${unit}.service.d`, "publish-branch.conf"), "utf8");
    assert.match(dropIn, /^\[Unit\]$/m);
    assert.ok(dropIn.split("\n").includes(expected), unit);
    assert.doesNotMatch(dropIn, /ExecCondition/, "ExecCondition skips still trigger OnSuccess=");
  }
  const source = await readFile(path.join(repositoryRoot, "systemd", "astock-xhs-source.service"), "utf8");
  assert.doesNotMatch(source, /ConditionPathExists/, "the source unit itself must always run");
});
