// Shared research-artifact integrity and immutable storage primitives.
// No XHS transformation, notification transport, or import-time I/O.
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { link, lstat, mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import path from "node:path";

const LAST_RUN_RELATIVE = "work/last-run.json";
const MAX_STATE_BYTES = 64 * 1024;
const CLOCK_SKEW_MS = 5_000;
const SHA256 = /^[0-9a-f]{64}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const UTC_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/;
const LOCAL_CUTOFF = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/;
const ALLOWED_DATA_STATUSES = new Set(["live", "partial"]);
const ALLOWED_SEMANTIC_REVIEWS = new Set(["not_human_reviewed", "human_passed"]);
const stateWriteSequence = { value: 0 };

export class XhsDraftIntegrityError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "XhsDraftIntegrityError";
    this.code = code;
  }
}

function integrity(code, message) {
  throw new XhsDraftIntegrityError(code, message);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}

function validateIsoDate(value, fieldName = "targetDate") {
  if (typeof value !== "string" || !ISO_DATE.test(value)) {
    integrity("INVALID_STATE", `${fieldName} 必须是 YYYY-MM-DD`);
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    integrity("INVALID_STATE", `${fieldName} 不是有效日期`);
  }
  return value;
}

function parseUtcInstant(value, fieldName) {
  if (typeof value !== "string" || !UTC_INSTANT.test(value)) {
    integrity("INVALID_STATE", `${fieldName} 必须是 UTC ISO-8601 时间`);
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    integrity("INVALID_STATE", `${fieldName} 不是有效时间`);
  }
  return parsed;
}

function dateInShanghai(now) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(now);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function dateTimeInShanghai(now) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23"
  }).formatToParts(now);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
}

function sameFileIdentity(left, right) {
  return ["dev", "ino", "mode", "nlink", "size", "mtimeNs", "ctimeNs"]
    .every((key) => left[key] === right[key]);
}

async function readBoundedRegularFile(filePath, maxBytes, label) {
  let before;
  try {
    before = await lstat(filePath, { bigint: true });
  } catch (error) {
    error.fileLabel = label;
    throw error;
  }
  if (before.isSymbolicLink() || !before.isFile()) {
    integrity("UNSAFE_INPUT_FILE", `${label} 必须是非符号链接普通文件`);
  }
  if (before.size <= 0n || before.size > BigInt(maxBytes)) {
    integrity("INPUT_SIZE_INVALID", `${label} 大小必须在 1—${maxBytes} 字节内`);
  }

  const noFollow = Number.isInteger(constants.O_NOFOLLOW) ? constants.O_NOFOLLOW : 0;
  let handle;
  try {
    handle = await open(filePath, constants.O_RDONLY | noFollow);
  } catch (error) {
    if (error?.code === "ELOOP") {
      integrity("UNSAFE_INPUT_FILE", `${label} 不得是符号链接`);
    }
    throw error;
  }
  try {
    const opened = await handle.stat({ bigint: true });
    if (!opened.isFile() || !sameFileIdentity(before, opened)) {
      integrity("INPUT_CHANGED_DURING_READ", `${label} 在打开时发生变化`);
    }
    const expectedBytes = Number(opened.size);
    const buffer = Buffer.allocUnsafe(expectedBytes);
    let offset = 0;
    while (offset < expectedBytes) {
      const { bytesRead } = await handle.read(buffer, offset, expectedBytes - offset, offset);
      if (bytesRead === 0) break;
      offset += bytesRead;
    }
    if (offset !== expectedBytes) {
      integrity("INPUT_CHANGED_DURING_READ", `${label} 读取长度发生变化`);
    }
    const after = await lstat(filePath, { bigint: true });
    if (after.isSymbolicLink() || !after.isFile() || !sameFileIdentity(opened, after)) {
      integrity("INPUT_CHANGED_DURING_READ", `${label} 在读取期间发生变化`);
    }
    return { buffer, sha256: hash(buffer) };
  } finally {
    await handle.close();
  }
}

function decodeUtf8(buffer, label) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buffer);
  } catch {
    integrity("INVALID_UTF8", `${label} 不是有效 UTF-8`);
  }
}

