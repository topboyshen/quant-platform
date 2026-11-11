// Hermes transport only. No research, state-store access, or import-time subprocess.
import { spawn } from "node:child_process";

const HERMES_PYTHON = "/home/ubuntu/.hermes/hermes-agent/venv/bin/python";
const HERMES_MODULE = "hermes_cli.main";
const MAX_HERMES_STDOUT_BYTES = 64 * 1024;
const DELIVERY_TIMEOUT_MS = 90 * 1000;
const TERMINATION_GRACE_MS = 5 * 1000;
const KILL_GRACE_MS = 2 * 1000;

// Keep the real spawn binding so injected/mocked spawn implementations never
// cause a negative-pid signal to be sent to the caller's process group.
const realSpawn = spawn;


const RECEIPT_REJECT_REASONS = new Set([
  "exit_not_zero",
  "stdout_missing",
  "stdout_too_large",
  "stdout_not_json",
  "receipt_not_object",
  "success_not_true",
  "error_present",
  "missing_platform",
  "platform_not_telegram",
  "message_id_not_string",
  "message_id_invalid",
  "unexpected_fields",
  "skipped",
  "warnings_present",
  "spawn_failed",
  "transport_error",
  "timed_out",
  "sender_threw_unconfirmed",
  "sender_threw_unproven",
  "unproven_after_invoke",
  "receipt_persist_failed",
  "stale_sending_no_spawn_confirmation"
]);
const ALLOWED_RECEIPT_KEYS = new Set([
  "success", "platform", "chat_id", "message_id", "note", "mirrored", "warnings", "skipped"
]);

function confirmsHermesDelivery(stdout, exitCode) {
  return parseHermesAck(stdout, exitCode) !== null;
}



/* 发送时本来就要校验这些字段，现在把它们取出来交给上层持久化。 */
function classifyHermesAck(stdout, exitCode) {
  if (exitCode !== 0) return { ack: null, receiptRejectReason: "exit_not_zero" };
  if (!(typeof stdout === "string" || Buffer.isBuffer(stdout))) {
    return { ack: null, receiptRejectReason: "stdout_missing" };
  }
  if (Buffer.byteLength(stdout) > MAX_HERMES_STDOUT_BYTES) {
    return { ack: null, receiptRejectReason: "stdout_too_large" };
  }
  let receipt;
  try {
    const decoded = Buffer.isBuffer(stdout)
      ? new TextDecoder("utf-8", { fatal: true }).decode(stdout)
      : stdout;
    receipt = JSON.parse(decoded);
  } catch {
    return { ack: null, receiptRejectReason: "stdout_not_json" };
  }
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
    return { ack: null, receiptRejectReason: "receipt_not_object" };
  }
  if (receipt.success !== true) {
    return {
      ack: null,
      receiptRejectReason: Object.hasOwn(receipt, "error") ? "error_present" : "success_not_true"
    };
  }
  if (!Object.hasOwn(receipt, "platform")) return { ack: null, receiptRejectReason: "missing_platform" };
  if (receipt.platform !== "telegram") return { ack: null, receiptRejectReason: "platform_not_telegram" };
  if (typeof receipt.message_id !== "string") return { ack: null, receiptRejectReason: "message_id_not_string" };
  if (receipt.message_id.trim() !== receipt.message_id || !/^[1-9][0-9]*$/.test(receipt.message_id)) {
    return { ack: null, receiptRejectReason: "message_id_invalid" };
  }
  if (Object.keys(receipt).some((key) => !ALLOWED_RECEIPT_KEYS.has(key))) {
    return { ack: null, receiptRejectReason: "unexpected_fields" };
  }
  if (Object.hasOwn(receipt, "skipped") && receipt.skipped !== false) {
    return { ack: null, receiptRejectReason: "skipped" };
  }
  if (Object.hasOwn(receipt, "error")) return { ack: null, receiptRejectReason: "error_present" };
  if (Object.hasOwn(receipt, "warnings") && !(Array.isArray(receipt.warnings) && receipt.warnings.length === 0)) {
    return { ack: null, receiptRejectReason: "warnings_present" };
  }
  return { ack: { messageId: receipt.message_id }, receiptRejectReason: null };
}

