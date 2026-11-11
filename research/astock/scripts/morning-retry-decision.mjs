#!/usr/bin/env node

import { createHash } from "node:crypto";
import { constants as fsConstants } from "node:fs";
import { open } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  classifyRunOutcome,
  dateInTimeZone
} from "../src/run-notification.mjs";
import { isMainEntry } from "../src/main-entry.mjs";

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MAX_LAST_RUN_BYTES = 64 * 1024;
const MAX_FROZEN_CONTEXT_BYTES = 10 * 1024 * 1024;
const FROZEN_CONTEXT_FILE = "work/morning-context.json";
const AUTO_RETRY_POLICIES = Object.freeze({
  data_bridge_timeout: Object.freeze({ phase: "collect", resumeMode: "recollect" }),
  data_bridge_failed: Object.freeze({ phase: "collect", resumeMode: "recollect" }),
  hermes_generation_failed: Object.freeze({ phase: "generate", resumeMode: "frozen_context" }),
  hermes_review_failed: Object.freeze({ phase: "review", resumeMode: "frozen_context" }),
  pi_generation_failed: Object.freeze({ phase: "generate", resumeMode: "frozen_context" }),
  pi_review_failed: Object.freeze({ phase: "review", resumeMode: "frozen_context" })
});
const AUTO_RETRY_PHASES = Object.freeze(Object.fromEntries(
  Object.entries(AUTO_RETRY_POLICIES).map(([failureCode, policy]) => [
    failureCode,
    policy.phase
  ])
));

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isIsoInstant(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

export function decideMorningRetry({
  exitCode,
  attempt,
  maxAttempts,
  attemptStartedAt,
  attemptEndedAt,
  frozenCutoffInstant,
  targetDate,
  lastRun
}) {
  const stop = (reasonCode) => ({ retry: false, reasonCode });
  if (
    exitCode !== 1
    || !Number.isInteger(attempt)
    || !Number.isInteger(maxAttempts)
    || attempt < 1
    || maxAttempts < 1
    || maxAttempts > 3
    || attempt >= maxAttempts
  ) {
    return stop("attempt_or_exit_not_retryable");
  }
  if (
    !/^\d{4}-\d{2}-\d{2}$/.test(targetDate ?? "")
    || !isIsoInstant(attemptStartedAt)
    || !isIsoInstant(attemptEndedAt)
    || !isIsoInstant(frozenCutoffInstant)
    || Date.parse(attemptStartedAt) > Date.parse(attemptEndedAt)
    || Date.parse(frozenCutoffInstant) > Date.parse(attemptStartedAt) + 5_000
    || dateInTimeZone("Asia/Shanghai", new Date(frozenCutoffInstant)) !== targetDate
  ) {
    return stop("attempt_identity_invalid");
  }
  if (!isPlainObject(lastRun)) return stop("last_run_invalid");
  if (
    !isIsoInstant(lastRun.frozenCutoffInstant)
    || Date.parse(lastRun.frozenCutoffInstant) !== Date.parse(frozenCutoffInstant)
  ) {
    return stop("frozen_cutoff_mismatch");
  }

  const retryPolicy = AUTO_RETRY_POLICIES[lastRun.failureCode];
  if (
    retryPolicy === undefined
    || lastRun.phase !== retryPolicy.phase
    || lastRun.status !== "failed"
    || lastRun.publicationGate !== "blocked"
    || lastRun.retryable !== true
    || (lastRun.reportFile !== undefined && lastRun.reportFile !== null)
  ) {
    return stop("failure_contract_not_retryable");
  }

  let classification;
  try {
    classification = classifyRunOutcome({
      exitCode,
      invocationStartedAt: attemptStartedAt,
      invocationEndedAt: attemptEndedAt,
      expectedTargetDate: targetDate,
      lastRun
    });
  } catch {
    return stop("classification_failed");
  }
  if (
    classification.outcome !== "failed"
    || classification.reasonCode !== lastRun.failureCode
    || classification.retryable !== true
  ) {
    return stop("fresh_failure_not_confirmed");
  }
  if (retryPolicy.resumeMode === "frozen_context") {
    if (
      lastRun.contextFile !== FROZEN_CONTEXT_FILE
      || !/^[0-9a-f]{64}$/.test(lastRun.contextSha256 ?? "")
    ) {
      return stop("frozen_snapshot_metadata_invalid");
    }
    return {
      retry: true,
      reasonCode: classification.reasonCode,
      resumeMode: retryPolicy.resumeMode,
      contextSha256: lastRun.contextSha256
    };
  }
  return {
    retry: true,
    reasonCode: classification.reasonCode,
    resumeMode: retryPolicy.resumeMode
  };
}

async function readLastRun(projectRoot) {
  const filePath = path.join(projectRoot, "work", "last-run.json");
  const noFollow = fsConstants.O_NOFOLLOW ?? 0;
  const handle = await open(filePath, fsConstants.O_RDONLY | noFollow);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size <= 0 || info.size > MAX_LAST_RUN_BYTES) {
      throw new Error("last-run state is not a bounded regular file");
    }
    const raw = await handle.readFile();
    if (raw.length !== info.size || raw.length > MAX_LAST_RUN_BYTES) {
      throw new Error("last-run state changed while being read");
    }
    const parsed = JSON.parse(raw.toString("utf8"));
    if (!isPlainObject(parsed)) throw new Error("last-run state is not an object");
    return parsed;
  } finally {
    await handle.close();
  }
}

