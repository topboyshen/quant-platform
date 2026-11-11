#!/usr/bin/env node

import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  MAX_RESEARCH_INPUT_BYTES,
  getOrchestratorStatus,
  getTaskCatalog,
  recordMorningLockConflict,
  runTask
} from "../src/task-orchestrator.mjs";
import { isMainEntry } from "../src/main-entry.mjs";

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function readBoundedStdin(maxBytes) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > maxBytes) throw new Error("stdin exceeds the fixed task limit");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export function parseTaskCli(argv) {
  if (argv.length === 1 && argv[0] === "catalog") return { command: "catalog" };
  if (argv.length === 1 && argv[0] === "status") return { command: "status" };
  if (argv.length === 2 && argv[0] === "run" && argv[1] === "morning-report") {
    return { command: "run", task: "morning-report", origin: "cron" };
  }
  if (argv.length === 2 && argv[0] === "run" && argv[1] === "research-context") {
    return { command: "run", task: "research-context", origin: "pi" };
  }
  if (argv.length === 2 && argv[0] === "record-lock-conflict" && argv[1] === "morning-report") {
    return { command: "record-lock-conflict", task: "morning-report", origin: "cron" };
  }
  throw new Error(
    "usage: task-orchestrator.mjs catalog|status|run morning-report|run research-context|record-lock-conflict morning-report"
  );
}

export async function main(argv = process.argv.slice(2)) {
  const parsed = parseTaskCli(argv);
  if (parsed.command === "catalog") {
    process.stdout.write(`${JSON.stringify(getTaskCatalog(), null, 2)}\n`);
    return 0;
  }
  if (parsed.command === "status") {
    process.stdout.write(`${JSON.stringify(await getOrchestratorStatus(PROJECT_ROOT), null, 2)}\n`);
    return 0;
  }
  if (parsed.command === "record-lock-conflict") {
    const event = await recordMorningLockConflict(PROJECT_ROOT);
    process.stdout.write(`${JSON.stringify({
      task: parsed.task,
      outcome: event.outcome,
      reasonCode: event.reasonCode,
      observedAt: event.observedAt,
      owner: event.lock.owner
    })}\n`);
    return 0;
  }

  const input = parsed.task === "research-context"
    ? await readBoundedStdin(MAX_RESEARCH_INPUT_BYTES)
    : undefined;
  const result = await runTask({ task: parsed.task, origin: parsed.origin, input }, {
    projectRoot: PROJECT_ROOT
  });
  if (parsed.task === "research-context" && result.exitCode === 0) {
    process.stdout.write(result.stdout);
    if (result.stdout.length > 0 && result.stdout.at(-1) !== 0x0a) process.stdout.write("\n");
  } else {
    process.stdout.write(`${JSON.stringify({
      task: parsed.task,
      outcome: result.outcome,
      targetDate: result.targetDate ?? null,
      exitCode: result.exitCode
    })}\n`);
  }
  return result.exitCode;
}

if (isMainEntry(import.meta.url)) {
  main().then((exitCode) => {
    process.exitCode = exitCode;
  }).catch((error) => {
    process.stderr.write(`task orchestrator failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
