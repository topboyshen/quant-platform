import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmod,
  lstat,
  mkdir,
  open,
  readFile,
  readdir,
  rename,
  unlink
} from "node:fs/promises";
import path from "node:path";

import { dateInTimeZone } from "./config.mjs";

const MAX_RESEARCH_INPUT_BYTES = 256 * 1024;
const MAX_RESEARCH_OUTPUT_BYTES = 16 * 1024 * 1024;
const MAX_STATE_BYTES = 64 * 1024;
const RESEARCH_TIMEOUT_MS = 340_000;
// The source-only runner has its own 55-minute shared workflow budget.
// Retain the historical adapter ceiling for compatibility; it no longer
// covers a sender or outbox drain, which belong to the seal success chain.
const MORNING_ADAPTER_TIMEOUT_MS = 95 * 60 * 1000;

const TASKS = Object.freeze({
  "morning-report": Object.freeze({
    task: "morning-report",
    origin: "cron",
    inputMode: "none",
    outputMode: "inherit",
    executable: "/usr/bin/bash",
    script: "scripts/run-morning-cron.sh",
    timeoutMs: MORNING_ADAPTER_TIMEOUT_MS,
    sideEffects: ["report_files", "last_run_state"],
    approval: "owner_schedule_preapproved",
    retryPolicy: "up_to_2_internal_retries_for_fresh_allowlisted_prepublication_failures",
    telegramAllowed: false
  }),
  "research-context": Object.freeze({
    task: "research-context",
    origin: "pi",
    inputMode: "research_json",
    outputMode: "json",
    executable: process.execPath,
    script: "scripts/research-context.mjs",
    timeoutMs: RESEARCH_TIMEOUT_MS,
    sideEffects: ["orchestrator_audit_only"],
    approval: "not_required_read_only",
    retryPolicy: "caller_may_retry_only_after_terminal_failure",
    telegramAllowed: false
  })
});

const BLOCKED_CAPABILITIES = Object.freeze([
  { capability: "telegram_to_pi", policy: "disabled_until_single_tool_channel_boundary" },
  { capability: "publish_or_resend", policy: "operator_approval_each_run" },
  { capability: "deploy", policy: "operator_approval_each_run" },
  { capability: "schedule_change", policy: "operator_approval_each_run" },
  { capability: "dev_shell", policy: "never_expose_to_telegram" },
  { capability: "trade_or_account_write", policy: "not_supported" }
]);

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function validateMorningOccurrenceIdentity(value) {
  if (
    !isPlainObject(value)
    || Object.keys(value).some((key) => !["targetDate", "frozenCutoffInstant"].includes(key))
    || typeof value.targetDate !== "string"
    || !/^\d{4}-\d{2}-\d{2}$/.test(value.targetDate)
    || typeof value.frozenCutoffInstant !== "string"
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value.frozenCutoffInstant)
  ) {
    throw new Error("morning occurrence identity is invalid");
  }
  const cutoff = new Date(value.frozenCutoffInstant);
  const targetInstant = new Date(`${value.targetDate}T00:00:00Z`);
  if (
    Number.isNaN(cutoff.getTime())
    || Number.isNaN(targetInstant.getTime())
    || targetInstant.toISOString().slice(0, 10) !== value.targetDate
    || dateInTimeZone("Asia/Shanghai", cutoff) !== value.targetDate
  ) {
    throw new Error("morning occurrence identity is inconsistent");
  }
  return {
    targetDate: value.targetDate,
    frozenCutoffInstant: cutoff.toISOString()
  };
}

function safeDefinition(definition) {
  return {
    task: definition.task,
    origin: definition.origin,
    inputMode: definition.inputMode,
    sideEffects: [...definition.sideEffects],
    approval: definition.approval,
    retryPolicy: definition.retryPolicy,
    telegramAllowed: definition.telegramAllowed
  };
}

export function getTaskCatalog() {
  return {
    schemaVersion: "1.0",
    owner: "deterministic_node_orchestrator",
    routes: Object.values(TASKS).map(safeDefinition),
    blockedCapabilities: BLOCKED_CAPABILITIES.map((item) => ({ ...item })),
    requestControlledExecutableOrArgs: false,
    hermesRole: "notification_channel_only",
    piRole: "stateless_research_worker_only"
  };
}