function parseJsonBuffer(buffer, label) {
  let value;
  try {
    value = JSON.parse(decodeUtf8(buffer, label));
  } catch (error) {
    if (error instanceof XhsDraftIntegrityError) throw error;
    integrity("INVALID_JSON", `${label} 不是有效 JSON`);
  }
  if (!isPlainObject(value)) {
    integrity("INVALID_JSON", `${label} 必须是 JSON 对象`);
  }
  return value;
}

async function loadJsonConfig(projectRoot, relativePath, label) {
  const raw = await readFile(path.join(projectRoot, ...relativePath.split("/")));
  return parseJsonBuffer(raw, label);
}

function stateEligibility(lastRun, {
  desiredDate,
  requireCurrentDate,
  maxAgeMinutes,
  now
}) {
  const reasons = [];
  if (lastRun.status !== "success") reasons.push(`RUN_STATUS_${String(lastRun.status ?? "MISSING").toUpperCase()}`);
  if (lastRun.phase !== "publish") reasons.push("RUN_PHASE_NOT_PUBLISH");
  if (lastRun.publicationGate !== "passed") reasons.push("PUBLICATION_GATE_NOT_PASSED");
  if (lastRun.evidenceReview !== "passed") reasons.push("EVIDENCE_REVIEW_NOT_PASSED");
  if (!ALLOWED_DATA_STATUSES.has(lastRun.dataStatus)) reasons.push("DATA_STATUS_NOT_ELIGIBLE");
  if (Array.isArray(lastRun.publicationBlockers) && lastRun.publicationBlockers.length > 0) {
    reasons.push("PUBLICATION_BLOCKERS_PRESENT");
  }
  if (desiredDate && lastRun.targetDate !== desiredDate) reasons.push("TARGET_DATE_MISMATCH");
  if (requireCurrentDate && lastRun.targetDate !== dateInShanghai(now)) reasons.push("NOT_CURRENT_SHANGHAI_DATE");

  if (lastRun.completedAt !== undefined) {
    const completed = parseUtcInstant(lastRun.completedAt, "completedAt");
    if (completed.getTime() > now.getTime() + CLOCK_SKEW_MS) reasons.push("COMPLETED_AT_IN_FUTURE");
    if (maxAgeMinutes !== null && now.getTime() - completed.getTime() > maxAgeMinutes * 60_000) {
      reasons.push("REPORT_TOO_OLD");
    }
  } else if (lastRun.status === "success") {
    reasons.push("COMPLETED_AT_MISSING");
  }
  return [...new Set(reasons)];
}

function validateSuccessfulState(lastRun) {
  if (lastRun.schemaVersion !== "1.0") integrity("INVALID_STATE", "last-run schemaVersion 必须是 1.0");
  validateIsoDate(lastRun.targetDate);
  if (!UTC_INSTANT.test(String(lastRun.frozenCutoffInstant ?? ""))) {
    integrity("INVALID_STATE", "last-run 缺少 frozenCutoffInstant");
  }
  const frozenCutoff = parseUtcInstant(lastRun.frozenCutoffInstant, "frozenCutoffInstant");
  if (typeof lastRun.cutoffLocal !== "string" || !LOCAL_CUTOFF.test(lastRun.cutoffLocal)) {
    integrity("INVALID_STATE", "last-run cutoffLocal 格式无效");
  }
  if (!lastRun.cutoffLocal.startsWith(`${lastRun.targetDate} `)) {
    integrity("INVALID_STATE", "last-run cutoffLocal 与 targetDate 不一致");
  }
  if (dateInShanghai(frozenCutoff) !== lastRun.targetDate) {
    integrity("INVALID_STATE", "frozenCutoffInstant 与 targetDate 不一致");
  }
  if (dateTimeInShanghai(frozenCutoff) !== lastRun.cutoffLocal) {
    integrity("INVALID_STATE", "frozenCutoffInstant 与 cutoffLocal 不一致");
  }
  const completedAt = parseUtcInstant(lastRun.completedAt, "completedAt");
  if (completedAt.getTime() < frozenCutoff.getTime()) {
    integrity("INVALID_STATE", "completedAt 不得早于 frozenCutoffInstant");
  }
  if (!SHA256.test(String(lastRun.contextSha256 ?? ""))) {
    integrity("INVALID_STATE", "last-run contextSha256 缺失或无效");
  }
  if (!ALLOWED_SEMANTIC_REVIEWS.has(lastRun.researchSemanticReview)) {
    integrity("INVALID_STATE", "last-run researchSemanticReview 无效");
  }
  if (lastRun.semanticCandidateCount !== undefined &&
      (!Number.isInteger(lastRun.semanticCandidateCount) || lastRun.semanticCandidateCount < 0)) {
    integrity("INVALID_STATE", "last-run semanticCandidateCount 无效");
  }
  if (lastRun.publicationBlockers !== undefined && !Array.isArray(lastRun.publicationBlockers)) {
    integrity("INVALID_STATE", "last-run publicationBlockers 必须是数组");
  }
}

