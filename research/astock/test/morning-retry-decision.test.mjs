import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {
  AUTO_RETRY_PHASES,
  AUTO_RETRY_POLICIES,
  FROZEN_CONTEXT_FILE,
  MAX_LAST_RUN_BYTES,
  decideMorningRetry,
  formatRetryDecision,
  hashFrozenContext,
  main,
  parseRetryArguments,
  readLastRun
} from "../scripts/morning-retry-decision.mjs";

const contextSha256 = "a".repeat(64);
const attempt = {
  exitCode: 1,
  attempt: 1,
  maxAttempts: 3,
  attemptStartedAt: "2026-08-03T00:30:00.000Z",
  attemptEndedAt: "2026-08-03T00:34:00.000Z",
  frozenCutoffInstant: "2026-08-03T00:29:59.000Z",
  targetDate: "2026-08-03"
};

function failedState(failureCode, overrides = {}) {
  return {
    schemaVersion: "1.0",
    targetDate: "2026-08-03",
    startedAt: "2026-08-03T00:31:00.000Z",
    failedAt: "2026-08-03T00:33:00.000Z",
    status: "failed",
    phase: AUTO_RETRY_PHASES[failureCode] ?? "validate",
    publicationGate: "blocked",
    failureCode,
    retryable: true,
    frozenCutoffInstant: attempt.frozenCutoffInstant,
    contextFile: FROZEN_CONTEXT_FILE,
    contextSha256,
    ...overrides
  };
}

test("only fresh bridge and model execution failures receive bounded automatic retry", () => {
  for (const failureCode of Object.keys(AUTO_RETRY_PHASES)) {
    const decision = decideMorningRetry({
      ...attempt,
      lastRun: failedState(failureCode)
    });
    const policy = AUTO_RETRY_POLICIES[failureCode];
    assert.deepEqual(decision, {
      retry: true,
      reasonCode: failureCode,
      resumeMode: policy.resumeMode,
      ...(policy.resumeMode === "frozen_context" ? { contextSha256 } : {})
    });
  }

  assert.equal(decideMorningRetry({
    ...attempt,
    attempt: 2,
    lastRun: failedState("data_bridge_failed")
  }).retry, true);
  assert.equal(decideMorningRetry({
    ...attempt,
    attempt: 3,
    lastRun: failedState("data_bridge_failed")
  }).retry, false);
});

test("an attempt may end after midnight while the frozen cutoff keeps the occurrence date", () => {
  const decision = decideMorningRetry({
    ...attempt,
    attemptStartedAt: "2026-08-03T15:58:00.000Z",
    attemptEndedAt: "2026-08-03T16:03:00.000Z",
    lastRun: failedState("data_bridge_failed", {
      startedAt: "2026-08-03T15:59:00.000Z",
      failedAt: "2026-08-03T16:02:00.000Z"
    })
  });
  assert.deepEqual(decision, {
    retry: true,
    reasonCode: "data_bridge_failed",
    resumeMode: "recollect"
  });
});

test("publication, content, timeout, notification and tampered states fail closed", () => {
  const cases = [
    { exitCode: 2, lastRun: failedState("data_bridge_failed") },
    { exitCode: 76, lastRun: failedState("data_bridge_failed") },
    { exitCode: 124, lastRun: failedState("data_bridge_timeout") },
    { lastRun: failedState("content_gate_exhausted", { retryable: false }) },
    { lastRun: failedState("report_publish_failed", { phase: "publish", retryable: false }) },
    { lastRun: failedState("report_publish_failed", { phase: "publish", retryable: true }) },
    { lastRun: failedState("workflow_internal_error", { retryable: false }) },
    { lastRun: failedState("trading_calendar_unknown") },
    { lastRun: failedState("data_bridge_failed", { phase: "publish" }) },
    { lastRun: failedState("data_bridge_failed", { retryable: false }) },
    { lastRun: failedState("data_bridge_failed", { publicationGate: "passed" }) },
    { lastRun: failedState("data_bridge_failed", { reportFile: "reports/daily/2026-08-03.md" }) },
    { lastRun: failedState("pi_generation_failed", { contextFile: "work/other.json" }) },
    { lastRun: failedState("pi_review_failed", { contextSha256: "bad" }) },
    { lastRun: failedState("pi_review_failed", { frozenCutoffInstant: "2026-08-03T00:29:58.000Z" }) },
    { lastRun: failedState("data_bridge_failed", {
      startedAt: "2026-08-02T00:31:00.000Z",
      failedAt: "2026-08-02T00:33:00.000Z"
    }) },
    { lastRun: null }
  ];
  for (const overrides of cases) {
    assert.equal(decideMorningRetry({
      ...attempt,
      ...overrides
    }).retry, false);
  }
});

