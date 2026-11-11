import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import {
  mkdtemp,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {
  authorizeTaskRequest,
  getOrchestratorStatus,
  getTaskCatalog,
  recordMorningLockConflict,
  runFixedTaskProcess,
  runTask
} from "../src/task-orchestrator.mjs";
import { parseTaskCli } from "../scripts/task-orchestrator.mjs";
import { filingIsEligible } from "../src/disclosure-evidence.mjs";

const fixedNow = () => new Date("2026-08-03T05:00:00.000Z");
const researchInput = {
  question: "分析东方电气",
  targetDate: "2026-08-03",
  symbols: ["600875.SH"]
};

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "astock-orchestrator-"));
  await mkdir(path.join(root, "scripts"), { recursive: true });
  await mkdir(path.join(root, "work"), { recursive: true });
  await mkdir(path.join(root, "config"), { recursive: true });
  await writeFile(path.join(root, "config", "workflow.json"), JSON.stringify({
    timezone: "Asia/Shanghai", scheduledTime: "08:00"
  }));
  await writeFile(path.join(root, "scripts", "research-context.mjs"), "// fixed research\n", "utf8");
  await writeFile(path.join(root, "scripts", "run-morning-cron.sh"), "#!/usr/bin/env bash\n", "utf8");
  return root;
}

