import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { link, lstat, mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { canonicalJson, jsonHash, sha256, validDate } from "./snapshot-integrity.mjs";
import { attachWatchlistResearchPlan, buildVerifiedBaselineReport } from "./baseline-report.mjs";
import { attachPreviousFinancialPeriods, collectCurrentFinancialSnapshots, previousFinancialPeriodsFromArchive } from "./financial-comparison.mjs";
import { assessPublicationGate, normalizeReportOutput, recoverStructuredAnalysisSelection, DATA_BRIEF_PUBLICATION_LEVEL } from "./report.mjs";
import { buildStructuredAnalysisCandidates, parseStructuredAnalysisOutput } from "./structured-analysis.mjs";
import { attachDocumentBaselines, documentSnapshot } from "./document-evidence.mjs";
import { documentManifest, copyDocumentAssets, verifyDocumentAssets } from "./document-archive.mjs";

const exec = promisify(execFile);
const MAX_INPUT = 16 * 1024 * 1024;
const MAX_REPORT = 1024 * 1024;
const encode = (value) => `${canonicalJson(value)}\n`;

function verifyCompilerIdentity(code) {
  const files = code?.files;
  if (!code || typeof code.filesSha256 !== "string" || !/^[0-9a-f]{64}$/.test(code.filesSha256)
      || !files || Object.getPrototypeOf(files) !== Object.prototype
      || Object.keys(files).length === 0
      || Object.entries(files).some(([name, hash]) => !name || name.startsWith("/")
        || name.includes("\\") || name.split("/").some((part) => !part || part === "." || part === "..")
        || typeof hash !== "string" || !/^[0-9a-f]{64}$/.test(hash))
      || jsonHash(files) !== code.filesSha256) {
    throw new Error("invalid compiler code identity");
  }
}

async function directory(dir, create = false) {
  if (create) await mkdir(dir, { recursive: true, mode: 0o700 });
  const info = await lstat(dir);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("archive directory must not be a symlink");
}

async function artifact(file, max = MAX_INPUT) {
  const info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size < 1 || info.size > max) throw new Error("invalid archive artifact");
  return readFile(file, "utf8");
}

async function exists(file) {
  try { await lstat(file); return true; } catch (error) { if (error.code === "ENOENT") return false; throw error; }
}

async function checkedJson(file, max) {
  return JSON.parse(await artifact(file, max));
}

async function writeOnce(file, content) {
  try { await writeFile(file, content, { flag: "wx", mode: 0o600 }); }
  catch (error) {
    if (error.code !== "EEXIST") throw error;
    if (await artifact(file) !== content) throw new Error("immutable archive collision");
  }
}

export async function compilerIdentity(projectRoot) {
  const files = {};
  for (const name of (await readdir(path.join(projectRoot, "src"))).filter((file) => file.endsWith(".mjs")).sort()) {
    const relative = `src/${name}`;
    files[relative] = sha256(await artifact(path.join(projectRoot, relative)));
  }
  for (const relative of ["agents/structured_analyst.md", "scripts/morning-report.mjs", "scripts/publish-reviewed.mjs", "scripts/replay-report.mjs", "mcp/akshare_provider.py", "mcp/index_turnover_http.py", "mcp/cninfo_source.py", "mcp/cninfo_evidence.py", "mcp/cninfo_staging.py", "mcp/precollect_cninfo.py", "mcp/us_equity_calendar.py", "mcp/research_data_quality.py", "config/us-equity-calendar.json", "mcp/contract.schema.json", "requirements.txt", "package.json", "package-lock.json"]) {
    if (await exists(path.join(projectRoot, relative))) files[relative] = sha256(await artifact(path.join(projectRoot, relative)));
  }
  let revision = null;
  try { revision = (await exec("git", ["rev-parse", "HEAD"], { cwd: projectRoot })).stdout.trim(); }
  catch { /* Isolated test fixtures have no git repository; file hashes remain mandatory. */ }
  return { revision, files, filesSha256: jsonHash(files), runtime: { node: process.version, icu: process.versions.icu } };
}

