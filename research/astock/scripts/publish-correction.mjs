#!/usr/bin/env node
import { createHash, randomBytes } from "node:crypto";
import {
  lstat,
  link,
  mkdir,
  open,
  readFile,
  realpath,
  unlink
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  dateTimeInTimeZone,
  loadProjectConfig,
  validateIsoDate
} from "../src/config.mjs";
import { validateMorningContext } from "../src/data.mjs";
import {
  assessPublicationGate,
  DATA_BRIEF_PUBLICATION_LEVEL,
  LEGACY_FORMAL_PUBLICATION_LEVEL,
  normalizeReportOutput,
  requiresStaleMarketLabel
} from "../src/report.mjs";
import { isMainEntry } from "../src/main-entry.mjs";

const modulePath = fileURLToPath(import.meta.url);
const defaultProjectRoot = path.resolve(path.dirname(modulePath), "..");
const MAX_INPUT_BYTES = 1024 * 1024;
const MAX_GATE_BYTES = 256 * 1024;
const ARGUMENTS = new Set(["--date", "--revision", "--input", "--context", "--cutoff"]);

function publicationLevelFromReport(report) {
  const match = String(report).match(/^>\s*发布级别[：:]\s*([^\n]+)$/m);
  const stated = match?.[1]?.trim().replace(/\s+/g, "");
  for (const candidate of [
    DATA_BRIEF_PUBLICATION_LEVEL,
    LEGACY_FORMAL_PUBLICATION_LEVEL
  ]) {
    if (stated === candidate.replace(/\s+/g, "")) return candidate;
  }
  throw new Error("更正版未使用受支持的发布级别");
}

function usage() {
  return "用法：node scripts/publish-correction.mjs --date YYYY-MM-DD --revision vN --input FILE --context FILE --cutoff ISO";
}

function parseCutoff(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new Error(`--cutoff 必须是带时区的 ISO 时间: ${value}`);
  }
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) {
    throw new Error(`--cutoff 不是有效时间: ${value}`);
  }
  return instant;
}

export function parseArgs(argv) {
  if (argv.length !== ARGUMENTS.size * 2) throw new Error(usage());
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!ARGUMENTS.has(name)) throw new Error(`未知参数: ${name}`);
    if (Object.hasOwn(values, name)) throw new Error(`参数不得重复: ${name}`);
    if (!value || value.startsWith("--")) throw new Error(`${name} 缺少参数`);
    values[name] = value;
  }
  for (const name of ARGUMENTS) {
    if (!Object.hasOwn(values, name)) throw new Error(`缺少参数: ${name}`);
  }
  const targetDate = validateIsoDate(values["--date"]);
  const revision = values["--revision"];
  if (!/^v[1-9]\d{0,5}$/.test(revision)) {
    throw new Error(`--revision 必须是 vN（N 为正整数）: ${revision}`);
  }
  return {
    targetDate,
    revision,
    input: values["--input"],
    context: values["--context"],
    cutoffInstant: parseCutoff(values["--cutoff"])
  };
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function samePath(left, right) {
  const normalize = (value) => process.platform === "win32"
    ? path.normalize(value).toLowerCase()
    : path.normalize(value);
  return normalize(left) === normalize(right);
}

async function readSafeInput(rawPath, {
  label,
  projectRoot,
  runtimeRoot,
  maxBytes = MAX_INPUT_BYTES
}) {
  if (typeof rawPath !== "string" || rawPath.trim() === "" || rawPath.includes("\0")) {
    throw new Error(`${label} 路径无效`);
  }
  const candidate = path.resolve(runtimeRoot, rawPath);
  const roots = await Promise.all([projectRoot, runtimeRoot].map(async (root) => ({
    lexical: path.resolve(root),
    canonical: await realpath(root)
  })));
  const canonical = await realpath(candidate).catch((error) => {
    throw new Error(`${label} 无法解析 realpath: ${error.message}`);
  });
  const memberships = roots.filter(({ lexical }) => isInside(lexical, candidate));
  if (memberships.length === 0) {
    throw new Error(`${label} 必须位于项目根或运行工作目录内`);
  }
  if (!memberships.some(({ lexical, canonical: canonicalRoot }) => {
    const relative = path.relative(lexical, candidate);
    return samePath(path.resolve(canonicalRoot, relative), canonical);
  })) {
    throw new Error(`${label} 路径包含符号链接`);
  }

  const info = await lstat(candidate);
  if (info.isSymbolicLink()) throw new Error(`${label} 不得是符号链接`);
  if (!info.isFile()) throw new Error(`${label} 必须是普通文件`);

  const handle = await open(canonical, "r");
  try {
    const openedInfo = await handle.stat();
    if (!openedInfo.isFile()) throw new Error(`${label} 必须是普通文件`);
    if (openedInfo.size <= 0 || openedInfo.size > maxBytes) {
      throw new Error(`${label} 为空或超过 ${Math.floor(maxBytes / 1024)} KiB`);
    }
    if (info.dev !== openedInfo.dev || info.ino !== openedInfo.ino) {
      throw new Error(`${label} 在安全检查期间发生变化`);
    }
    const body = Buffer.alloc(openedInfo.size);
    let offset = 0;
    while (offset < body.length) {
      const { bytesRead } = await handle.read(body, offset, body.length - offset, offset);
      if (bytesRead === 0) break;
      offset += bytesRead;
    }
    if (offset !== body.length) throw new Error(`${label} 在读取期间发生变化`);
    const extra = Buffer.alloc(1);
    const { bytesRead: extraBytes } = await handle.read(extra, 0, 1, offset);
    if (extraBytes !== 0) throw new Error(`${label} 在读取期间超过大小限制`);
    return { path: canonical, body };
  } finally {
    await handle.close();
  }
}