function normalizeResearchInput(input) {
  let value;
  if (isPlainObject(input)) {
    value = input;
  } else {
    const buffer = Buffer.isBuffer(input) ? input : Buffer.from(input ?? "", "utf8");
    if (buffer.length === 0 || buffer.length > MAX_RESEARCH_INPUT_BYTES) {
      throw new Error("research input must be 1—256 KiB");
    }
    try {
      value = JSON.parse(buffer.toString("utf8"));
    } catch {
      throw new Error("research input must be valid JSON");
    }
  }
  if (!isPlainObject(value)) throw new Error("research input must be a JSON object");
  const allowedKeys = new Set(["question", "targetDate", "symbols"]);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    throw new Error("research input contains a forbidden field");
  }
  if (typeof value.question !== "string" || value.question.trim().length < 2 || value.question.length > 500) {
    throw new Error("research question is invalid");
  }
  if (value.targetDate !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(value.targetDate)) {
    throw new Error("research targetDate is invalid");
  }
  if (value.symbols !== undefined) {
    if (!Array.isArray(value.symbols) || value.symbols.length < 1 || value.symbols.length > 10) {
      throw new Error("research symbols are invalid");
    }
    if (new Set(value.symbols).size !== value.symbols.length) {
      throw new Error("research symbols must be unique");
    }
    if (value.symbols.some((symbol) => typeof symbol !== "string" || !/^\d{6}\.(SH|SZ|BJ)$/.test(symbol))) {
      throw new Error("research symbol format is invalid");
    }
  }
  const normalized = Buffer.from(JSON.stringify(value), "utf8");
  if (normalized.length === 0 || normalized.length > MAX_RESEARCH_INPUT_BYTES) {
    throw new Error("research input must be 1—256 KiB");
  }
  return normalized;
}

export function authorizeTaskRequest(request) {
  if (!isPlainObject(request)) throw new Error("task request must be an object");
  const allowedKeys = new Set(["task", "origin", "input"]);
  if (Object.keys(request).some((key) => !allowedKeys.has(key))) {
    throw new Error("task request contains a forbidden field");
  }
  const definition = TASKS[request.task];
  if (!definition || request.origin !== definition.origin) {
    throw new Error("task route is not authorized");
  }
  let input = Buffer.alloc(0);
  if (definition.inputMode === "research_json") {
    input = normalizeResearchInput(request.input);
  } else if (request.input !== undefined && request.input !== null && request.input.length !== 0) {
    throw new Error("task does not accept input");
  }
  return { definition, input };
}

async function ensurePrivateDirectory(directory) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const info = await lstat(directory);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("invalid orchestrator directory");
  if (process.platform !== "win32") await chmod(directory, 0o700);
  return directory;
}

async function ensureLayout(projectRoot) {
  const root = await ensurePrivateDirectory(path.join(projectRoot, "work", "orchestrator"));
  return {
    root,
    events: await ensurePrivateDirectory(path.join(root, "events")),
    states: await ensurePrivateDirectory(path.join(root, "states")),
    locks: await ensurePrivateDirectory(path.join(root, "locks"))
  };
}