export function prepareCompilerContext(context, corePool, baseline) {
  return attachDocumentBaselines(attachPreviousFinancialPeriods(attachWatchlistResearchPlan(context, corePool), baseline), baseline);
}

async function validateFrozenArchive(archiveDirectory, { compilerCode = null, allowCrossEraCatalogue = false } = {}) {
  await directory(archiveDirectory);
  const body = await artifact(path.join(archiveDirectory, "input.json"));
  const expected = (await artifact(path.join(archiveDirectory, "input.sha256"), 128)).trim();
  if (sha256(body) !== expected) throw new Error("frozen input hash mismatch");
  const input = JSON.parse(body);
  if (input.schemaVersion !== "1.0" || !validDate(input.targetDate) ||
      !["production", "historical_validation"].includes(input.mode)) throw new Error("invalid frozen input contract");
  if (input.mode === "production" && input.rawContext?.meta?.documentReconstruction) throw new Error("posthoc document context cannot be a production run");
  if (sha256(input.rawContextText) !== input.rawContextTextSha256 || jsonHash(JSON.parse(input.rawContextText)) !== input.rawContextSha256 ||
      jsonHash(input.compilerContext) !== input.compilerContextSha256 || jsonHash(input.rawContext) !== input.rawContextSha256) {
    throw new Error("frozen context hash mismatch");
  }
  const reconstructed = prepareCompilerContext(input.rawContext, input.configuration.stocks.corePool, input.financialBaseline);
  if (jsonHash(reconstructed) !== input.compilerContextSha256) throw new Error("compiler context is not derivable from archived inputs");
  // A missing or self-inconsistent identity is not evidence of an older era.
  verifyCompilerIdentity(input.code);
  if (compilerCode !== null) verifyCompilerIdentity(compilerCode);
  // Cross-era sealed-baseline loads do not rebuild the catalogue under the freeze-era
  // rule — that rule is not available in-process. The rebuild is skipped; integrity then
  // rests on the hash chain above plus the caller's sealed result, receipt, and
  // financial-snapshot binding. Missing/malformed identities have already been
  // rejected above; a valid different fingerprint remains an era lock elsewhere.
  const archivedFingerprint = input.code?.filesSha256;
  const knownCrossEra = Boolean(compilerCode?.filesSha256) && compilerCode.filesSha256 !== archivedFingerprint;
  if (knownCrossEra && !allowCrossEraCatalogue) throw new Error("compiler code differs from archived version");
  const skipCatalogueRebuild = knownCrossEra && allowCrossEraCatalogue;
  if (!skipCatalogueRebuild && jsonHash(input.candidateIds) !== jsonHash(buildStructuredAnalysisCandidates(input.compilerContext).map((item) => item.id))) {
    throw new Error("frozen candidate catalogue mismatch");
  }
  await verifyDocumentAssets(archiveDirectory, input);
  return {
    input, inputSha256: expected, archiveDirectory,
    candidateCatalogueEraVerified: !skipCatalogueRebuild && compilerCode?.filesSha256 === archivedFingerprint
  };
}

export async function readFrozenInput(archiveDirectory, options = {}) {
  // Public reads cannot opt out. allowCrossEraCatalogue is ignored here.
  return validateFrozenArchive(archiveDirectory, { compilerCode: options.compilerCode ?? null, allowCrossEraCatalogue: false });
}

async function readResult(frozen) {
  const dir = path.join(frozen.archiveDirectory, "result");
  await directory(dir);
  const result = await checkedJson(path.join(dir, "result.json"));
  const report = await artifact(path.join(dir, "report.md"), MAX_REPORT);
  if (result.schemaVersion !== "1.0" || result.inputSha256 !== frozen.inputSha256 || result.reportSha256 !== sha256(report)) {
    throw new Error("archive result hash mismatch");
  }
  if (result.selectionSha256 !== jsonHash(result.selection) || result.compilationInputSha256 !== jsonHash({ inputSha256: frozen.inputSha256, selection: result.selection })) throw new Error("archive selection/input hash mismatch");
  return { result, report };
}