test("retry decision CLI accepts only the fixed complete grammar", () => {
  const argv = [
    "--exit-code", "1",
    "--attempt", "1",
    "--max-attempts", "3",
    "--started-at", attempt.attemptStartedAt,
    "--ended-at", attempt.attemptEndedAt,
    "--frozen-cutoff", attempt.frozenCutoffInstant,
    "--target-date", attempt.targetDate
  ];
  assert.deepEqual(parseRetryArguments(argv), attempt);
  assert.throws(() => parseRetryArguments([...argv, "--command", "bash"]), /invalid/);
  assert.throws(() => parseRetryArguments([
    ...argv.slice(0, -2),
    "--attempt", "2"
  ]), /invalid/);
  assert.throws(() => parseRetryArguments(argv.slice(0, -2)), /invalid/);
  assert.throws(() => parseRetryArguments([
    ...argv.slice(0, 1), "not-an-integer", ...argv.slice(2)
  ]), /numeric/);
  assert.equal(
    formatRetryDecision(decideMorningRetry({
      ...attempt,
      lastRun: failedState("pi_generation_failed")
    })),
    `retry\tfrozen_context\t${contextSha256}`
  );
  assert.equal(
    formatRetryDecision(decideMorningRetry({
      ...attempt,
      lastRun: failedState("data_bridge_failed")
    })),
    "retry\trecollect\t-"
  );
});

test("last-run retry input is read from one bounded no-follow file handle", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-retry-state-"));
  try {
    await mkdir(path.join(root, "work"), { recursive: true });
    const statePath = path.join(root, "work", "last-run.json");
    const contextPath = path.join(root, ...FROZEN_CONTEXT_FILE.split("/"));
    const state = failedState("data_bridge_failed");
    await writeFile(statePath, `${JSON.stringify(state)}\n`, "utf8");
    assert.deepEqual(await readLastRun(root), state);

    const context = Buffer.from('{"schemaVersion":"1.0"}\n', "utf8");
    await writeFile(contextPath, context);
    assert.equal(
      await hashFrozenContext(root),
      createHash("sha256").update(context).digest("hex")
    );

    await writeFile(statePath, "x".repeat(MAX_LAST_RUN_BYTES + 1), "utf8");
    await assert.rejects(() => readLastRun(root), /bounded/);

    if (process.platform !== "win32") {
      const targetPath = path.join(root, "work", "target.json");
      await writeFile(targetPath, `${JSON.stringify(state)}\n`, "utf8");
      await rm(statePath);
      await symlink(targetPath, statePath);
      await assert.rejects(() => readLastRun(root));
    } else {
      t.diagnostic("symlink no-follow assertion is Linux-only");
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("retry CLI authorizes a frozen resume only while the exact context hash still matches", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-frozen-retry-"));
  try {
    await mkdir(path.join(root, "work"), { recursive: true });
    const context = Buffer.from('{"schemaVersion":"1.0","snapshot":"fixed"}\n', "utf8");
    const observedHash = createHash("sha256").update(context).digest("hex");
    await writeFile(path.join(root, ...FROZEN_CONTEXT_FILE.split("/")), context);
    await writeFile(
      path.join(root, "work", "last-run.json"),
      `${JSON.stringify(failedState("pi_generation_failed", {
        contextSha256: observedHash
      }))}\n`,
      "utf8"
    );
    const argv = [
      "--exit-code", "1",
      "--attempt", "1",
      "--max-attempts", "3",
      "--started-at", attempt.attemptStartedAt,
      "--ended-at", attempt.attemptEndedAt,
      "--frozen-cutoff", attempt.frozenCutoffInstant,
      "--target-date", attempt.targetDate
    ];
    let output = "";
    assert.equal(await main(argv, {
      projectRoot: root,
      output: { write: (chunk) => { output += chunk; } }
    }), 0);
    assert.equal(output, `retry\tfrozen_context\t${observedHash}\n`);

    await writeFile(
      path.join(root, ...FROZEN_CONTEXT_FILE.split("/")),
      '{"schemaVersion":"1.0","snapshot":"changed"}\n',
      "utf8"
    );
    output = "";
    assert.equal(await main(argv, {
      projectRoot: root,
      output: { write: (chunk) => { output += chunk; } }
    }), 1);
    assert.equal(output, "stop\tfrozen_snapshot_hash_mismatch\t-\n");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