async function hashFrozenContext(projectRoot) {
  const filePath = path.join(projectRoot, ...FROZEN_CONTEXT_FILE.split("/"));
  const noFollow = fsConstants.O_NOFOLLOW ?? 0;
  const handle = await open(filePath, fsConstants.O_RDONLY | noFollow);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size <= 0 || info.size > MAX_FROZEN_CONTEXT_BYTES) {
      throw new Error("frozen context is not a bounded regular file");
    }
    const raw = await handle.readFile();
    if (raw.length !== info.size || raw.length > MAX_FROZEN_CONTEXT_BYTES) {
      throw new Error("frozen context changed while being read");
    }
    return createHash("sha256").update(raw).digest("hex");
  } finally {
    await handle.close();
  }
}

export function formatRetryDecision(decision) {
  if (decision?.retry !== true) {
    const reasonCode = /^[a-z0-9_]{1,80}$/.test(decision?.reasonCode ?? "")
      ? decision.reasonCode
      : "retry_decision_invalid";
    return `stop\t${reasonCode}\t-`;
  }
  if (decision.resumeMode === "recollect") return "retry\trecollect\t-";
  if (
    decision.resumeMode === "frozen_context"
    && /^[0-9a-f]{64}$/.test(decision.contextSha256 ?? "")
  ) {
    return `retry\tfrozen_context\t${decision.contextSha256}`;
  }
  return "stop\tretry_decision_invalid\t-";
}

export function parseRetryArguments(argv) {
  const allowed = new Set([
    "--exit-code",
    "--attempt",
    "--max-attempts",
    "--started-at",
    "--ended-at",
    "--frozen-cutoff",
    "--target-date"
  ]);
  const parsed = {};
  if (argv.length !== allowed.size * 2) throw new Error("invalid retry-decision arguments");
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!allowed.has(key) || value === undefined || parsed[key] !== undefined) {
      throw new Error("invalid retry-decision arguments");
    }
    parsed[key] = value;
  }
  const exitCode = Number(parsed["--exit-code"]);
  const attempt = Number(parsed["--attempt"]);
  const maxAttempts = Number(parsed["--max-attempts"]);
  if (
    !Number.isInteger(exitCode)
    || exitCode < 0
    || exitCode > 255
    || !Number.isInteger(attempt)
    || !Number.isInteger(maxAttempts)
  ) {
    throw new Error("invalid retry-decision numeric argument");
  }
  return {
    exitCode,
    attempt,
    maxAttempts,
    attemptStartedAt: parsed["--started-at"],
    attemptEndedAt: parsed["--ended-at"],
    frozenCutoffInstant: parsed["--frozen-cutoff"],
    targetDate: parsed["--target-date"]
  };
}

export async function main(argv = process.argv.slice(2), {
  projectRoot = PROJECT_ROOT,
  output = process.stdout
} = {}) {
  let decision = { retry: false, reasonCode: "retry_decision_invalid" };
  try {
    decision = decideMorningRetry({
      ...parseRetryArguments(argv),
      lastRun: await readLastRun(projectRoot)
    });
    if (decision.retry && decision.resumeMode === "frozen_context") {
      const observedHash = await hashFrozenContext(projectRoot);
      if (observedHash !== decision.contextSha256) {
        decision = { retry: false, reasonCode: "frozen_snapshot_hash_mismatch" };
      }
    }
  } catch {
    // Fail closed. Never expose state contents or parser details to cron logs.
  }
  const formatted = formatRetryDecision(decision);
  output.write(`${formatted}\n`);
  return formatted.startsWith("retry\t") ? 0 : 1;
}

if (isMainEntry(import.meta.url)) {
  main().then((exitCode) => {
    process.exitCode = exitCode;
  }).catch(() => {
    process.stderr.write("morning retry decision failed closed\n");
    process.exitCode = 1;
  });
}

export {
  AUTO_RETRY_PHASES,
  AUTO_RETRY_POLICIES,
  FROZEN_CONTEXT_FILE,
  MAX_FROZEN_CONTEXT_BYTES,
  MAX_LAST_RUN_BYTES,
  hashFrozenContext,
  readLastRun
};