export async function loadVerifiedFinancialBaseline(archiveRoot, outputDirectory, targetDate, compilerCode = null) {
  if (!validDate(targetDate)) throw new Error("invalid comparison target date");
  if (!await exists(archiveRoot)) return {};
  await directory(archiveRoot);
  const dates = (await readdir(archiveRoot)).filter((name) => validDate(name) && name < targetDate).sort().reverse();
  for (const date of dates) {
    const dir = path.join(archiveRoot, date);
    await directory(dir);
    // An interrupted run has input evidence but no publication receipt.
    if (!await exists(path.join(dir, "published.json"))) continue;
    const frozen = await validateFrozenArchive(dir, { compilerCode, allowCrossEraCatalogue: true });
    const { result, report } = await readResult(frozen);
    const receipt = await checkedJson(path.join(dir, "published.json"), 4096);
    if (frozen.input.mode !== "production" || frozen.input.targetDate !== date || receipt.status !== "sealed" ||
        receipt.inputSha256 !== frozen.inputSha256 || receipt.resultSha256 !== jsonHash(result) ||
        receipt.reportSha256 !== sha256(report) ||
        sha256(await artifact(path.join(outputDirectory, `${date}.md`), MAX_REPORT)) !== receipt.reportSha256) {
      throw new Error("financial baseline lacks a matching sealed report");
    }
    const snapshots = collectCurrentFinancialSnapshots(frozen.input.compilerContext);
    if (jsonHash(snapshots) !== jsonHash(result.financialSnapshots)) throw new Error("financial snapshots do not match frozen input");
    const baseline = previousFinancialPeriodsFromArchive({
      schemaVersion: "2.0", targetDate: date, reportSha256: receipt.reportSha256, periods: snapshots
    }, targetDate);
    for (const [code, entry] of Object.entries(baseline)) {
      const doc = documentSnapshot(frozen.input.compilerContext, code, frozen.input.cutoffInstant);
      if (doc) entry.officialDocument = doc;
    }
    return baseline;
  }
  return {};
}

export async function freezeRunInput({ projectRoot, archiveRoot, outputDirectory, rawContext, configuration,
  targetDate, cutoffInstant, cutoffLocal, mode = "production", documentStore = null, rawContextText = `${JSON.stringify(rawContext, null, 2)}\n` }) {
  if (!validDate(targetDate) || !["production", "historical_validation"].includes(mode) || rawContext.meta?.targetDate !== targetDate) throw new Error("invalid archive identity");
  if (mode === "production" && rawContext.meta?.documentReconstruction) throw new Error("posthoc document context cannot be a production run");
  if (jsonHash(JSON.parse(rawContextText)) !== jsonHash(rawContext)) throw new Error("raw packet bytes do not match parsed context");
  const cutoff = new Date(cutoffInstant).toISOString();
  const expectedLocal = new Date(Date.parse(cutoff) + 8 * 3_600_000).toISOString().slice(0, 16).replace("T", " ");
  if (!expectedLocal.startsWith(targetDate) || cutoffLocal !== expectedLocal) throw new Error("archive cutoff date/time mismatch");
  await directory(archiveRoot, true);
  const dir = path.join(archiveRoot, targetDate);
  const code = await compilerIdentity(projectRoot);
  if (await exists(dir)) {
    const frozen = await readFrozenInput(dir, { compilerCode: code });
    const previous = frozen.input;
    if (previous.mode !== mode || previous.cutoffInstant !== cutoff || previous.cutoffLocal !== cutoffLocal ||
        previous.rawContextSha256 !== jsonHash(rawContext) || previous.rawContextTextSha256 !== sha256(rawContextText) || jsonHash(previous.configuration) !== jsonHash(configuration) ||
        previous.code.filesSha256 !== code.filesSha256 || jsonHash(previous.code.runtime) !== jsonHash(code.runtime)) throw new Error("immutable run inputs changed; recollection/reconfiguration is forbidden");
    return frozen;
  }
  const financialBaseline = mode === "production"
    ? await loadVerifiedFinancialBaseline(archiveRoot, outputDirectory, targetDate, code) : {};
  const compilerContext = prepareCompilerContext(rawContext, configuration.stocks.corePool, financialBaseline);
  const publicationAudit = assessPublicationGate({ context: compilerContext, cutoffInstant: cutoff, targetDate });
  if (!publicationAudit.passed) throw new Error("cannot archive a publish-ready input while core gate is blocked");
  const input = {
    schemaVersion: "1.0", mode, targetDate, cutoffInstant: cutoff, cutoffLocal,
    rawContext, rawContextSha256: jsonHash(rawContext), rawContextText, rawContextTextSha256: sha256(rawContextText), configuration, code, financialBaseline,
    compilerContext, compilerContextSha256: jsonHash(compilerContext), publicationAudit,
    sourceDocuments: documentManifest(compilerContext, cutoff),
    candidateIds: buildStructuredAnalysisCandidates(compilerContext).map((item) => item.id)
  };
  const body = encode(input);
  const temp = await mkdtemp(path.join(archiveRoot, ".input-"));
  try {
    await writeOnce(path.join(temp, "input.json"), body);
    await writeOnce(path.join(temp, "input.sha256"), `${sha256(body)}\n`);
    await copyDocumentAssets({ projectRoot, documentStore, archiveDirectory: temp, manifest: input.sourceDocuments });
    try { await rename(temp, dir); }
    catch (error) {
      if (!["EEXIST", "ENOTEMPTY"].includes(error.code)) throw error;
      if ((await readFrozenInput(dir, { compilerCode: code })).inputSha256 !== sha256(body)) throw new Error("concurrent archive collision");
    }
  } finally { await rm(temp, { recursive: true, force: true }); }
  return readFrozenInput(dir, { compilerCode: code });
}