function parseHermesAck(stdout, exitCode) {
  return classifyHermesAck(stdout, exitCode).ack;
}

function transportDiagnostic(eventId, fields) {
  if (typeof eventId !== "string" || !/^astock-\d{4}-\d{2}-\d{2}-(?:success|failed|skipped)-telegram-v1$/.test(eventId)) {
    throw new Error("diagnostic eventId invalid");
  }
  const reason = fields?.receiptRejectReason;
  if (typeof reason !== "string" || !RECEIPT_REJECT_REASONS.has(reason)) {
    throw new Error("diagnostic reason invalid");
  }
  const started = fields?.started === true;
  const exitCode = fields?.exitCode === null || fields?.exitCode === undefined ? null : fields.exitCode;
  if (exitCode !== null && !Number.isInteger(exitCode)) throw new Error("diagnostic exitCode invalid");
  for (const key of ["stdoutBytes", "stderrBytes"]) {
    if (!Number.isSafeInteger(fields?.[key]) || fields[key] < 0) throw new Error("diagnostic byte count invalid");
  }
  return {
    schemaVersion: "1.0",
    eventId,
    started,
    exitCode,
    timedOut: fields?.timedOut === true,
    stdoutBytes: fields.stdoutBytes,
    stderrBytes: fields.stderrBytes,
    receiptRejectReason: reason
  };
}



