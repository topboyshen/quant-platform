// Mandatory research sealing is independent of the optional XHS writer.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DIGEST_KIND, DIGEST_SCHEMA_VERSION, buildDigestView } from "./digest-view.mjs";
import { compilerIdentity, readFrozenInput } from "./run-archive.mjs";
import { jsonHash, sha256 as snapshotSha256 } from "./snapshot-integrity.mjs";
import { integrity, hash, validateIsoDate, readBoundedRegularFile, decodeUtf8, parseJsonBuffer, loadJsonConfig, stateEligibility, validateSuccessfulState, validateReportPath, validateAndParseReport, ensureDirectory, publishImmutable, LAST_RUN_RELATIVE, MAX_STATE_BYTES, SHA256 } from "./research-artifacts.mjs";

// Bind to executing code, never a caller's artifact directory or declared era.
const COMPILER_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/**
 * Research seal: the research pipeline's own qualified-research proof.
 *
 * Deliberately independent of config/xhs.json and of the XHS draft: the seal
 * carries only what the relay needs (date, gates, hashes, semantic review) and
 * the numbered-source contract validation. A display-only transformation in the
 * draft must never be able to block research delivery, and a source that cannot
 * be resolved must never be sealed.
 */
const RESEARCH_SEAL_RELATIVE_ROOT = "work/research-seal";
const RESEARCH_SEAL_MAX_REPORT_BYTES = 1024 * 1024;
const RESEARCH_SEAL_SCHEMA = "astock.research-seal.v1";
const ARCHIVE_RELATIVE_ROOT = "work/report-archives";
const ARCHIVE_INPUT_MAX_BYTES = 16 * 1024 * 1024;
const ARCHIVE_RESULT_MAX_BYTES = 4 * 1024 * 1024;
const DIGEST_MAX_BYTES = 4 * 1024 * 1024;
const MAX_SEAL_BYTES = 64 * 1024;