function expectedReportRelative(targetDate) {
  return `reports/daily/${targetDate}.md`;
}

function validateReportPath(reportFile, targetDate) {
  if (typeof reportFile !== "string") return { eligible: false, reason: "FORMAL_REPORT_PATH_MISSING" };
  if (reportFile.includes("\0") || reportFile.includes("\\") || path.isAbsolute(reportFile) ||
      reportFile.split("/").includes("..") || reportFile.split("/").includes(".")) {
    integrity("UNSAFE_REPORT_PATH", "last-run reportFile 包含不安全路径");
  }
  if (reportFile !== expectedReportRelative(targetDate)) {
    return { eligible: false, reason: "FORMAL_REPORT_PATH_REQUIRED" };
  }
  return { eligible: true };
}

function escapePattern(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function validateAndParseReport(report, lastRun, disclaimer) {
  if (/\u0000/u.test(report)) integrity("INVALID_REPORT", "正式报告包含 NUL 字符");
  const titlePattern = new RegExp(`^# [^\\n｜]+｜${escapePattern(lastRun.targetDate)}\\s*$`, "m");
  if (!titlePattern.test(report)) integrity("REPORT_CONTRACT_MISMATCH", "正式报告标题日期与 last-run 不一致");
  const cutoffLine = `> 数据截止：${lastRun.cutoffLocal}（Asia/Shanghai）`;
  if (!report.split(/\r?\n/).includes(cutoffLine)) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告数据截止与 last-run 不一致");
  }
  const expectedDataLabel = lastRun.dataStatus === "live" ? "实时数据包" : "部分可用";
  if (!report.split(/\r?\n/).includes(`> 数据状态：${expectedDataLabel}`)) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告数据状态与 last-run 不一致");
  }
  const requiredHeadings = [
    "## 一句话结论",
    "## 1. 数据覆盖与可用性",
    "## 2. 上一交易日 A 股行情",
    "## 4. 板块与股票池",
    "## 7. 风险提示与数据缺口",
    "## 来源"
  ];
  for (const heading of requiredHeadings) {
    if (heading === "## 2. 上一交易日 A 股行情") {
      if (!/^## 2\. 上一交易日 A 股行情（\d{4}-\d{2}-\d{2}）\s*$/m.test(report)) {
        integrity("REPORT_CONTRACT_MISMATCH", `正式报告缺少章节: ${heading}`);
      }
    } else if (!report.split(/\r?\n/).includes(heading)) {
      integrity("REPORT_CONTRACT_MISMATCH", `正式报告缺少章节: ${heading}`);
    }
  }
  if (!report.trimEnd().endsWith(disclaimer)) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告末尾免责声明与风险配置不一致");
  }

  const sourceStart = report.lastIndexOf("\n## 来源");
  const sourceEnd = report.indexOf("\n---", sourceStart);
  if (sourceStart < 0 || sourceEnd < 0) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告来源区结构无效");
  }
  const sourceSection = report.slice(sourceStart, sourceEnd);
  const sources = [];
  let numberedSourceLines = 0;
  for (const line of sourceSection.split(/\r?\n/)) {
    if (/^[1-9]\d*\.\s/u.test(line)) numberedSourceLines += 1;
    const match = line.match(/^([1-9]\d*)\.\s+(?:\[陈旧\]\s*)?(.+?)\s+—\s+.+?；(?:披露日期：.+?；)?(?:发布时间|数据时间|实际采集时间|实际查询完成时间)：(.+?)；(https?:\/\/\S+)\s*$/u);
    if (!match) continue;
    let parsedUrl;
    try {
      parsedUrl = new URL(match[4]);
    } catch {
      integrity("REPORT_CONTRACT_MISMATCH", "正式报告来源 URL 无效");
    }
    if (!new Set(["http:", "https:"]).has(parsedUrl.protocol) || parsedUrl.username || parsedUrl.password) {
      integrity("REPORT_CONTRACT_MISMATCH", "正式报告来源 URL 不安全");
    }
    sources.push({
      index: Number(match[1]),
      title: match[2].trim(),
      dataTime: match[3].trim(),
      url: match[4]
    });
  }
  if (sources.length === 0) integrity("REPORT_CONTRACT_MISMATCH", "正式报告没有可解析来源");
  if (sources.length !== numberedSourceLines) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告含无法解析的编号来源");
  }
  const sourceIndexes = new Set(sources.map((source) => source.index));
  if (sourceIndexes.size !== sources.length) integrity("REPORT_CONTRACT_MISMATCH", "正式报告来源编号重复");
  if (sources.some((source, index) => source.index !== index + 1)) {
    integrity("REPORT_CONTRACT_MISMATCH", "正式报告来源编号必须从 1 连续递增");
  }
  const bodyBeforeSources = report.slice(0, sourceStart);
  for (const marker of bodyBeforeSources.matchAll(/\[来源\s+([1-9]\d*(?:、[1-9]\d*)*)\]/gu)) {
    for (const value of marker[1].split("、")) {
      if (!sourceIndexes.has(Number(value))) {
        integrity("REPORT_CONTRACT_MISMATCH", `正式报告引用了不存在的来源 ${value}`);
      }
    }
  }
  return { sources };
}