function sendViaHermes(delivery, {
  timeoutMs = DELIVERY_TIMEOUT_MS,
  terminationGraceMs = TERMINATION_GRACE_MS,
  killGraceMs = KILL_GRACE_MS,
  spawnImpl = spawn,
  killImpl = (...args) => process.kill(...args),
  useProcessGroup = spawnImpl === realSpawn && process.platform !== "win32"
} = {}) {
  return new Promise((resolve) => {
    const child = spawnImpl(
      HERMES_PYTHON,
      ["-B", "-m", HERMES_MODULE, "send", "--to", "telegram", "--file", "-", "--json"],
      {
        cwd: "/home/ubuntu/.hermes",
        env: {
          HOME: "/home/ubuntu",
          HERMES_HOME: "/home/ubuntu/.hermes",
          VIRTUAL_ENV: "/home/ubuntu/.hermes/hermes-agent/venv",
          PATH: "/home/ubuntu/.hermes/hermes-agent/venv/bin:/usr/local/bin:/usr/bin:/bin",
          LANG: "C.UTF-8",
          LC_ALL: "C.UTF-8",
          TZ: "Asia/Shanghai",
          TELEGRAM_PROXY: "socks5h://127.0.0.1:7891"
        },
        shell: false,
        detached: process.platform !== "win32",
        stdio: ["pipe", "pipe", "pipe"]
      }
    );
    let started = false;
    let settled = false;
    let stderrBytes = 0;
    let stdoutBytes = 0;
    let stdoutChunks = [];
    let timer;
    let overflowed = false;
    let closeObserved = false;
    let closeCode = null;
    let terminationStarted = false;
    let terminationReason = null;
    let terminationState = "none";
    let terminationTimers = [];
    const settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      for (const handle of terminationTimers) clearTimeout(handle);
      terminationTimers = [];
      stdoutChunks = [];
      resolve(result);
    };
    const signalProcess = (signalName) => {
      if (useProcessGroup && Number.isInteger(child.pid) && child.pid > 0) {
        try {
          killImpl(-child.pid, signalName);
          return true;
        } catch {
          // Fall back to the direct child when the group signal is refused or
          // the group has already disappeared.
        }
      }
      try {
        return child.kill(signalName) !== false;
      } catch {
        return false;
      }
    };
    const settleAfterClose = (code) => {
      if (terminationReason === "stdout_too_large") {
        settle({
          ok: false,
          started,
          exitCode: null,
          timedOut: false,
          processExited: true,
          termination: terminationState,
          stdoutBytes,
          stderrBytes,
          receiptRejectReason: "stdout_too_large"
        });
        return;
      }
      if (terminationReason === "timed_out") {
        const classified = classifyHermesAck(Buffer.concat(stdoutChunks), code);
        // A late but valid acknowledgement still proves platform acceptance;
        // it is not a reason to resend.
        if (code === 0 && classified.ack !== null) {
          settle({
            ok: true,
            ack: classified.ack,
            started,
            exitCode: 0,
            timedOut: false,
            processExited: true,
            termination: terminationState,
            stdoutBytes,
            stderrBytes,
            receiptRejectReason: null
          });
          return;
        }
        settle({
          ok: false,
          started,
          exitCode: Number.isInteger(code) ? code : null,
          timedOut: true,
          processExited: true,
          termination: terminationState,
          stdoutBytes,
          stderrBytes,
          receiptRejectReason: "timed_out"
        });
        return;
      }
      // Defensive: an unexpected termination reason must not be reported as a
      // confirmed send or as a proven process exit.
      settle({
        ok: false,
        started,
        exitCode: Number.isInteger(code) ? code : null,
        timedOut: false,
        processExited: true,
        termination: terminationState,
        stdoutBytes,
        stderrBytes,
        receiptRejectReason: terminationReason === "stdout_too_large" ? "stdout_too_large" : "transport_error"
      });
    };
    const startTermination = (reason, timedOut) => {
      if (terminationStarted || settled) return;
      terminationStarted = true;
      terminationReason = reason;
      clearTimeout(timer);
      terminationState = "sigterm";
      if (!signalProcess("SIGTERM")) terminationState = "kill_failed";
      if (closeObserved) {
        settleAfterClose(closeCode);
        return;
      }
      terminationTimers.push(setTimeout(() => {
        if (closeObserved || settled) return;
        terminationState = "sigkill";
        if (!signalProcess("SIGKILL")) terminationState = "kill_failed";
        if (closeObserved) {
          settleAfterClose(closeCode);
          return;
        }
        terminationTimers.push(setTimeout(() => {
          if (closeObserved || settled) return;
          settle({
            ok: false,
            started,
            exitCode: null,
            timedOut,
            processExited: false,
            termination: "unconfirmed",
            stdoutBytes,
            stderrBytes,
            receiptRejectReason: reason
          });
        }, killGraceMs));
      }, terminationGraceMs));
    };
    child.once("spawn", () => { started = true; });
    child.stderr.on("data", (chunk) => { stderrBytes += chunk.length; });
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      stdoutBytes += bytes.length;
      if (overflowed) return;
      if (stdoutBytes > MAX_HERMES_STDOUT_BYTES) {
        overflowed = true;
        stdoutChunks = [];
        startTermination("stdout_too_large", false);
        return;
      }
      stdoutChunks.push(bytes);
    });
    child.once("error", () => {
      if (terminationStarted) return;
      // A post-spawn error does not prove the process exited; keep the queue
      // from starting another send until the unknown state is resolved.
      settle({
        ok: false,
        started,
        exitCode: null,
        timedOut: false,
        processExited: started ? false : true,
        termination: started ? "unconfirmed" : "none",
        stdoutBytes,
        stderrBytes,
        receiptRejectReason: started ? "transport_error" : "spawn_failed"
      });
    });
    child.once("close", (code) => {
      closeObserved = true;
      closeCode = code;
      if (terminationStarted) {
        settleAfterClose(code);
        return;
      }
      const classified = classifyHermesAck(Buffer.concat(stdoutChunks), code);
      settle({
        ok: started && classified.ack !== null,
        ack: classified.ack,
        started,
        exitCode: code,
        timedOut: false,
        processExited: true,
        termination: "none",
        stdoutBytes,
        stderrBytes,
        receiptRejectReason: classified.ack === null ? classified.receiptRejectReason : null
      });
    });
    timer = setTimeout(() => {
      startTermination("timed_out", true);
    }, timeoutMs);
    child.stdin.on("error", () => {});
    child.stdin.end(delivery.message);
  });
}

export { confirmsHermesDelivery, classifyHermesAck, parseHermesAck, transportDiagnostic, sendViaHermes, RECEIPT_REJECT_REASONS };