export function compileFrozenReport(input, overlay = null, capture = null) {
  const risk = input.configuration.risk;
  const options = {
    context: input.compilerContext, targetDate: input.targetDate,
    cutoffInstant: new Date(input.cutoffInstant), cutoffLocal: input.cutoffLocal,
    publicationLevel: DATA_BRIEF_PUBLICATION_LEVEL, publicationAudit: input.publicationAudit,
    disclaimer: risk.disclaimer, marketDataMaxAgeHours: risk.marketDataMaxAgeHours, newsMaxAgeHours: risk.newsMaxAgeHours
  };
  const audit = assessPublicationGate(options);
  if (!audit.passed || jsonHash(audit) !== jsonHash(input.publicationAudit)) throw new Error("frozen publication audit mismatch");
  const compiled = buildVerifiedBaselineReport({ ...options, analysisOverlay: overlay, capture });
  return normalizeReportOutput(compiled, { ...options, strictContextSources: true, enforcePublicationReady: true, requireDeterministicCompilerOutput: true });
}

export async function sealRunResult(frozen, compilation) {
  const report = compileFrozenReport(frozen.input, compilation.overlay ?? null);
  if (report !== compilation.report) throw new Error("archived compilation cannot be reproduced");
  const result = {
    schemaVersion: "1.0", status: "validated", inputSha256: frozen.inputSha256, reportSha256: sha256(report),
    selection: compilation.overlay ?? null, selectionSha256: jsonHash(compilation.overlay ?? null),
    compilationInputSha256: jsonHash({ inputSha256: frozen.inputSha256, selection: compilation.overlay ?? null }),
    analysisStatus: compilation.analysisStatus, analysisFailures: compilation.analysisFailures ?? [],
    mode: compilation.mode ?? (compilation.overlay ? "structured_enhanced" : "verified_baseline"),
    analysisAttempts: compilation.analysisAttempts ?? 0,
    semanticCandidateCount: frozen.input.candidateIds.length,
    financialSnapshots: collectCurrentFinancialSnapshots(frozen.input.compilerContext)
  };
  const temp = await mkdtemp(path.join(frozen.archiveDirectory, ".result-"));
  const destination = path.join(frozen.archiveDirectory, "result");
  try {
    await writeOnce(path.join(temp, "report.md"), report);
    await writeOnce(path.join(temp, "result.json"), encode(result));
    try { await rename(temp, destination); }
    catch (error) {
      if (!["EEXIST", "ENOTEMPTY"].includes(error.code)) throw error;
      const previous = await readResult(frozen);
      if (jsonHash(previous.result) !== jsonHash(result)) throw new Error("immutable result changed");
    }
  } finally { await rm(temp, { recursive: true, force: true }); }
  return result;
}