function sha256(body) {
  return createHash("sha256").update(body).digest("hex");
}

async function publishOnce(finalPath, data) {
  const temporary = path.join(
    path.dirname(finalPath),
    `.${path.basename(finalPath)}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`
  );
  const handle = await open(temporary, "wx", 0o600);
  try {
    await handle.writeFile(data);
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await link(temporary, finalPath);
    return true;
  } catch (error) {
    if (error.code === "EEXIST") return false;
    throw error;
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

async function ensureOutputDirectory(projectRoot, directory) {
  await mkdir(directory, { recursive: true });
  const [canonicalRoot, canonicalDirectory] = await Promise.all([
    realpath(projectRoot),
    realpath(directory)
  ]);
  const expected = path.resolve(canonicalRoot, path.relative(path.resolve(projectRoot), directory));
  if (!samePath(expected, canonicalDirectory)) {
    throw new Error(`输出目录不得经过符号链接: ${directory}`);
  }
}

async function inspectImmutableFile(filePath, expectedBody, label) {
  let info;
  try {
    info = await lstat(filePath);
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
  if (info.isSymbolicLink() || !info.isFile()) throw new Error(`${label} 已存在但不是普通文件`);
  if (info.size !== expectedBody.length) throw new Error(`${label} 已存在且内容冲突`);
  const actual = await readFile(filePath);
  if (actual.length !== expectedBody.length || sha256(actual) !== sha256(expectedBody)) {
    throw new Error(`${label} 已存在且内容冲突`);
  }
  return true;
}

function isCanonicalIso(value) {
  if (typeof value !== "string") return false;
  const instant = new Date(value);
  return !Number.isNaN(instant.getTime()) && instant.toISOString() === value;
}

async function inspectGate(filePath, expected) {
  let info;
  try {
    info = await lstat(filePath);
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
  if (info.isSymbolicLink() || !info.isFile() || info.size <= 0 || info.size > MAX_GATE_BYTES) {
    throw new Error("更正版 gate 回执已存在但无效");
  }
  let receipt;
  try {
    receipt = JSON.parse(await readFile(filePath, "utf8"));
  } catch (error) {
    throw new Error(`更正版 gate 回执已存在但无法解析: ${error.message}`);
  }
  const { createdAt, ...comparable } = receipt ?? {};
  if (!isCanonicalIso(createdAt) || JSON.stringify(comparable) !== JSON.stringify(expected)) {
    throw new Error("更正版 gate 回执已存在且内容冲突");
  }
  return receipt;
}

export async function publishCorrection({
  projectRoot = defaultProjectRoot,
  runtimeRoot = process.cwd(),
  targetDate,
  revision,
  input,
  context: contextInput,
  cutoffInstant,
  now = new Date()
}) {
  projectRoot = path.resolve(projectRoot);
  runtimeRoot = path.resolve(runtimeRoot);
  validateIsoDate(targetDate);
  if (!/^v[1-9]\d{0,5}$/.test(revision ?? "")) throw new Error("更正版本必须是 vN（N 为正整数）");
  const cutoff = cutoffInstant instanceof Date ? cutoffInstant : parseCutoff(String(cutoffInstant));
  if (Number.isNaN(cutoff.getTime())) throw new Error("证据截止时间无效");

  const [{ body: reportInput }, { body: contextBody }, config] = await Promise.all([
    readSafeInput(input, { label: "--input", projectRoot, runtimeRoot }),
    readSafeInput(contextInput, { label: "--context", projectRoot, runtimeRoot }),
    loadProjectConfig(projectRoot)
  ]);
  let parsedContext;
  try {
    parsedContext = JSON.parse(contextBody.toString("utf8"));
  } catch (error) {
    throw new Error(`--context JSON 格式错误: ${error.message}`);
  }
  const context = validateMorningContext(parsedContext, {
    targetDate,
    timezone: config.workflow.timezone
  });
  const publicationAudit = assessPublicationGate({ context, cutoffInstant: cutoff, targetDate });
  if (!publicationAudit.passed) {
    throw new Error(`更正版发布门禁未通过: ${publicationAudit.blockers.join("；")}`);
  }
  const cutoffLocal = dateTimeInTimeZone(config.workflow.timezone, cutoff).display;
  const reportText = reportInput.toString("utf8");
  const publicationLevel = publicationLevelFromReport(reportText);
  const normalizedReport = normalizeReportOutput(reportText, {
    targetDate,
    disclaimer: config.risk.disclaimer,
    cutoffLocal,
    cutoffInstant: cutoff,
    publicationLevel,
    requireStaleMarketLabel: requiresStaleMarketLabel({
      context,
      cutoffInstant: cutoff,
      marketDataMaxAgeHours: config.risk.marketDataMaxAgeHours
    }),
    context,
    strictContextSources: true,
    marketDataMaxAgeHours: config.risk.marketDataMaxAgeHours,
    newsMaxAgeHours: config.risk.newsMaxAgeHours,
    publicationAudit,
    enforcePublicationReady: true,
    requireDeterministicCompilerOutput: true
  });
  const reportBody = Buffer.from(normalizedReport, "utf8");
  if (reportBody.length > MAX_INPUT_BYTES) throw new Error("规范化更正版超过 1024 KiB");

  const basename = `${targetDate}-${revision}`;
  const reportRelative = `reports/corrections/${basename}.md`;
  const contextRelative = `work/corrections/evidence/${basename}.json`;
  const gateRelative = `work/corrections/gates/${basename}.json`;
  const reportPath = path.join(projectRoot, ...reportRelative.split("/"));
  const evidencePath = path.join(projectRoot, ...contextRelative.split("/"));
  const gatePath = path.join(projectRoot, ...gateRelative.split("/"));
  await Promise.all([
    ensureOutputDirectory(projectRoot, path.dirname(reportPath)),
    ensureOutputDirectory(projectRoot, path.dirname(evidencePath)),
    ensureOutputDirectory(projectRoot, path.dirname(gatePath))
  ]);

  const expectedReceipt = {
    schemaVersion: "1.0",
    eventType: "correction_gate",
    targetDate,
    revision,
    evidenceCutoffIso: cutoff.toISOString(),
    contextMetaAsOf: context.meta.asOf,
    contextFile: contextRelative,
    contextSha256: sha256(contextBody),
    contextBytes: contextBody.length,
    reportFile: reportRelative,
    reportSha256: sha256(reportBody),
    reportBytes: reportBody.length,
    publicationAudit,
    passed: true
  };

  const [reportExists, evidenceExists, existingGate] = await Promise.all([
    inspectImmutableFile(reportPath, reportBody, "更正版报告"),
    inspectImmutableFile(evidencePath, contextBody, "更正版上下文快照"),
    inspectGate(gatePath, expectedReceipt)
  ]);
  if (existingGate && (!reportExists || !evidenceExists)) {
    throw new Error("更正版 gate 回执存在但不可变报告或上下文快照缺失");
  }

  let created = false;
  if (!reportExists) {
    created = await publishOnce(reportPath, reportBody) || created;
    await inspectImmutableFile(reportPath, reportBody, "更正版报告");
  }
  if (!evidenceExists) {
    created = await publishOnce(evidencePath, contextBody) || created;
    await inspectImmutableFile(evidencePath, contextBody, "更正版上下文快照");
  }
  let receipt = existingGate;
  if (!receipt) {
    const createdAt = now instanceof Date ? now.toISOString() : new Date(now).toISOString();
    const candidate = { ...expectedReceipt, createdAt };
    const candidateBody = Buffer.from(`${JSON.stringify(candidate, null, 2)}\n`, "utf8");
    if (candidateBody.length > MAX_GATE_BYTES) throw new Error("更正版 gate 回执过大");
    created = await publishOnce(gatePath, candidateBody) || created;
    receipt = await inspectGate(gatePath, expectedReceipt);
  }
  return {
    created,
    idempotent: !created,
    reportPath,
    evidencePath,
    gatePath,
    receipt
  };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const result = await publishCorrection(options);
  const state = result.idempotent ? "幂等命中" : "发布完成";
  process.stdout.write(`${state}：${result.receipt.reportFile}\n`);
}

if (isMainEntry(import.meta.url)) {
  main().catch((error) => {
    console.error(`更正版发布失败：${error.message}`);
    process.exitCode = 1;
  });
}