async function ensureDirectory(directoryPath, mode = 0o700) {
  try {
    await mkdir(directoryPath, { mode });
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  }
  const stats = await lstat(directoryPath);
  if (stats.isSymbolicLink() || !stats.isDirectory()) {
    integrity("UNSAFE_OUTPUT_DIRECTORY", "XHS 输出路径必须是非符号链接目录");
  }
}

async function writeAtomicReplace(filePath, content) {
  const temporaryPath = `${filePath}.${process.pid}.${++stateWriteSequence.value}.tmp`;
  try {
    try {
      const existing = await lstat(filePath);
      if (existing.isSymbolicLink() || !existing.isFile()) {
        integrity("UNSAFE_OUTPUT_FILE", "XHS 状态路径必须是普通文件");
      }
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    const handle = await open(temporaryPath, "wx", 0o600);
    try {
      await handle.writeFile(content, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporaryPath, filePath);
  } finally {
    await unlink(temporaryPath).catch(() => {});
  }
}

async function immutableFileState(filePath, expected) {
  try {
    const actual = await readBoundedRegularFile(filePath, expected.length + 1, "既有 XHS 草稿");
    if (!actual.buffer.equals(expected)) {
      integrity("IMMUTABLE_OUTPUT_CONFLICT", "既有 XHS 草稿与相同内容地址冲突");
    }
    return "existing";
  } catch (error) {
    if (error?.code === "ENOENT") return "missing";
    throw error;
  }
}

async function publishImmutable(filePath, expected) {
  if (await immutableFileState(filePath, expected) === "existing") return false;
  const temporaryPath = `${filePath}.${process.pid}.${++stateWriteSequence.value}.tmp`;
  try {
    const handle = await open(temporaryPath, "wx", 0o600);
    try {
      await handle.writeFile(expected);
      await handle.sync();
    } finally {
      await handle.close();
    }
    try {
      await link(temporaryPath, filePath);
      return true;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      await immutableFileState(filePath, expected);
      return false;
    }
  } finally {
    await unlink(temporaryPath).catch(() => {});
  }
}


export { integrity, isPlainObject, hash, validateIsoDate, parseUtcInstant, dateInShanghai, dateTimeInShanghai, sameFileIdentity, readBoundedRegularFile, decodeUtf8, parseJsonBuffer, loadJsonConfig, stateEligibility, validateSuccessfulState, expectedReportRelative, validateReportPath, escapePattern, validateAndParseReport, ensureDirectory, writeAtomicReplace, immutableFileState, publishImmutable, LAST_RUN_RELATIVE, MAX_STATE_BYTES, SHA256 };