async function writeExclusiveJson(filePath, value) {
  const handle = await open(filePath, "wx", 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function writeAtomicJson(filePath, value) {
  const temporary = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${process.pid}.${randomUUID()}.tmp`
  );
  await writeExclusiveJson(temporary, value);
  await rename(temporary, filePath);
}

async function readBoundedJson(filePath, maxBytes = MAX_STATE_BYTES) {
  const info = await lstat(filePath);
  if (!info.isFile() || info.isSymbolicLink() || info.size > maxBytes) {
    throw new Error("invalid orchestrator state file");
  }
  const value = JSON.parse(await readFile(filePath, "utf8"));
  if (!isPlainObject(value)) throw new Error("invalid orchestrator state object");
  return value;
}

async function writeAuditEvent(layout, invocationId, phase, value) {
  await writeExclusiveJson(path.join(layout.events, `${invocationId}.${phase}.json`), value);
}

async function readMorningLockOwner(layout) {
  try {
    const value = await readBoundedJson(path.join(layout.root, "morning-report.lock"), 4096);
    if (
      value.task !== "morning-report"
      || !Number.isInteger(value.pid)
      || value.pid <= 0
      || typeof value.startedAt !== "string"
    ) {
      return { availability: "invalid" };
    }
    return {
      availability: "available",
      task: "morning-report",
      pid: value.pid,
      startedAt: value.startedAt
    };
  } catch (error) {
    return { availability: error.code === "ENOENT" ? "missing" : "invalid" };
  }
}

export async function recordMorningLockConflict(projectRoot, {
  now = () => new Date()
} = {}) {
  if (typeof projectRoot !== "string" || !path.isAbsolute(projectRoot)) {
    throw new Error("projectRoot must be an absolute path");
  }
  const layout = await ensureLayout(projectRoot);
  const observationId = randomUUID();
  const observedAt = now().toISOString();
  const owner = await readMorningLockOwner(layout);
  const event = {
    schemaVersion: "1.0",
    observationId,
    phase: "lock_conflict",
    task: "morning-report",
    origin: "cron",
    observedAt,
    outcome: "blocked",
    reasonCode: "outer_process_lock_busy",
    lock: {
      path: "work/orchestrator/morning-report.lock",
      owner
    }
  };
  await writeAuditEvent(layout, observationId, "lock-conflict", event);
  return event;
}

async function acquireResearchLock(layout, invocationId, now) {
  const lockPath = path.join(layout.locks, "research-context.lock");
  const value = { schemaVersion: "1.0", invocationId, pid: process.pid, startedAt: now.toISOString() };
  const attempt = async () => {
    try {
      await writeExclusiveJson(lockPath, value);
      return async () => {
        try {
          const current = await readBoundedJson(lockPath, 4096);
          if (current.invocationId === invocationId) await unlink(lockPath);
        } catch {
          // A missing/replaced lock must not mask the task's primary result.
        }
      };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      return null;
    }
  };

  // Never steal an existing lock automatically. A crashed owner may have
  // left a model descendant alive, and read/unlink/recreate recovery races
  // with other contenders. Status exposes the lock for operator review.
  return attempt();
}

async function assertFixedScript(projectRoot, definition) {
  const scriptPath = path.join(projectRoot, ...definition.script.split("/"));
  const relative = path.relative(projectRoot, scriptPath);
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("fixed task path escaped root");
  const info = await lstat(scriptPath);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error("fixed task script is invalid");
  return scriptPath;
}

function terminateProcessTree(child, signal, {
  platform = process.platform,
  processKill = process.kill
} = {}) {
  let groupSignalled = false;
  try {
    if (platform !== "win32" && child.pid) {
      processKill(-child.pid, signal);
      groupSignalled = true;
    }
  } catch {
    // Fall back to the direct child when a process group is unavailable.
  }
  if (!groupSignalled) {
    try {
      child.kill(signal);
    } catch {
      // The child may already have exited.
    }
  }
}

export async function runFixedTaskProcess(definition, input, {
  projectRoot,
  spawnImpl = spawn,
  platform = process.platform,
  processKill = process.kill,
  occurrenceIdentity = null
}) {
  const scriptPath = await assertFixedScript(projectRoot, definition);
  const args = [scriptPath];
  if (definition.task === "morning-report") {
    const identity = validateMorningOccurrenceIdentity(occurrenceIdentity);
    args.push(
      "--target-date",
      identity.targetDate,
      "--frozen-cutoff",
      identity.frozenCutoffInstant
    );
  } else if (occurrenceIdentity !== null && occurrenceIdentity !== undefined) {
    throw new Error("occurrence identity is only valid for the morning report");
  }
  const options = {
    cwd: projectRoot,
    env: { ...process.env },
    shell: false,
    windowsHide: true,
    detached: platform !== "win32",
    stdio: definition.outputMode === "json"
      ? ["pipe", "pipe", "pipe"]
      : ["ignore", "inherit", "inherit"]
  };
  const child = spawnImpl(definition.executable, args, options);
  let stdout = Buffer.alloc(0);
  let outputTooLarge = false;
  let timedOut = false;
  let interrupted = false;

  if (definition.outputMode === "json") {
    const chunks = [];
    let bytes = 0;
    child.stdout.on("data", (chunk) => {
      const buffer = Buffer.from(chunk);
      bytes += buffer.length;
      if (bytes > MAX_RESEARCH_OUTPUT_BYTES) {
        outputTooLarge = true;
        terminateProcessTree(child, "SIGTERM", { platform, processKill });
        return;
      }
      chunks.push(buffer);
    });
    child.stderr.on("data", () => {});
    child.stdin.on("error", () => {});
    child.stdin.end(input);
    child.once("close", () => { stdout = Buffer.concat(chunks); });
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    let killTimer = null;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      process.removeListener("SIGTERM", onTerm);
      process.removeListener("SIGINT", onInt);
      resolve(result);
    };
    const stop = (signal) => {
      terminateProcessTree(child, signal, { platform, processKill });
      killTimer ??= setTimeout(
        () => terminateProcessTree(child, "SIGKILL", { platform, processKill }),
        5_000
      );
    };
    const onTerm = () => { interrupted = true; stop("SIGTERM"); };
    const onInt = () => { interrupted = true; stop("SIGINT"); };
    process.once("SIGTERM", onTerm);
    process.once("SIGINT", onInt);
    const timer = setTimeout(() => {
      timedOut = true;
      stop("SIGTERM");
    }, definition.timeoutMs);

    child.once("error", (error) => {
      if (settled) return;
      clearTimeout(timer);
      process.removeListener("SIGTERM", onTerm);
      process.removeListener("SIGINT", onInt);
      reject(error);
    });
    child.once("close", (code, signal) => {
      let exitCode = Number.isInteger(code) ? code : 1;
      if (timedOut) exitCode = 124;
      else if (outputTooLarge) exitCode = 65;
      else if (interrupted && exitCode === 0) exitCode = 130;
      finish({
        exitCode,
        signal: signal ?? null,
        stdout,
        timedOut,
        interrupted,
        outputTooLarge
      });
    });
  });
}

function occurrencePath(layout, targetDate) {
  return path.join(layout.states, `morning-report-${targetDate}.json`);
}

async function claimMorningOccurrence(layout, identity, invocationId, startedAt) {
  const { targetDate, frozenCutoffInstant } = identity;
  const filePath = occurrencePath(layout, targetDate);
  const running = {
    schemaVersion: "2.0",
    task: "morning-report",
    targetDate,
    status: "running",
    invocationId,
    startedAt,
    frozenCutoffInstant
  };
  try {
    await writeExclusiveJson(filePath, running);
    return { claimed: true, filePath };
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
    const state = await readBoundedJson(filePath);
    if (["success", "skipped"].includes(state.status)) {
      return { claimed: false, filePath, disposition: "already_terminal", state };
    }
    return { claimed: false, filePath, disposition: "operator_review_required", state };
  }
}

async function classifyMorningTerminal(projectRoot, targetDate, processResult) {
  if (processResult.exitCode === 124 || processResult.timedOut || processResult.interrupted || processResult.signal) {
    return "unknown";
  }
  if (processResult.exitCode === 76) return "notification_pending";
  try {
    const lastRun = await readBoundedJson(path.join(projectRoot, "work", "last-run.json"));
    if (lastRun.targetDate === targetDate) {
      if (processResult.exitCode === 0 && ["success", "skipped"].includes(lastRun.status)) {
        return lastRun.status;
      }
      if (processResult.exitCode === 2 && lastRun.status === "blocked") {
        return "blocked";
      }
    }
  } catch {
    // The legacy runner remains the source of truth; an invalid/missing state
    // is recorded as failed without changing its original exit code.
  }
  return "failed";
}

export async function runTask(request, {
  projectRoot,
  now = () => new Date(),
  processRunner = runFixedTaskProcess
} = {}) {
  if (typeof projectRoot !== "string" || !path.isAbsolute(projectRoot)) {
    throw new Error("projectRoot must be an absolute path");
  }
  const { definition, input } = authorizeTaskRequest(request);
  const layout = await ensureLayout(projectRoot);
  const invocationId = randomUUID();
  const started = now();
  const startedAt = started.toISOString();
  const inputHash = sha256(input);
  const policy = safeDefinition(definition);
  await writeAuditEvent(layout, invocationId, "started", {
    schemaVersion: "1.0",
    invocationId,
    phase: "started",
    task: definition.task,
    origin: definition.origin,
    startedAt,
    input: { sha256: inputHash, bytes: input.length },
    policy
  });

  let release = null;
  let occurrence = null;
  let result;
  let reasonCode = "task_completed";
  try {
    if (definition.task === "morning-report") {
      const workflow = await readBoundedJson(path.join(projectRoot, "config", "workflow.json"));
      // Match the existing fixed schedule contract in validateProjectConfig;
      // v2 readers validate this occurrence without consulting mutable config.
      if (workflow.timezone !== "Asia/Shanghai"
          || workflow.scheduledTime !== "08:00") {
        throw new Error("morning schedule is invalid");
      }
      const targetDate = dateInTimeZone(workflow.timezone, started);
      const identity = validateMorningOccurrenceIdentity({
        targetDate,
        frozenCutoffInstant: new Date(`${targetDate}T${workflow.scheduledTime}:00+08:00`).toISOString()
      });
      if (Date.parse(identity.frozenCutoffInstant) > started.getTime()) {
        throw new Error("morning occurrence has not reached its scheduled time");
      }
      occurrence = await claimMorningOccurrence(layout, identity, invocationId, startedAt);
      occurrence.identity = identity;
      if (!occurrence.claimed) {
        result = {
          exitCode: occurrence.disposition === "already_terminal" ? 0 : 75,
          stdout: Buffer.alloc(0),
          signal: null,
          timedOut: false,
          outputTooLarge: false,
          outcome: occurrence.disposition === "already_terminal" ? "noop" : "blocked",
          targetDate
        };
        reasonCode = occurrence.disposition;
      } else {
        result = await processRunner(definition, input, {
          projectRoot,
          occurrenceIdentity: identity
        });
        const status = await classifyMorningTerminal(projectRoot, targetDate, result);
        const completedAt = now().toISOString();
        await writeAtomicJson(occurrence.filePath, {
          schemaVersion: "2.0",
          task: definition.task,
          targetDate,
          status,
          invocationId,
          startedAt,
          frozenCutoffInstant: identity.frozenCutoffInstant,
          completedAt,
          childExitCode: result.exitCode,
          childSignal: result.signal ?? null,
          timedOut: result.timedOut === true,
          interrupted: result.interrupted === true
        });
        result = { ...result, outcome: status, targetDate };
        if (status === "unknown") reasonCode = "manual_review_after_timeout_or_signal";
        else if (status === "notification_pending") reasonCode = "manual_review_notification_unconfirmed";
        else if (status === "blocked") reasonCode = "publication_gate_blocked";
        else reasonCode = "task_completed";
      }
    } else {
      release = await acquireResearchLock(layout, invocationId, started);
      if (!release) {
        result = {
          exitCode: 75,
          stdout: Buffer.alloc(0),
          signal: null,
          timedOut: false,
          outputTooLarge: false,
          outcome: "busy"
        };
        reasonCode = "single_instance_busy";
      } else {
        result = await processRunner(definition, input, { projectRoot });
        if (result.exitCode === 0) {
          try {
            const parsed = JSON.parse(result.stdout.toString("utf8"));
            if (!isPlainObject(parsed)) throw new Error("invalid result");
            result.outcome = "success";
          } catch {
            result = { ...result, exitCode: 65, stdout: Buffer.alloc(0), outcome: "failed" };
            reasonCode = "invalid_research_output";
          }
        } else {
          result.outcome = result.timedOut || result.interrupted || result.signal ? "unknown" : "failed";
          if (result.outcome === "unknown") reasonCode = "manual_review_after_timeout_or_signal";
        }
      }
    }
  } catch {
    result = {
      exitCode: 70,
      stdout: Buffer.alloc(0),
      signal: null,
      timedOut: false,
      outputTooLarge: false,
      outcome: "failed"
    };
    reasonCode = "orchestrator_internal_failure";
    if (occurrence?.claimed) {
      await writeAtomicJson(occurrence.filePath, {
        schemaVersion: "2.0",
        task: definition.task,
        targetDate: dateInTimeZone("Asia/Shanghai", started),
        status: "unknown",
        invocationId,
        startedAt,
        frozenCutoffInstant: occurrence.identity.frozenCutoffInstant,
        completedAt: now().toISOString(),
        childExitCode: null
      }).catch(() => {});
    }
  } finally {
    if (release) await release();
  }

  const completed = now();
  const output = Buffer.isBuffer(result.stdout) ? result.stdout : Buffer.alloc(0);
  await writeAuditEvent(layout, invocationId, "terminal", {
    schemaVersion: "1.0",
    invocationId,
    phase: "terminal",
    task: definition.task,
    origin: definition.origin,
    completedAt: completed.toISOString(),
    durationMs: Math.max(0, completed.getTime() - started.getTime()),
    outcome: result.outcome,
    reasonCode,
    exitCode: result.exitCode,
    signal: result.signal,
    timedOut: result.timedOut === true,
    interrupted: result.interrupted === true,
    output: { sha256: sha256(output), bytes: output.length },
    policy
  });
  return result;
}

async function readLastRunSummary(projectRoot) {
  try {
    const value = await readBoundedJson(path.join(projectRoot, "work", "last-run.json"));
    if (
      typeof value.targetDate !== "string"
      || !/^\d{4}-\d{2}-\d{2}$/.test(value.targetDate)
      || typeof value.status !== "string"
      || value.status.length === 0
      || value.status.length > 64
    ) {
      return { availability: "invalid" };
    }
    return {
      availability: "available",
      targetDate: value.targetDate,
      status: value.status,
      ...(typeof value.startedAt === "string" ? { startedAt: value.startedAt } : {}),
      ...(typeof value.completedAt === "string" ? { completedAt: value.completedAt } : {})
    };
  } catch (error) {
    return { availability: error.code === "ENOENT" ? "missing" : "invalid" };
  }
}

export async function getOrchestratorStatus(projectRoot) {
  const root = path.join(projectRoot, "work", "orchestrator");
  let rootAvailable = true;
  try {
    const info = await lstat(root);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("invalid orchestrator root");
  } catch (error) {
    if (error.code === "ENOENT") {
      rootAvailable = false;
    } else {
      throw error;
    }
  }
  const eventsDir = path.join(root, "events");
  const statesDir = path.join(root, "states");
  const locksDir = path.join(root, "locks");
  const eventCount = rootAvailable
    ? await readdir(eventsDir).then((items) => items.filter((item) => item.endsWith(".json")).length).catch(() => 0)
    : 0;
  const morningOccurrences = [];
  for (const name of rootAvailable ? await readdir(statesDir).catch(() => []) : []) {
    if (!/^morning-report-\d{4}-\d{2}-\d{2}\.json$/.test(name)) continue;
    try {
      const state = await readBoundedJson(path.join(statesDir, name));
      morningOccurrences.push({ targetDate: state.targetDate, status: state.status });
    } catch {
      morningOccurrences.push({ targetDate: name.slice(15, 25), status: "invalid" });
    }
  }
  const lastRun = await readLastRunSummary(projectRoot);
  const divergences = [];
  for (const occurrence of morningOccurrences) {
    if (lastRun.availability !== "available" || lastRun.targetDate !== occurrence.targetDate) {
      occurrence.lastRunStatus = null;
      occurrence.stateConsistency = "not_comparable";
      continue;
    }
    occurrence.lastRunStatus = lastRun.status;
    occurrence.stateConsistency = occurrence.status === lastRun.status ? "consistent" : "diverged";
    if (occurrence.stateConsistency === "diverged") {
      divergences.push({
        targetDate: occurrence.targetDate,
        occurrenceStatus: occurrence.status,
        lastRunStatus: lastRun.status,
        action: "operator_review_required"
      });
    }
  }
  if (
    lastRun.availability === "available"
    && !morningOccurrences.some((occurrence) => occurrence.targetDate === lastRun.targetDate)
  ) {
    divergences.push({
      targetDate: lastRun.targetDate,
      occurrenceStatus: "missing",
      lastRunStatus: lastRun.status,
      action: "operator_review_required"
    });
  }
  return {
    schemaVersion: "1.0",
    eventCount,
    morningOccurrences: morningOccurrences.sort((a, b) => a.targetDate.localeCompare(b.targetDate)),
    lastRun,
    morningStateDivergence: divergences.length > 0,
    morningStateDivergences: divergences.sort((a, b) => a.targetDate.localeCompare(b.targetDate)),
    researchBusy: rootAvailable
      ? await lstat(path.join(locksDir, "research-context.lock")).then(() => true).catch(() => false)
      : false
  };
}

export { MAX_RESEARCH_INPUT_BYTES, MAX_RESEARCH_OUTPUT_BYTES };
