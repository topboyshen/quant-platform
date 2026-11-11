/**
 * Publish-branch gate (batch 1, 2026-09-26).
 *
 * The scheduled source unit exits 0 for both a published run and a validated
 * trading-calendar skip, and systemd OnSuccess= only sees that exit code. This
 * module closes the source-to-seal daily-report publish branch
 * (research seal, XHS draft, Telegram daily notify, and their success triggers)
 * for a validated non-trading day. It writes one marker file; the downstream
 * units carry ConditionPathExists=!<marker>, which systemd evaluates without a
 * state change, so no OnSuccess=/OnFailure= dependents start and nothing is
 * marked failed.
 * An independently scheduled relay may still emit a signed research_skip;
 * that status receipt is not a daily report or a Telegram daily notification.
 *
 * Fail-open by design: anything short of a fully validated skip (state file,
 * last-run record and hash-bound trading-calendar context all agreeing on the
 * same Shanghai date) leaves the branch open, i.e. exactly the previous
 * behaviour, where the seal's own gates still hold and alert. It never
 * converts a held/blocked/failed result into success, never sends, and never
 * writes anything except the marker.
 */
import { createHash } from "node:crypto";
import { lstat, open, readFile, rename, rm } from "node:fs/promises";
import path from "node:path";

export const PUBLISH_BRANCH_MARKER = "publish-branch.closed";
export const CALENDAR_SOURCE_ID = "akshare:sina:trade-calendar";
const MAX_JSON_BYTES = 4 * 1024 * 1024;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function shanghaiDate(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"
  }).formatToParts(now);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

export function markerPath(projectRoot) {
  return path.join(projectRoot, "work", "orchestrator", PUBLISH_BRANCH_MARKER);
}

async function readRegularBytes(filePath) {
  const info = await lstat(filePath);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error("not_regular_file");
  if (info.size > MAX_JSON_BYTES) throw new Error("file_too_large");
  return readFile(filePath);
}

async function readJson(filePath) {
  const bytes = await readRegularBytes(filePath);
  return { bytes, value: JSON.parse(bytes.toString("utf8")) };
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

/**
 * Pure-ish evaluation: reads only. Returns {decision, reason, targetDate, ...}.
 */
export async function evaluatePublishBranch({ projectRoot, now = new Date(), targetDate = null } = {}) {
  if (typeof projectRoot !== "string" || !path.isAbsolute(projectRoot)) {
    throw new Error("projectRoot must be an absolute path");
  }
  const date = targetDate ?? shanghaiDate(now);
  const open_ = (reason) => ({ decision: "open", reason, targetDate: date });
  if (!DATE_RE.test(date)) return open_("target_date_invalid");
  const work = path.join(projectRoot, "work");

  let state;
  try {
    state = (await readJson(path.join(work, "orchestrator", "states", `morning-report-${date}.json`))).value;
  } catch {
    return open_("orchestrator_state_unavailable");
  }
  if (!isObject(state) || state.task !== "morning-report" || state.targetDate !== date) {
    return open_("orchestrator_state_identity_mismatch");
  }
  if (state.status !== "skipped") return open_(`orchestrator_status_${String(state.status).slice(0, 32)}`);
  if (state.childExitCode !== 0 || state.timedOut === true || state.interrupted === true) {
    return open_("orchestrator_skip_not_clean");
  }

  let lastRun;
  try {
    lastRun = (await readJson(path.join(work, "last-run.json"))).value;
  } catch {
    return open_("last_run_unavailable");
  }
  if (!isObject(lastRun) || lastRun.targetDate !== date) return open_("last_run_date_mismatch");
  if (lastRun.status !== "skipped" || lastRun.phase !== "collect"
      || lastRun.tradingStatus !== "non_trading_day" || lastRun.publicationGate !== "not_applicable") {
    return open_("last_run_not_calendar_skip");
  }

  // The skip must be bound to the frozen context that carried the calendar.
  const contextFile = lastRun.contextFile;
  if (typeof contextFile !== "string" || !/^work\/[A-Za-z0-9._/-]+\.json$/.test(contextFile)
      || contextFile.includes("..")) {
    return open_("context_file_invalid");
  }
  let context;
  try {
    const read = await readJson(path.join(projectRoot, contextFile));
    const digest = createHash("sha256").update(read.bytes).digest("hex");
    if (digest !== lastRun.contextSha256) return open_("context_hash_mismatch");
    context = read.value;
  } catch {
    return open_("context_unavailable");
  }
  const calendar = context?.data?.tradingCalendar;
  if (!isObject(calendar) || calendar.targetDate !== date || calendar.status !== "non_trading_day") {
    return open_("calendar_not_non_trading_day");
  }
  if (!Array.isArray(calendar.sourceIds) || !calendar.sourceIds.includes(CALENDAR_SOURCE_ID)) {
    return open_("calendar_source_unrecognized");
  }
  const previous = DATE_RE.test(calendar.previousTradingDate ?? "") ? calendar.previousTradingDate : null;
  const next = DATE_RE.test(calendar.nextTradingDate ?? "") ? calendar.nextTradingDate : null;
  if (previous !== null && !(previous < date)) return open_("calendar_previous_date_invalid");
  if (next !== null && !(next > date)) return open_("calendar_next_date_invalid");

  return {
    decision: "closed",
    reason: "validated_non_trading_day",
    targetDate: date,
    invocationId: typeof state.invocationId === "string" ? state.invocationId.slice(0, 64) : null,
    contextSha256: lastRun.contextSha256,
    calendarSourceId: CALENDAR_SOURCE_ID,
    previousTradingDate: previous,
    nextTradingDate: next
  };
}

async function writeMarker(filePath, record) {
  const directory = path.dirname(filePath);
  const dirInfo = await lstat(directory);
  if (!dirInfo.isDirectory() || dirInfo.isSymbolicLink()) throw new Error("marker_directory_invalid");
  try {
    const existing = await lstat(filePath);
    if (!existing.isFile() || existing.isSymbolicLink()) throw new Error("marker_path_not_regular_file");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const temporary = path.join(directory, `.${PUBLISH_BRANCH_MARKER}.${process.pid}.tmp`);
  const handle = await open(temporary, "wx", 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(record)}\n`, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await rename(temporary, filePath);
  } catch (error) {
    await rm(temporary, { force: true });
    throw error;
  }
}

/**
 * Decide and (unless dryRun) write the marker when the branch must close.
 * Never removes a marker here: clearing is done by the wrapper before every
 * run, so this function cannot reopen a branch another run closed.
 */
export async function decidePublishBranch({ projectRoot, now = new Date(), dryRun = false, targetDate = null } = {}) {
  const result = await evaluatePublishBranch({ projectRoot, now, targetDate });
  if (result.decision === "closed" && !dryRun) {
    await writeMarker(markerPath(projectRoot), {
      schemaVersion: "1.0",
      ...result,
      decidedAt: now.toISOString()
    });
    return { ...result, markerWritten: true };
  }
  return { ...result, markerWritten: false, dryRun };
}