export async function loadArchivedCompilation(frozen) {
  if (!await exists(path.join(frozen.archiveDirectory, "result"))) return null;
  const { result, report } = await readResult(frozen);
  if (compileFrozenReport(frozen.input, result.selection) !== report) throw new Error("archived result is not reproducible");
  return {
    report, overlay: result.selection, mode: result.mode,
    analysisStatus: result.analysisStatus, analysisAttempts: result.analysisAttempts,
    semanticCandidateCount: result.semanticCandidateCount, analysisFailures: result.analysisFailures
  };
}

export async function publishArchivedReport(frozen, outputFile) {
  if (frozen.input.mode !== "production") throw new Error("validation archive cannot publish");
  const { result, report } = await readResult(frozen);
  if (compileFrozenReport(frozen.input, result.selection) !== report) throw new Error("publication result is not reproducible");
  await directory(path.dirname(outputFile), true);
  const temp = await mkdtemp(path.join(path.dirname(outputFile), ".sealed-"));
  try {
    const file = path.join(temp, "report.md");
    await writeOnce(file, report);
    try { await link(file, outputFile); }
    catch (error) {
      if (error.code !== "EEXIST") throw error;
      if (await artifact(outputFile, MAX_REPORT) !== report) throw new Error("refusing to overwrite an existing sealed report");
    }
  } finally { await rm(temp, { recursive: true, force: true }); }
  await recordArchivePublication(frozen, outputFile);
}

export async function captureReviewedReport(frozen, report) {
  const completed = await loadArchivedCompilation(frozen);
  if (completed) {
    if (completed.report !== report) throw new Error("reviewed output differs from sealed compilation");
    return (await readResult(frozen)).result;
  }
  const risk = frozen.input.configuration.risk;
  const overlay = recoverStructuredAnalysisSelection(report, {
    context: frozen.input.compilerContext,
    cutoffInstant: new Date(frozen.input.cutoffInstant),
    marketDataMaxAgeHours: risk.marketDataMaxAgeHours,
    newsMaxAgeHours: risk.newsMaxAgeHours
  });
  if (compileFrozenReport(frozen.input, overlay) === report) {
    return sealRunResult(frozen, { report, overlay, analysisStatus: "recovered_deterministic" });
  }
  throw new Error("reviewed report is not an archived deterministic candidate");
}

export async function recordArchivePublication(frozen, outputFile) {
  if (frozen.input.mode !== "production") throw new Error("validation archive cannot record publication");
  const { result } = await readResult(frozen);
  if (sha256(await artifact(outputFile, MAX_REPORT)) !== result.reportSha256) throw new Error("published report differs from archive");
  await writeOnce(path.join(frozen.archiveDirectory, "published.json"), encode({
    status: "sealed", inputSha256: frozen.inputSha256, resultSha256: jsonHash(result), reportSha256: result.reportSha256
  }));
}

export async function replayRunArchive(projectRoot, archiveDirectory) {
  const code = await compilerIdentity(projectRoot);
  const frozen = await readFrozenInput(archiveDirectory, { compilerCode: code });
  if (code.filesSha256 !== frozen.input.code.filesSha256) throw new Error("compiler code differs from archived version");
  if (jsonHash(code.runtime) !== jsonHash(frozen.input.code.runtime)) throw new Error("compiler runtime differs from archived version");
  const { result, report } = await readResult(frozen);
  const reproduced = compileFrozenReport(frozen.input, result.selection);
  if (sha256(reproduced) !== result.reportSha256 || reproduced !== report) throw new Error("replay output hash mismatch");
  return { mode: frozen.input.mode, targetDate: frozen.input.targetDate, inputSha256: frozen.inputSha256, compilationInputSha256: result.compilationInputSha256, reportSha256: result.reportSha256, matched: true };
}

export function selectFrozenCandidates(input, candidateIds = []) {
  return candidateIds.length ? parseStructuredAnalysisOutput(JSON.stringify({ schemaVersion: "2.0", candidateIds }), { context: input.compilerContext }) : null;
}