async function readArchiveArtifact(filePath, maxBytes, label) {
  try {
    return await readBoundedRegularFile(filePath, maxBytes, label);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

/**
 * Load the frozen compilation that produced this report. The digest must bind to
 * the same report, date and compilation input, so every link is verified here:
 * input sidecar, result binding, sealed report hash and selection hash.
 * A missing or inconsistent archive holds the seal; it never falls back to
 * re-parsing the report body.
 */
async function loadSealedDigestInputs({ projectRoot, targetDate, reportSha256 }) {
  const base = path.join(projectRoot, ARCHIVE_RELATIVE_ROOT, targetDate);
  const inputSnapshot = await readArchiveArtifact(path.join(base, "input.json"), ARCHIVE_INPUT_MAX_BYTES, "冻结输入");
  if (!inputSnapshot) return { status: "held", reason: "ARCHIVE_INPUT_MISSING" };
  const sidecarSnapshot = await readArchiveArtifact(path.join(base, "input.sha256"), 256, "冻结输入哈希");
  if (!sidecarSnapshot) return { status: "held", reason: "ARCHIVE_INPUT_SIDECAR_MISSING" };
  const inputSha256 = decodeUtf8(sidecarSnapshot.buffer, "冻结输入哈希").trim();
  if (!SHA256.test(inputSha256) || inputSnapshot.sha256 !== inputSha256) {
    return { status: "held", reason: "ARCHIVE_INPUT_HASH_MISMATCH" };
  }
  const resultSnapshot = await readArchiveArtifact(path.join(base, "result", "result.json"), ARCHIVE_RESULT_MAX_BYTES, "封存结果");
  if (!resultSnapshot) return { status: "held", reason: "ARCHIVE_RESULT_MISSING" };
  const sealedReport = await readArchiveArtifact(path.join(base, "result", "report.md"), RESEARCH_SEAL_MAX_REPORT_BYTES, "封存正文");
  if (!sealedReport) return { status: "held", reason: "ARCHIVE_REPORT_MISSING" };
  if (sealedReport.sha256 !== reportSha256) return { status: "held", reason: "ARCHIVE_REPORT_HASH_MISMATCH" };
  const input = parseJsonBuffer(inputSnapshot.buffer, "冻结输入");
  const result = parseJsonBuffer(resultSnapshot.buffer, "封存结果");
  if (input.targetDate !== targetDate) return { status: "held", reason: "ARCHIVE_TARGET_DATE_MISMATCH" };
  if (result.inputSha256 !== inputSha256) return { status: "held", reason: "ARCHIVE_RESULT_BINDING_MISMATCH" };
  if (result.reportSha256 !== reportSha256) return { status: "held", reason: "ARCHIVE_RESULT_REPORT_MISMATCH" };
  const selectionSha256 = jsonHash(result.selection ?? null);
  if (result.selectionSha256 !== selectionSha256) return { status: "held", reason: "ARCHIVE_SELECTION_HASH_MISMATCH" };
  // The strict archive reader also binds the selection to the frozen input; the seal
  // must reject an archive whose compilation identity does not reproduce.
  if (result.compilationInputSha256 !== jsonHash({ inputSha256, selection: result.selection ?? null })) {
    return { status: "held", reason: "ARCHIVE_COMPILATION_INPUT_MISMATCH" };
  }
  // Same internal context hashes the strict archive reader enforces. A file whose
  // sidecar and publication identity were rewritten must still be rejected here.
  try {
    if (typeof input.rawContextText !== "string"
        || snapshotSha256(input.rawContextText) !== input.rawContextTextSha256
        || jsonHash(JSON.parse(input.rawContextText)) !== input.rawContextSha256
        || jsonHash(input.compilerContext) !== input.compilerContextSha256
        || jsonHash(input.rawContext) !== input.rawContextSha256) {
      return { status: "held", reason: "ARCHIVE_CONTEXT_HASH_MISMATCH" };
    }
  } catch {
    return { status: "held", reason: "ARCHIVE_CONTEXT_HASH_MISMATCH" };
  }
  if (typeof input.code?.filesSha256 !== "string" || !SHA256.test(input.code.filesSha256)) {
    return { status: "held", reason: "ARCHIVE_CODE_FINGERPRINT_MISSING" };
  }
  // New digests must be compiled in the archived code era. The baseline-only
  // cross-era exception must never be used to create a new sealed artifact.
  try {
    const frozen = await readFrozenInput(base, { compilerCode: await compilerIdentity(COMPILER_ROOT) });
    if (frozen.inputSha256 !== inputSha256) {
      return { status: "held", reason: "ARCHIVE_INPUT_HASH_MISMATCH" };
    }
  } catch {
    return { status: "held", reason: "ARCHIVE_STRICT_READ_FAILED" };
  }
  // R05: the full archive publication identity is reused here, so a digest can only
  // bind to a compilation whose result was actually sealed and published.
  const publishedSnapshot = await readArchiveArtifact(path.join(base, "published.json"), MAX_SEAL_BYTES, "封存发布记录");
  if (!publishedSnapshot) return { status: "held", reason: "ARCHIVE_PUBLICATION_MISSING" };
  const published = parseJsonBuffer(publishedSnapshot.buffer, "封存发布记录");
  if (published.status !== "sealed" || published.inputSha256 !== inputSha256
      || published.reportSha256 !== reportSha256 || published.resultSha256 !== jsonHash(result)) {
    return { status: "held", reason: "ARCHIVE_PUBLICATION_IDENTITY_MISMATCH" };
  }
  return { status: "ok", input, selection: result.selection ?? null, inputSha256, selectionSha256 };
}

function sealStatusRecord({ status, reasonCodes = [], targetDate = null, sealFile = null, now }) {
  return {
    schemaVersion: "1.0",
    status,
    reasonCodes,
    targetDate,
    sealFile,
    completedAt: now.toISOString()
  };
}

export async function buildResearchSeal({
  projectRoot,
  desiredDate = null,
  requireCurrentDate = false,
  maxAgeMinutes = null,
  dryRun = false,
  now = new Date()
} = {}) {
  if (typeof projectRoot !== "string" || !path.isAbsolute(projectRoot)) {
    throw new Error("projectRoot 必须是绝对路径");
  }
  if (desiredDate !== null) validateIsoDate(desiredDate, "--date");
  if (maxAgeMinutes !== null && (!Number.isInteger(maxAgeMinutes) || maxAgeMinutes < 1 || maxAgeMinutes > 24 * 60)) {
    throw new Error("--max-age-minutes 必须是 1—1440 的整数");
  }
  if (!(now instanceof Date) || Number.isNaN(now.getTime())) throw new Error("now 必须是有效时间");

  const riskConfig = await loadJsonConfig(projectRoot, "config/risk.json", "风险配置");
  if (typeof riskConfig.disclaimer !== "string" || riskConfig.disclaimer.trim() === "") {
    throw new Error("风险配置缺少免责声明");
  }

  const lastRunPath = path.join(projectRoot, "work", "last-run.json");
  let stateSnapshot;
  try {
    stateSnapshot = await readBoundedRegularFile(lastRunPath, MAX_STATE_BYTES, "work/last-run.json");
  } catch (error) {
    if (error?.code === "ENOENT") {
      return sealStatusRecord({ status: "held", reasonCodes: ["LAST_RUN_MISSING"], targetDate: desiredDate, now });
    }
    throw error;
  }
  const lastRun = parseJsonBuffer(stateSnapshot.buffer, "work/last-run.json");
  if (typeof lastRun.targetDate === "string") validateIsoDate(lastRun.targetDate);
  const reasons = stateEligibility(lastRun, { desiredDate, requireCurrentDate, maxAgeMinutes, now });
  if (reasons.length > 0) {
    return sealStatusRecord({
      status: "held",
      reasonCodes: reasons,
      targetDate: lastRun.targetDate ?? desiredDate,
      now
    });
  }
  validateSuccessfulState(lastRun);
  const reportPathCheck = validateReportPath(lastRun.reportFile, lastRun.targetDate);
  if (!reportPathCheck.eligible) {
    return sealStatusRecord({
      status: "held",
      reasonCodes: [reportPathCheck.reason],
      targetDate: lastRun.targetDate,
      now
    });
  }

  const reportPath = path.join(projectRoot, "reports", "daily", `${lastRun.targetDate}.md`);
  let reportSnapshot;
  try {
    reportSnapshot = await readBoundedRegularFile(reportPath, RESEARCH_SEAL_MAX_REPORT_BYTES, "正式晨报");
  } catch (error) {
    if (error?.code === "ENOENT") {
      return sealStatusRecord({
        status: "held",
        reasonCodes: ["FORMAL_REPORT_MISSING"],
        targetDate: lastRun.targetDate,
        now
      });
    }
    throw error;
  }
  const report = decodeUtf8(reportSnapshot.buffer, "正式晨报");
  // Date, gate, hash and numbered-source validation all belong to research.
  validateAndParseReport(report, lastRun, riskConfig.disclaimer.trim());

  // The structured digest is produced here, inside the sealing step, from the
  // frozen compilation of this same run. The relay never recompiles an archive.
  const digestInputs = await loadSealedDigestInputs({
    projectRoot,
    targetDate: lastRun.targetDate,
    reportSha256: reportSnapshot.sha256
  });
  if (digestInputs.status !== "ok") {
    return sealStatusRecord({
      status: "held",
      reasonCodes: [digestInputs.reason],
      targetDate: lastRun.targetDate,
      now
    });
  }
  const digestView = buildDigestView({
    input: digestInputs.input,
    selection: digestInputs.selection,
    inputSha256: digestInputs.inputSha256,
    selectionSha256: digestInputs.selectionSha256,
    reportSha256: reportSnapshot.sha256,
    generatedAt: lastRun.completedAt
  });
  const digestRelative = `${RESEARCH_SEAL_RELATIVE_ROOT}/${lastRun.targetDate}/${reportSnapshot.sha256}.digest.json`;
  // The digest timestamp is part of its evidence identity: it must be the run's own
  // completion instant, never a build-time default.
  if (digestView.digest.generatedAt !== lastRun.completedAt) {
    return sealStatusRecord({
      status: "held", reasonCodes: ["DIGEST_GENERATED_AT_MISMATCH"], targetDate: lastRun.targetDate, now
    });
  }
  // R05: the digest is rendered by a deterministic recompilation, so that
  // compilation must reproduce the very report being sealed byte for byte. A
  // copied reportSha256 field is not content proof.
  if (digestView.compiledSha256 !== reportSnapshot.sha256) {
    return sealStatusRecord({
      status: "held",
      reasonCodes: ["DIGEST_REPORT_RECOMPILE_MISMATCH"],
      targetDate: lastRun.targetDate,
      now
    });
  }

  const seal = {
    schemaVersion: RESEARCH_SEAL_SCHEMA,
    kind: "research_seal",
    targetDate: lastRun.targetDate,
    state: "SEALED",
    generatedAt: lastRun.completedAt,
    source: {
      lastRunFile: LAST_RUN_RELATIVE,
      lastRunSha256: stateSnapshot.sha256,
      reportFile: lastRun.reportFile,
      reportSha256: reportSnapshot.sha256,
      contextSha256: lastRun.contextSha256,
      dataStatus: lastRun.dataStatus,
      researchSemanticReview: lastRun.researchSemanticReview,
      frozenCutoffInstant: lastRun.frozenCutoffInstant,
      cutoffLocal: lastRun.cutoffLocal,
      publicationGate: lastRun.publicationGate,
      evidenceReview: lastRun.evidenceReview,
      digestFile: digestRelative,
      digestSha256: digestView.digestSha256
    }
  };
  const sealContent = Buffer.from(`${JSON.stringify(seal, null, 2)}\n`, "utf8");
  const relative = `${RESEARCH_SEAL_RELATIVE_ROOT}/${lastRun.targetDate}/${reportSnapshot.sha256}.json`;
  const digestPath = path.join(projectRoot, RESEARCH_SEAL_RELATIVE_ROOT, lastRun.targetDate, `${reportSnapshot.sha256}.digest.json`);
  const sealPath = path.join(projectRoot, RESEARCH_SEAL_RELATIVE_ROOT, lastRun.targetDate, `${reportSnapshot.sha256}.json`);
  // R10: every read-only integrity check runs before the dry-run branch, so a
  // preview can never report ready for a damaged sealed pair. Only the writes
  // differ between preview and the real path.
  const existingSeal = await readArchiveArtifact(sealPath, MAX_SEAL_BYTES, "研究密封");
  if (existingSeal) {
    // Idempotency verifies integrity, not merely existence: a deleted or tampered
    // digest, or a seal whose declared identity was changed, must not return noop
    // and must never be silently rewritten.
    const existing = parseJsonBuffer(existingSeal.buffer, "研究密封");
    const identity = existing.source ?? {};
    const digestSnapshot = await readArchiveArtifact(digestPath, DIGEST_MAX_BYTES, "结构化摘要");
    if (!digestSnapshot) {
      return sealStatusRecord({
        status: "held", reasonCodes: ["DIGEST_MISSING_AFTER_SEAL"], targetDate: lastRun.targetDate, now
      });
    }
    if (digestSnapshot.sha256 !== identity.digestSha256) {
      return sealStatusRecord({
        status: "held", reasonCodes: ["DIGEST_HASH_MISMATCH_AFTER_SEAL"], targetDate: lastRun.targetDate, now
      });
    }
    // Hash equality alone does not prove what the digest binds. Re-verify its declared
    // content bindings (including generatedAt == this run's completedAt) so a digest
    // rewritten together with its declared hash is still rejected.
    const existingDigest = parseJsonBuffer(digestSnapshot.buffer, "结构化摘要");
    const bindingProblems = [
      existingDigest.schemaVersion === DIGEST_SCHEMA_VERSION ? null : "schemaVersion",
      existingDigest.kind === DIGEST_KIND ? null : "kind",
      existingDigest.reportDate === lastRun.targetDate ? null : "reportDate",
      existingDigest.reportSha256 === reportSnapshot.sha256 ? null : "reportSha256",
      existingDigest.inputSha256 === digestInputs.inputSha256 ? null : "inputSha256",
      existingDigest.selectionSha256 === digestInputs.selectionSha256 ? null : "selectionSha256",
      existingDigest.generatedAt === lastRun.completedAt ? null : "generatedAt"
    ].filter(Boolean);
    if (bindingProblems.length > 0) {
      return sealStatusRecord({
        status: "held", reasonCodes: ["EXISTING_DIGEST_BINDING_MISMATCH"], targetDate: lastRun.targetDate, now
      });
    }
    if (digestSnapshot.sha256 !== digestView.digestSha256) {
      return sealStatusRecord({
        status: "held", reasonCodes: ["DIGEST_CONTENT_MISMATCH_AFTER_SEAL"], targetDate: lastRun.targetDate, now
      });
    }
    // Compare the complete deterministic manifest, not a field allowlist. This
    // includes top-level generatedAt and any future declaration, and restores
    // publishImmutable's byte-for-byte protection before returning noop. Keep
    // digest checks above it so their existing, more specific reasons survive.
    if (!existingSeal.buffer.equals(sealContent)) {
      return sealStatusRecord({
        status: "held", reasonCodes: ["EXISTING_SEAL_IDENTITY_MISMATCH"], targetDate: lastRun.targetDate, now
      });
    }
    return sealStatusRecord({ status: "noop", targetDate: lastRun.targetDate, sealFile: relative, now });
  }
  // A conflicting digest without a seal: the immutable write would fail, so the
  // preview must report the same conflict instead of "ready".
  const pendingDigest = await readArchiveArtifact(digestPath, DIGEST_MAX_BYTES, "结构化摘要");
  if (pendingDigest && pendingDigest.sha256 !== digestView.digestSha256) {
    return sealStatusRecord({
      status: "held", reasonCodes: ["DIGEST_IMMUTABLE_CONFLICT"], targetDate: lastRun.targetDate, now
    });
  }
  if (dryRun) {
    return sealStatusRecord({ status: "ready", targetDate: lastRun.targetDate, sealFile: relative, now });
  }

  const workRoot = path.join(projectRoot, "work");
  const sealRoot = path.join(workRoot, "research-seal");
  const dated = path.join(sealRoot, lastRun.targetDate);
  await ensureDirectory(workRoot);
  await ensureDirectory(sealRoot);
  await ensureDirectory(dated);
  // Ordering: the digest is written atomically and read back for its hash before
  // the seal manifest declares it; only then does the seal become deliverable.
  await publishImmutable(digestPath, Buffer.from(digestView.body, "utf8"));
  const digestSnapshot = await readBoundedRegularFile(digestPath, DIGEST_MAX_BYTES, "结构化摘要");
  if (digestSnapshot.sha256 !== digestView.digestSha256) {
    return sealStatusRecord({
      status: "held",
      reasonCodes: ["DIGEST_WRITE_HASH_MISMATCH"],
      targetDate: lastRun.targetDate,
      now
    });
  }
  const created = await publishImmutable(sealPath, sealContent);
  return sealStatusRecord({
    status: created ? "sealed" : "noop",
    targetDate: lastRun.targetDate,
    sealFile: relative,
    now
  });
}