test("a delayed scheduled start freezes the configured cutoff and excludes later disclosures", async () => {
  const root = await fixture();
  const startedAt = "2026-08-03T00:00:30.000Z";
  try {
    const result = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: () => new Date(startedAt),
      processRunner: async (_definition, _input, { occurrenceIdentity }) => {
        assert.deepEqual(occurrenceIdentity, {
          targetDate: "2026-08-03", frozenCutoffInstant: "2026-08-03T00:00:00.000Z"
        });
        for (const [time, eligible] of [["07:59:59", true], ["08:00:01", false]]) {
          const filing = {
            code: "002617", title: `披露 ${time}`, sourceType: "official_announcement",
            disclosureDate: "2026-08-03", publishedAt: `2026-08-03T${time}+08:00`, sourceIds: ["filing"]
          };
          assert.equal(filingIsEligible(filing, [{ ...filing, id: "filing" }], {
            cutoffInstant: occurrenceIdentity.frozenCutoffInstant, previousDate: "2026-07-31", code: "002617"
          }), eligible);
        }
        const claimed = JSON.parse(await readFile(path.join(root, "work/orchestrator/states/morning-report-2026-08-03.json")));
        assert.equal(claimed.schemaVersion, "2.0");
        assert.equal(claimed.startedAt, startedAt);
        assert.equal(claimed.frozenCutoffInstant, occurrenceIdentity.frozenCutoffInstant);
        await writeFile(path.join(root, "work/last-run.json"), JSON.stringify({ targetDate: "2026-08-03", status: "success" }));
        return { exitCode: 0, signal: null, stdout: Buffer.alloc(0), timedOut: false };
      }
    });
    assert.equal(result.outcome, "success");
    const terminal = JSON.parse(await readFile(path.join(root, "work/orchestrator/states/morning-report-2026-08-03.json")));
    assert.equal(terminal.schemaVersion, "2.0");
    assert.equal(terminal.startedAt, startedAt);
    assert.equal(terminal.frozenCutoffInstant, "2026-08-03T00:00:00.000Z");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("the configured schedule must obey the fixed contract and cannot use a future occurrence", async () => {
  const root = await fixture();
  let calls = 0;
  try {
    await writeFile(path.join(root, "config/workflow.json"), JSON.stringify({
      timezone: "Asia/Shanghai", scheduledTime: "08:15"
    }));
    const early = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root, now: () => new Date("2026-08-03T00:15:30Z"),
      processRunner: async () => { calls += 1; throw new Error("must not spawn"); }
    });
    assert.equal(early.outcome, "failed");
    assert.equal(calls, 0);
    await writeFile(path.join(root, "config/workflow.json"), JSON.stringify({
      timezone: "Asia/Shanghai", scheduledTime: "08:00"
    }));
    const beforeSchedule = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root, now: () => new Date("2026-08-02T23:59:59Z"),
      processRunner: async () => { calls += 1; throw new Error("must not spawn"); }
    });
    assert.equal(beforeSchedule.outcome, "failed");
    assert.equal(calls, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("catalog exposes two fixed routes and keeps sensitive capabilities blocked", () => {
  const catalog = getTaskCatalog();
  assert.deepEqual(catalog.routes.map((route) => [route.task, route.origin]), [
    ["morning-report", "cron"],
    ["research-context", "pi"]
  ]);
  assert.equal(catalog.routes.every((route) => route.telegramAllowed === false), true);
  assert.deepEqual(
    catalog.routes.find((route) => route.task === "morning-report")?.sideEffects,
    ["report_files", "last_run_state"]
  );
  assert.equal(
    catalog.routes.find((route) => route.task === "morning-report")?.retryPolicy,
    "up_to_2_internal_retries_for_fresh_allowlisted_prepublication_failures"
  );
  assert.equal(catalog.requestControlledExecutableOrArgs, false);
  assert.match(JSON.stringify(catalog.blockedCapabilities), /deploy/);
  assert.match(JSON.stringify(catalog.blockedCapabilities), /publish_or_resend/);
});

test("route authorization is fail-closed and request fields cannot select execution", () => {
  assert.throws(
    () => authorizeTaskRequest({ task: "deploy", origin: "telegram" }),
    /not authorized/
  );
  assert.throws(
    () => authorizeTaskRequest({ task: "morning-report", origin: "telegram" }),
    /not authorized/
  );
  for (const field of ["command", "path", "tool", "url", "model", "env", "args"]) {
    assert.throws(() => authorizeTaskRequest({
      task: "research-context",
      origin: "pi",
      input: researchInput,
      [field]: "attacker-controlled"
    }), /forbidden field/);
  }
  assert.throws(() => authorizeTaskRequest({
    task: "research-context",
    origin: "pi",
    input: { ...researchInput, command: "bash" }
  }), /forbidden field/);
});

test("research JSON from CLI stdin is normalized from a Buffer", () => {
  const serialized = Buffer.from(JSON.stringify(researchInput), "utf8");
  const { definition, input } = authorizeTaskRequest({
    task: "research-context",
    origin: "pi",
    input: serialized
  });
  assert.equal(definition.task, "research-context");
  assert.deepEqual(JSON.parse(input.toString("utf8")), researchInput);
});

test("CLI grammar has no generic task, origin, retry or arbitrary argument route", () => {
  assert.deepEqual(parseTaskCli(["run", "morning-report"]), {
    command: "run", task: "morning-report", origin: "cron"
  });
  assert.deepEqual(parseTaskCli(["run", "research-context"]), {
    command: "run", task: "research-context", origin: "pi"
  });
  assert.deepEqual(parseTaskCli(["record-lock-conflict", "morning-report"]), {
    command: "record-lock-conflict", task: "morning-report", origin: "cron"
  });
  assert.throws(() => parseTaskCli(["run", "deploy"]), /usage/);
  assert.throws(() => parseTaskCli(["retry", "morning-report"]), /usage/);
  assert.throws(() => parseTaskCli(["record-lock-conflict", "research-context"]), /usage/);
  assert.throws(() => parseTaskCli(["run", "morning-report", "--date", "2026-08-04"]), /usage/);
});

test("an outer morning lock conflict records a bounded diagnostic audit event", async () => {
  const root = await fixture();
  try {
    const orchestratorRoot = path.join(root, "work", "orchestrator");
    await mkdir(orchestratorRoot, { recursive: true });
    await writeFile(path.join(orchestratorRoot, "morning-report.lock"), JSON.stringify({
      schemaVersion: "1.0",
      task: "morning-report",
      pid: 4321,
      startedAt: "2026-08-03T00:00:01.000Z"
    }), "utf8");

    const event = await recordMorningLockConflict(root, { now: fixedNow });
    assert.equal(event.outcome, "blocked");
    assert.equal(event.reasonCode, "outer_process_lock_busy");
    assert.deepEqual(event.lock.owner, {
      availability: "available",
      task: "morning-report",
      pid: 4321,
      startedAt: "2026-08-03T00:00:01.000Z"
    });

    const eventDirectory = path.join(orchestratorRoot, "events");
    const names = await readdir(eventDirectory);
    assert.equal(names.length, 1);
    assert.match(names[0], /\.lock-conflict\.json$/);
    const stored = JSON.parse(await readFile(path.join(eventDirectory, names[0]), "utf8"));
    assert.equal(stored.reasonCode, "outer_process_lock_busy");
    assert.equal(stored.lock.owner.pid, 4321);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("research audit stores hashes and state but not the question or raw result", async () => {
  const root = await fixture();
  try {
    const result = await runTask({
      task: "research-context",
      origin: "pi",
      input: researchInput
    }, {
      projectRoot: root,
      now: fixedNow,
      processRunner: async () => ({
        exitCode: 0,
        signal: null,
        stdout: Buffer.from('{"privateResult":"evidence-body"}\n'),
        timedOut: false,
        outputTooLarge: false
      })
    });
    assert.equal(result.outcome, "success");
    const eventDirectory = path.join(root, "work", "orchestrator", "events");
    const names = await readdir(eventDirectory);
    assert.equal(names.length, 2);
    const combined = (await Promise.all(names.map((name) => readFile(path.join(eventDirectory, name), "utf8")))).join("\n");
    assert.doesNotMatch(combined, /分析东方电气/);
    assert.doesNotMatch(combined, /evidence-body/);
    assert.match(combined, /"sha256": "[a-f0-9]{64}"/);
    assert.equal((await getOrchestratorStatus(root)).eventCount, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("research route is single-instance and a busy request does not spawn", async () => {
  const root = await fixture();
  let releaseFirst;
  let calls = 0;
  const first = runTask({ task: "research-context", origin: "pi", input: researchInput }, {
    projectRoot: root,
    now: fixedNow,
    processRunner: async () => {
      calls += 1;
      await new Promise((resolve) => { releaseFirst = resolve; });
      return {
        exitCode: 0,
        signal: null,
        stdout: Buffer.from("{}\n"),
        timedOut: false,
        outputTooLarge: false
      };
    }
  });
  try {
    for (let index = 0; index < 100 && !releaseFirst; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    assert.equal(typeof releaseFirst, "function");
    const second = await runTask({ task: "research-context", origin: "pi", input: researchInput }, {
      projectRoot: root,
      now: fixedNow,
      processRunner: async () => { throw new Error("must not spawn"); }
    });
    assert.equal(second.exitCode, 75);
    assert.equal(second.outcome, "busy");
    assert.equal(calls, 1);
    releaseFirst();
    assert.equal((await first).outcome, "success");
  } finally {
    releaseFirst?.();
    await first.catch(() => {});
    await rm(root, { recursive: true, force: true });
  }
});

test("an old research lock is never stolen automatically", async () => {
  const root = await fixture();
  let calls = 0;
  try {
    const locks = path.join(root, "work", "orchestrator", "locks");
    await mkdir(locks, { recursive: true });
    await writeFile(path.join(locks, "research-context.lock"), JSON.stringify({
      schemaVersion: "1.0",
      invocationId: "stale-owner",
      pid: 999999,
      startedAt: "2000-01-01T00:00:00.000Z"
    }), "utf8");
    const result = await runTask({ task: "research-context", origin: "pi", input: researchInput }, {
      projectRoot: root,
      now: fixedNow,
      processRunner: async () => {
        calls += 1;
        return { exitCode: 0, signal: null, stdout: Buffer.from("{}\n") };
      }
    });
    assert.equal(result.outcome, "busy");
    assert.equal(result.exitCode, 75);
    assert.equal(calls, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a completed morning occurrence is a no-op and failed state requires review", async () => {
  const root = await fixture();
  let calls = 0;
  try {
    const successRunner = async (_definition, _input, { occurrenceIdentity }) => {
      assert.deepEqual(occurrenceIdentity, {
        targetDate: "2026-08-03",
        frozenCutoffInstant: "2026-08-03T00:00:00.000Z"
      });
      calls += 1;
      await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
        targetDate: "2026-08-03",
        status: "success"
      }), "utf8");
      return { exitCode: 0, signal: null, stdout: Buffer.alloc(0), timedOut: false, outputTooLarge: false };
    };
    const first = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root, now: fixedNow, processRunner: successRunner
    });
    assert.equal(first.outcome, "success");
    const duplicate = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: fixedNow,
      processRunner: async () => { throw new Error("must not spawn"); }
    });
    assert.equal(duplicate.outcome, "noop");
    assert.equal(duplicate.exitCode, 0);
    assert.equal(calls, 1);

    const failedDate = () => new Date("2026-08-04T05:00:00.000Z");
    const failed = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: failedDate,
      processRunner: async () => {
        calls += 1;
        return { exitCode: 1, signal: null, stdout: Buffer.alloc(0), timedOut: false, outputTooLarge: false };
      }
    });
    assert.equal(failed.outcome, "failed");
    const blocked = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: failedDate,
      processRunner: async () => { throw new Error("must not spawn"); }
    });
    assert.equal(blocked.exitCode, 75);
    assert.equal(blocked.outcome, "blocked");
    assert.equal(calls, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a sealed legacy occurrence remains byte-identical under the scheduled cutoff change", async () => {
  const root = await fixture();
  try {
    const statePath = path.join(root, "work/orchestrator/states/morning-report-2026-08-03.json");
    await mkdir(path.dirname(statePath), { recursive: true });
    const original = JSON.stringify({
      schemaVersion: "1.0", task: "morning-report", targetDate: "2026-08-03", status: "success",
      invocationId: "aa5e44f3-c26c-4f0d-a31a-6e65da8e6639", startedAt: "2026-08-03T00:00:30.000Z",
      completedAt: "2026-08-03T00:05:00.000Z"
    }) + "\n";
    await writeFile(statePath, original);
    const result = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root, now: fixedNow,
      processRunner: async () => { throw new Error("sealed legacy occurrence must not run again"); }
    });
    assert.equal(result.outcome, "noop");
    assert.equal(await readFile(statePath, "utf8"), original);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("the outer occurrence identity remains pinned when completion crosses midnight", async () => {
  const root = await fixture();
  const instants = [
    new Date("2026-08-03T15:59:59.000Z"),
    new Date("2026-08-03T16:01:00.000Z"),
    new Date("2026-08-03T16:02:00.000Z")
  ];
  let clockIndex = 0;
  const now = () => instants[Math.min(clockIndex++, instants.length - 1)];
  try {
    const result = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now,
      processRunner: async (_definition, _input, { occurrenceIdentity }) => {
        assert.deepEqual(occurrenceIdentity, {
          targetDate: "2026-08-03",
          frozenCutoffInstant: "2026-08-03T00:00:00.000Z"
        });
        await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
          targetDate: occurrenceIdentity.targetDate,
          status: "success"
        }), "utf8");
        return {
          exitCode: 0,
          signal: null,
          stdout: Buffer.alloc(0),
          timedOut: false,
          interrupted: false,
          outputTooLarge: false
        };
      }
    });
    assert.equal(result.targetDate, "2026-08-03");
    assert.equal(result.outcome, "success");
    const state = JSON.parse(await readFile(path.join(
      root,
      "work",
      "orchestrator",
      "states",
      "morning-report-2026-08-03.json"
    ), "utf8"));
    assert.equal(state.targetDate, "2026-08-03");
    assert.equal(state.status, "success");
    assert.equal(state.startedAt, "2026-08-03T15:59:59.000Z");
    assert.equal(state.frozenCutoffInstant, "2026-08-03T00:00:00.000Z");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("status exposes unknown occurrence versus successful last-run and the next day remains independent", async () => {
  const root = await fixture();
  try {
    const interrupted = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: fixedNow,
      processRunner: async () => {
        await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
          targetDate: "2026-08-03",
          status: "success",
          startedAt: "2026-08-03T05:00:00.000Z",
          completedAt: "2026-08-03T05:10:00.000Z"
        }), "utf8");
        return {
          exitCode: 143,
          signal: null,
          stdout: Buffer.alloc(0),
          timedOut: false,
          interrupted: true,
          outputTooLarge: false
        };
      }
    });
    assert.equal(interrupted.outcome, "unknown");

    const divergentStatus = await getOrchestratorStatus(root);
    assert.equal(divergentStatus.morningStateDivergence, true);
    assert.deepEqual(divergentStatus.morningStateDivergences, [{
      targetDate: "2026-08-03",
      occurrenceStatus: "unknown",
      lastRunStatus: "success",
      action: "operator_review_required"
    }]);
    assert.deepEqual(divergentStatus.morningOccurrences, [{
      targetDate: "2026-08-03",
      status: "unknown",
      lastRunStatus: "success",
      stateConsistency: "diverged"
    }]);

    const nextDay = () => new Date("2026-08-04T05:00:00.000Z");
    const nextResult = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: nextDay,
      processRunner: async () => {
        await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
          targetDate: "2026-08-04",
          status: "success"
        }), "utf8");
        return {
          exitCode: 0,
          signal: null,
          stdout: Buffer.alloc(0),
          timedOut: false,
          interrupted: false,
          outputTooLarge: false
        };
      }
    });
    assert.equal(nextResult.outcome, "success");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("an unconfirmed Hermes delivery can never make a successful report occurrence successful", async () => {
  const root = await fixture();
  try {
    const result = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: fixedNow,
      processRunner: async () => {
        await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
          targetDate: "2026-08-03",
          status: "success"
        }), "utf8");
        return {
          exitCode: 76,
          signal: null,
          stdout: Buffer.alloc(0),
          timedOut: false,
          interrupted: false,
          outputTooLarge: false
        };
      }
    });
    assert.equal(result.outcome, "notification_pending");
    const status = await getOrchestratorStatus(root);
    assert.equal(status.morningOccurrences[0].status, "notification_pending");
    assert.equal(status.morningOccurrences[0].lastRunStatus, "success");
    assert.equal(status.morningOccurrences[0].stateConsistency, "diverged");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("an intentional publication gate block stays blocked instead of diverging as failed", async () => {
  const root = await fixture();
  try {
    const result = await runTask({ task: "morning-report", origin: "cron" }, {
      projectRoot: root,
      now: fixedNow,
      processRunner: async () => {
        await writeFile(path.join(root, "work", "last-run.json"), JSON.stringify({
          targetDate: "2026-08-03",
          status: "blocked"
        }), "utf8");
        return {
          exitCode: 2,
          signal: null,
          stdout: Buffer.alloc(0),
          timedOut: false,
          interrupted: false,
          outputTooLarge: false
        };
      }
    });
    assert.equal(result.outcome, "blocked");
    const status = await getOrchestratorStatus(root);
    assert.equal(status.morningOccurrences[0].stateConsistency, "consistent");
    assert.equal(status.morningStateDivergence, false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a fixed morning task receives only the pinned occurrence arguments", async () => {
  const root = await fixture();
  const { definition, input } = authorizeTaskRequest({ task: "morning-report", origin: "cron" });
  const signals = [];
  let child;
  const fakeSpawn = (_command, args, options) => {
    assert.deepEqual(args, [
      path.join(root, "scripts", "run-morning-cron.sh"),
      "--target-date",
      "2026-08-03",
      "--frozen-cutoff",
      "2026-08-03T05:00:00.000Z"
    ]);
    assert.equal(options.detached, true);
    child = new EventEmitter();
    child.pid = 24680;
    child.kill = (signal) => {
      signals.push({ target: "child", signal });
      return true;
    };
    return child;
  };
  const fakeProcessKill = (target, signal) => {
    signals.push({ target, signal });
    if (target === -24680 && signal === "SIGTERM") {
      queueMicrotask(() => child.emit("close", 143, null));
    }
    return true;
  };
  try {
    const result = await runFixedTaskProcess({ ...definition, timeoutMs: 5 }, input, {
      projectRoot: root,
      spawnImpl: fakeSpawn,
      platform: "linux",
      processKill: fakeProcessKill,
      occurrenceIdentity: {
        targetDate: "2026-08-03",
        frozenCutoffInstant: "2026-08-03T05:00:00.000Z"
      }
    });
    assert.equal(result.exitCode, 124);
    assert.equal(result.timedOut, true);
    assert.equal(result.interrupted, false);
    assert.deepEqual(signals, [{ target: -24680, signal: "SIGTERM" }]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("fixed research spawn never uses request-controlled executable, args, cwd or shell", async () => {
  const root = await fixture();
  const { definition, input } = authorizeTaskRequest({
    task: "research-context",
    origin: "pi",
    input: researchInput
  });
  let spawnCall;
  let received = "";
  const fakeSpawn = (command, args, options) => {
    spawnCall = { command, args, options };
    const child = new EventEmitter();
    child.stdin = new PassThrough();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin.on("data", (chunk) => { received += chunk.toString("utf8"); });
    child.kill = () => true;
    queueMicrotask(() => {
      child.stdout.end("{}\n");
      child.stderr.end();
      child.emit("close", 0, null);
    });
    return child;
  };
  try {
    const result = await runFixedTaskProcess(definition, input, {
      projectRoot: root,
      spawnImpl: fakeSpawn
    });
    assert.equal(result.exitCode, 0);
    assert.equal(spawnCall.command, process.execPath);
    assert.deepEqual(spawnCall.args, [path.join(root, "scripts", "research-context.mjs")]);
    assert.equal(spawnCall.options.cwd, root);
    assert.equal(spawnCall.options.shell, false);
    assert.equal(JSON.parse(received).question, researchInput.question);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
